"""Local mempool acceptance via relay path."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import MempoolError
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import make_payment_tx, make_relay_node, seed_spendable_chain


def test_valid_tx_enters_mempool(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path / "a", kp)
    mgr, chain, mp, relay = make_relay_node(tmp_path / "a")
    try:
        mgr.start()
        tx = make_payment_tx(genesis, kp)
        hx = relay.accept_local(tx)
        assert mp.contains(hx)
        assert len(mp) == 1
    finally:
        mgr.stop()
        chain.close()


def test_invalid_tx_rejected(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    mgr, chain, mp, relay = make_relay_node(tmp_path / "a")
    try:
        mgr.start()
        bad = Transaction(
            inputs=[TxIn(prev_txid=b"\xff" * 32, prev_vout=0, script_sig=b"", sequence=0xFFFFFFFF)],
            outputs=[TxOut.p2pkh(1000, hash160(kp.public_key_compressed))],
        )
        with pytest.raises(MempoolError):
            relay.accept_local(bad)
        assert len(mp) == 0
    finally:
        mgr.stop()
        chain.close()


def test_duplicate_rejected(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path / "a", kp)
    mgr, chain, mp, relay = make_relay_node(tmp_path / "a")
    try:
        mgr.start()
        tx = make_payment_tx(genesis, kp)
        relay.accept_local(tx)
        with pytest.raises(MempoolError):
            mp.add(tx, chain.utxo, height=0)
    finally:
        mgr.stop()
        chain.close()


def test_double_spend_rejected(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path / "a", kp)
    mgr, chain, mp, relay = make_relay_node(tmp_path / "a")
    try:
        mgr.start()
        t1 = make_payment_tx(genesis, kp, amount=1_000_000)
        t2 = make_payment_tx(genesis, kp, amount=2_000_000)
        relay.accept_local(t1)
        with pytest.raises(MempoolError):
            relay.accept_local(t2)
        assert len(mp) == 1
    finally:
        mgr.stop()
        chain.close()


def test_unknown_utxo_rejected(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    mgr, chain, mp, relay = make_relay_node(tmp_path / "a")
    try:
        mgr.start()
        bad = Transaction(
            inputs=[TxIn(prev_txid=b"\x11" * 32, prev_vout=7, script_sig=b"\x00", sequence=0xFFFFFFFF)],
            outputs=[TxOut.p2pkh(1, hash160(kp.public_key_compressed))],
        )
        with pytest.raises(MempoolError):
            relay.accept_local(bad)
    finally:
        mgr.stop()
        chain.close()
