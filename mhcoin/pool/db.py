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
        if hashrate is not None and float(hashrate) > 0:
            # Aggregate address hashrate sample for miner charts.
            self.record_hashrate("miner", address, self.address_hashrate(address))

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
    ) -> list[dict]:
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
        return [{"t": float(r["ts"]), "h": float(r["hashrate"] or 0)} for r in rows]

    def payments_history(self, address: str, *, limit: int = 100) -> list[dict]:
        """Cumulative paid series for charts (step after each payout)."""
        address = (address or "").strip()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT ts, amount_sats FROM payouts
                WHERE address=? AND status='sent'
                ORDER BY ts ASC LIMIT ?
                """,
                (address, int(limit)),
            ).fetchall()
        out: list[dict] = []
        cum = 0
        for r in rows:
            cum += int(r["amount_sats"] or 0)
            out.append({"t": float(r["ts"]), "sats": cum, "amount_sats": int(r["amount_sats"] or 0)})
        return out

    def earnings_history(self, address: str, *, limit: int = 100) -> list[dict]:
        """Cumulative credited (immature+matured awards) over time."""
        address = (address or "").strip()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT created_ts AS ts, amount_sats FROM credits
                WHERE address=?
                ORDER BY created_ts ASC LIMIT ?
                """,
                (address, int(limit)),
            ).fetchall()
        out: list[dict] = []
        cum = 0
        for r in rows:
            cum += int(r["amount_sats"] or 0)
            out.append({"t": float(r["ts"]), "sats": cum})
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

    def clear_share_block_flag(
        self, share_id: int, *, reason: str | None = None
    ) -> None:
        """Mark a PoW share as not an accepted network block (reject/stale).

        Keeps ``block_hash`` for forensic history; clears ``is_block`` so stats
        / Your blocks (JOIN rounds) do not count it as accepted.
        """
        reason_s = (reason or "rejected")[:240]
        with self._lock:
            self._conn.execute(
                """
                UPDATE shares
                SET is_block=0,
                    reject_reason=COALESCE(?, reject_reason, 'rejected')
                WHERE id=?
                """,
                (reason_s, int(share_id)),
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
    ) -> int:
        """PROP-split reward for current round; open a new round. Returns closed round id.

        Idempotent on ``block_hash``: a second call for the same hash returns the
        existing round id and does not create duplicate credits.
        """
        with self._lock:
            existing = self._conn.execute(
                "SELECT id FROM rounds WHERE block_hash=?", (block_hash,)
            ).fetchone()
            if existing:
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
            blocks_total = self._conn.execute(
                "SELECT COUNT(*) AS n FROM rounds WHERE block_hash IS NOT NULL"
            ).fetchone()["n"]
            blocks = self._conn.execute(
                """
                SELECT height, block_hash, reward_sats, ended_ts, status
                FROM rounds WHERE block_hash IS NOT NULL
                ORDER BY id DESC LIMIT 200
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
                "workers": [dict(w) for w in top],
                "ts": now,
            }
        overview["hashrate_history"] = self.hashrate_history("pool", "", hours=6.0)
        return overview

    def miner_stats(
        self,
        address: str,
        *,
        payout_threshold_sats: int = 10_000_000_000,
        mature_confirms: int = 20,
        short_window: float = 1800.0,
        long_window: float = 10800.0,
    ) -> dict:
        """Full miner dashboard payload (balances, workers, payments, blocks)."""
        address = (address or "").strip()
        now = time.time()
        short_w = max(60.0, float(short_window))
        long_w = max(short_w, float(long_window))
        sick_after = short_w / 2.0  # highlight if silent for ½ short window
        with self._lock:
            bal = self._conn.execute(
                "SELECT * FROM balances WHERE address=?", (address,)
            ).fetchone()
            workers_rows = self._conn.execute(
                "SELECT worker, hashrate, last_seen FROM workers WHERE address=? ORDER BY worker",
                (address,),
            ).fetchall()
            shares = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(difficulty),0) AS d, MAX(ts) AS last_ts "
                "FROM shares WHERE address=?",
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
            # Per-worker last share + short/long difficulty sums
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
                str(r["worker"]): float(r["last_ts"] or 0)
                for r in self._conn.execute(
                    "SELECT worker, MAX(ts) AS last_ts FROM shares WHERE address=? GROUP BY worker",
                    (address,),
                ).fetchall()
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
            blocks_found = self._conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM shares s
                INNER JOIN rounds r ON r.block_hash = s.block_hash
                WHERE s.address=? AND s.is_block=1 AND s.block_hash IS NOT NULL
                """,
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
        last_share = float(shares["last_ts"] or 0) if shares else 0.0

        # Hashrate: prefer client-reported (share_factor is NOT bitcoin-diff;
        # the old diff*2^32 formula showed TH/s falsely).
        # Only ONLINE workers count — dead rows kept stale H/s and inflated totals.
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
            reported = float(w["hashrate"] or 0) if status == "online" else 0.0
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
            "shares": int(shares["n"] or 0) if shares else 0,
            "shares_short": short_n,
            "shares_long": long_n,
            "share_difficulty": float(shares["d"] or 0) if shares else 0.0,
            "windows": {"short_sec": short_w, "long_sec": long_w, "sick_sec": sick_after},
            "workers": workers_out,
            "payments": [dict(p) for p in payments],
            "blocks": [dict(b) for b in blocks],
            "last_credit": last_credit_out,
            "hashrate_history": self.hashrate_history("miner", address, hours=6.0),
            "payments_history": self.payments_history(address),
            "earnings_history": self.earnings_history(address),
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
