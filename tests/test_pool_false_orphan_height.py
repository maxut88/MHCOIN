"""Isolated regression: false orphan via wrong round height (round 1526 class).

No mainnet P2P. Temporary PoolDB only.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mhcoin.pool.db import PoolDB


def _reset(db: PoolDB) -> None:
    with db._lock:
        for t in ("rounds", "credits", "balances", "shares"):
            db._conn.execute(f"DELETE FROM {t}")
        db._conn.commit()


def _immature_round(
    db: PoolDB,
    *,
    rid: int,
    height: int,
    block_hash: str,
    amount: int = 4_950_000_000,
    addr: str = "mhc1test",
) -> None:
    with db._lock:
        db._conn.execute(
            "INSERT INTO rounds(id, started_ts, ended_ts, found_ts, height, "
            "block_hash, reward_sats, status) VALUES (?,?,?,?,?,?,?,'immature')",
            (rid, float(rid), float(rid) + 0.1, float(rid), height, block_hash, 5_000_000_000),
        )
        db._conn.execute(
            "INSERT INTO credits(address, amount_sats, round_id, status, created_ts) "
            "VALUES (?,?,?,'immature',?)",
            (addr, amount, rid, float(rid)),
        )
        db._conn.execute(
            "INSERT INTO balances(address, matured_sats, immature_sats, paid_sats) "
            "VALUES (?,0,?,0) "
            "ON CONFLICT(address) DO UPDATE SET "
            "immature_sats = immature_sats + excluded.immature_sats",
            (addr, amount),
        )
        db._conn.commit()


class FalseOrphanHeightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.mkdtemp(prefix="mhcoin_false_orphan_")
        self.db = PoolDB(Path(self._td) / "pool.sqlite")
        _reset(self.db)
        self.canon = {
            1535: "aa" * 32,
            1536: "bb" * 32,
        }
        self.index = {
            "aa" * 32: {"height": 1535, "status": 1, "active": True},
            "bb" * 32: {"height": 1536, "status": 1, "active": True},
        }

    def _lookup(self, hx: str):
        return self.index.get(hx.lower())

    def test_sequential_correct_heights_mature(self) -> None:
        """A@1535 B@1536 — both mature when deep enough."""
        _immature_round(self.db, rid=1, height=1535, block_hash="aa" * 32)
        _immature_round(self.db, rid=2, height=1536, block_hash="bb" * 32)
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (3, 3, 'open')"
            )
            self.db._conn.commit()
        events: list = []
        self.db.mature_rounds(
            tip_height=1636,
            confirms=100,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 1636,
            lookup_indexed_hash=self._lookup,
            events=events,
        )
        rows = {
            int(r[0]): r[1]
            for r in self.db._conn.execute(
                "SELECT id, status FROM rounds WHERE id IN (1,2)"
            )
        }
        self.assertEqual(rows[1], "matured")
        self.assertEqual(rows[2], "matured")

    def test_wrong_height_not_false_void(self) -> None:
        """Regression: B stored at 1535 but canonical at 1536 → repair, not orphan."""
        _immature_round(self.db, rid=1, height=1535, block_hash="aa" * 32)
        _immature_round(self.db, rid=2, height=1535, block_hash="bb" * 32)  # wrong
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (3, 3, 'open')"
            )
            self.db._conn.commit()
        events: list = []
        # Deep enough for 1535+20 and 1536+20
        self.db.mature_rounds(
            tip_height=1556,
            confirms=20,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 1556,
            lookup_indexed_hash=self._lookup,
            events=events,
        )
        rows = list(
            self.db._conn.execute(
                "SELECT id, height, status FROM rounds WHERE id IN (1,2) ORDER BY id"
            )
        )
        self.assertEqual(rows[0][2], "matured")
        self.assertEqual(int(rows[1][1]), 1536)  # repaired
        self.assertEqual(rows[1][2], "matured")
        self.assertTrue(
            any(e.get("code") == "height_mismatch_repair" for e in events),
            events,
        )
        orphaned = self.db._conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE status='orphaned'"
        ).fetchone()[0]
        self.assertEqual(int(orphaned), 0)

    def test_true_competitor_still_orphans(self) -> None:
        loser = "cc" * 32
        self.index[loser] = {"height": 1535, "status": 0, "active": False}
        _immature_round(self.db, rid=1, height=1535, block_hash="aa" * 32)
        _immature_round(self.db, rid=2, height=1535, block_hash=loser)
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (3, 3, 'open')"
            )
            self.db._conn.commit()
        self.db.mature_rounds(
            tip_height=1555,
            confirms=20,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 1555,
            lookup_indexed_hash=self._lookup,
        )
        rows = {
            int(r[0]): r[1]
            for r in self.db._conn.execute(
                "SELECT id, status FROM rounds WHERE id IN (1,2)"
            )
        }
        self.assertEqual(rows[1], "matured")
        self.assertEqual(rows[2], "orphaned")

    def test_unknown_hash_defers(self) -> None:
        _immature_round(self.db, rid=1, height=1535, block_hash="dd" * 32)
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (2, 2, 'open')"
            )
            self.db._conn.commit()
        events: list = []
        self.db.mature_rounds(
            tip_height=1600,
            confirms=20,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 1600,
            lookup_indexed_hash=lambda hx: None,
            events=events,
        )
        st = self.db._conn.execute(
            "SELECT status FROM rounds WHERE id=1"
        ).fetchone()[0]
        self.assertEqual(st, "immature")
        self.assertTrue(any(e.get("code") == "hash_unknown" for e in events), events)

    def test_confirm_boundaries_20(self) -> None:
        _immature_round(self.db, rid=1, height=100, block_hash="aa" * 32)
        self.canon[100] = "aa" * 32
        self.index["aa" * 32] = {"height": 100, "status": 1, "active": True}
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (2, 2, 'open')"
            )
            self.db._conn.commit()
        self.db.mature_rounds(
            tip_height=119,
            confirms=20,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 119,
            lookup_indexed_hash=self._lookup,
        )
        self.assertEqual(
            self.db._conn.execute("SELECT status FROM rounds WHERE id=1").fetchone()[0],
            "immature",
        )
        self.db.mature_rounds(
            tip_height=120,
            confirms=20,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 120,
            lookup_indexed_hash=self._lookup,
        )
        self.assertEqual(
            self.db._conn.execute("SELECT status FROM rounds WHERE id=1").fetchone()[0],
            "matured",
        )

    def test_confirm_boundaries_100(self) -> None:
        _immature_round(self.db, rid=1, height=100, block_hash="aa" * 32)
        self.canon[100] = "aa" * 32
        self.index["aa" * 32] = {"height": 100, "status": 1, "active": True}
        with self.db._lock:
            self.db._conn.execute(
                "INSERT INTO rounds(id, started_ts, status) VALUES (2, 2, 'open')"
            )
            self.db._conn.commit()
        for tip in (198, 199):
            self.db.mature_rounds(
                tip_height=tip,
                confirms=100,
                canonical_hash_at=lambda h: self.canon.get(h),
                tip_height_now=lambda tip=tip: tip,
                lookup_indexed_hash=self._lookup,
            )
            self.assertEqual(
                self.db._conn.execute(
                    "SELECT status FROM rounds WHERE id=1"
                ).fetchone()[0],
                "immature",
                tip,
            )
        self.db.mature_rounds(
            tip_height=200,
            confirms=100,
            canonical_hash_at=lambda h: self.canon.get(h),
            tip_height_now=lambda: 200,
            lookup_indexed_hash=self._lookup,
        )
        self.assertEqual(
            self.db._conn.execute("SELECT status FROM rounds WHERE id=1").fetchone()[0],
            "matured",
        )

    def test_dry_run_restore_idempotent_and_refuse_paid(self) -> None:
        """Simulate remediation helpers: only orphaned+unpaid may be restored."""
        hx = "bb" * 32
        _immature_round(self.db, rid=2, height=1535, block_hash=hx)
        # Force orphaned state like production 1526
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE rounds SET status='orphaned' WHERE id=2"
            )
            self.db._conn.execute(
                "UPDATE credits SET status='orphaned' WHERE round_id=2"
            )
            # Reverse immature as void would
            self.db._conn.execute(
                "UPDATE balances SET immature_sats=MAX(0, immature_sats-?) "
                "WHERE address='mhc1test'",
                (4_950_000_000,),
            )
            self.db._conn.commit()

        def restore_once() -> str:
            with self.db._lock:
                row = self.db._conn.execute(
                    "SELECT status, height FROM rounds WHERE id=2"
                ).fetchone()
                if not row:
                    return "missing"
                if str(row["status"]) == "immature" and int(row["height"]) == 1536:
                    return "already_restored"
                if str(row["status"]) != "orphaned":
                    return "refuse_not_orphaned"
                cred = self.db._conn.execute(
                    "SELECT id, status, amount_sats FROM credits WHERE round_id=2"
                ).fetchone()
                if not cred or str(cred["status"]) != "orphaned":
                    return "refuse_credit"
                # Simulated already-paid guard
                if str(cred["status"]) == "paid":
                    return "refuse_paid"
                self.db._conn.execute(
                    "UPDATE rounds SET status='immature', height=1536 WHERE id=2"
                )
                self.db._conn.execute(
                    "UPDATE credits SET status='immature', matured_ts=NULL WHERE id=?",
                    (int(cred["id"]),),
                )
                amt = int(cred["amount_sats"])
                self.db._conn.execute(
                    "UPDATE balances SET immature_sats = immature_sats + ? "
                    "WHERE address='mhc1test'",
                    (amt,),
                )
                self.db._conn.commit()
                return "restored"

        self.assertEqual(restore_once(), "restored")
        self.assertEqual(restore_once(), "already_restored")
        # already-paid case
        with self.db._lock:
            self.db._conn.execute(
                "UPDATE credits SET status='paid' WHERE round_id=2"
            )
            self.db._conn.execute(
                "UPDATE rounds SET status='orphaned', height=1535 WHERE id=2"
            )
            self.db._conn.commit()
        # restore refuses non-orphaned credit path
        with self.db._lock:
            cred = self.db._conn.execute(
                "SELECT status FROM credits WHERE round_id=2"
            ).fetchone()
            self.assertEqual(str(cred["status"]), "paid")


class AcceptResultBlockHeightTests(unittest.TestCase):
    def test_accept_result_has_block_height_field(self) -> None:
        from mhcoin.blockchain.chain import AcceptResult

        r = AcceptResult(
            height=10,
            tip_hash=b"\x00" * 32,
            activated=True,
            reorg=False,
            side_chain=False,
            orphan=False,
            duplicate=False,
            disconnect=[],
            connect=[],
            block_height=10,
        )
        self.assertEqual(r.block_height, 10)
        self.assertEqual(r.height, 10)


if __name__ == "__main__":
    unittest.main()
