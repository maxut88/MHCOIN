"""Cancellable PoW search for live mining (outside consensus fingerprint).

Same HASH256 / target check as ``mine_block``, with cheap in-memory
``abort_check`` polls. Does **not** chunk via ``mine_block`` + RuntimeError
(that cut Mac CLI hashrate ~1.5× vs a tight PoW loop / Desktop).

Optional multi-worker search (nonce stride) for Desktop max-CPU mining.
``hashlib`` releases the GIL during digest, so threads scale across cores.

``duty_cycle`` (0.05–1.0) time-slices hashing so CPU load % is real even when
extra workers no longer increase hashrate (common on Apple Silicon).
"""

from __future__ import annotations

import copy
import threading
import time
from typing import Callable

from mhcoin.blockchain.block import Block
from mhcoin.consensus.difficulty import hash_meets_target

# Nonce steps between abort polls. Cheap in-memory checks only — no SQLite.
# Keep low enough that Stop feels instant (~few ms at typical Desktop rates).
ABORT_CHECK_INTERVAL = 8_000
# Match classic Desktop / mine_block progress cadence.
PROGRESS_INTERVAL = 100_000


class MiningAborted(Exception):
    """PoW search cancelled (user stop or canonical tip moved)."""


def _clamp_duty(duty_cycle: float) -> float:
    try:
        d = float(duty_cycle)
    except (TypeError, ValueError):
        d = 1.0
    if d >= 0.999:
        return 1.0
    if d < 0.05:
        return 0.05
    return d


def _interruptible_sleep(
    seconds: float,
    abort_check: Callable[[], bool] | None,
) -> None:
    """Sleep in short slices so Stop/reconfigure stays responsive."""
    if seconds <= 0:
        return
    deadline = time.perf_counter() + float(seconds)
    while True:
        if abort_check is not None and abort_check():
            raise MiningAborted("mining aborted")
        left = deadline - time.perf_counter()
        if left <= 0:
            return
        time.sleep(min(0.05, left))


def mine_block_cancellable(
    block: Block,
    *,
    abort_check: Callable[[], bool] | None = None,
    abort_every: int = ABORT_CHECK_INTERVAL,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    start_nonce: int = 0,
    nonce_step: int = 1,
    fix_merkle: bool = True,
    duty_cycle: float = 1.0,
) -> Block:
    """Mine with periodic ``abort_check``; raises ``MiningAborted`` if True.

    ``nonce_step`` > 1 assigns a stride lane (worker ``start_nonce`` in
    ``0..step-1``) so multiple threads can search the same template.

    ``duty_cycle`` < 1.0 inserts interruptible sleeps so wall-clock hashrate
    tracks the requested CPU load percent.
    """
    if abort_every < 1:
        abort_every = 1
    step = 1 if nonce_step < 1 else int(nonce_step)
    duty = _clamp_duty(duty_cycle)
    if fix_merkle:
        block.set_merkle_root()
    t0 = time.time()
    hashes = 0
    chunk_t0 = time.perf_counter()
    for nonce in range(int(start_nonce), int(max_nonce) + 1, step):
        if abort_check is not None and (
            hashes == 0 or hashes % abort_every == 0
        ):
            if abort_check():
                raise MiningAborted("mining aborted")
            if duty < 1.0 and hashes > 0:
                worked = time.perf_counter() - chunk_t0
                if worked > 0:
                    _interruptible_sleep(worked * (1.0 - duty) / duty, abort_check)
                chunk_t0 = time.perf_counter()
        block.header.nonce = nonce
        h = block.header.block_hash()
        hashes += 1
        if hash_meets_target(h, block.header.bits):
            if progress is not None:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, hashes / elapsed)
            return block
        if progress is not None:
            # step=1: keep classic nonce cadence (tests / Desktop log).
            # stride workers: tick on hashes done in this lane.
            tick = (
                (nonce != start_nonce and nonce % PROGRESS_INTERVAL == 0)
                if step == 1
                else (hashes % PROGRESS_INTERVAL == 0)
            )
            if tick:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, hashes / elapsed)
    raise RuntimeError("nonce space exhausted without finding PoW")


def mine_block_parallel(
    block: Block,
    *,
    workers: int = 1,
    abort_check: Callable[[], bool] | None = None,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    fix_merkle: bool = True,
    duty_cycle: float = 1.0,
) -> Block:
    """Mine with ``workers`` threads (nonce stride). Returns winning block copy applied to ``block``."""
    n = max(1, int(workers))
    duty = _clamp_duty(duty_cycle)
    if fix_merkle:
        block.set_merkle_root()
    if n == 1:
        return mine_block_cancellable(
            block,
            abort_check=abort_check,
            progress=progress,
            max_nonce=max_nonce,
            start_nonce=0,
            nonce_step=1,
            fix_merkle=False,
            duty_cycle=duty,
        )

    stop = threading.Event()
    found_lock = threading.Lock()
    found: list[Block] = []
    hps_lock = threading.Lock()
    hps_parts = [0.0] * n
    errors: list[BaseException] = []

    def _worker(wid: int) -> None:
        local = copy.deepcopy(block)

        def _abort() -> bool:
            if stop.is_set():
                return True
            return bool(abort_check and abort_check())

        def _prog(nonce: int, h: bytes, hps: float) -> None:
            with hps_lock:
                hps_parts[wid] = float(hps)
                total = sum(hps_parts)
            if progress is not None:
                progress(nonce, h, total)

        try:
            mine_block_cancellable(
                local,
                abort_check=_abort,
                progress=_prog,
                max_nonce=max_nonce,
                start_nonce=wid,
                nonce_step=n,
                fix_merkle=False,
                duty_cycle=duty,
            )
            with found_lock:
                if not found:
                    found.append(local)
                    stop.set()
        except (MiningAborted, KeyboardInterrupt):
            return
        except Exception as exc:  # noqa: BLE001
            with found_lock:
                errors.append(exc)
                stop.set()

    threads = [
        threading.Thread(target=_worker, args=(i,), name=f"mhcoin-pow-{i}", daemon=True)
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if found:
        winner = found[0]
        block.header.nonce = int(winner.header.nonce)
        # Keep other header fields identical; merkle already set on template.
        return block
    if errors:
        raise errors[0]
    raise MiningAborted("mining aborted")
