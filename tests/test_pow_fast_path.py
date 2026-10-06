"""Fast-path PoW must match consensus header.block_hash() / mine_block."""

from __future__ import annotations

import time

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.difficulty import hash_meets_target, target_to_bits
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.mining.abortable_pow import (
    ABORT_CHECK_INTERVAL_WIN32,
    mine_block_cancellable,
)


def _easy_bits() -> int:
    # Very easy target so a nonce is found quickly in tests.
    return target_to_bits((1 << 240) - 1)


def test_fast_path_matches_header_hash():
    block = Block(
        header=BlockHeader(
            version=1,
            previous_block_hash=b"\xab" * 32,
            merkle_root=b"\xcd" * 32,
            timestamp=1_700_000_000,
            bits=_easy_bits(),
            nonce=0,
        )
    )
    mine_block_cancellable(block, abort_every=1000, fix_merkle=False)
    assert hash_meets_target(block.header.block_hash(), block.header.bits)
    assert block.header.block_hash() == block.block_hash()


def test_fast_path_same_nonce_as_mine_block():
    hdr_kwargs = dict(
        version=1,
        previous_block_hash=b"\x11" * 32,
        merkle_root=b"\x22" * 32,
        timestamp=1_700_000_123,
        bits=_easy_bits(),
        nonce=0,
    )
    a = Block(header=BlockHeader(**hdr_kwargs))
    b = Block(header=BlockHeader(**hdr_kwargs))
    mine_block(a, fix_merkle=False)
    mine_block_cancellable(b, abort_every=500, fix_merkle=False)
    assert a.header.nonce == b.header.nonce
    assert a.header.block_hash() == b.header.block_hash()


def test_win32_abort_interval_raised():
    # Guard against regressing back to the 2_000 tax on Windows CPUs.
    assert ABORT_CHECK_INTERVAL_WIN32 >= 10_000


def test_fast_path_throughput_smoke():
    """Sanity: in-place loop should clear tens of kH/s even on slow CI hosts."""
    block = Block(
        header=BlockHeader(
            version=1,
            previous_block_hash=b"\x33" * 32,
            merkle_root=b"\x44" * 32,
            timestamp=1_700_000_000,
            bits=0x1d00ffff,
            nonce=0,
        )
    )
    n = 80_000
    t0 = time.perf_counter()
    try:
        mine_block_cancellable(
            block,
            abort_every=10_000,
            start_nonce=0,
            max_nonce=n - 1,
            fix_merkle=False,
        )
    except RuntimeError:
        pass
    dt = max(time.perf_counter() - t0, 1e-9)
    hps = n / dt
    assert hps > 40_000, f"unexpectedly slow PoW loop: {hps:.0f} H/s"
