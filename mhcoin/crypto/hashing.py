"""SHA-256 hashing utilities (consensus uses double-SHA256)."""

from __future__ import annotations

import hashlib


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash256(data: bytes) -> bytes:
    """Double SHA-256 as used in block/transaction IDs."""
    return sha256(sha256(data))


def _ripemd160(data: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, TypeError):
        pass
    try:
        from Crypto.Hash import RIPEMD160
    except ImportError:
        from Cryptodome.Hash import RIPEMD160  # type: ignore

    h = RIPEMD160.new()
    h.update(data)
    return h.digest()


def hash160(data: bytes) -> bytes:
    """RIPEMD-160(SHA-256(data)) for pubkey fingerprints in addresses."""
    return _ripemd160(sha256(data))
