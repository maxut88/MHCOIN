"""Flat block files (Bitcoin-style ``blocks/blkNNNNN.dat``).

Index metadata stays in ``chain.sqlite``; raw block bytes live on disk here.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

# ~128 MiB per file, same ballpark as Bitcoin Core's max block file size.
DEFAULT_MAX_FILE_BYTES = 128 * 1024 * 1024


class BlockFileStore:
    """Append-only blk*.dat writer/reader under ``<datadir>/blocks/``."""

    def __init__(
        self,
        blocks_dir: Path,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.blocks_dir = Path(blocks_dir)
        self.max_file_bytes = int(max_file_bytes)
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_n, self._size = self._discover_tip_file()

    def _path(self, file_n: int) -> Path:
        return self.blocks_dir / f"blk{file_n:05d}.dat"

    def _discover_tip_file(self) -> tuple[int, int]:
        files = sorted(self.blocks_dir.glob("blk*.dat"))
        if not files:
            return 0, 0
        last = files[-1]
        try:
            n = int(last.stem.replace("blk", ""))
        except ValueError:
            n = 0
        return n, last.stat().st_size

    def append(self, raw: bytes) -> tuple[int, int, int]:
        """Append raw block bytes. Returns ``(file_n, offset, size)``."""
        if not raw:
            raise ValueError("empty block bytes")
        with self._lock:
            if self._size > 0 and self._size + len(raw) > self.max_file_bytes:
                self._file_n += 1
                self._size = 0
            path = self._path(self._file_n)
            offset = self._size
            with path.open("ab") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            self._size = offset + len(raw)
            return self._file_n, offset, len(raw)

    def read(self, file_n: int, offset: int, size: int) -> bytes:
        with self._lock:
            path = self._path(int(file_n))
            if not path.is_file():
                raise FileNotFoundError(path)
            with path.open("rb") as f:
                f.seek(int(offset))
                data = f.read(int(size))
            if len(data) != int(size):
                raise OSError(
                    f"short read blk{file_n:05d}.dat@{offset}+{size} (got {len(data)})"
                )
            return data
