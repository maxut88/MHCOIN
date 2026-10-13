"""Milestone 2 — transaction serialization, fee, txid."""

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction import Transaction, TxIn, TxOut, fee_of
from mhcoin.transaction.input import TxIn as TI  # noqa: F401 — kept for clarity in older imports


def test_txid_deterministic():
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tx = Transaction(
        inputs=[TxIn(prev_txid=b"\x11" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    raw = tx.serialize()
    assert Transaction.deserialize(raw).serialize() == raw
    assert len(tx.txid()) == 32
    assert tx.txid() == hash256_via_module(raw)


def hash256_via_module(raw: bytes) -> bytes:
    from mhcoin.crypto.hashing import hash256

    return hash256(raw)


def test_fee_calculation():
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tx = Transaction(
        inputs=[TxIn(prev_txid=b"\x22" * 32, prev_vout=1)],
        outputs=[TxOut.p2pkh(900, pkh)],
    )
    assert fee_of(tx, [1000]) == 100
