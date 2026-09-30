"""STEP 1 — transaction structures and amount rules."""

import pytest

from mhcoin.constants import MAX_SUPPLY_SATOSHIS, SATOSHI_PER_COIN
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction import Transaction, TxIn, TxOut, fee_of
from mhcoin.transaction.input import TxInput, TxOutput


def _pkh() -> bytes:
    return hash160(generate_keypair().public_key_compressed)


def test_aliases():
    assert TxInput is TxIn
    assert TxOutput is TxOut


def test_amount_one_mhc_equals_1e8_base_units():
    assert 1 * SATOSHI_PER_COIN == 100_000_000
    assert int(1.25 * SATOSHI_PER_COIN) == 125_000_000  # display math only; storage uses int


def test_txout_rejects_negative():
    with pytest.raises(ValueError, match="negative"):
        TxOut(value=-1, script_pubkey=b"\x00" + b"\x11" * 20)


def test_txout_rejects_float():
    with pytest.raises(ValueError, match="int"):
        TxOut(value=1.25, script_pubkey=b"\x00" + b"\x11" * 20)  # type: ignore[arg-type]


def test_txout_rejects_above_max_supply():
    with pytest.raises(ValueError, match="max supply"):
        TxOut(value=MAX_SUPPLY_SATOSHIS + 1, script_pubkey=b"\x00" + b"\x11" * 20)


def test_txin_rejects_bad_txid_length():
    with pytest.raises(ValueError, match="32 bytes"):
        TxIn(prev_txid=b"\x00" * 31, prev_vout=0)


def test_txin_rejects_bad_vout():
    with pytest.raises(ValueError, match="uint32"):
        TxIn(prev_txid=b"\x00" * 32, prev_vout=-1)


def test_p2pkh_output_structure():
    pkh = _pkh()
    out = TxOut.p2pkh(125_000_000, pkh)
    assert out.value == 125_000_000
    assert out.pubkey_hash() == pkh


def test_fee_non_negative():
    pkh = _pkh()
    tx = Transaction(
        inputs=[TxIn(prev_txid=b"\xaa" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(900, pkh)],
    )
    assert fee_of(tx, [1000]) == 100
    with pytest.raises(ValueError, match="exceed"):
        fee_of(tx, [800])


def test_transaction_fields():
    pkh = _pkh()
    tx = Transaction(
        version=1,
        inputs=[TxIn(prev_txid=b"\x01" * 32, prev_vout=2, script_sig=b"sig", sequence=0xFFFFFFFE)],
        outputs=[TxOut.p2pkh(50, pkh)],
        locktime=100,
    )
    assert tx.version == 1
    assert len(tx.inputs) == 1
    assert len(tx.outputs) == 1
    assert tx.locktime == 100
    assert tx.output_value() == 50
