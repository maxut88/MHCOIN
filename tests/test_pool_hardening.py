"""Isolated tests for pool mature_rounds / reject forensic / template UTXO."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template
from mhcoin.pool.db import PoolDB
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.utxo import UTXOSet


ADDR = "mhc1qnkfc9zcfpun8pkud45gz9qutv6xtrmrun9p07r"


def _seed_immature_round(db: PoolDB, *, height: int, block_hash: str, reward: int = 5_000_000_000):
    rid = db.current_round_id()
    db.add_share(
        address=ADDR,
        worker="t",
        job_id="j",
        height=height,
        difficulty=1.0,
        is_block=True,
        block_hash=block_hash,
    )
    db.close_round_with_block(
        height=height,
        block_hash=block_hash,
        reward_sats=reward,
        fee_percent=1.0,
    )
    return rid


def test_mature_canon_none_defers(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    h = "aa" * 32
    rid = _seed_immature_round(db, height=100, block_hash=h)
    before = db._conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()[0]
    n = db.mature_rounds(200, 20, canonical_hash_at=lambda _h: None)
    after = db._conn.execute(
        "SELECT status FROM rounds WHERE id=?", (rid,)
    ).fetchone()[0]
    bal = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert before == "immature"
    assert n == 0
    assert after == "immature"
    assert int(bal[0]) > 0 and int(bal[1]) == 0
    db.close()


def test_mature_rpc_exception_defers(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    h = "bb" * 32
    rid = _seed_immature_round(db, height=100, block_hash=h)

    def boom(_h):
        raise TimeoutError("rpc timeout")

    n = db.mature_rounds(200, 20, canonical_hash_at=boom)
    st = db._conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()[0]
    assert n == 0 and st == "immature"
    db.close()


def test_mature_canonical_matures(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    h = "cc" * 32
    rid = _seed_immature_round(db, height=100, block_hash=h)
    n = db.mature_rounds(200, 20, canonical_hash_at=lambda _h: h)
    st = db._conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()[0]
    bal = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert n == 1 and st == "matured"
    assert int(bal[0]) == 0 and int(bal[1]) == 4_950_000_000
    # idempotent
    n2 = db.mature_rounds(200, 20, canonical_hash_at=lambda _h: h)
    assert n2 == 0
    bal2 = db._conn.execute(
        "SELECT matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()[0]
    assert int(bal2) == 4_950_000_000
    db.close()


def test_mature_non_canonical_orphans(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    stored = "dd" * 32
    other = "ee" * 32
    rid = _seed_immature_round(db, height=100, block_hash=stored)
    n = db.mature_rounds(200, 20, canonical_hash_at=lambda _h: other)
    st = db._conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()[0]
    cst = db._conn.execute(
        "SELECT status FROM credits WHERE round_id=?", (rid,)
    ).fetchone()[0]
    bal = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert n == 0 and st == "orphaned" and cst == "orphaned"
    assert int(bal[0]) == 0 and int(bal[1]) == 0
    # idempotent — no second void / negative balance
    db.mature_rounds(200, 20, canonical_hash_at=lambda _h: other)
    bal2 = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert int(bal2[0]) == 0 and int(bal2[1]) == 0
    db.close()


def test_reject_keeps_block_hash(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    hx = "b4e8d90c" + "00" * 28
    sid = db.add_share(
        address=ADDR,
        worker="t",
        job_id="j",
        height=1170,
        difficulty=1.0,
        is_block=True,
        block_hash=hx,
    )
    db.clear_share_block_flag(sid, reason="missing UTXO demo")
    row = db._conn.execute(
        "SELECT is_block, block_hash, reject_reason FROM shares WHERE id=?", (sid,)
    ).fetchone()
    assert int(row[0]) == 0
    assert row[1] == hx
    assert "missing UTXO" in (row[2] or "")
    # reconcile idempotent + preserves hash
    n = db.reconcile_rejected_block_shares()
    assert n == 0  # already is_block=0
    row2 = db._conn.execute(
        "SELECT is_block, block_hash FROM shares WHERE id=?", (sid,)
    ).fetchone()
    assert int(row2[0]) == 0 and row2[1] == hx
    # miner_stats must not count as accepted
    st = db.miner_stats(ADDR)
    assert st["blocks_found"] == 0
    assert all((b.get("block_hash") or "") != hx for b in st["blocks"])
    db.close()


def test_reconcile_preserves_hash_clears_flag(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    hx = "ff" * 32
    db.add_share(
        address=ADDR,
        worker="t",
        job_id="j",
        height=1,
        difficulty=1.0,
        is_block=True,
        block_hash=hx,
    )
    n1 = db.reconcile_rejected_block_shares()
    n2 = db.reconcile_rejected_block_shares()
    row = db._conn.execute(
        "SELECT is_block, block_hash, reject_reason FROM shares WHERE block_hash=?",
        (hx,),
    ).fetchone()
    assert n1 == 1 and n2 == 0
    assert int(row[0]) == 0 and row[1] == hx
    assert row[2] == "reconciled_unaccepted"
    db.close()


def _funded(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    utxo = UTXOSet(path=None)
    cb = Transaction(
        inputs=[TxIn.coinbase(0)],
        outputs=[TxOut.p2pkh(5_000_000_000, pkh)],
    )
    utxo.apply_transaction(cb, height=0)
    return kp, pkh, utxo, cb


def test_template_excludes_double_spend(tmp_path: Path):
    kp, pkh, utxo, cb = _funded(tmp_path)
    dest = hash160(generate_keypair().public_key_compressed)
    tx1 = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_900_000_000, dest)],
    )
    sign_input(
        tx1,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    tx2 = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_800_000_000, pkh)],
    )
    sign_input(
        tx2,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    # Bypass mempool double-spend guard to simulate conflicting candidates.
    mp = Mempool(path=None)
    mp._txs[tx1.txid().hex()] = tx1
    mp._txs[tx2.txid().hex()] = tx2
    block = build_block_template(
        height=1,
        previous_hash=b"\x11" * 32,
        timestamp=1_700_000_000,
        bits=REGTEST_NBITS,
        mempool=mp,
        utxo=utxo,
        miner_pubkey_hash=pkh,
    )
    non_cb = block.transactions[1:]
    assert len(non_cb) == 1
    assert non_cb[0].txid() == tx1.txid()


def test_template_includes_dependent_txs(tmp_path: Path):
    kp, pkh, utxo, cb = _funded(tmp_path)
    mid = hash160(generate_keypair().public_key_compressed)
    dest = hash160(generate_keypair().public_key_compressed)
    tx1 = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_900_000_000, mid)],
    )
    sign_input(
        tx1,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    # Child spends tx1 output — only valid after tx1 applied to working set.
    # Need key for mid — create properly:
    kp2 = generate_keypair()
    mid = hash160(kp2.public_key_compressed)
    tx1 = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_900_000_000, mid)],
    )
    sign_input(
        tx1,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    tx2 = Transaction(
        inputs=[TxIn(prev_txid=tx1.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_800_000_000, dest)],
    )
    sign_input(
        tx2,
        0,
        kp2.private_key,
        kp2.public_key_compressed,
        tx1.outputs[0].script_pubkey,
        tx1.outputs[0].value,
    )
    mp = Mempool(path=None)
    # Order: parent then child (list order).
    mp._txs[tx1.txid().hex()] = tx1
    mp._txs[tx2.txid().hex()] = tx2
    # Force insertion order
    mp._txs = {tx1.txid().hex(): tx1, tx2.txid().hex(): tx2}
    block = build_block_template(
        height=1,
        previous_hash=b"\x22" * 32,
        timestamp=1_700_000_000,
        bits=REGTEST_NBITS,
        mempool=mp,
        utxo=utxo,
        miner_pubkey_hash=pkh,
    )
    ids = [t.txid() for t in block.transactions[1:]]
    assert ids == [tx1.txid(), tx2.txid()]


def test_template_skips_missing_utxo(tmp_path: Path):
    kp, pkh, utxo, cb = _funded(tmp_path)
    dest = hash160(generate_keypair().public_key_compressed)
    bad = Transaction(
        inputs=[TxIn(prev_txid=b"\x99" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1, dest)],
    )
    mp = Mempool(path=None)
    mp._txs[bad.txid().hex()] = bad
    block = build_block_template(
        height=1,
        previous_hash=b"\x33" * 32,
        timestamp=1_700_000_000,
        bits=REGTEST_NBITS,
        mempool=mp,
        utxo=utxo,
        miner_pubkey_hash=pkh,
    )
    assert len(block.transactions) == 1  # coinbase only
