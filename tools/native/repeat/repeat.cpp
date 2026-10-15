// SPDX-License-Identifier: MIT
#include "repeat.hpp"
#include "libsais.h"

#include <algorithm>
#include <array>
#include <limits>
#include <memory>
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
    // Reject short spans before the variable integer division.
    if (length / 3 < period) return;
    const Index repeats = length / period;
    const Index coverage = repeats * period; // <= length <= n
    const Index previous = best.repeats * best.unit;
    if (coverage > previous || (coverage == previous &&
        (period < best.unit || (period == best.unit && start < best.start)))) {
        best = {start, period, repeats};
    }
}

// Bounded scans: <=32 periods, either from n<=96 or the proven LCP bound.
// This checks every feasible period, without a semantic period limit.
Result small_repeat(const std::uint32_t* text, Index n, Index limit) {
    Result best;
    for (Index period = 1; period <= limit; ++period) {
        Index start = 0;
        const Index minimum = 3 * period; // period <= n/3
        for (Index end = period; end < n; ++end) {
            if (text[end] != text[end - period]) {
                if (end - start >= minimum) consider(best, start, period, end - start);
                start = end - period + 1;
            }
        }
        if (n - start >= minimum) consider(best, start, period, n - start);
        if (best.unit * best.repeats == n) break;
    }
    return best;
}

// For 97..512 points, compare suffixes and extend Lyndon roots directly.
// The bounded stack avoids allocating suffix/RMQ structures. At most O(n^2)
// comparisons on this fixed-size path; n<=512 makes its work <=constant*n.
bool suffix_less(const std::uint32_t* text, Index n, Index left, Index right) {
    while (left < n && right < n && text[left] == text[right]) { ++left; ++right; }
    return right < n && (left == n || text[left] < text[right]);
}

Result short_lyndon(const std::uint32_t* text, Index n) {
    Result best;
    std::array<Index, 512> stack{};
    for (int inverse = 0; inverse < 2; ++inverse) {
        Index count = 0;
        for (Index i = n - 1; i >= 0; --i) {
            while (count && (inverse ? suffix_less(text, n, stack[count - 1], i)
                                     : suffix_less(text, n, i, stack[count - 1]))) --count;
            const Index next = count ? stack[count - 1] : n;
            const Index period = next - i;
            stack[count++] = i;
            if (period > n / 3) continue;
            Index left = 0, right = 0;
            while (left < i && text[i - left - 1] == text[next - left - 1]) ++left;
            while (right < n - next && text[i + right] == text[next + right]) ++right;
            consider(best, i - left, period, left + period + right);
        }
    }
    return best;
}

// At most four stable byte-radix passes, so compression does not hide an
// O(n log n) comparison sort. All uint32 values (including U+0000) are valid.
std::vector<Index> compress(const std::uint32_t* text, Index n, Index& alphabet) {
    std::vector<Index> order(n), buffer(n), symbols(n);
    std::uint32_t used_bits = 0;
    for (Index i = 0; i < n; ++i) { order[i] = i; used_bits |= text[i]; }
    for (unsigned shift = 0; shift < 32 && (used_bits >> shift); shift += 8) {
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
    Index maximum_ = 0;
    std::vector<Index> ranks_;
    std::vector<Index> lcp_;
    std::unique_ptr<Minima> minima_;

    std::vector<Index> build(std::vector<Index>& text, Index alphabet) {
        std::vector<Index> suffixes(size_), lcp(size_);
        // libsais may mutate its INTEGER input temporarily: text is a private
        // vector, never Python storage. OpenMP is deliberately not compiled in.
        Index status;
        if (alphabet <= 256) {
            // Compression preserves ordering; byte SA-IS reduces traffic for
            // small alphabets without truncating any original Unicode value.
            std::vector<std::uint8_t> bytes(text.begin(), text.end());
            status = libsais(bytes.data(), suffixes.data(), size_, 0, nullptr);
        } else {
            status = libsais_int(text.data(), suffixes.data(), size_, alphabet, 0);
        }
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
            maximum_ = std::max(maximum_, common);
            if (common) --common;
        }
        return lcp;
    }
public:
    Lce(std::vector<Index>& text, Index alphabet)
        : size_(static_cast<Index>(text.size())), ranks_(text.size()), lcp_(build(text, alphabet)) {}
    Index maximum() const { return maximum_; }
    void prepare_queries() { minima_ = std::make_unique<Minima>(std::move(lcp_)); }
    const std::vector<Index>& ranks() const { return ranks_; }
    Index common(Index left, Index right) const {
        if (left == size_ || right == size_) return 0;
        if (left == right) return size_ - left;
        Index a = ranks_[left], b = ranks_[right];
        if (a > b) std::swap(a, b);
        return minima_->query(a + 1, b);
    }
};

Result find_repeat_impl(const std::uint32_t* text, Index n, bool partition) {
    if (n < 3) return {};
    if (n <= 96) return small_repeat(text, n, n / 3);
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
    if (n <= 512) return short_lyndon(text, n);
    Index alphabet;
    auto symbols = compress(text, n, alphabet);
    if (partition) {
        // A cube cannot contain a symbol occurring fewer than three times in
        // the entire input. Such symbols are exact separators, not heuristics.
        std::vector<Index> counts(alphabet);
        for (Index symbol : symbols) ++counts[symbol];
        if (std::any_of(counts.begin(), counts.end(), [](Index count) { return count < 3; })) {
            Result best;
            Index start = 0;
            for (Index end = 0; end <= n; ++end) {
                if (end == n || counts[symbols[end]] < 3) {
                    const Index length = end - start;
                    if (length >= 3 && length >= best.unit * best.repeats) {
                        // Split only once: disjoint pieces have total size <=n,
                        // so both preprocessing and search remain O(n).
                        const Result part = find_repeat_impl(text + start, length, false);
                        if (part.unit) consider(best, start + part.start, part.unit, part.unit * part.repeats);
                    }
                    start = end + 1;
                }
            }
            return best;
        }
    }
    Lce forward(symbols, alphabet);
    // A cube of period p implies LCP(start, start+p) >= 2p. The largest
    // adjacent suffix LCP bounds every pair, so this scans ALL possible
    // periods when the proven bound is <=32; it is not a period cap.
    const Index limit = std::min(n / 3, forward.maximum() / 2);
    if (limit <= 32) return small_repeat(text, n, limit);
    forward.prepare_queries();
    std::reverse(symbols.begin(), symbols.end());
    Lce backward(symbols, alphabet);
    backward.prepare_queries();
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
            if (period > limit) continue;
            const Index left = i > 0 && text[i - 1] == text[next - 1]
                ? backward.common(n - i, n - next) : 0;
            const Index right = next < n && text[i] == text[next]
                ? forward.common(i, next) : 0;
            consider(best, i - left, period, left + period + right);
        }
    }
    return best;
}
}

Result find_repeat(const std::uint32_t* text, std::size_t size) {
    if (size > static_cast<std::size_t>(std::numeric_limits<Index>::max() - 1)) {
        throw std::overflow_error("repeat input exceeds signed 32-bit index limit");
    }
    if (!text && size) throw std::invalid_argument("null repeat input");
    return find_repeat_impl(text, static_cast<Index>(size), true);
}
}
