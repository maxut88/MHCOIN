"""Proof-of-Work: SHA256(SHA256(header))."""

from __future__ import annotations

import hashlib
import struct
import time
from typing import Callable

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.difficulty import bits_to_target, hash_meets_target

_NONCE_OFF = 76


def verify_proof_of_work(header: BlockHeader) -> bool:
    return hash_meets_target(header.block_hash(), header.bits)


def mine_block(
    block: Block,
    *,
    max_nonce: int = 0xFFFFFFFF,
    start_nonce: int = 0,
    progress: Callable[[int, bytes, float], None] | None = None,
    fix_merkle: bool = True,
) -> Block:
    """
    Search nonce until header hash meets bits target.
    Mutates block.header.nonce and returns the same block.

    Hot path matches live mining: in-place 80-byte header buffer + HASH256.
    """
    if fix_merkle:
        block.set_merkle_root()
    buf = bytearray(block.header.serialize())
    if len(buf) != 80:
        raise ValueError(f"bad header length {len(buf)}")
    target = bits_to_target(block.header.bits)
    pack_nonce = struct.pack_into
    sha256 = hashlib.sha256
    t0 = time.time()
    start = int(start_nonce)
    for nonce in range(start, max_nonce + 1):
        pack_nonce("<I", buf, _NONCE_OFF, nonce)
        h = sha256(sha256(buf).digest()).digest()
        if int.from_bytes(h, "little") <= target:
            block.header.nonce = nonce
            if progress:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, (nonce - start + 1) / elapsed)
            return block
        if progress and nonce % 100_000 == 0 and nonce != start:
            elapsed = max(time.time() - t0, 1e-9)
            progress(nonce, h, (nonce - start + 1) / elapsed)
    raise RuntimeError("nonce space exhausted without finding PoW")
