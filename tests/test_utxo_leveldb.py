"""LMDB chainstate UTXO + migration from legacy utxo.sqlite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction import Transaction, TxIn, TxOut
from mhcoin.utxo import OutPoint, UTXOSet, resolve_chainstate_dir


def test_resolve_chainstate_dir(tmp_path: Path):
    assert resolve_chainstate_dir(tmp_path / "utxo.sqlite") == tmp_path / "chainstate"
    assert resolve_chainstate_dir(tmp_path / "chainstate") == tmp_path / "chainstate"


def test_lmdb_persist_restart(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    path = tmp_path / "chainstate"
    utxo = UTXOSet(path)
    cb = Transaction(
        inputs=[TxIn.coinbase(0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    utxo.apply_transaction(cb, height=0)
    utxo.set_meta("tip", "abc")
    assert utxo.count() == 1
    utxo.close()

    utxo2 = UTXOSet(path)
    assert utxo2.count() == 1
    assert utxo2.get_meta("tip") == "abc"
    assert utxo2.balance_for_pubkey_hash(pkh) == 1000
    e = utxo2.get(OutPoint(txid=cb.txid(), vout=0))
    assert e is not None
    assert e.height == 0
    assert e.coinbase is True
    utxo2.close()


def test_migrate_utxo_sqlite_to_chainstate(tmp_path: Path):
    legacy = tmp_path / "utxo.sqlite"
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    # Build a real coinbase txid for the sqlite row.
    cb = Transaction(
        inputs=[TxIn.coinbase(0)],
        outputs=[TxOut.p2pkh(42, pkh)],
    )
    txid = cb.txid()
    op_key = f"{txid.hex()}:0"
    con = sqlite3.connect(str(legacy))
    con.execute(
        """
        CREATE TABLE utxo (
            outpoint TEXT PRIMARY KEY,
            txid BLOB NOT NULL,
            vout INTEGER NOT NULL,
            value INTEGER NOT NULL,
            script_pubkey BLOB NOT NULL,
            height INTEGER NOT NULL,
            coinbase INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    con.execute(
        "INSERT INTO utxo VALUES (?,?,?,?,?,?,?)",
        (op_key, txid, 0, 42, cb.outputs[0].script_pubkey, 0, 1),
    )
    con.execute("INSERT INTO meta VALUES (?,?)", ("tip_height", "0"))
    con.commit()
    con.close()

    utxo = UTXOSet(legacy)  # maps to chainstate + migrates
    assert (tmp_path / "chainstate").is_dir()
    assert utxo.count() == 1
    assert utxo.balance_for_pubkey_hash(pkh) == 42
    assert utxo.get_meta("tip_height") == "0"
    assert (tmp_path / "utxo.sqlite.bak").is_file() or not legacy.is_file()
    utxo.close()

    # Reopen from chainstate directly.
    utxo2 = UTXOSet(tmp_path / "chainstate")
    assert utxo2.count() == 1
    utxo2.close()


def test_shared_open_same_process(tmp_path: Path):
    path = tmp_path / "chainstate"
    a = UTXOSet(path)
    b = UTXOSet(path)  # must not hit exclusive lock
    assert a.count() == b.count() == 0
    from mhcoin.crypto.hashing import hash160
    from mhcoin.crypto.keys import generate_keypair
    from mhcoin.transaction import Transaction, TxIn, TxOut

    pkh = hash160(generate_keypair().public_key_compressed)
    cb = Transaction(
        inputs=[TxIn.coinbase(0)],
        outputs=[TxOut.p2pkh(7, pkh)],
    )
    a.apply_transaction(cb, height=0)
    assert b.count() == 1  # shared mem view
    b.close()
    assert a.count() == 1
    a.close()
