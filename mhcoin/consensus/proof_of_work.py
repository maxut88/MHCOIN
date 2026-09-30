"""Proof-of-Work: SHA256(SHA256(header))."""

from __future__ import annotations

import time
from typing import Callable

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.difficulty import hash_meets_target


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
    """
    if fix_merkle:
        block.set_merkle_root()
    t0 = time.time()
    for nonce in range(start_nonce, max_nonce + 1):
        block.header.nonce = nonce
        h = block.header.block_hash()
        if hash_meets_target(h, block.header.bits):
            if progress:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, (nonce - start_nonce + 1) / elapsed)
            return block
        if progress and nonce % 100_000 == 0 and nonce != start_nonce:
            elapsed = max(time.time() - t0, 1e-9)
            progress(nonce, h, (nonce - start_nonce + 1) / elapsed)
    raise RuntimeError("nonce space exhausted without finding PoW")
