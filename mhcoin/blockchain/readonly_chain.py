"""Read-only active-chain access for wallet history.

Safe to open while NodeRuntime holds the same datadir: uses SQLite WAL
``mode=ro`` on ``chain.sqlite`` only (never touches utxo.sqlite / disk lock).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.chain import STATUS_ACTIVE


class ReadOnlyChain:
    """Minimal chain tip + block-by-height reader for Desktop history scans."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        db_path = self.data_dir / "chain.sqlite"
        if not db_path.is_file():
            self._db: sqlite3.Connection | None = None
            self._height = -1
            self._tip_hash: bytes | None = None
            return
        # uri mode=ro: concurrent with NodeRuntime WAL writers.
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            self._db.execute("PRAGMA query_only=ON")
            self._db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            pass
        self._height = -1
        self._tip_hash = None
        self._load_tip()

    def _meta_get(self, key: str) -> str | None:
        if self._db is None:
            return None
        try:
            row = self._db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return str(row[0]) if row else None

    def _load_tip(self) -> None:
        tip_hex = self._meta_get("tip_hash")
        tip_h = self._meta_get("tip_height")
        if tip_hex and tip_h is not None:
            try:
                self._tip_hash = bytes.fromhex(tip_hex)
                self._height = int(tip_h)
                return
            except (ValueError, TypeError):
                pass
        if self._db is None:
            self._height = -1
            self._tip_hash = None
            return
        try:
            row = self._db.execute(
                "SELECT block_hash, height FROM block_index "
                "WHERE status=? ORDER BY height DESC LIMIT 1",
                (STATUS_ACTIVE,),
            ).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            self._tip_hash = row[0]
            self._height = int(row[1])
        else:
            self._tip_hash = None
            self._height = -1

    @property
    def height(self) -> int:
        return self._height

    @property
    def tip_hash(self) -> bytes | None:
        return self._tip_hash

    def refresh_tip(self) -> None:
        """Re-read tip meta (call between HTTP requests while node advances)."""
        self._load_tip()

    def get_block_by_height(self, height: int) -> Block | None:
        if self._db is None or height < 0:
            return None
        try:
            row = self._db.execute(
                "SELECT raw FROM block_index WHERE height=? AND status=?",
                (height, STATUS_ACTIVE),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return Block.deserialize(row[0])
        except Exception:
            return None

    def get_block_by_hash(self, block_hash: bytes) -> Block | None:
        if self._db is None or not block_hash:
            return None
        try:
            row = self._db.execute(
                "SELECT raw FROM block_index WHERE block_hash=?",
                (block_hash,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        try:
            return Block.deserialize(row[0])
        except Exception:
            return None

    def get_height_of_hash(self, block_hash: bytes) -> int | None:
        if self._db is None or not block_hash:
            return None
        try:
            row = self._db.execute(
                "SELECT height FROM block_index WHERE block_hash=?",
                (block_hash,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return int(row[0])

    def get_index_status(self, block_hash: bytes) -> int | None:
        if self._db is None or not block_hash:
            return None
        try:
            row = self._db.execute(
                "SELECT status FROM block_index WHERE block_hash=?",
                (block_hash,),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        return int(row[0])

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
