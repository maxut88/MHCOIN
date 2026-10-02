"""Cancellable PoW search for live mining (outside consensus fingerprint).

Same HASH256 / target check as ``mine_block``, with cheap in-memory
``abort_check`` polls. Does **not** chunk via ``mine_block`` + RuntimeError
(that cut Mac CLI hashrate ~1.5× vs a tight PoW loop / Desktop).
"""

from __future__ import annotations

import time
from typing import Callable

from mhcoin.blockchain.block import Block
from mhcoin.consensus.difficulty import hash_meets_target

# Nonce steps between abort polls. Cheap in-memory checks only — no SQLite.
ABORT_CHECK_INTERVAL = 25_000
# Match classic Desktop / mine_block progress cadence.
PROGRESS_INTERVAL = 100_000


class MiningAborted(Exception):
    """PoW search cancelled (user stop or canonical tip moved)."""


def mine_block_cancellable(
    block: Block,
    *,
    abort_check: Callable[[], bool] | None = None,
    abort_every: int = ABORT_CHECK_INTERVAL,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    start_nonce: int = 0,
    fix_merkle: bool = True,
) -> Block:
    """Mine with periodic ``abort_check``; raises ``MiningAborted`` if True."""
    if abort_every < 1:
        abort_every = 1
    if fix_merkle:
        block.set_merkle_root()
    t0 = time.time()
    for nonce in range(start_nonce, max_nonce + 1):
        if abort_check is not None and (
            nonce == start_nonce or (nonce - start_nonce) % abort_every == 0
        ):
            if abort_check():
                raise MiningAborted("mining aborted")
        block.header.nonce = nonce
        h = block.header.block_hash()
        if hash_meets_target(h, block.header.bits):
            if progress is not None:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, (nonce - start_nonce + 1) / elapsed)
            return block
        if (
            progress is not None
            and nonce % PROGRESS_INTERVAL == 0
            and nonce != start_nonce
        ):
            elapsed = max(time.time() - t0, 1e-9)
            progress(nonce, h, (nonce - start_nonce + 1) / elapsed)
    raise RuntimeError("nonce space exhausted without finding PoW")
