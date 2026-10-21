"""SQLite persistence for pool shares, blocks, balances, payouts."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("mhcoin.pool.db")


class PoolDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # Wait on contention instead of failing shares under multi-miner load.
        self._conn.execute("PRAGMA busy_timeout=5000")
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
                CREATE INDEX IF NOT EXISTS idx_shares_addr_ts ON shares(address, ts);
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
                CREATE TABLE IF NOT EXISTS hashrate_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    scope TEXT NOT NULL,
                    address TEXT NOT NULL DEFAULT '',
                    hashrate REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_hr_scope_ts
                    ON hashrate_samples(scope, address, ts);
                """
            )
            c.commit()
            # Forensic: keep rejected PoW hashes after clearing is_block.
            cols = {r[1] for r in c.execute("PRAGMA table_info(shares)").fetchall()}
            if "reject_reason" not in cols:
                c.execute("ALTER TABLE shares ADD COLUMN reject_reason TEXT")
                c.commit()
            # Block header time (matches explorer) — separate from round started/ended wall clock.
            round_cols = {r[1] for r in c.execute("PRAGMA table_info(rounds)").fetchall()}
            if "found_ts" not in round_cols:
                c.execute("ALTER TABLE rounds ADD COLUMN found_ts REAL")
                c.commit()
            # Crash-recovery intent log + duplicate-block guard (additive / safe).
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_accepts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    share_id INTEGER,
                    block_hash TEXT NOT NULL UNIQUE,
                    height INTEGER,
                    reward_sats INTEGER,
                    status TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pending_status
                    ON pending_accepts(status);
                CREATE TABLE IF NOT EXISTS share_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    extranonce2 TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    ntime TEXT NOT NULL DEFAULT '',
                    share_id INTEGER,
                    ts REAL NOT NULL,
                    UNIQUE(job_id, extranonce2, nonce, ntime)
                );
                """
            )
            # Unique block_hash among closed rounds (NULLs allowed for open).
            try:
                c.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_block_hash_unique "
                    "ON rounds(block_hash) WHERE block_hash IS NOT NULL"
                )
            except sqlite3.IntegrityError:
                logger.warning(
                    "cannot create unique rounds.block_hash index — duplicates exist"
                )
            # Miner dashboard: avoid full-table scans over 1M+ shares per address.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_shares_addr_worker_ts "
                "ON shares(address, worker, ts)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_shares_addr_block "
                "ON shares(address) WHERE is_block=1"
            )
            wcols = {r[1] for r in c.execute("PRAGMA table_info(workers)").fetchall()}
            if "last_share_ts" not in wcols:
                c.execute(
                    "ALTER TABLE workers ADD COLUMN last_share_ts REAL NOT NULL DEFAULT 0"
                )
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS miner_rollup (
                    address TEXT PRIMARY KEY,
                    shares_n INTEGER NOT NULL DEFAULT 0,
                    shares_d REAL NOT NULL DEFAULT 0,
                    last_share_ts REAL NOT NULL DEFAULT 0,
                    blocks_found INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS payout_intents (
                    id TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    amount_sats INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    txid TEXT,
                    created_ts REAL NOT NULL,
                    updated_ts REAL NOT NULL,
                    error TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_ts REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_payout_intents_status
                    ON payout_intents(status, next_attempt_ts);
                CREATE INDEX IF NOT EXISTS idx_payout_intents_addr
                    ON payout_intents(address, status);
                """
            )
            # Harden legacy payouts table for idempotent completion.
            pcols = {r[1] for r in c.execute("PRAGMA table_info(payouts)").fetchall()}
            if "intent_id" not in pcols:
                c.execute("ALTER TABLE payouts ADD COLUMN intent_id TEXT")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_payouts_intent_id "
                "ON payouts(intent_id) WHERE intent_id IS NOT NULL"
            )
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_payouts_txid "
                "ON payouts(txid) WHERE txid IS NOT NULL AND txid != ''"
            )
            # Crash-safe broadcast: keep raw tx + reserved inputs for resubmit /
            # reconcile without rebuilding a conflicting spend.
            intent_cols = {
                r[1] for r in c.execute("PRAGMA table_info(payout_intents)").fetchall()
            }
            if "raw_tx_hex" not in intent_cols:
                c.execute("ALTER TABLE payout_intents ADD COLUMN raw_tx_hex TEXT")
            if "input_keys" not in intent_cols:
                c.execute("ALTER TABLE payout_intents ADD COLUMN input_keys TEXT")
            c.commit()
            self._ensure_miner_rollups(c)
            row = c.execute("SELECT id FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                    (time.time(),),
                )
                c.commit()

    def _ensure_miner_rollups(self, c: sqlite3.Connection) -> None:
        """One-time backfill of miner_rollup + workers.last_share_ts (idempotent)."""
        done = c.execute(
            "SELECT value FROM meta WHERE key='miner_rollup_v1'"
        ).fetchone()
        if done and str(done["value"]) == "1":
            return
        t0 = time.perf_counter()
        c.execute("DELETE FROM miner_rollup")
        c.execute(
            """
            INSERT INTO miner_rollup(address, shares_n, shares_d, last_share_ts, blocks_found)
            SELECT address,
                   COUNT(*),
                   COALESCE(SUM(difficulty), 0),
                   COALESCE(MAX(ts), 0),
                   COALESCE(SUM(CASE WHEN is_block=1 THEN 1 ELSE 0 END), 0)
            FROM shares
            GROUP BY address
            """
        )
        c.execute(
            """
            UPDATE workers SET last_share_ts = COALESCE((
                SELECT MAX(s.ts) FROM shares s
                WHERE s.address = workers.address AND s.worker = workers.worker
            ), 0)
            """
        )
        c.execute(
            """
            INSERT INTO meta(key, value) VALUES ('miner_rollup_v1', '1')
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """
        )
        c.commit()
        logger.info(
            "miner_rollup backfill done in %.2fs (%d addresses)",
            time.perf_counter() - t0,
            c.execute("SELECT COUNT(*) FROM miner_rollup").fetchone()[0],
        )

    def _bump_miner_rollup(
        self,
        address: str,
        *,
        difficulty: float,
        ts: float,
        is_block: bool,
    ) -> None:
        """Incremental share totals (caller holds ``_lock``)."""
        self._conn.execute(
            """
            INSERT INTO miner_rollup(address, shares_n, shares_d, last_share_ts, blocks_found)
            VALUES (?, 1, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                shares_n = miner_rollup.shares_n + 1,
                shares_d = miner_rollup.shares_d + excluded.shares_d,
                last_share_ts = CASE
                    WHEN excluded.last_share_ts > miner_rollup.last_share_ts
                    THEN excluded.last_share_ts
                    ELSE miner_rollup.last_share_ts
                END,
                blocks_found = miner_rollup.blocks_found + excluded.blocks_found
            """,
            (address, float(difficulty), float(ts), 1 if is_block else 0),
        )

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
        if hashrate is not None and float(hashrate) > 0:
            # Aggregate address hashrate sample for miner charts.
            self.record_hashrate("miner", address, self.address_hashrate(address))

    def worker_stored_hashrate(self, address: str, worker: str) -> float:
        """Last stored H/s for a worker (0 if unknown)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT hashrate FROM workers WHERE address=? AND worker=?",
                (address, worker or "default"),
            ).fetchone()
            return float(row["hashrate"] or 0) if row else 0.0

    def estimate_worker_hashrate(
        self,
        address: str,
        worker: str,
        *,
        share_work: float,
        window: float = 180.0,
    ) -> float:
        """Estimate H/s from recent accepted shares.

        ``share_work`` = expected hashes per share (block work / share_factor).
        Used when the miner does not report ``hashrate`` RPC (e.g. older clients).
        """
        work = float(share_work or 0)
        if work <= 0:
            return 0.0
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts FROM shares WHERE address=? AND worker=? AND ts > ? ORDER BY ts ASC",
                (address, worker or "default", now - float(window)),
            ).fetchall()
        if len(rows) < 3:
            return 0.0
        t0 = float(rows[0]["ts"])
        # Use full observation to *now*, not (t_last - t_first). Endpoint-only
        # dt inflates H/s when shares arrive in bursts (common with 2+ miners
        # on a shared uplink) — UI jumps 10↔22 while real rate is flat.
        elapsed = now - t0
        if elapsed < 2.0:
            return 0.0
        elapsed = min(elapsed, float(window))
        return (len(rows) * work) / elapsed

    @staticmethod
    def sanitize_hashrate(reported: float, estimated: float) -> float:
        """Keep client-reported H/s only when it agrees with share reality.

        Some miners (or old Bitcoin ``diff×2^32`` formulas) report ~100× inflated
        H/s while share rate stays normal — that produced fake ~1 GH/s spikes.
        """
        rep = max(0.0, float(reported or 0))
        est = max(0.0, float(estimated or 0))
        if est <= 0:
            # No share baseline yet — still cap absurd solo reports for this pool.
            return min(rep, 100e9)  # 100 GH/s hard ceiling
        if rep <= 0:
            return est
        # Allow modest optimism (UI lag / burst), reject order-of-magnitude lies.
        ceiling = max(est * 4.0, est + 25e6)
        if rep > ceiling:
            return est
        return rep

    def address_hashrate(self, address: str, *, window: float = 600.0) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(hashrate),0) AS h FROM workers "
                "WHERE address=? AND last_seen > ?",
                (address, time.time() - window),
            ).fetchone()
            return float(row["h"] or 0)

    def record_hashrate(
        self, scope: str, address: str, hashrate: float, *, min_interval: float = 12.0
    ) -> None:
        """Append a hashrate sample (throttled). Prunes samples older than 48h."""
        scope = (scope or "pool").strip() or "pool"
        address = (address or "").strip()
        now = time.time()
        hps = max(0.0, float(hashrate or 0))
        with self._lock:
            last = self._conn.execute(
                "SELECT ts FROM hashrate_samples WHERE scope=? AND address=? "
                "ORDER BY id DESC LIMIT 1",
                (scope, address),
            ).fetchone()
            if last and (now - float(last["ts"])) < min_interval:
                return
            self._conn.execute(
                "INSERT INTO hashrate_samples(ts, scope, address, hashrate) VALUES (?,?,?,?)",
                (now, scope, address, hps),
            )
            self._conn.execute(
                "DELETE FROM hashrate_samples WHERE ts < ?",
                (now - 48 * 3600,),
            )
            self._conn.commit()

    def hashrate_history(
        self,
        scope: str,
        address: str = "",
        *,
        hours: float = 6.0,
        limit: int = 240,
        bucket_sec: float | None = None,
    ) -> list[dict]:
        """Return hashrate samples (ASC). Optional ``bucket_sec`` averages into fixed buckets."""
        since = time.time() - max(0.25, float(hours)) * 3600.0
        with self._lock:
            # Newest N samples in the window (ASC for charts). LIMIT on ASC
            # would keep the oldest slice and freeze the chart after disconnect.
            rows = self._conn.execute(
                """
                SELECT ts, hashrate FROM (
                    SELECT ts, hashrate FROM hashrate_samples
                    WHERE scope=? AND address=? AND ts >= ?
                    ORDER BY ts DESC
                    LIMIT ?
                ) AS recent
                ORDER BY ts ASC
                """,
                (scope, address or "", since, int(limit)),
            ).fetchall()
        points = [{"t": float(r["ts"]), "h": float(r["hashrate"] or 0)} for r in rows]
        bs = float(bucket_sec or 0)
        if bs <= 0 or not points:
            return points
        # Average into fixed-width buckets; keep chronological order.
        buckets: dict[int, list[float]] = {}
        order: list[int] = []
        for p in points:
            key = int(p["t"] // bs) * int(bs)
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(float(p["h"]))
        order.sort()
        return [
            {"t": float(k) + bs / 2.0, "h": sum(buckets[k]) / len(buckets[k])}
            for k in order
        ]

    def payments_history(self, address: str, *, limit: int = 500) -> list[dict]:
        """Cumulative paid series for charts (newest ``limit`` payouts, ASC)."""
        address = (address or "").strip()
        lim = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, amount_sats FROM (
                  SELECT ts, amount_sats FROM payouts
                  WHERE address=? AND status='sent'
                  ORDER BY ts DESC LIMIT ?
                ) ORDER BY ts ASC
                """,
                (address, lim),
            ).fetchall()
            base = 0
            if rows:
                base_row = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_sats), 0) AS s FROM payouts
                    WHERE address=? AND status='sent' AND ts < ?
                    """,
                    (address, float(rows[0]["ts"])),
                ).fetchone()
                base = int(base_row["s"] or 0) if base_row else 0
        out: list[dict] = []
        cum = base
        for r in rows:
            amt = int(r["amount_sats"] or 0)
            cum += amt
            out.append({"t": float(r["ts"]), "sats": cum, "amount_sats": amt})
        return out

    def earnings_history(self, address: str, *, limit: int = 500) -> list[dict]:
        """Cumulative credited awards for charts (newest ``limit`` credits, ASC).

        Uses DESC…LIMIT then reorders so recent hours are not dropped when a
        miner has >100 lifetime credits (ASC LIMIT was truncating the tail).
        """
        address = (address or "").strip()
        lim = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, amount_sats FROM (
                  SELECT created_ts AS ts, amount_sats FROM credits
                  WHERE address=?
                  ORDER BY created_ts DESC LIMIT ?
                ) ORDER BY ts ASC
                """,
                (address, lim),
            ).fetchall()
            base = 0
            if rows:
                base_row = self._conn.execute(
                    """
                    SELECT COALESCE(SUM(amount_sats), 0) AS s FROM credits
                    WHERE address=? AND created_ts < ?
                    """,
                    (address, float(rows[0]["ts"])),
                ).fetchone()
                base = int(base_row["s"] or 0) if base_row else 0
        out: list[dict] = []
        cum = base
        for r in rows:
            amt = int(r["amount_sats"] or 0)
            cum += amt
            out.append({"t": float(r["ts"]), "sats": cum, "amount_sats": amt})
        return out

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
        ts = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO shares(ts, address, worker, job_id, height, difficulty, is_block, block_hash, round_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
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
            self._bump_miner_rollup(
                address, difficulty=float(difficulty), ts=ts, is_block=bool(is_block)
            )
            self._conn.execute(
                """
                UPDATE workers SET last_share_ts=?
                WHERE address=? AND worker=? AND last_share_ts < ?
                """,
                (ts, address, worker, ts),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def clear_share_block_flag(
        self, share_id: int, *, reason: str | None = None
    ) -> None:
        """Mark a PoW share as not an accepted network block (reject/stale).

        Keeps ``block_hash`` for forensic history; clears ``is_block`` so stats
        / Your blocks (JOIN rounds) do not count it as accepted.
        """
        reason_s = (reason or "rejected")[:240]
        with self._lock:
            row = self._conn.execute(
                "SELECT address, is_block FROM shares WHERE id=?",
                (int(share_id),),
            ).fetchone()
            self._conn.execute(
                """
                UPDATE shares
                SET is_block=0,
                    reject_reason=COALESCE(?, reject_reason, 'rejected')
                WHERE id=?
                """,
                (reason_s, int(share_id)),
            )
            if row and int(row["is_block"] or 0) == 1:
                self._conn.execute(
                    """
                    UPDATE miner_rollup
                    SET blocks_found = CASE
                        WHEN blocks_found > 0 THEN blocks_found - 1 ELSE 0 END
                    WHERE address=?
                    """,
                    (str(row["address"]),),
                )
            self._conn.commit()

    def reconcile_rejected_block_shares(self) -> int:
        """Clear is_block on shares whose hash was never accepted into rounds.

        Preserves ``block_hash``; idempotent (already-cleared rows are skipped).
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE shares
                SET is_block=0,
                    reject_reason=COALESCE(reject_reason, 'reconciled_unaccepted')
                WHERE is_block=1
                  AND block_hash IS NOT NULL
                  AND block_hash NOT IN (
                    SELECT block_hash FROM rounds WHERE block_hash IS NOT NULL
                  )
                """
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def round_id_for_block_hash(self, block_hash: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM rounds WHERE block_hash=?", (block_hash,)
            ).fetchone()
            return int(row["id"]) if row else None

    def record_share_submission(
        self,
        *,
        job_id: str,
        extranonce2: str,
        nonce: str,
        ntime: str,
        share_id: int | None,
    ) -> bool:
        """Return True if this is a new submission; False if duplicate."""
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO share_submissions(job_id, extranonce2, nonce, ntime, share_id, ts)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(job_id),
                        str(extranonce2).lower(),
                        str(nonce).lower(),
                        str(ntime or "").lower(),
                        share_id,
                        time.time(),
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def begin_pending_accept(self, *, share_id: int, block_hash: str) -> int:
        """Record intent before node.accept_block (crash-recovery)."""
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, status FROM pending_accepts WHERE block_hash=?",
                (block_hash,),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = self._conn.execute(
                """
                INSERT INTO pending_accepts(
                    share_id, block_hash, height, reward_sats, status, created_ts, updated_ts
                ) VALUES (?, ?, NULL, NULL, 'intent', ?, ?)
                """,
                (int(share_id), block_hash, now, now),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def mark_pending_node_accepted(
        self, block_hash: str, *, height: int, reward_sats: int
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE pending_accepts
                SET status='accepted_node', height=?, reward_sats=?, updated_ts=?
                WHERE block_hash=? AND status IN ('intent', 'accepted_node')
                """,
                (int(height), int(reward_sats), now, block_hash),
            )
            self._conn.commit()

    def mark_pending_committed(self, block_hash: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE pending_accepts
                SET status='committed', updated_ts=?
                WHERE block_hash=?
                """,
                (now, block_hash),
            )
            self._conn.commit()

    def mark_pending_failed(self, block_hash: str, *, reason: str = "failed") -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE pending_accepts
                SET status=?, updated_ts=?
                WHERE block_hash=? AND status != 'committed'
                """,
                (reason[:64], now, block_hash),
            )
            self._conn.commit()

    def list_pending_accepts(self, *, statuses: tuple[str, ...] = ("intent", "accepted_node")) -> list[dict]:
        with self._lock:
            q = ",".join("?" * len(statuses))
            rows = self._conn.execute(
                f"SELECT * FROM pending_accepts WHERE status IN ({q}) ORDER BY id",
                statuses,
            ).fetchall()
            return [dict(r) for r in rows]

    def close_round_with_block(
        self,
        *,
        height: int,
        block_hash: str,
        reward_sats: int,
        fee_percent: float,
        found_ts: float | None = None,
    ) -> int:
        """PROP-split reward for current round; open a new round. Returns closed round id.

        Idempotent on ``block_hash``: a second call for the same hash returns the
        existing round id and does not create duplicate credits.

        ``found_ts`` should be the block header timestamp (explorer time).
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, found_ts FROM rounds WHERE block_hash=?", (block_hash,)
            ).fetchone()
            if existing:
                if found_ts is not None and existing["found_ts"] is None:
                    self._conn.execute(
                        "UPDATE rounds SET found_ts=? WHERE id=?",
                        (float(found_ts), int(existing["id"])),
                    )
                    self._conn.commit()
                return int(existing["id"])

            rid = self.current_round_id()
            # Guard: open round must not already have a different hash.
            open_row = self._conn.execute(
                "SELECT block_hash FROM rounds WHERE id=?", (rid,)
            ).fetchone()
            if open_row and open_row["block_hash"]:
                # Unexpected — open a fresh round.
                self._conn.execute(
                    "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                    (time.time(),),
                )
                rid = int(self._conn.execute("SELECT last_insert_rowid()").fetchone()[0])

            rows = self._conn.execute(
                "SELECT address, SUM(difficulty) AS d FROM shares WHERE round_id=? GROUP BY address",
                (rid,),
            ).fetchall()
            total = sum(float(r["d"] or 0) for r in rows)
            fee = max(0.0, min(100.0, float(fee_percent))) / 100.0
            distributable = int(int(reward_sats) * (1.0 - fee))
            now = time.time()
            header_ts = float(found_ts) if found_ts is not None else now
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
                UPDATE rounds SET ended_ts=?, found_ts=?, height=?, block_hash=?,
                       reward_sats=?, status='immature'
                WHERE id=?
                """,
                (now, header_ts, int(height), block_hash, int(reward_sats), rid),
            )
            self._conn.execute(
                "INSERT INTO rounds(started_ts, status) VALUES (?, 'open')",
                (now,),
            )
            self._conn.commit()
            return rid

    def set_round_found_ts(self, block_hash: str, found_ts: float) -> None:
        """Persist header timestamp for an existing round (backfill / reconcile)."""
        hx = (block_hash or "").strip()
        if not hx:
            return
        with self._lock:
            self._conn.execute(
                """
                UPDATE rounds SET found_ts=?
                WHERE block_hash=? AND (found_ts IS NULL OR found_ts=0)
                """,
                (float(found_ts), hx),
            )
            self._conn.commit()

    def mature_rounds(
        self,
        tip_height: int,
        confirms: int,
        *,
        canonical_hash_at: Callable[[int], str | None] | None = None,
        tip_height_now: Callable[[], int] | None = None,
    ) -> int:
        """Move immature rounds/credits to matured when tip is deep enough.

        Canonical outcomes when ``canonical_hash_at`` is set:
          * CANONICAL — hash matches → mature
          * NON_CANONICAL — different non-empty hash → orphan (void immature credits)
          * UNKNOWN — None / exception → defer (no DB changes)

        Node lookups run outside the DB lock. Before write, outcomes are
        re-checked to reduce tip/hash races.
        """
        confirms_i = int(confirms)
        with self._lock:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT id, height, block_hash FROM rounds "
                    "WHERE status='immature' AND height IS NOT NULL"
                ).fetchall()
            ]

        to_mature: list[tuple[int, int, str]] = []
        to_void: list[tuple[int, int, str, str]] = []
        for r in rows:
            h = int(r["height"])
            if tip_height < h + confirms_i:
                continue
            rid = int(r["id"])
            stored = str(r["block_hash"] or "").lower()
            if canonical_hash_at is not None:
                try:
                    canon = canonical_hash_at(h)
                except Exception:
                    logger.exception(
                        "canonical check failed for round id=%s height=%s — defer",
                        rid,
                        h,
                    )
                    continue  # UNKNOWN → defer
                if not canon:
                    logger.info(
                        "canonical hash unavailable for round id=%s height=%s — defer",
                        rid,
                        h,
                    )
                    continue  # UNKNOWN → defer
                canon_l = str(canon).lower()
                if canon_l != stored:
                    to_void.append((rid, h, stored, canon_l))
                    continue  # NON_CANONICAL
            to_mature.append((rid, h, stored))

        matured = 0
        now = time.time()
        with self._lock:
            for rid, h, stored, canon_l in to_void:
                row = self._conn.execute(
                    "SELECT status, block_hash FROM rounds WHERE id=?", (rid,)
                ).fetchone()
                if not row or str(row["status"]) != "immature":
                    continue
                # Re-validate before void: UNKNOWN must not orphan.
                if tip_height_now is not None:
                    try:
                        if int(tip_height_now()) < h + confirms_i:
                            continue
                    except Exception:
                        continue
                if canonical_hash_at is not None:
                    try:
                        again = canonical_hash_at(h)
                    except Exception:
                        continue
                    if not again:
                        continue
                    again_l = str(again).lower()
                    stored_now = str(row["block_hash"] or "").lower()
                    if again_l == stored_now:
                        # Became canonical — mature on a later tick / below.
                        to_mature.append((rid, h, stored_now))
                        continue
                    canon_l = again_l
                logger.warning(
                    "voiding non-canonical immature round id=%s height=%s "
                    "stored=%s canonical=%s",
                    rid,
                    h,
                    stored[:16] if stored else None,
                    canon_l[:16] if canon_l else None,
                )
                self._void_immature_round_locked(rid, now)

            # Dedupe mature list (void path may re-queue).
            seen_mature: set[int] = set()
            for rid, h, stored in to_mature:
                if rid in seen_mature:
                    continue
                seen_mature.add(rid)
                row = self._conn.execute(
                    "SELECT status, block_hash FROM rounds WHERE id=?", (rid,)
                ).fetchone()
                if not row or str(row["status"]) != "immature":
                    continue
                if tip_height_now is not None:
                    try:
                        if int(tip_height_now()) < h + confirms_i:
                            continue
                    except Exception:
                        continue
                if canonical_hash_at is not None:
                    try:
                        again = canonical_hash_at(h)
                    except Exception:
                        continue
                    if not again:
                        continue
                    stored_now = str(row["block_hash"] or "").lower()
                    if str(again).lower() != stored_now:
                        # Flipped non-canonical — void instead of mature.
                        logger.warning(
                            "voiding non-canonical immature round id=%s height=%s "
                            "stored=%s canonical=%s (pre-write recheck)",
                            rid,
                            h,
                            stored_now[:16] if stored_now else None,
                            str(again).lower()[:16],
                        )
                        self._void_immature_round_locked(rid, now)
                        continue
                credits = self._conn.execute(
                    "SELECT id, address, amount_sats FROM credits "
                    "WHERE round_id=? AND status='immature'",
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

    def _void_immature_round_locked(self, round_id: int, now: float) -> None:
        """Reverse immature credits for a round that left the canonical chain."""
        rid = int(round_id)
        credits = self._conn.execute(
            "SELECT id, address, amount_sats FROM credits WHERE round_id=? AND status='immature'",
            (rid,),
        ).fetchall()
        for c in credits:
            amt = int(c["amount_sats"])
            addr = str(c["address"])
            self._conn.execute(
                "UPDATE credits SET status='orphaned', matured_ts=? WHERE id=?",
                (now, int(c["id"])),
            )
            self._conn.execute(
                """
                UPDATE balances SET
                    immature_sats = MAX(0, immature_sats - ?)
                WHERE address=?
                """,
                (amt, addr),
            )
        self._conn.execute(
            "UPDATE rounds SET status='orphaned' WHERE id=?",
            (rid,),
        )

    def balances_above(self, threshold: int) -> list[tuple[str, int]]:
        """Return (address, matured_sats) at/above threshold, minus open intent locks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT address, matured_sats FROM balances WHERE matured_sats >= ?",
                (int(threshold),),
            ).fetchall()
            out: list[tuple[str, int]] = []
            for r in rows:
                addr = str(r["address"])
                matured = int(r["matured_sats"])
                locked = self._locked_intent_sats_unlocked(addr)
                avail = max(0, matured - locked)
                if avail >= int(threshold):
                    out.append((addr, avail))
            return out

    def _locked_intent_sats_unlocked(self, address: str) -> int:
        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(amount_sats), 0) AS s FROM payout_intents
            WHERE address=? AND status IN ('pending', 'broadcast')
            """,
            (address,),
        ).fetchone()
        return int(row["s"] if row else 0)

    def locked_intent_sats(self, address: str) -> int:
        with self._lock:
            return self._locked_intent_sats_unlocked(address)

    def create_payout_intent(self, address: str, amount_sats: int) -> str:
        """Persist payout intent before broadcast. Returns stable intent id."""
        import uuid

        intent_id = f"po_{uuid.uuid4().hex}"
        now = time.time()
        with self._lock:
            # Cap: never lock more than current matured − already locked.
            row = self._conn.execute(
                "SELECT matured_sats FROM balances WHERE address=?", (address,)
            ).fetchone()
            matured = int(row["matured_sats"]) if row else 0
            locked = self._locked_intent_sats_unlocked(address)
            room = max(0, matured - locked)
            amt = min(int(amount_sats), room)
            if amt <= 0:
                raise ValueError("no matured balance available for intent")
            self._conn.execute(
                """
                INSERT INTO payout_intents(
                    id, address, amount_sats, status, txid, created_ts, updated_ts,
                    error, attempts, next_attempt_ts
                ) VALUES (?, ?, ?, 'pending', NULL, ?, ?, NULL, 0, ?)
                """,
                (intent_id, address, amt, now, now, now),
            )
            self._conn.commit()
            return intent_id

    def get_payout_intent(self, intent_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM payout_intents WHERE id=?", (intent_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_open_payout_intents(self, *, due_only: bool = True) -> list[dict]:
        with self._lock:
            now = time.time()
            if due_only:
                rows = self._conn.execute(
                    """
                    SELECT * FROM payout_intents
                    WHERE status IN ('pending', 'broadcast')
                      AND next_attempt_ts <= ?
                    ORDER BY created_ts
                    """,
                    (now,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT * FROM payout_intents
                    WHERE status IN ('pending', 'broadcast')
                    ORDER BY created_ts
                    """
                ).fetchall()
            return [dict(r) for r in rows]

    def mark_intent_broadcast(
        self,
        intent_id: str,
        txid: str,
        *,
        raw_tx_hex: str | None = None,
        input_keys: list[str] | None = None,
    ) -> None:
        """Mark intent broadcast and persist signed body + reserved outpoints."""
        import json as _json

        now = time.time()
        keys_json = _json.dumps(list(input_keys or []))
        with self._lock:
            self._conn.execute(
                """
                UPDATE payout_intents SET
                    status='broadcast', txid=?, updated_ts=?, error=NULL,
                    attempts=attempts + 1,
                    raw_tx_hex=COALESCE(?, raw_tx_hex),
                    input_keys=COALESCE(?, input_keys)
                WHERE id=? AND status IN ('pending', 'broadcast')
                """,
                (txid, now, raw_tx_hex, keys_json, intent_id),
            )
            self._conn.commit()

    def mark_intent_failed(
        self, intent_id: str, error: str, *, backoff_sec: float = 60.0
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE payout_intents SET
                    updated_ts=?, error=?, attempts=attempts + 1,
                    next_attempt_ts=?
                WHERE id=? AND status IN ('pending', 'broadcast')
                """,
                (now, str(error)[:500], now + float(backoff_sec), intent_id),
            )
            self._conn.commit()

    def cancel_intent(self, intent_id: str, reason: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE payout_intents SET
                    status='cancelled', updated_ts=?, error=?
                WHERE id=? AND status IN ('pending', 'broadcast')
                """,
                (now, str(reason)[:500], intent_id),
            )
            self._conn.commit()

    def complete_payout_intent(self, intent_id: str, txid: str) -> bool:
        """Mark intent sent and debit balances once. Returns False if already done."""
        with self._lock:
            intent = self._conn.execute(
                "SELECT * FROM payout_intents WHERE id=?", (intent_id,)
            ).fetchone()
            if intent is None:
                return False
            if str(intent["status"]) == "sent":
                return False
            # Already recorded under this intent?
            exists = self._conn.execute(
                "SELECT id FROM payouts WHERE intent_id=? OR (txid=? AND txid IS NOT NULL)",
                (intent_id, txid),
            ).fetchone()
            if exists:
                self._conn.execute(
                    """
                    UPDATE payout_intents SET status='sent', txid=?, updated_ts=?, error=NULL
                    WHERE id=?
                    """,
                    (txid, time.time(), intent_id),
                )
                self._conn.commit()
                return False
            address = str(intent["address"])
            amount = int(intent["amount_sats"])
            now = time.time()
            self._conn.execute(
                """
                INSERT INTO payouts(ts, address, amount_sats, txid, status, intent_id)
                VALUES (?, ?, ?, ?, 'sent', ?)
                """,
                (now, address, amount, txid, intent_id),
            )
            self._conn.execute(
                """
                UPDATE balances SET
                    matured_sats = MAX(0, matured_sats - ?),
                    paid_sats = paid_sats + ?
                WHERE address=?
                """,
                (amount, amount, address),
            )
            self._conn.execute(
                """
                UPDATE payout_intents SET
                    status='sent', txid=?, updated_ts=?, error=NULL
                WHERE id=?
                """,
                (txid, now, intent_id),
            )
            self._conn.commit()
            return True

    def record_payout(
        self, address: str, amount: int, txid: str, *, intent_id: str | None = None
    ) -> None:
        """Legacy helper — prefer ``complete_payout_intent`` for new code."""
        if intent_id:
            self.complete_payout_intent(intent_id, txid)
            return
        with self._lock:
            dup = self._conn.execute(
                "SELECT id FROM payouts WHERE txid=? AND txid IS NOT NULL AND txid != ''",
                (txid,),
            ).fetchone()
            if dup:
                return
            self._conn.execute(
                """
                INSERT INTO payouts(ts, address, amount_sats, txid, status, intent_id)
                VALUES (?, ?, ?, ?, 'sent', ?)
                """,
                (time.time(), address, int(amount), txid, intent_id),
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

    def stats_overview(self, *, share_factor: int = 1024) -> dict:
        sw = float(1 << 32) / float(max(1, int(share_factor)))
        with self._lock:
            now = time.time()
            workers = self._conn.execute(
                "SELECT COUNT(*) AS n FROM workers WHERE last_seen > ?",
                (now - 600,),
            ).fetchone()["n"]
            online = self._conn.execute(
                "SELECT address, worker, hashrate, last_seen FROM workers WHERE last_seen > ?",
                (now - 600,),
            ).fetchall()
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
            blocks_total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rounds WHERE block_hash IS NOT NULL"
            ).fetchone()["n"]
            blocks = self._conn.execute(
                """
                SELECT id AS round_id, height, block_hash, reward_sats,
                       found_ts, started_ts, ended_ts, status
                FROM rounds WHERE block_hash IS NOT NULL
                ORDER BY id DESC LIMIT 200
                """
            ).fetchall()
        # Sanitize outside the write lock (estimate queries take their own lock).
        top_clean: list[dict] = []
        hashrate = 0.0
        for w in online:
            addr = str(w["address"])
            name = str(w["worker"] or "default")
            raw = float(w["hashrate"] or 0)
            try:
                est = self.estimate_worker_hashrate(addr, name, share_work=sw)
            except Exception:
                est = 0.0
            cleaned = self.sanitize_hashrate(raw, est)
            hashrate += cleaned
            top_clean.append(
                {
                    "address": addr,
                    "worker": name,
                    "hashrate": cleaned,
                    "last_seen": float(w["last_seen"] or 0),
                }
            )
        top_clean.sort(key=lambda r: float(r["hashrate"]), reverse=True)
        overview = {
            "workers_active": int(workers),
            "miners_total": int(miners_total),
            "pool_hashrate": float(hashrate or 0),
            "shares_1h": int(shares_1h),
            "shares_round": int(shares_round),
            "current_round": self.current_round_id(),
            "blocks": [dict(b) for b in blocks],
            "blocks_total": int(blocks_total),
            "blocks_per_page": 20,
            "workers": top_clean[:50],
            "ts": time.time(),
        }
        # ~15s samples → need thousands of raw rows for 24h; return 5‑min averages.
        overview["hashrate_history"] = self.hashrate_history(
            "pool", "", hours=24.0, limit=8000, bucket_sec=300.0
        )
        return overview

    def round_detail(
        self,
        round_id: int,
        *,
        fee_percent: float | None = None,
    ) -> dict | None:
        """Audit payload for one round: per-address shares, PROP %, fee split."""
        rid = int(round_id)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, started_ts, ended_ts, found_ts, height, block_hash,
                       reward_sats, status
                FROM rounds WHERE id=?
                """,
                (rid,),
            ).fetchone()
            if not row:
                return None
            share_rows = self._conn.execute(
                """
                SELECT address,
                       COUNT(*) AS n,
                       COALESCE(SUM(difficulty), 0) AS d
                FROM shares WHERE round_id=?
                GROUP BY address
                ORDER BY d DESC, address ASC
                """,
                (rid,),
            ).fetchall()
            credit_rows = self._conn.execute(
                """
                SELECT address, amount_sats, status
                FROM credits WHERE round_id=?
                """,
                (rid,),
            ).fetchall()
            share_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM shares WHERE round_id=?",
                (rid,),
            ).fetchone()["n"]
            prev_row = self._conn.execute(
                "SELECT id FROM rounds WHERE id < ? ORDER BY id DESC LIMIT 1",
                (rid,),
            ).fetchone()
            next_row = self._conn.execute(
                "SELECT id FROM rounds WHERE id > ? ORDER BY id ASC LIMIT 1",
                (rid,),
            ).fetchone()
            # Newest-first list page that contains this round (20/page default).
            newer_or_eq = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS n FROM rounds WHERE id >= ?",
                    (rid,),
                ).fetchone()["n"]
                or 0
            )
            rounds_total = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()["n"]
                or 0
            )

        credits_by: dict[str, dict] = {}
        distributable = 0
        for c in credit_rows:
            addr = str(c["address"])
            amt = int(c["amount_sats"] or 0)
            distributable += amt
            credits_by[addr] = {
                "amount_sats": amt,
                "status": str(c["status"] or ""),
            }

        total_diff = sum(float(r["d"] or 0) for r in share_rows)
        reward = int(row["reward_sats"] or 0) if row["reward_sats"] is not None else 0
        fee_sats = max(0, reward - distributable) if reward > 0 else None
        # Prefer reconstructed fee % from ledger; fall back to configured fee.
        if reward > 0 and fee_sats is not None:
            fee_pct = 100.0 * float(fee_sats) / float(reward)
        elif fee_percent is not None:
            fee_pct = float(fee_percent)
        else:
            fee_pct = None

        miners: list[dict] = []
        for r in share_rows:
            addr = str(r["address"])
            diff = float(r["d"] or 0)
            prop = (100.0 * diff / total_diff) if total_diff > 0 else 0.0
            cred = credits_by.get(addr)
            miners.append(
                {
                    "address": addr,
                    "shares": int(r["n"] or 0),
                    "difficulty": diff,
                    "prop_percent": prop,
                    "credit_sats": int(cred["amount_sats"]) if cred else 0,
                    "credit_status": cred["status"] if cred else None,
                }
            )
        # Credits with zero shares (shouldn't happen, but keep audit-complete).
        for addr, cred in credits_by.items():
            if any(m["address"] == addr for m in miners):
                continue
            miners.append(
                {
                    "address": addr,
                    "shares": 0,
                    "difficulty": 0.0,
                    "prop_percent": 0.0,
                    "credit_sats": int(cred["amount_sats"]),
                    "credit_status": cred["status"],
                }
            )

        return {
            "id": int(row["id"]),
            "started_ts": float(row["started_ts"] or 0),
            "ended_ts": float(row["ended_ts"]) if row["ended_ts"] is not None else None,
            "found_ts": float(row["found_ts"]) if row["found_ts"] is not None else None,
            "height": row["height"],
            "block_hash": row["block_hash"],
            "reward_sats": reward if row["reward_sats"] is not None else None,
            "status": str(row["status"] or ""),
            "shares_total": int(share_count or 0),
            "difficulty_total": total_diff,
            "miners_count": len(miners),
            "distributable_sats": distributable,
            "fee_sats": fee_sats,
            "fee_percent": fee_pct,
            "miners": miners,
            "prev_id": int(prev_row["id"]) if prev_row else None,
            "next_id": int(next_row["id"]) if next_row else None,
            "rounds_total": rounds_total,
            "list_page": max(1, (newer_or_eq - 1) // 20 + 1) if newer_or_eq else 1,
        }

    def rounds_list(self, *, page: int = 1, per_page: int = 20) -> dict:
        """Paginated rounds index for audit browsing (newest first)."""
        per = max(1, min(100, int(per_page)))
        pg = max(1, int(page))
        with self._lock:
            total = int(
                self._conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()["n"]
                or 0
            )
            total_pages = max(1, (total + per - 1) // per) if total else 1
            if pg > total_pages:
                pg = total_pages
            offset = (pg - 1) * per
            rows = self._conn.execute(
                """
                SELECT r.id, r.started_ts, r.ended_ts, r.found_ts, r.height,
                       r.block_hash, r.reward_sats, r.status,
                       (SELECT COUNT(*) FROM shares s WHERE s.round_id = r.id) AS shares,
                       (SELECT COUNT(DISTINCT s.address) FROM shares s WHERE s.round_id = r.id) AS miners
                FROM rounds r
                ORDER BY r.id DESC
                LIMIT ? OFFSET ?
                """,
                (per, offset),
            ).fetchall()
        out = []
        for r in rows:
            reward = r["reward_sats"]
            out.append(
                {
                    "id": int(r["id"]),
                    "started_ts": float(r["started_ts"] or 0),
                    "ended_ts": float(r["ended_ts"]) if r["ended_ts"] is not None else None,
                    "found_ts": float(r["found_ts"]) if r["found_ts"] is not None else None,
                    "height": r["height"],
                    "block_hash": r["block_hash"],
                    "reward_sats": int(reward) if reward is not None else None,
                    "status": str(r["status"] or ""),
                    "shares": int(r["shares"] or 0),
                    "miners": int(r["miners"] or 0),
                }
            )
        return {
            "rounds": out,
            "page": pg,
            "per_page": per,
            "total": total,
            "total_pages": total_pages,
        }

    def miner_stats(
        self,
        address: str,
        *,
        payout_threshold_sats: int = 10_000_000_000,
        mature_confirms: int = 20,
        short_window: float = 1800.0,
        long_window: float = 10800.0,
        share_factor: int = 1024,
        share_work: float | None = None,
        light: bool = False,
    ) -> dict:
        """Full miner dashboard payload (balances, workers, payments, blocks).

        ``light=True`` skips chart histories (SSR boot / first paint).
        Lifetime share totals come from ``miner_rollup`` — O(1), not a 1M-row scan.
        """
        address = (address or "").strip()
        now = time.time()
        short_w = max(60.0, float(short_window))
        long_w = max(short_w, float(long_window))
        sick_after = short_w / 2.0  # highlight if silent for ½ short window
        # Expected hashes/share at current easy nbits≈diff1 (2^32 / share_factor).
        sw = float(share_work) if share_work and share_work > 0 else (
            float(1 << 32) / float(max(1, int(share_factor)))
        )
        with self._lock:
            bal = self._conn.execute(
                "SELECT * FROM balances WHERE address=?", (address,)
            ).fetchone()
            workers_rows = self._conn.execute(
                "SELECT worker, hashrate, last_seen, last_share_ts "
                "FROM workers WHERE address=? ORDER BY worker",
                (address,),
            ).fetchall()
            rollup = self._conn.execute(
                "SELECT shares_n, shares_d, last_share_ts, blocks_found "
                "FROM miner_rollup WHERE address=?",
                (address,),
            ).fetchone()
            short_row = self._conn.execute(
                "SELECT COALESCE(SUM(difficulty),0) AS d, COUNT(*) AS n FROM shares "
                "WHERE address=? AND ts > ?",
                (address, now - short_w),
            ).fetchone()
            long_row = self._conn.execute(
                "SELECT COALESCE(SUM(difficulty),0) AS d, COUNT(*) AS n FROM shares "
                "WHERE address=? AND ts > ?",
                (address, now - long_w),
            ).fetchone()
            # Per-worker short/long difficulty sums (time-bounded — cheap with idx).
            per_short = {
                str(r["worker"]): float(r["d"] or 0)
                for r in self._conn.execute(
                    "SELECT worker, COALESCE(SUM(difficulty),0) AS d FROM shares "
                    "WHERE address=? AND ts > ? GROUP BY worker",
                    (address, now - short_w),
                ).fetchall()
            }
            per_long = {
                str(r["worker"]): float(r["d"] or 0)
                for r in self._conn.execute(
                    "SELECT worker, COALESCE(SUM(difficulty),0) AS d FROM shares "
                    "WHERE address=? AND ts > ? GROUP BY worker",
                    (address, now - long_w),
                ).fetchall()
            }
            per_last = {
                str(r["worker"]): float(r["last_share_ts"] or 0) for r in workers_rows
            }
            payments = self._conn.execute(
                """
                SELECT ts, amount_sats, txid, status
                FROM payouts WHERE address=?
                ORDER BY id DESC LIMIT 50
                """,
                (address,),
            ).fetchall()
            blocks = self._conn.execute(
                """
                SELECT s.ts, s.height, s.block_hash, s.difficulty, r.status
                FROM shares s
                INNER JOIN rounds r ON r.block_hash = s.block_hash
                WHERE s.address=? AND s.is_block=1 AND s.block_hash IS NOT NULL
                ORDER BY s.id DESC LIMIT 20
                """,
                (address,),
            ).fetchall()
            # Accepted network blocks ≈ shares still flagged is_block=1 (partial idx).
            if rollup is not None:
                blocks_found = int(rollup["blocks_found"] or 0)
            else:
                blocks_found = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM shares "
                    "WHERE address=? AND is_block=1",
                    (address,),
                ).fetchone()["n"]
            payments_count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM payouts WHERE address=?",
                (address,),
            ).fetchone()["n"]
            # Latest credit + round context (explain immature vs 1% fee).
            last_credit = self._conn.execute(
                """
                SELECT c.amount_sats, c.status, c.round_id, c.created_ts,
                       r.height, r.reward_sats, r.block_hash, r.status AS round_status
                FROM credits c
                LEFT JOIN rounds r ON r.id = c.round_id
                WHERE c.address=?
                ORDER BY c.id DESC LIMIT 1
                """,
                (address,),
            ).fetchone()
            round_share_pct = None
            round_fee_sats = None
            round_distributable = None
            if last_credit and last_credit["round_id"] is not None:
                rid = int(last_credit["round_id"])
                reward = int(last_credit["reward_sats"] or 0)
                # Reconstruct fee from reward vs sum(credits) if possible.
                credit_sum = self._conn.execute(
                    "SELECT COALESCE(SUM(amount_sats),0) AS s FROM credits WHERE round_id=?",
                    (rid,),
                ).fetchone()["s"]
                round_distributable = int(credit_sum or 0)
                if reward > 0:
                    round_fee_sats = max(0, reward - round_distributable)
                my_amt = int(last_credit["amount_sats"] or 0)
                if round_distributable > 0:
                    round_share_pct = 100.0 * my_amt / float(round_distributable)

        immature = int(bal["immature_sats"]) if bal else 0
        pending = int(bal["matured_sats"]) if bal else 0  # awaiting payout
        paid = int(bal["paid_sats"]) if bal else 0
        if rollup is not None:
            last_share = float(rollup["last_share_ts"] or 0)
            shares_n = int(rollup["shares_n"] or 0)
            shares_d = float(rollup["shares_d"] or 0)
        else:
            last_share = max((float(v) for v in per_last.values()), default=0.0)
            shares_n = 0
            shares_d = 0.0

        # Hashrate: client-reported, but MUST match share reality (sanitize).
        # Fake ~1 GH/s spikes came from miners reporting inflated H/s while
        # share rate stayed ~9 MH/s — share-window estimate is ground truth.
        workers_out: list[dict] = []
        workers_online = 0
        reported_total = 0.0
        for w in workers_rows:
            name = str(w["worker"] or "default")
            last = float(per_last.get(name) or w["last_seen"] or 0)
            age = now - last if last else 1e9
            if age <= 600:
                status = "online"
                workers_online += 1
            elif age <= sick_after:
                status = "sick"
            else:
                status = "dead"
            raw = float(w["hashrate"] or 0) if status == "online" else 0.0
            est = 0.0
            if status == "online":
                try:
                    est = self.estimate_worker_hashrate(
                        address, name, share_work=sw, window=min(180.0, short_w)
                    )
                except Exception:
                    est = 0.0
            reported = self.sanitize_hashrate(raw, est) if status == "online" else 0.0
            if status == "online":
                reported_total += reported
            # Per-worker short/long from that worker's own shares only.
            w_short = reported
            w_long = reported
            sd = per_short.get(name, 0.0)
            ld = per_long.get(name, 0.0)
            if reported > 0 and sd > 0 and ld > 0 and short_w > 0 and long_w > 0:
                ratio = (ld / long_w) / (sd / short_w)
                # Clamp reconnect blow-ups (thin short window vs fat long window).
                ratio = min(max(ratio, 0.25), 1.5)
                w_long = reported * ratio
            workers_out.append(
                {
                    "worker": name,
                    "hashrate": reported,
                    "hashrate_short": w_short,
                    "hashrate_long": w_long,
                    "last_seen": float(w["last_seen"] or 0),
                    "last_share": last,
                    "status": status,
                }
            )

        short_n = int(short_row["n"] or 0)
        long_n = int(long_row["n"] or 0)
        # Address totals = sum of ONLINE workers only. Never scale by
        # address-wide share counts (dead workers' old shares inflate long).
        hps_short = reported_total
        hps_long = sum(
            float(w["hashrate_long"] or 0)
            for w in workers_out
            if w.get("status") == "online"
        )

        last_credit_out = None
        if last_credit:
            last_credit_out = {
                "amount_sats": int(last_credit["amount_sats"] or 0),
                "status": str(last_credit["status"] or ""),
                "round_id": int(last_credit["round_id"] or 0),
                "height": last_credit["height"],
                "reward_sats": int(last_credit["reward_sats"] or 0),
                "block_hash": last_credit["block_hash"],
                "round_status": last_credit["round_status"],
                "created_ts": float(last_credit["created_ts"] or 0),
                "share_percent": round_share_pct,
                "fee_sats": round_fee_sats,
                "distributable_sats": round_distributable,
            }

        return {
            "address": address,
            # Balances (open-ethereum-pool naming)
            "immature_sats": immature,
            "pending_sats": pending,  # matured, waiting for payout
            "matured_sats": pending,  # alias
            "paid_sats": paid,
            "payout_threshold_sats": int(payout_threshold_sats),
            "mature_confirms": int(mature_confirms),
            "payout_progress": min(
                1.0,
                (pending / float(payout_threshold_sats))
                if payout_threshold_sats > 0
                else 0.0,
            ),
            # Activity
            "last_share": last_share,
            "workers_online": workers_online,
            "hashrate": reported_total or hps_short,
            "hashrate_short": hps_short,
            "hashrate_long": hps_long,
            "blocks_found": int(blocks_found),
            "payments_count": int(payments_count),
            "shares": shares_n,
            "shares_short": short_n,
            "shares_long": long_n,
            "share_difficulty": shares_d,
            "windows": {"short_sec": short_w, "long_sec": long_w, "sick_sec": sick_after},
            "workers": workers_out,
            "payments": [dict(p) for p in payments],
            "blocks": [dict(b) for b in blocks],
            "last_credit": last_credit_out,
            "hashrate_history": (
                [] if light else self.hashrate_history("miner", address, hours=6.0)
            ),
            "payments_history": [] if light else self.payments_history(address),
            "earnings_history": [] if light else self.earnings_history(address),
            "ts": now,
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
