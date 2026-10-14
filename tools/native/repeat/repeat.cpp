// SPDX-License-Identifier: MIT
#include "repeat.hpp"
#include "libsais.h"

#include <algorithm>
#include <array>
#include <limits>
#include <numeric>
#include <stdexcept>
#include <utility>
#include <vector>
#ifdef _MSC_VER
#include <intrin.h>
#endif

namespace blc_repeat {
namespace {
using Index = std::int32_t;
using Mask = std::uint32_t;

// Nonzero unsigned operands only. The block width never exceeds 30 bits.
unsigned highest(Mask value) {
#ifdef _MSC_VER
    unsigned long index;
    _BitScanReverse(&index, value);
    return static_cast<unsigned>(index);
#else
    return 31U - static_cast<unsigned>(__builtin_clz(value));
#endif
}
unsigned lowest(Mask value) {
#ifdef _MSC_VER
    unsigned long index;
    _BitScanForward(&index, value);
    return static_cast<unsigned>(index);
#else
    return static_cast<unsigned>(__builtin_ctz(value));
#endif
}

void consider(Result& best, Index start, Index period, Index length) {
    const Index repeats = length / period;
    if (repeats < 3) return;
    const Index coverage = repeats * period; // <= length <= n
    const Index previous = best.repeats * best.unit;
    if (coverage > previous || (coverage == previous &&
        (period < best.unit || (period == best.unit && start < best.start)))) {
        best = {start, period, repeats};
    }
}

// Bounded base case: at most 85 scans of <=256 code points. Its work is <=85n,
// avoids suffix-array allocation on short ASR strings, and checks EVERY period.
Result small_repeat(const std::uint32_t* text, Index n) {
    Result best;
    for (Index period = 1; period <= n / 3; ++period) {
        Index start = 0;
        for (Index end = period; end <= n; ++end) {
            if (end == n || text[end] != text[end - period]) {
                consider(best, start, period, end - start);
                start = end - period + 1;
            }
        }
        if (best.unit * best.repeats == n) break;
    }
    return best;
}

// Four stable byte-radix passes, so alphabet compression does not hide an
// O(n log n) comparison sort. All uint32 values (including U+0000) are valid.
std::vector<Index> compress(const std::uint32_t* text, Index n, Index& alphabet) {
    std::vector<Index> order(n), buffer(n), symbols(n);
    std::iota(order.begin(), order.end(), 0);
    for (unsigned shift = 0; shift < 32; shift += 8) {
        std::array<std::size_t, 256> counts{};
        for (Index index : order) ++counts[(text[index] >> shift) & 255U];
        std::size_t total = 0;
        for (auto& count : counts) {
            const auto size = count;
            count = total;
            total += size;
        }
        for (Index index : order) buffer[counts[(text[index] >> shift) & 255U]++] = index;
        order.swap(buffer);
    }
    alphabet = 1;
    symbols[order[0]] = 0;
    for (Index i = 1; i < n; ++i) {
        if (text[order[i]] != text[order[i - 1]]) ++alphabet;
        symbols[order[i]] = alphabet - 1;
    }
    return symbols;
}

// O(n) preprocessing and O(1) RMQ. Each block has floor(log2 n) entries.
// A prefix monotone-stack bitmask answers intra-block minima using bit scans.
// Only the n/log(n) block minima get a sparse table: its total size/work is O(n).
class Minima {
    std::vector<Index> values_;
    std::vector<Mask> masks_;
    std::vector<std::vector<Index>> levels_;
    Index width_ = 1;

    Index within(Index left, Index right) const {
        const Index base = (right / width_) * width_;
        const Mask candidates = masks_[right] & (~Mask{0} << (left - base));
        return values_[base + static_cast<Index>(lowest(candidates))];
    }
public:
    explicit Minima(std::vector<Index> values) : values_(std::move(values)), masks_(values_.size()) {
        const Index n = static_cast<Index>(values_.size());
        width_ = std::max<Index>(1, highest(static_cast<Mask>(n)));
        std::vector<Index> blocks;
        blocks.reserve(static_cast<std::size_t>(n) / width_ + 1);
        Mask mask = 0;
        for (Index i = 0; i < n; ++i) {
            const Index offset = i % width_;
            if (offset == 0) mask = 0;
            while (mask && values_[i - offset + highest(mask)] >= values_[i]) {
                mask &= ~(Mask{1} << highest(mask));
            }
            mask |= Mask{1} << offset;
            masks_[i] = mask;
            if (offset == width_ - 1 || i == n - 1) {
                blocks.push_back(values_[i - offset + lowest(mask)]);
            }
        }
        levels_.push_back(std::move(blocks));
        const std::size_t count = levels_[0].size();
        for (std::size_t half = 1; half <= count / 2; half *= 2) {
            const auto& previous = levels_.back();
            std::vector<Index> level(count - 2 * half + 1);
            for (std::size_t i = 0; i < level.size(); ++i) {
                level[i] = std::min(previous[i], previous[i + half]);
            }
            levels_.push_back(std::move(level));
        }
    }
    Index query(Index left, Index right) const {
        const Index first = left / width_, last = right / width_;
        if (first == last) return within(left, right);
        Index value = std::min(within(left, (first + 1) * width_ - 1), within(last * width_, right));
        const Index count = last - first - 1;
        if (count > 0) {
            const unsigned level = highest(static_cast<Mask>(count));
            const Index span = Index{1} << level;
            value = std::min(value, std::min(levels_[level][first + 1], levels_[level][last - span]));
        }
        return value;
    }
};

class Lce {
    Index size_;
    std::vector<Index> ranks_;
    Minima minima_;

