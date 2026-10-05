"""Read-only active-chain access for wallet history.

Safe to open while NodeRuntime holds the same datadir: uses SQLite WAL
``mode=ro`` on ``chain.sqlite`` only (never touches ``chainstate/`` / disk lock).
Raw block bytes come from ``blocks/blk*.dat`` (schema v3+) with a fallback to
legacy embedded ``raw`` blobs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.blockstore import BlockFileStore
from mhcoin.blockchain.chain import STATUS_ACTIVE


class ReadOnlyChain:
    """Minimal chain tip + block-by-height reader for Desktop history scans."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        db_path = self.data_dir / "chain.sqlite"
        self._blockstore = BlockFileStore(self.data_dir / "blocks")
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

    def _raw_from_index_row(self, row: tuple) -> bytes | None:
        """Row: raw, file_id, data_pos, data_len (file_* may be missing on old DBs)."""
        raw = row[0] if row else None
        file_id = row[1] if len(row) > 1 else None
        data_pos = row[2] if len(row) > 2 else None
        data_len = row[3] if len(row) > 3 else None
        if file_id is not None and data_pos is not None and data_len is not None:
            try:
                return self._blockstore.read(int(file_id), int(data_pos), int(data_len))
            except Exception:
                return None
        if raw:
            return bytes(raw)
        return None

    def _fetch_raw_row(self, sql: str, params: tuple) -> tuple | None:
        if self._db is None:
            return None
        try:
            return self._db.execute(sql, params).fetchone()
        except sqlite3.Error:
            # Pre-v3 index without flat-file columns.
            try:
                legacy_sql = sql.replace(
                    "raw, file_id, data_pos, data_len", "raw"
                )
                row = self._db.execute(legacy_sql, params).fetchone()
            except sqlite3.Error:
                return None
            if not row:
                return None
            return (row[0], None, None, None)

    def get_block_by_height(self, height: int) -> Block | None:
        if self._db is None or height < 0:
            return None
        row = self._fetch_raw_row(
            "SELECT raw, file_id, data_pos, data_len FROM block_index "
            "WHERE height=? AND status=?",
            (height, STATUS_ACTIVE),
        )
        if not row:
            return None
        data = self._raw_from_index_row(row)
        if data is None:
            return None
        try:
            return Block.deserialize(data)
        except Exception:
            return None

    def get_block_by_hash(self, block_hash: bytes) -> Block | None:
        if self._db is None or not block_hash:
            return None
        row = self._fetch_raw_row(
            "SELECT raw, file_id, data_pos, data_len FROM block_index "
            "WHERE block_hash=?",
            (block_hash,),
        )
        if not row:
            return None
        data = self._raw_from_index_row(row)
        if data is None:
            return None
        try:
            return Block.deserialize(data)
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
