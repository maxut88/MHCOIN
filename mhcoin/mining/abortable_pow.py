"""Cancellable PoW search for live mining (outside consensus fingerprint).

Same HASH256 / target check as ``mine_block``, with cheap in-memory
``abort_check`` polls. Does **not** chunk via ``mine_block`` + RuntimeError
(that cut Mac CLI hashrate ~1.5× vs a tight PoW loop / Desktop).

Hot path: mutate an 80-byte header buffer in place (nonce @ offset 76) and
double-SHA256 without re-serializing the header each attempt. Final
``header.nonce`` / ``header.block_hash()`` stay consensus-identical.

Desktop default: single-lane. Multi-worker search remains for experiments;
GIL thrashing on many machines made parallel Desktop mining show high CPU
with *lower* H/s.
"""

from __future__ import annotations

import copy
import hashlib
import struct
import sys
import threading
import time
from typing import Callable

from mhcoin.blockchain.block import Block
from mhcoin.consensus.difficulty import bits_to_target

# Rare polls keep the PoW loop tight (macOS / Linux).
ABORT_CHECK_INTERVAL = 25_000
# Windows: balance Stop/Start responsiveness vs hashrate. 2_000 was safe but
# taxed older CPUs; 12_000 ≈ 50–80 ms latency at ~150–250 kH/s.
ABORT_CHECK_INTERVAL_WIN32 = 12_000
PROGRESS_INTERVAL = 100_000

_HEADER_LEN = 80
_NONCE_OFF = 76


def _default_abort_every() -> int:
    if sys.platform == "win32":
        return ABORT_CHECK_INTERVAL_WIN32
    return ABORT_CHECK_INTERVAL


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


def _header_work_buf(block: Block) -> tuple[bytearray, int]:
    """Return mutable 80-byte header + integer target for the hot PoW loop."""
    raw = bytearray(block.header.serialize())
    if len(raw) != _HEADER_LEN:
        raise ValueError(f"bad header length {len(raw)}")
    return raw, bits_to_target(block.header.bits)


def mine_block_cancellable(
    block: Block,
    *,
    abort_check: Callable[[], bool] | None = None,
    abort_every: int | None = None,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    start_nonce: int = 0,
    nonce_step: int = 1,
    fix_merkle: bool = True,
    duty_cycle: float = 1.0,
) -> Block:
    """Mine with periodic ``abort_check``; raises ``MiningAborted`` if True.

    Default path (step=1, duty=1) is the tight single-lane loop.
    """
    if abort_every is None:
        abort_every = _default_abort_every()
    if abort_every < 1:
        abort_every = 1
    step = 1 if nonce_step < 1 else int(nonce_step)
    duty = _clamp_duty(duty_cycle)
    if fix_merkle:
        block.set_merkle_root()

    buf, target = _header_work_buf(block)
    pack_nonce = struct.pack_into
    sha256 = hashlib.sha256

    # —— Fast path: in-place nonce + HASH256 (Windows / old Mac friendly) ——
    if step == 1 and duty >= 1.0:
        t0 = time.time()
        start = int(start_nonce)
        for nonce in range(start, int(max_nonce) + 1):
            if abort_check is not None and (
                nonce == start or (nonce - start) % abort_every == 0
            ):
                if abort_check():
                    raise MiningAborted("mining aborted")
            pack_nonce("<I", buf, _NONCE_OFF, nonce)
            h = sha256(sha256(buf).digest()).digest()
            if int.from_bytes(h, "little") <= target:
                block.header.nonce = nonce
                if progress is not None:
                    elapsed = max(time.time() - t0, 1e-9)
                    progress(nonce, h, (nonce - start + 1) / elapsed)
                return block
            if (
                progress is not None
                and nonce % PROGRESS_INTERVAL == 0
                and nonce != start
            ):
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, (nonce - start + 1) / elapsed)
        raise RuntimeError("nonce space exhausted without finding PoW")

    # —— Stride / duty-cycle path (parallel workers or CPU throttle) ——
    t0 = time.time()
    hashes = 0
    chunk_t0 = time.perf_counter()
    for nonce in range(int(start_nonce), int(max_nonce) + 1, step):
        if abort_check is not None and (hashes == 0 or hashes % abort_every == 0):
            if abort_check():
                raise MiningAborted("mining aborted")
            if duty < 1.0 and hashes > 0:
                worked = time.perf_counter() - chunk_t0
                if worked > 0:
                    _interruptible_sleep(worked * (1.0 - duty) / duty, abort_check)
                chunk_t0 = time.perf_counter()
        pack_nonce("<I", buf, _NONCE_OFF, nonce)
        h = sha256(sha256(buf).digest()).digest()
        hashes += 1
        if int.from_bytes(h, "little") <= target:
            block.header.nonce = nonce
            if progress is not None:
                elapsed = max(time.time() - t0, 1e-9)
                progress(nonce, h, hashes / elapsed)
            return block
        if progress is not None and hashes % PROGRESS_INTERVAL == 0:
            elapsed = max(time.time() - t0, 1e-9)
            progress(nonce, h, hashes / elapsed)
    raise RuntimeError("nonce space exhausted without finding PoW")


def mine_block_parallel(
    block: Block,
    *,
    workers: int = 1,
    abort_check: Callable[[], bool] | None = None,
    abort_every: int | None = None,
    progress: Callable[[int, bytes, float], None] | None = None,
    max_nonce: int = 0xFFFFFFFF,
    fix_merkle: bool = True,
    duty_cycle: float = 1.0,
) -> Block:
    """Multi-worker nonce stride (optional). Desktop uses single-lane instead."""
    n = max(1, int(workers))
    duty = _clamp_duty(duty_cycle)
    if abort_every is None:
        abort_every = _default_abort_every()
    if fix_merkle:
        block.set_merkle_root()
    if n == 1:
        return mine_block_cancellable(
            block,
            abort_check=abort_check,
            abort_every=abort_every,
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
            if abort_check and abort_check():
                stop.set()
                return True
            return False

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
                abort_every=abort_every,
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
            stop.set()
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
        return block
    if errors:
        raise errors[0]
    raise MiningAborted("mining aborted")
