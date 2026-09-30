"""Cross-process/thread exclusive lock for a chain datadir.

Multiple Blockchain() instances on one datadir race UTXO tip repair; this serializes them.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # Windows — best-effort no-op (Desktop uses thread lock too)
    fcntl = None  # type: ignore


@contextmanager
def chain_disk_lock(data_dir: Path, *, timeout: float = 30.0) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / ".mhcoin_chain.lock"
    fd = open(path, "a+", encoding="utf-8")
    try:
        if fcntl is None:
            yield
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"chain lock timeout: {path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()
