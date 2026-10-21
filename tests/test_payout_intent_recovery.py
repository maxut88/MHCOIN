"""Isolated payout intent recovery / idempotency (no mainnet DB)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.pool.db import PoolDB


def _bal(db: PoolDB, addr: str, matured: int) -> None:
    db._conn.execute(
        "INSERT INTO balances(address, matured_sats, immature_sats, paid_sats) VALUES (?,?,0,0)",
        (addr, matured),
    )
    db._conn.commit()


def test_complete_payout_intent_idempotent_no_double_debit(tmp_path: Path):
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestaddr000000000000000000000000001"
    _bal(db, addr, 10_000_000)
    iid = db.create_payout_intent(addr, 9_000_000)
    assert db.complete_payout_intent(iid, "ab" * 32) is True
    assert db.complete_payout_intent(iid, "ab" * 32) is False
    row = db._conn.execute(
        "SELECT matured_sats, paid_sats FROM balances WHERE address=?", (addr,)
    ).fetchone()
    assert int(row["matured_sats"]) == 1_000_000
    assert int(row["paid_sats"]) == 9_000_000
    n = db._conn.execute("SELECT COUNT(*) AS n FROM payouts").fetchone()["n"]
    assert int(n) == 1
    db.close()


def test_intent_create_caps_and_locks_against_duplicate_room(tmp_path: Path):
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestaddr000000000000000000000000002"
    _bal(db, addr, 1_000_000)
    iid = db.create_payout_intent(addr, 5_000_000)
    intent = db.get_payout_intent(iid)
    assert intent is not None
    assert intent["amount_sats"] == 1_000_000
    with pytest.raises(ValueError):
        db.create_payout_intent(addr, 1)
    db.close()


def test_balances_above_subtracts_open_intents(tmp_path: Path):
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestaddr000000000000000000000000003"
    _bal(db, addr, 200_000_000)
    db.create_payout_intent(addr, 150_000_000)
    assert db.balances_above(100_000_000) == []
    assert db.balances_above(40_000_000) == [(addr, 50_000_000)]
    db.close()


def test_mark_broadcast_then_complete_survives_restart_semantics(tmp_path: Path):
    """Simulate crash after broadcast mark: status=broadcast with txid+raw; complete once."""
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestaddr000000000000000000000000004"
    _bal(db, addr, 50_000_000)
    iid = db.create_payout_intent(addr, 40_000_000)
    txid = "cd" * 32
    raw = "01000000"
    keys = ["aa" * 32 + ":0"]
    db.mark_intent_broadcast(iid, txid, raw_tx_hex=raw, input_keys=keys)
    intent = db.get_payout_intent(iid)
    assert intent is not None
    assert intent["status"] == "broadcast"
    assert intent["txid"] == txid
    assert intent["raw_tx_hex"] == raw
    # Re-open DB (restart)
    db.close()
    db2 = PoolDB(tmp_path / "pool.sqlite")
    open_i = db2.list_open_payout_intents(due_only=False)
    assert any(i["id"] == iid and i["status"] == "broadcast" for i in open_i)
    assert db2.complete_payout_intent(iid, txid) is True
    assert db2.complete_payout_intent(iid, txid) is False
    assert db2.get_payout_intent(iid)["status"] == "sent"
    db2.close()


def test_threshold_1000_mhc_does_not_block_open_intent_listing(tmp_path: Path):
    """Open intents remain listable regardless of auto-payout threshold value."""
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestaddr000000000000000000000000005"
    _bal(db, addr, 150_0000_0000)  # 150 MHC
    iid = db.create_payout_intent(addr, 148_0000_0000)
    # Threshold 1000 MHC would not create NEW intents for 150 MHC avail,
    # but existing open intents must still appear.
    open_i = db.list_open_payout_intents(due_only=False)
    assert any(i["id"] == iid for i in open_i)
    due = db.balances_above(100_000_000_000)  # 1000 MHC
    assert due == []
    db.close()
