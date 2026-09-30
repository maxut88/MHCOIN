"""Cumulative Proof-of-Work (chain selection by work, not height alone)."""

from __future__ import annotations

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.difficulty import bits_to_target


# Max target for 256-bit hash space (little-endian PoW comparison uses full range)
_POW_LIMIT = (1 << 256) - 1


def work_for_bits(bits: int) -> int:
    """
    Approximate work contributed by one block: 2^256 / (target + 1).
    Higher difficulty (lower target) → more work.
    """
    target = bits_to_target(bits)
    if target <= 0:
        raise ValueError("invalid target")
    return _POW_LIMIT // (target + 1)


def work_for_header(header: BlockHeader) -> int:
    return work_for_bits(header.bits)


def get_chain_work(blocks: list[Block]) -> int:
    """Sum of work for a contiguous valid chain (genesis → tip order)."""
    return sum(work_for_header(b.header) for b in blocks)


def select_best_chain(candidates: list[list[Block]]) -> list[Block]:
    """
    Choose the chain with the greatest cumulative PoW (offline helper).

    Tie-break for this helper: longer height, then tip hash hex.
    Live Stage 6 reorg policy differs: equal work keeps the current active tip
    (no unnecessary reorganization). See docs/REORG.md.
    """
    if not candidates:
        raise ValueError("no candidate chains")
    best = candidates[0]
    best_work = get_chain_work(best)
    for chain in candidates[1:]:
        w = get_chain_work(chain)
        if w > best_work:
            best, best_work = chain, w
        elif w == best_work:
            if len(chain) > len(best):
                best = chain
            elif len(chain) == len(best):
                tip_a = chain[-1].block_hash().hex() if chain else ""
                tip_b = best[-1].block_hash().hex() if best else ""
                if tip_a > tip_b:
                    best = chain
    return best
