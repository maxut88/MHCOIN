"""Cancellable PoW search for live mining (outside consensus fingerprint).

Wraps ``mhcoin.consensus.proof_of_work.mine_block`` in small nonce chunks so
callers can abort when the canonical tip moves — without changing consensus
PoW / fingerprint sources.
"""

from __future__ import annotations

import time
from typing import Callable

from mhcoin.blockchain.block import Block
from mhcoin.consensus.proof_of_work import mine_block

# Nonce steps between abort polls. Cheap in-memory checks only — no SQLite.
ABORT_CHECK_INTERVAL = 25_000


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
) -> Block:
    """Mine with periodic ``abort_check``; raises ``MiningAborted`` if True.

    Progress is reported at chunk boundaries because ``mine_block`` only
    callbacks every 100k nonces — smaller abort chunks would otherwise
    silence the Desktop mining log (nonce / H/s lines).
    """
    if abort_every < 1:
        abort_every = 1
    nonce = start_nonce
    first = True
    t0 = time.time()
    while nonce <= max_nonce:
        if abort_check is not None and abort_check():
            raise MiningAborted("mining aborted")
        end = min(nonce + abort_every - 1, max_nonce)
        try:
            return mine_block(
                block,
                start_nonce=nonce,
                max_nonce=end,
                progress=progress,
                fix_merkle=first,
            )
        except RuntimeError:
            # Chunk exhausted without PoW — continue.
            first = False
            if progress is not None:
                elapsed = max(time.time() - t0, 1e-9)
                tried = end - start_nonce + 1
                progress(end, block.header.block_hash(), tried / elapsed)
            nonce = end + 1
    raise RuntimeError("nonce space exhausted without finding PoW")
