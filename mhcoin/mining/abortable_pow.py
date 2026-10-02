"""Cancellable PoW search for live mining (outside consensus fingerprint).

Same HASH256 / target check as ``mine_block``, with cheap in-memory
``abort_check`` polls. Does **not** chunk via ``mine_block`` + RuntimeError
(that cut Mac CLI hashrate ~1.5× vs a tight PoW loop / Desktop).

Optional multi-process workers search disjoint nonce strides so all CPU
cores contribute. Consensus-identical hashes; only the searcher changes.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from queue import Empty
from typing import Any, Callable

from mhcoin.blockchain.block import Block
from mhcoin.consensus.difficulty import hash_meets_target
from mhcoin.mining.pow_worker import pow_worker

# Nonce steps between abort polls (serial path). Cheap in-memory checks only.
ABORT_CHECK_INTERVAL = 25_000
# Match classic Desktop / mine_block progress cadence (serial path).
PROGRESS_INTERVAL = 100_000


class MiningAborted(Exception):
    """PoW search cancelled (user stop or canonical tip moved)."""


def resolve_worker_count(workers: int | None = None) -> int:
    """Resolve PoW process count.

    Priority: explicit ``workers`` → ``MHCOIN_MINER_WORKERS`` → ``os.cpu_count()``.
    """
    if workers is not None:
        return max(1, int(workers))
    env = (os.environ.get("MHCOIN_MINER_WORKERS") or "").strip()
    if env:
        return max(1, int(env))
    return max(1, int(os.cpu_count() or 1))


def _mp_context() -> Any:
    import threading

    # spawn: safe with PyInstaller / non-main threads (Desktop miner thread).
    # fork: faster on Linux/macOS when started from the main thread (CLI).
    if getattr(sys, "frozen", False) or sys.platform == "win32":
        return mp.get_context("spawn")
    if threading.current_thread() is not threading.main_thread():
        return mp.get_context("spawn")
    return mp.get_context("fork")


def _mine_serial(
    block: Block,
    *,
    abort_check: Callable[[], bool] | None,
    abort_every: int,
    progress: Callable[[int, bytes, float], None] | None,
    max_nonce: int,
    start_nonce: int,
) -> Block:
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


def _mine_parallel(
    block: Block,
    *,
    workers: int,
    abort_check: Callable[[], bool] | None,
    progress: Callable[[int, bytes, float], None] | None,
    max_nonce: int,
    start_nonce: int,
) -> Block:
    header = block.header.serialize()
    if len(header) != 80:
        raise RuntimeError(f"unexpected header length {len(header)}")
    prefix = header[:76]
    bits = block.header.bits

    ctx = _mp_context()
    stop_event = ctx.Event()
    result_queue = ctx.Queue(maxsize=max(workers, 1))
    hashes_value = ctx.Value("Q", 0)
    # Flush shared counter less often — lock contention kills multi-core scaling.
    progress_every = 200_000

    procs: list[Any] = []
    for i in range(workers):
        worker_start = start_nonce + i
        if worker_start > max_nonce:
            break
        p = ctx.Process(
            target=pow_worker,
            args=(
                prefix,
                bits,
                worker_start,
                workers,
                max_nonce,
                stop_event,
                result_queue,
                hashes_value,
                progress_every,
            ),
            name=f"mhcoin-pow-{i}",
            daemon=True,
        )
        procs.append(p)
        p.start()

    t0 = time.time()
    last_prog = 0.0
    last_hashes = 0
    found: tuple[int, bytes] | None = None
    aborted = False
    try:
        while True:
            if abort_check is not None and abort_check():
                aborted = True
                stop_event.set()
                break

            try:
                item = result_queue.get(timeout=0.15)
            except Empty:
                item = None
            if item is not None:
                found = item
                stop_event.set()
                break

            alive = [p for p in procs if p.is_alive()]
            if not alive:
                try:
                    found = result_queue.get_nowait()
                except Empty:
                    found = None
                # Surface worker crashes instead of a vague exhausted message.
                bad = [p for p in procs if p.exitcode not in (0, None)]
                if found is None and bad:
                    codes = ", ".join(f"{p.name}={p.exitcode}" for p in bad)
                    raise RuntimeError(f"PoW workers exited abnormally: {codes}")
                break

            if progress is not None:
                now = time.time()
                if now - last_prog >= 0.45:
                    total = int(hashes_value.value)
                    if total > last_hashes:
                        elapsed = max(now - t0, 1e-9)
                        progress(total, b"", total / elapsed)
                        last_prog = now
                        last_hashes = total
    finally:
        stop_event.set()
        for p in procs:
            p.join(timeout=3.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=1.0)

    total = int(hashes_value.value)
    elapsed = max(time.time() - t0, 1e-9)
    if progress is not None and total > 0:
        progress(total if found is None else found[0], b"" if found is None else found[1], total / elapsed)

    if aborted:
        raise MiningAborted("mining aborted")

    if found is None:
        raise RuntimeError("nonce space exhausted without finding PoW")

    nonce, h = found
    block.header.nonce = int(nonce)
    # Verify in-process (same path as serial / consensus).
    got = block.header.block_hash()
    if got != h or not hash_meets_target(got, bits):
        raise RuntimeError("parallel PoW worker returned invalid solution")
    return block


def mine_block_cancellable(
    block: Block,
    *,
    abort_check: Callable[[], bool] | None = None,
    abort_every: int = ABORT_CHECK_INTERVAL,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    start_nonce: int = 0,
    fix_merkle: bool = True,
    workers: int | None = None,
) -> Block:
    """Mine with periodic ``abort_check``; raises ``MiningAborted`` if True.

    ``workers`` > 1 uses one process per worker (all CPU cores by default).
    """
    if abort_every < 1:
        abort_every = 1
    if fix_merkle:
        block.set_merkle_root()

    n_workers = resolve_worker_count(workers)
    if n_workers <= 1:
        return _mine_serial(
            block,
            abort_check=abort_check,
            abort_every=abort_every,
            progress=progress,
            max_nonce=max_nonce,
            start_nonce=start_nonce,
        )
    return _mine_parallel(
        block,
        workers=n_workers,
        abort_check=abort_check,
        progress=progress,
        max_nonce=max_nonce,
        start_nonce=start_nonce,
    )
