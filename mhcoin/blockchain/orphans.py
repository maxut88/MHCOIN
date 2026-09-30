"""Bounded orphan block pool (Stage 6)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from mhcoin.blockchain.block import Block

logger = logging.getLogger("mhcoin.chain")

# Node safety policy (not consensus)
MAX_ORPHAN_BLOCKS = 64
MAX_ORPHAN_BYTES = 2_000_000


@dataclass
class OrphanEntry:
    block: Block
    size: int
    received_at: float


class OrphanPool:
    """
    Holds blocks whose parent is unknown.

    Indexed by block hash and by prev_hash. Strict size/count limits.
    Does not validate chain state until parent arrives.
    """

    def __init__(
        self,
        *,
        max_blocks: int = MAX_ORPHAN_BLOCKS,
        max_bytes: int = MAX_ORPHAN_BYTES,
    ) -> None:
        self.max_blocks = max_blocks
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._by_hash: dict[str, OrphanEntry] = {}
        self._by_prev: dict[str, set[str]] = {}
        self._total_bytes = 0

    def __len__(self) -> int:
        return len(self._by_hash)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def contains(self, block_hash: bytes) -> bool:
        return block_hash.hex() in self._by_hash

    def add(self, block: Block) -> bool:
        """
        Add orphan. Returns False if duplicate or rejected by limits.
        Evicts oldest orphans when over limit.
        """
        hx = block.block_hash().hex()
        raw_size = len(block.serialize())
        with self._lock:
            if hx in self._by_hash:
                return False
            if raw_size > self.max_bytes:
                logger.info("Orphan rejected (too large) hash=%s size=%s", hx, raw_size)
                return False
            while (
                len(self._by_hash) >= self.max_blocks
                or self._total_bytes + raw_size > self.max_bytes
            ):
                if not self._by_hash:
                    break
                self._evict_oldest_unlocked()
            if (
                len(self._by_hash) >= self.max_blocks
                or self._total_bytes + raw_size > self.max_bytes
            ):
                logger.info("Orphan rejected (pool full) hash=%s", hx)
                return False
            entry = OrphanEntry(block=block, size=raw_size, received_at=time.time())
            self._by_hash[hx] = entry
            prev = block.header.previous_block_hash.hex()
            self._by_prev.setdefault(prev, set()).add(hx)
            self._total_bytes += raw_size
            logger.info(
                "Orphan stored hash=%s prev=%s pool=%s/%s",
                hx,
                prev,
                len(self._by_hash),
                self.max_blocks,
            )
            return True

    def remove(self, block_hash: bytes) -> Block | None:
        with self._lock:
            return self._remove_unlocked(block_hash.hex())

    def pop_children(self, parent_hash: bytes) -> list[Block]:
        """Remove and return orphans that claim `parent_hash` as previous."""
        with self._lock:
            keys = list(self._by_prev.get(parent_hash.hex(), set()))
            out: list[Block] = []
            for hx in keys:
                blk = self._remove_unlocked(hx)
                if blk is not None:
                    out.append(blk)
            return out

    def _remove_unlocked(self, hx: str) -> Block | None:
        entry = self._by_hash.pop(hx, None)
        if entry is None:
            return None
        prev = entry.block.header.previous_block_hash.hex()
        bucket = self._by_prev.get(prev)
        if bucket is not None:
            bucket.discard(hx)
            if not bucket:
                del self._by_prev[prev]
        self._total_bytes -= entry.size
        if self._total_bytes < 0:
            self._total_bytes = 0
        return entry.block

    def _evict_oldest_unlocked(self) -> None:
        oldest_hx = None
        oldest_t = None
        for hx, e in self._by_hash.items():
            if oldest_t is None or e.received_at < oldest_t:
                oldest_t = e.received_at
                oldest_hx = hx
        if oldest_hx is not None:
            logger.info("Orphan evicted hash=%s", oldest_hx)
            self._remove_unlocked(oldest_hx)
