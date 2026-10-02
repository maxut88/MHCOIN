"""Stdlib-only PoW worker for multiprocessing spawn (no mhcoin imports).

Kept outside consensus fingerprint. Parent verifies solutions with consensus hash256.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any


def _hash256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _bits_to_target(bits: int) -> int:
    exponent = bits >> 24
    mantissa = bits & 0x007FFFFF
    if bits & 0x00800000:
        raise ValueError("negative target not allowed")
    if exponent <= 3:
        return mantissa >> (8 * (3 - exponent))
    return mantissa << (8 * (exponent - 3))


def pow_worker(
    header_prefix: bytes,
    bits: int,
    start_nonce: int,
    stride: int,
    max_nonce: int,
    stop_event: Any,
    result_queue: Any,
    hashes_value: Any,
    progress_every: int,
) -> None:
    """Search nonces ``start_nonce, start_nonce+stride, …`` until stop or hit."""
    target = _bits_to_target(bits)
    local = 0
    nonce = start_nonce
    pack = struct.pack
    while nonce <= max_nonce:
        if stop_event.is_set():
            if local:
                hashes_value.value += local
            return
        digest = _hash256(header_prefix + pack("<I", nonce))
        local += 1
        if int.from_bytes(digest, "little") <= target:
            hashes_value.value += local
            result_queue.put((nonce, digest))
            stop_event.set()
            return
        if progress_every > 0 and local >= progress_every:
            hashes_value.value += local
            local = 0
            if stop_event.is_set():
                return
        nonce += stride
    if local:
        hashes_value.value += local
