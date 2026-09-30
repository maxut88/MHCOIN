"""Compact nBits difficulty target helpers."""

from __future__ import annotations


def bits_to_target(bits: int) -> int:
    """Decode Bitcoin-style compact bits into integer target (MHCOIN-owned)."""
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
    if target <= 0:
        raise ValueError("non-positive target")
    h = f"{target:x}"
    if len(h) % 2:
        h = "0" + h
    size = len(h) // 2
    if size <= 3:
        mantissa = target << (8 * (3 - size))
        return (size << 24) | mantissa
    # take top 3 bytes
    shift = 8 * (size - 3)
    mantissa = target >> shift
    if mantissa & 0x00800000:
        mantissa >>= 8
        size += 1
    return (size << 24) | (mantissa & 0x007FFFFF)


def hash_meets_target(block_hash: bytes, bits: int) -> bool:
    target = bits_to_target(bits)
    # Interpret hash as little-endian integer (Bitcoin-like)
    value = int.from_bytes(block_hash, "little")
    return value <= target
