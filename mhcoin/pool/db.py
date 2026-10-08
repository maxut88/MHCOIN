"""SQLite persistence for pool shares, blocks, balances, payouts."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class PoolDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock:
            c = self._conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    address TEXT NOT NULL,
                    worker TEXT NOT NULL DEFAULT '',
                    extranonce1 BLOB NOT NULL,
                    last_seen REAL NOT NULL,
                    hashrate REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (address, worker)
                );
                CREATE TABLE IF NOT EXISTS shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    address TEXT NOT NULL,
                    worker TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    height INTEGER NOT NULL,
                    difficulty REAL NOT NULL,
                    is_block INTEGER NOT NULL DEFAULT 0,
                    block_hash TEXT,
                    round_id INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shares_round ON shares(round_id);
                CREATE INDEX IF NOT EXISTS idx_shares_addr ON shares(address);
                CREATE TABLE IF NOT EXISTS rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_ts REAL NOT NULL,
                    ended_ts REAL,
                    height INTEGER,
                    block_hash TEXT,
                    reward_sats INTEGER,
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS credits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    amount_sats INTEGER NOT NULL,
                    round_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'immature',
                    created_ts REAL NOT NULL,
                    matured_ts REAL
                );
                CREATE INDEX IF NOT EXISTS idx_credits_addr ON credits(address, status);
                CREATE TABLE IF NOT EXISTS balances (
                    address TEXT PRIMARY KEY,
                    matured_sats INTEGER NOT NULL DEFAULT 0,
                    immature_sats INTEGER NOT NULL DEFAULT 0,
                    paid_sats INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    address TEXT NOT NULL,
                    amount_sats INTEGER NOT NULL,
                    txid TEXT,
                    status TEXT NOT NULL DEFAULT 'sent'
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            c.commit()
            row = c.execute("SELECT id FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                    (time.time(),),
                )
                c.commit()

    def current_round_id(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                    (time.time(),),
                )
                self._conn.commit()
                return int(cur.lastrowid)
            return int(row["id"])

    def upsert_worker(
        self, address: str, worker: str, extranonce1: bytes, hashrate: float = 0.0
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO workers(address, worker, extranonce1, last_seen, hashrate)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(address, worker) DO UPDATE SET
                    extranonce1=excluded.extranonce1,
                    last_seen=excluded.last_seen,
                    hashrate=CASE WHEN excluded.hashrate>0 THEN excluded.hashrate ELSE workers.hashrate END
                """,
                (address, worker, extranonce1, time.time(), float(hashrate)),
            )
            self._conn.commit()

    def touch_worker(self, address: str, worker: str, hashrate: float | None = None) -> None:
        with self._lock:
            if hashrate is None:
                self._conn.execute(
                    "UPDATE workers SET last_seen=? WHERE address=? AND worker=?",
                    (time.time(), address, worker),
                )
            else:
                self._conn.execute(
                    "UPDATE workers SET last_seen=?, hashrate=? WHERE address=? AND worker=?",
                    (time.time(), float(hashrate), address, worker),
                )
            self._conn.commit()

    def add_share(
        self,
        *,
        address: str,
        worker: str,
        job_id: str,
        height: int,
        difficulty: float,
        is_block: bool = False,
        block_hash: str | None = None,
    ) -> int:
        rid = self.current_round_id()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO shares(ts, address, worker, job_id, height, difficulty, is_block, block_hash, round_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    address,
                    worker,
                    job_id,
                    int(height),
                    float(difficulty),
                    1 if is_block else 0,
                    block_hash,
                    rid,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def close_round_with_block(
        self,
        *,
        height: int,
        block_hash: str,
        reward_sats: int,
        fee_percent: float,
    ) -> int:
        """PROP-split reward for current round; open a new round. Returns closed round id."""
        with self._lock:
            rid = self.current_round_id()
            rows = self._conn.execute(
                "SELECT address, SUM(difficulty) AS d FROM shares WHERE round_id=? GROUP BY address",
                (rid,),
            ).fetchall()
            total = sum(float(r["d"] or 0) for r in rows)
            fee = max(0.0, min(100.0, float(fee_percent))) / 100.0
            distributable = int(int(reward_sats) * (1.0 - fee))
            now = time.time()
            if total > 0 and distributable > 0:
                for r in rows:
                    share = float(r["d"] or 0) / total
                    amt = int(distributable * share)
                    if amt <= 0:
                        continue
                    addr = str(r["address"])
                    self._conn.execute(
                        """
                        INSERT INTO credits(address, amount_sats, round_id, status, created_ts)
                        VALUES (?, ?, ?, 'immature', ?)
                        """,
                        (addr, amt, rid, now),
                    )
                    self._conn.execute(
                        """
                        INSERT INTO balances(address, matured_sats, immature_sats, paid_sats)
                        VALUES (?, 0, ?, 0)
                        ON CONFLICT(address) DO UPDATE SET
                            immature_sats = immature_sats + excluded.immature_sats
                        """,
                        (addr, amt),
                    )
            self._conn.execute(
                """
                UPDATE rounds SET ended_ts=?, height=?, block_hash=?, reward_sats=?, status='immature'
                WHERE id=?
                """,
                (now, int(height), block_hash, int(reward_sats), rid),
            )
            self._conn.execute(
                "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                (now,),
            )
            self._conn.commit()
            return rid

    def mature_rounds(self, tip_height: int, confirms: int) -> int:
        """Move immature rounds/credits to matured when tip is deep enough."""
        matured = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, height FROM rounds WHERE status='immature' AND height IS NOT NULL"
            ).fetchall()
            now = time.time()
            for r in rows:
                h = int(r["height"])
                if tip_height < h + int(confirms):
                    continue
                rid = int(r["id"])
                credits = self._conn.execute(
                    "SELECT id, address, amount_sats FROM credits WHERE round_id=? AND status='immature'",
                    (rid,),
                ).fetchall()
                for c in credits:
                    amt = int(c["amount_sats"])
                    addr = str(c["address"])
                    self._conn.execute(
                        "UPDATE credits SET status='matured', matured_ts=? WHERE id=?",
                        (now, int(c["id"])),
                    )
                    self._conn.execute(
                        """
                        UPDATE balances SET
                            immature_sats = MAX(0, immature_sats - ?),
                            matured_sats = matured_sats + ?
                        WHERE address=?
                        """,
                        (amt, amt, addr),
                    )
                    matured += 1
                self._conn.execute(
                    "UPDATE rounds SET status='matured' WHERE id=?",
                    (rid,),
                )
            self._conn.commit()
        return matured

    def balances_above(self, threshold: int) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, matured_sats FROM balances WHERE matured_sats >= ?",
                (int(threshold),),
            ).fetchall()
            return [(str(r["address"]), int(r["matured_sats"])) for r in rows]

    def record_payout(self, address: str, amount: int, txid: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO payouts(ts, address, amount_sats, txid, status)
                VALUES (?, ?, ?, ?, 'sent')
                """,
                (time.time(), address, int(amount), txid),
            )
            self._conn.execute(
                """
                UPDATE balances SET
                    matured_sats = MAX(0, matured_sats - ?),
                    paid_sats = paid_sats + ?
                WHERE address=?
                """,
                (int(amount), int(amount), address),
            )
            self._conn.commit()

    def stats_overview(self) -> dict:
        with self._lock:
            now = time.time()
            workers = self._conn.execute(
                "SELECT COUNT(*) AS n FROM workers WHERE last_seen > ?",
                (now - 600,),
            ).fetchone()["n"]
            hashrate = self._conn.execute(
                "SELECT COALESCE(SUM(hashrate),0) AS h FROM workers WHERE last_seen > ?",
                (now - 600,),
            ).fetchone()["h"]
            shares_1h = self._conn.execute(
                "SELECT COUNT(*) AS n FROM shares WHERE ts > ?",
                (now - 3600,),
            ).fetchone()["n"]
            shares_round = self._conn.execute(
                "SELECT COUNT(*) AS n FROM shares WHERE round_id=?",
                (self.current_round_id(),),
            ).fetchone()["n"]
            miners_total = self._conn.execute(
                "SELECT COUNT(DISTINCT address) AS n FROM workers"
            ).fetchone()["n"]
            blocks = self._conn.execute(
                """
                SELECT height, block_hash, reward_sats, ended_ts, status
                FROM rounds WHERE block_hash IS NOT NULL
                ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
            top = self._conn.execute(
                """
                SELECT address, worker, hashrate, last_seen
                FROM workers
                WHERE last_seen > ?
                ORDER BY hashrate DESC
                LIMIT 50
                """,
                (now - 600,),
            ).fetchall()
            return {
                "workers_active": int(workers),
                "miners_total": int(miners_total),
                "pool_hashrate": float(hashrate or 0),
                "shares_1h": int(shares_1h),
                "shares_round": int(shares_round),
                "current_round": self.current_round_id(),
                "blocks": [dict(b) for b in blocks],
                "workers": [dict(w) for w in top],
                "ts": now,
            }

    def miner_stats(self, address: str) -> dict:
        with self._lock:
            bal = self._conn.execute(
                "SELECT * FROM balances WHERE address=?", (address,)
            ).fetchone()
            workers = self._conn.execute(
                "SELECT worker, hashrate, last_seen FROM workers WHERE address=?",
                (address,),
            ).fetchall()
            shares = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(difficulty),0) AS d FROM shares WHERE address=?",
                (address,),
            ).fetchone()
            return {
                "address": address,
                "matured_sats": int(bal["matured_sats"]) if bal else 0,
                "immature_sats": int(bal["immature_sats"]) if bal else 0,
                "paid_sats": int(bal["paid_sats"]) if bal else 0,
                "workers": [dict(w) for w in workers],
                "shares": int(shares["n"]),
                "share_difficulty": float(shares["d"]),
            }

    def next_extranonce1(self) -> bytes:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='en1_counter'"
            ).fetchone()
            n = int(row["value"]) if row else 1
            self._conn.execute(
                """
                INSERT INTO meta(key, value) VALUES ('en1_counter', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(n + 1),),
            )
            self._conn.commit()
            return int(n).to_bytes(4, "big")
