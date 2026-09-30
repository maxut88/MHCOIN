"""Merkle tree — deterministic, double-SHA256 nodes."""

from __future__ import annotations

from mhcoin.crypto.hashing import hash256


def merkle_root(txids: list[bytes]) -> bytes:
    """
    Compute Merkle root of transaction ids (32-byte hashes).
    Odd nodes duplicated (Bitcoin-like behaviour, MHCOIN-owned code).
    Empty list → hash of empty bytes (should not happen for real blocks).
    """
    if not txids:
        return hash256(b"")
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            nxt.append(hash256(level[i] + level[i + 1]))
        level = nxt
    return level[0]


def verify_merkle_root(txids: list[bytes], expected: bytes) -> bool:
    return merkle_root(txids) == expected
