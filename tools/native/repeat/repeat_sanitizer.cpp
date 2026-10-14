// SPDX-License-Identifier: MIT
// Standalone adversarial test executable; never linked into the Python module.
#include "repeat.hpp"
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <new>
#include <iostream>
#include <limits>
#include <random>
#include <stdexcept>
#include <vector>

// Inject allocation failure only in this standalone harness, never production.
static int allocation_budget = -1;
void* operator new(std::size_t size) {
    if (allocation_budget == 0) throw std::bad_alloc();
    if (allocation_budget > 0) --allocation_budget;
    if (void* ptr = std::malloc(size ? size : 1)) return ptr;
    throw std::bad_alloc();
}
void* operator new[](std::size_t size) { return ::operator new(size); }
void operator delete(void* ptr) noexcept { std::free(ptr); }
void operator delete[](void* ptr) noexcept { std::free(ptr); }
void operator delete(void* ptr, std::size_t) noexcept { std::free(ptr); }
void operator delete[](void* ptr, std::size_t) noexcept { std::free(ptr); }

blc_repeat::Result oracle(const std::vector<std::uint32_t>& text) {
    const int n = static_cast<int>(text.size());
    blc_repeat::Result best;
    for (int p = 1; p <= n / 3; ++p) {
        for (int start = 0; start <= n - 3 * p; ++start) {
            int end = start + p;
            while (end <= n - p && std::equal(text.begin() + start, text.begin() + start + p, text.begin() + end)) end += p;
            const int count = (end - start) / p;
            if (count >= 3 && count * p > best.unit * best.repeats) best = {start, p, count};
            if (best.unit * best.repeats == n) return best;
        }
    }
    return best;
}
void check(const std::vector<std::uint32_t>& text) {
    const auto saved = text;
    const auto expected = oracle(text);
    const auto result = blc_repeat::find_repeat(text.data(), text.size());
    assert(result.start == expected.start && result.unit == expected.unit && result.repeats == expected.repeats);
    assert(text == saved);
}
int main() {
    assert(blc_repeat::find_repeat(nullptr, 0).repeats == 0);
    bool invalid = false, overflow = false;
    try { blc_repeat::find_repeat(nullptr, 1); } catch (const std::invalid_argument&) { invalid = true; }
    try { blc_repeat::find_repeat(nullptr, static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())); }
    catch (const std::overflow_error&) { overflow = true; }
    assert(invalid && overflow);
    std::size_t tested = 0;
    for (int size = 0; size <= 16; ++size) {
        for (std::uint32_t bits = 0; bits < (std::uint32_t{1} << size); ++bits) {
            std::vector<std::uint32_t> text(size);
            for (int i = 0; i < size; ++i) text[i] = (bits & (std::uint32_t{1} << i)) ? 0xffffffffU : 0;
            check(text); ++tested;
        }
    }
    // Unique padding forces the linear path for the exhaustive binary corpus.
    for (int size = 0; size <= 13; ++size) {
        for (std::uint32_t bits = 0; bits < (std::uint32_t{1} << size); ++bits) {
            std::vector<std::uint32_t> sample(size), text(257);
            for (int i = 0; i < 257; ++i) text[i] = static_cast<std::uint32_t>(1000 + i);
            for (int i = 0; i < size; ++i) sample[i] = (bits >> i) & 1;
            text.insert(text.end(), sample.begin(), sample.end());
            auto expected = oracle(sample);
            if (expected.repeats) expected.start += 257;
            const auto result = blc_repeat::find_repeat(text.data(), text.size());
            assert(result.start == expected.start && result.unit == expected.unit && result.repeats == expected.repeats);
            ++tested;
        }
    }
    std::vector<std::uint32_t> failure_input(4096);
    for (std::size_t i = 0; i < failure_input.size(); ++i) failure_input[i] = static_cast<std::uint32_t>(i % 17);
    failure_input.back() = 12345;
    const auto original = failure_input;
    int failures = 0;
    bool eventually_succeeded = false;
    for (int budget = 0; budget < 200; ++budget) {
        allocation_budget = budget;
        try {
            const auto result = blc_repeat::find_repeat(failure_input.data(), failure_input.size());
            allocation_budget = -1;
            assert(result.start == 0 && result.unit == 17 && result.repeats == 240);
            eventually_succeeded = true;
            break;
        } catch (const std::bad_alloc&) {
            allocation_budget = -1;
            ++failures;
        }
        assert(failure_input == original);
    }
    assert(failures > 10 && eventually_succeeded && failure_input == original);
    std::mt19937 rng(80317);
    for (int round = 0; round < 1500; ++round) {
        const auto size = 250U + rng() % 800U;
        const auto alphabet = 1U + rng() % 30U;
        std::vector<std::uint32_t> text(size);
        for (auto& value : text) value = (rng() % alphabet) * 0x10204081U;
        const auto period = 1U + rng() % 200U;
        const auto start = rng() % (size / 3);
        for (std::size_t i = start + period; i < std::min<std::size_t>(size, start + period * 4 + 3); ++i) text[i] = text[i - period];
        check(text); ++tested;
    }
    for (std::size_t size : {65535U, 65536U, 65537U, 1000000U}) {
        std::vector<std::uint32_t> text(size);
        for (std::size_t i = 0; i < size; ++i) text[i] = static_cast<std::uint32_t>(i);
        assert(blc_repeat::find_repeat(text.data(), text.size()).repeats == 0);
        std::fill(text.begin() + 1, text.end() - 1, 0);
        const auto result = blc_repeat::find_repeat(text.data(), text.size());
        assert(result.start == 0 && result.unit == 1 && result.repeats == static_cast<int>(size - 1));
        ++tested;
    }
    std::cout << "PASS: " << tested << " exhaustive/adversarial native cases; input immutable; bounds rejected\n";
}
