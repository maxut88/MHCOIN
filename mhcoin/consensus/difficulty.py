"""Compact nBits difficulty + authoritative get_next_work / MTP.

Mainnet/testnet difficulty:
  * height < DIFFICULTY_BTC_ACTIVATION_HEIGHT — legacy per-block W30 + 1/16 damping
    (preserves historical mainnet validation).
  * height >= activation — Bitcoin-style epoch retarget every
    DIFFICULTY_ADJUSTMENT_INTERVAL blocks, full step, timespan clamp ×1/4..×4,
    no damping.
"""

from __future__ import annotations

from collections.abc import Sequence

from mhcoin.consensus.params import (
    DIFFICULTY_ADJUSTMENT_INTERVAL,
    DIFFICULTY_BTC_ACTIVATION_HEIGHT,
    DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
    DIFFICULTY_LEGACY_DAMPING_NUMERATOR,
    DIFFICULTY_LEGACY_WINDOW,
    MTP_WINDOW,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
)


def bits_to_target(bits: int) -> int:
    """Decode compact nBits into integer target."""
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
    # Interpret hash as little-endian integer
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


def _clamp_timespan(actual: int, expected: int) -> int:
    if expected <= 0:
        return actual
    min_timespan = max(1, expected // 4)
    max_timespan = expected * 4
    if actual < min_timespan:
        return min_timespan
    if actual > max_timespan:
        return max_timespan
    return actual


def _apply_target(
    old_target: int, adjusted_timespan: int, expected_timespan: int, limit: int
) -> int:
    if old_target <= 0:
        raise ValueError("invalid parent target")
    if old_target > limit:
        old_target = limit
    if expected_timespan <= 0:
        return min(old_target, limit)
    calculated = old_target * adjusted_timespan // expected_timespan
    if calculated < 1:
        calculated = 1
    if calculated > limit:
        calculated = limit
    return calculated


def _get_next_work_legacy(
    *,
    parent_height: int,
    parent_bits: int,
    window_timestamps: Sequence[int],
    limit: int,
) -> int:
    """Pre-activation: per-block retarget, W30, damping 1/16."""
    if parent_height <= 0:
        return parent_bits

    n = len(window_timestamps)
    if n < 2:
        return parent_bits

    if n > DIFFICULTY_LEGACY_WINDOW:
        window_timestamps = window_timestamps[-DIFFICULTY_LEGACY_WINDOW:]
        n = len(window_timestamps)

    intervals = n - 1
    expected_timespan = intervals * TARGET_BLOCK_TIME_SECONDS
    if expected_timespan <= 0:
        return parent_bits

    actual_timespan = int(window_timestamps[-1]) - int(window_timestamps[0])
    if actual_timespan < 0:
        actual_timespan = 0
    adjusted_timespan = _clamp_timespan(actual_timespan, expected_timespan)

    old_target = bits_to_target(parent_bits)
    calculated_target = _apply_target(
        old_target, adjusted_timespan, expected_timespan, limit
    )

    # Damped step: move 1/16 of the gap.
    if calculated_target < old_target:
        delta = old_target - calculated_target
        adjustment = max(
            1,
            delta
            * DIFFICULTY_LEGACY_DAMPING_NUMERATOR
            // DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
        )
        new_target = old_target - adjustment
    elif calculated_target > old_target:
        delta = calculated_target - old_target
        adjustment = max(
            1,
            delta
            * DIFFICULTY_LEGACY_DAMPING_NUMERATOR
            // DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
        )
        new_target = old_target + adjustment
    else:
        new_target = old_target

    if new_target < 1:
        new_target = 1
    if new_target > limit:
        new_target = limit
    return target_to_bits(new_target)


def _get_next_work_bitcoin(
    *,
    parent_height: int,
    parent_bits: int,
    window_timestamps: Sequence[int],
    limit: int,
) -> int:
    """Bitcoin-style: retarget only every DIFFICULTY_ADJUSTMENT_INTERVAL blocks."""
    next_height = parent_height + 1
    interval = DIFFICULTY_ADJUSTMENT_INTERVAL
    if next_height % interval != 0:
        return parent_bits

    # Need `interval` timestamps ending at parent (heights next_height-interval .. parent).
    if len(window_timestamps) < interval:
        # Not enough history (should not happen on mainnet at height>=interval).
        return parent_bits

    window = list(window_timestamps[-interval:])
    actual_timespan = int(window[-1]) - int(window[0])
    if actual_timespan < 0:
        actual_timespan = 0
    expected_timespan = interval * TARGET_BLOCK_TIME_SECONDS
    adjusted_timespan = _clamp_timespan(actual_timespan, expected_timespan)

    old_target = bits_to_target(parent_bits)
    new_target = _apply_target(old_target, adjusted_timespan, expected_timespan, limit)
    return target_to_bits(new_target)


def get_next_work(
    *,
    network: str,
    parent_height: int,
    parent_bits: int,
    window_timestamps: Sequence[int],
) -> int:
    """
    Authoritative next-block compact bits for the child of ``parent``.

    ``window_timestamps``: oldest → newest timestamps ending at parent.
    Caller should supply up to DIFFICULTY_ADJUSTMENT_INTERVAL samples.

    Deterministic, integer-only, no wall-clock dependence.
    """
    net = network.strip().lower()
    params = get_network_params(net)
    limit = pow_limit_for_network(net)

    if uses_fixed_easy_difficulty(net):
        return params.genesis_bits

    next_height = parent_height + 1
    if next_height < DIFFICULTY_BTC_ACTIVATION_HEIGHT:
        return _get_next_work_legacy(
            parent_height=parent_height,
            parent_bits=parent_bits,
            window_timestamps=window_timestamps,
            limit=limit,
        )
    return _get_next_work_bitcoin(
        parent_height=parent_height,
        parent_bits=parent_bits,
        window_timestamps=window_timestamps,
        limit=limit,
    )


def assert_target_within_pow_limit(bits: int, *, network: str) -> None:
    """Raise ValueError if compact bits decode above network POW_LIMIT."""
    target = bits_to_target(bits)
    if target <= 0:
        raise ValueError("non-positive target")
    if target > pow_limit_for_network(network):
        raise ValueError("target exceeds POW_LIMIT")
