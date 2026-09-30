"""UTXO + double-spend + signatures."""

from pathlib import Path

import pytest

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input, verify_input
from mhcoin.utxo import OutPoint, UTXOError, UTXOSet


def _funded_utxo(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    utxo = UTXOSet(tmp_path / "utxo.sqlite")
    coinbase = Transaction(
        inputs=[TxIn.coinbase(0)],
        outputs=[TxOut.p2pkh(5_000_000_000, pkh)],
    )
    utxo.apply_transaction(coinbase, height=0)
    return kp, pkh, utxo, coinbase


def test_utxo_create_and_spend(tmp_path: Path):
    kp, pkh, utxo, coinbase = _funded_utxo(tmp_path)
    assert utxo.balance_for_pubkey_hash(pkh) == 5_000_000_000
    dest = hash160(generate_keypair().public_key_compressed)
    tx = Transaction(
        inputs=[TxIn(prev_txid=coinbase.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(1_000_000_000, dest), TxOut.p2pkh(3_999_999_000, pkh)],
    )
    sign_input(tx, 0, kp.private_key, kp.public_key_compressed, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    assert verify_input(tx, 0, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    utxo.apply_transaction(tx, height=1)
    assert utxo.balance_for_pubkey_hash(pkh) == 3_999_999_000
    assert utxo.balance_for_pubkey_hash(dest) == 1_000_000_000
    utxo.close()


def test_double_spend_rejected(tmp_path: Path):
    kp, pkh, utxo, coinbase = _funded_utxo(tmp_path)
    dest = hash160(generate_keypair().public_key_compressed)
    tx1 = Transaction(
        inputs=[TxIn(prev_txid=coinbase.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_000_000_000, dest)],
    )
    sign_input(tx1, 0, kp.private_key, kp.public_key_compressed, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    utxo.apply_transaction(tx1, height=1)
    tx2 = Transaction(
        inputs=[TxIn(prev_txid=coinbase.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(4_000_000_000, pkh)],
    )
    with pytest.raises(UTXOError):
        utxo.apply_transaction(tx2, height=2)
    utxo.close()


def test_invalid_signature_rejected(tmp_path: Path):
    kp, pkh, utxo, coinbase = _funded_utxo(tmp_path)
    attacker = generate_keypair()
    dest = hash160(attacker.public_key_compressed)
    tx = Transaction(
        inputs=[TxIn(prev_txid=coinbase.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(1, dest)],
    )
    # sign with wrong key
    sign_input(tx, 0, attacker.private_key, attacker.public_key_compressed, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    assert not verify_input(tx, 0, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    utxo.close()


def test_modified_output_breaks_signature(tmp_path: Path):
    kp, pkh, utxo, coinbase = _funded_utxo(tmp_path)
    dest = hash160(generate_keypair().public_key_compressed)
    tx = Transaction(
        inputs=[TxIn(prev_txid=coinbase.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, dest)],
    )
    sign_input(tx, 0, kp.private_key, kp.public_key_compressed, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    tx.outputs[0].value = 999_999_999  # tamper
    assert not verify_input(tx, 0, coinbase.outputs[0].script_pubkey, coinbase.outputs[0].value)
    utxo.close()
