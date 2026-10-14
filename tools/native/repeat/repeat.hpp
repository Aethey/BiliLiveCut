// SPDX-License-Identifier: MIT
#pragma once
#include <cstddef>
#include <cstdint>

namespace blc_repeat {
struct Result {
    std::int32_t start = 0;
    std::int32_t unit = 0;
    std::int32_t repeats = 0;
};
// Exact code-point indices; caller owns immutable input for the entire call.
// No Python, filesystem, network, tensor, CUDA or thread-pool dependencies.
Result find_repeat(const std::uint32_t* text, std::size_t size);
}
