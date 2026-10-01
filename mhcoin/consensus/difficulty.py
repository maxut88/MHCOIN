"""Compact nBits difficulty + authoritative get_next_work / MTP."""

from __future__ import annotations

from collections.abc import Sequence

from mhcoin.consensus.params import (
    DIFFICULTY_DAMPING_DENOMINATOR,
    DIFFICULTY_DAMPING_NUMERATOR,
    DIFFICULTY_WINDOW,
    MTP_WINDOW,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
)


def bits_to_target(bits: int) -> int:
    """Decode Bitcoin-style compact bits into integer target (MHCOIN-owned)."""
    if bits < 0 or bits > 0xFFFFFFFF:
        raise ValueError("bits out of u32 range")
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    if bits & 0x00800000:
        raise ValueError("negative target not allowed")
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))
    return target


def target_to_bits(target: int) -> int:
    """Encode integer target into canonical compact bits (no sign bit)."""
    if target <= 0:
        raise ValueError("non-positive target")
    h = f"{target:x}"
    if len(h) % 2:
        h = "0" + h
    size = len(h) // 2
    if size <= 3:
        mantissa = target << (8 * (3 - size))
    else:
        shift = 8 * (size - 3)
        mantissa = target >> shift
    # Compact encoding uses bit 0x00800000 as a sign flag; never leave it set.
    if mantissa & 0x00800000:
        mantissa >>= 8
        size += 1
    return (size << 24) | (mantissa & 0x007FFFFF)


def hash_meets_target(block_hash: bytes, bits: int) -> bool:
    target = bits_to_target(bits)
    # Interpret hash as little-endian integer (Bitcoin-like)
    value = int.from_bytes(block_hash, "little")
    return value <= target


# ---------------------------------------------------------------------------
# POW_LIMIT — maximum allowed target (easiest difficulty) per network
# ---------------------------------------------------------------------------
# Mainnet POW_LIMIT equals the frozen genesis target so genesis remains valid
# and no easier-than-genesis target is ever accepted.

_MAINNET_GENESIS_BITS = get_network_params("mainnet").genesis_bits
POW_LIMIT_MAINNET = bits_to_target(_MAINNET_GENESIS_BITS)
POW_LIMIT_MAINNET_BITS = _MAINNET_GENESIS_BITS


def pow_limit_for_network(network: str) -> int:
    """Maximum allowed PoW target for ``network`` (integer)."""
    params = get_network_params(network)
    return bits_to_target(params.genesis_bits)


def uses_fixed_easy_difficulty(network: str) -> bool:
    """Regtest/localnet keep constant easy bits for development speed."""
    return network.strip().lower() in ("regtest", "localnet")


def median_time_past(timestamps: Sequence[int]) -> int:
    """
    Deterministic median of up to MTP_WINDOW prior block timestamps.

    ``timestamps`` should be oldest → newest (or any order; sorted copy used).
    Requires at least one timestamp.
    """
    if not timestamps:
        raise ValueError("median_time_past requires at least one timestamp")
    window = list(timestamps[-MTP_WINDOW:])
    window.sort()
    return window[len(window) // 2]


def get_next_work(
    *,
    network: str,
    parent_height: int,
    parent_bits: int,
    window_timestamps: Sequence[int],
) -> int:
    """
    Authoritative next-block compact bits for the child of ``parent``.

    ``window_timestamps``: oldest → newest timestamps ending at parent,
    length = min(DIFFICULTY_WINDOW, parent_height + 1). Must include parent.

    Deterministic, integer-only, no wall-clock dependence.
    """
    net = network.strip().lower()
    params = get_network_params(net)
    limit = pow_limit_for_network(net)

    if uses_fixed_easy_difficulty(net):
        return params.genesis_bits

    # Block #1 (parent = genesis): not enough interval history.
    if parent_height <= 0:
        return parent_bits

    n = len(window_timestamps)
    if n < 2:
        return parent_bits

    # Use at most DIFFICULTY_WINDOW samples (caller should already truncate).
    if n > DIFFICULTY_WINDOW:
        window_timestamps = window_timestamps[-DIFFICULTY_WINDOW:]
        n = len(window_timestamps)

    intervals = n - 1
    expected_timespan = intervals * TARGET_BLOCK_TIME_SECONDS
    if expected_timespan <= 0:
        return parent_bits

    actual_timespan = int(window_timestamps[-1]) - int(window_timestamps[0])
    # Guard pathological inverted clocks: treat as minimum span.
    if actual_timespan < 0:
        actual_timespan = 0

    min_timespan = expected_timespan // 4
    max_timespan = expected_timespan * 4
    if min_timespan < 1:
        min_timespan = 1
    adjusted_timespan = actual_timespan
    if adjusted_timespan < min_timespan:
        adjusted_timespan = min_timespan
    elif adjusted_timespan > max_timespan:
        adjusted_timespan = max_timespan

    old_target = bits_to_target(parent_bits)
    if old_target <= 0:
        raise ValueError("invalid parent target")
    if old_target > limit:
        # Should not happen on a valid chain; clamp before arithmetic.
        old_target = limit

    calculated_target = old_target * adjusted_timespan // expected_timespan
    if calculated_target < 1:
        calculated_target = 1
    if calculated_target > limit:
        calculated_target = limit

    # Damped step: move 1/8 of the gap (at least 1 when non-zero delta).
    if calculated_target < old_target:
        delta = old_target - calculated_target
        adjustment = max(
            1,
            delta * DIFFICULTY_DAMPING_NUMERATOR // DIFFICULTY_DAMPING_DENOMINATOR,
        )
        new_target = old_target - adjustment
    elif calculated_target > old_target:
        delta = calculated_target - old_target
        adjustment = max(
            1,
            delta * DIFFICULTY_DAMPING_NUMERATOR // DIFFICULTY_DAMPING_DENOMINATOR,
        )
        new_target = old_target + adjustment
    else:
        new_target = old_target

    if new_target < 1:
        new_target = 1
    if new_target > limit:
        new_target = limit

    return target_to_bits(new_target)


def assert_target_within_pow_limit(bits: int, *, network: str) -> None:
    """Raise ValueError if compact bits decode above network POW_LIMIT."""
    target = bits_to_target(bits)
    if target <= 0:
        raise ValueError("non-positive target")
    if target > pow_limit_for_network(network):
        raise ValueError("target exceeds POW_LIMIT")