    std::vector<Index> build(std::vector<Index>& text, Index alphabet) {
        std::vector<Index> suffixes(size_), lcp(size_);
        // libsais may mutate its INTEGER input temporarily: text is a private
        // vector, never Python storage. OpenMP is deliberately not compiled in.
        const Index status = libsais_int(text.data(), suffixes.data(), size_, alphabet, 0);
        if (status == -2) throw std::bad_alloc();
        if (status != 0) throw std::runtime_error("suffix array construction failed");
        for (Index i = 0; i < size_; ++i) ranks_[suffixes[i]] = i;
        Index common = 0;
        for (Index i = 0; i < size_; ++i) {
            const Index rank = ranks_[i];
            if (rank == 0) { common = 0; continue; }
            const Index j = suffixes[rank - 1];
            while (common < size_ - i && common < size_ - j && text[i + common] == text[j + common]) ++common;
            lcp[rank] = common;
            if (common) --common;
        }
        return lcp;
    }
public:
    Lce(std::vector<Index>& text, Index alphabet)
        : size_(static_cast<Index>(text.size())), ranks_(text.size()), minima_(build(text, alphabet)) {}
    const std::vector<Index>& ranks() const { return ranks_; }
    Index common(Index left, Index right) const {
        if (left == size_ || right == size_) return 0;
        if (left == right) return size_ - left;
        Index a = ranks_[left], b = ranks_[right];
        if (a > b) std::swap(a, b);
        return minima_.query(a + 1, b);
    }
};
}

Result find_repeat(const std::uint32_t* text, std::size_t size) {
    if (size > static_cast<std::size_t>(std::numeric_limits<Index>::max() - 1)) {
        throw std::overflow_error("repeat input exceeds signed 32-bit index limit");
    }
    if (!text && size) throw std::invalid_argument("null repeat input");
    const Index n = static_cast<Index>(size);
    if (n < 3) return {};
    if (n <= 256) return small_repeat(text, n);
    {
        // O(n) KMP fast path for exact whole-string repeats; it returns only
        // when coverage is n with the minimum possible period.
        std::vector<Index> prefix(n);
        for (Index i = 1; i < n; ++i) {
            Index length = prefix[i - 1];
            while (length && text[i] != text[length]) length = prefix[length - 1];
            if (text[i] == text[length]) ++length;
            prefix[i] = length;
        }
        const Index period = n - prefix.back();
        if (n % period == 0 && n / period >= 3) return {0, period, n / period};
    }
    Index alphabet;
    auto symbols = compress(text, n, alphabet);
    const Lce forward(symbols, alphabet);
    std::reverse(symbols.begin(), symbols.end());
    const Lce backward(symbols, alphabet);
    const auto& rank = forward.ranks();
    Result best;
    std::vector<Index> stack;
    stack.reserve(n);
    // Bannai et al., The Runs Theorem (2015), sections 4.1 and 4.2.
    // Reverse the ENTIRE alphabet, including the unique end sentinel: inverse
    // suffix order is then exactly reversed, so no third suffix array is needed.
    // Each position supplies at most one longest Lyndon root per order.
    for (int inverse = 0; inverse < 2; ++inverse) {
        stack.clear();
        for (Index i = n - 1; i >= 0; --i) {
            while (!stack.empty() && (inverse ? rank[i] > rank[stack.back()] : rank[i] < rank[stack.back()])) {
                stack.pop_back();
            }
            const Index next = stack.empty() ? n : stack.back();
            const Index period = next - i;
            stack.push_back(i);
            if (period > n / 3) continue;
            const Index left = backward.common(n - i, n - next);
            const Index right = forward.common(i, next);
            consider(best, i - left, period, left + period + right);
        }
    }
    return best;
}
}
