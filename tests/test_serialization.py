"""STEP 1 — canonical serialization + TXID determinism."""

import pytest

from mhcoin.crypto.hashing import hash160, hash256
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction import (
    Transaction,
    TxIn,
    TxOut,
    deserialize_transaction,
    serialize_transaction,
)
from mhcoin.transaction.serialization import SerializationError, write_u32, write_varint


def _sample_tx(*, version: int = 1, amount: int = 125_000_000, locktime: int = 0) -> Transaction:
    pkh = hash160(generate_keypair().public_key_compressed)
    return Transaction(
        version=version,
        inputs=[
            TxIn(
                prev_txid=bytes.fromhex("11" * 32),
                prev_vout=0,
                script_sig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        outputs=[TxOut.p2pkh(amount, pkh)],
        locktime=locktime,
    )


def test_serialize_deterministic():
    tx = _sample_tx()
    a = serialize_transaction(tx)
    b = serialize_transaction(tx)
    assert a == b
    assert isinstance(a, bytes)


def test_roundtrip_deserialize_serialize():
    tx = _sample_tx(amount=50_000_000, locktime=42)
    raw = serialize_transaction(tx)
    restored = deserialize_transaction(raw)
    assert serialize_transaction(restored) == raw
    assert restored.version == tx.version
    assert restored.locktime == tx.locktime
    assert restored.inputs[0].prev_txid == tx.inputs[0].prev_txid
    assert restored.inputs[0].prev_vout == tx.inputs[0].prev_vout
    assert restored.outputs[0].value == tx.outputs[0].value
    assert restored.outputs[0].script_pubkey == tx.outputs[0].script_pubkey


def test_txid_is_double_sha256_of_canonical_bytes():
    tx = _sample_tx()
    raw = serialize_transaction(tx)
    assert tx.txid() == hash256(raw)
    assert len(tx.txid()) == 32
    assert tx.txid_hex() == tx.txid().hex()


def test_same_tx_same_txid():
    # Build with fixed fields (not random key for pubkey — use fixed hash)
    pkh = b"\xab" * 20
    tx1 = Transaction(
        inputs=[TxIn(prev_txid=b"\x22" * 32, prev_vout=1)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    tx2 = Transaction(
        inputs=[TxIn(prev_txid=b"\x22" * 32, prev_vout=1)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    assert serialize_transaction(tx1) == serialize_transaction(tx2)
    assert tx1.txid() == tx2.txid()


def test_changing_output_changes_txid():
    pkh = b"\xcd" * 20
    tx_a = Transaction(
        inputs=[TxIn(prev_txid=b"\x33" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    tx_b = Transaction(
        inputs=[TxIn(prev_txid=b"\x33" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1001, pkh)],
    )
    assert tx_a.txid() != tx_b.txid()


def test_changing_input_changes_txid():
    pkh = b"\xef" * 20
    tx_a = Transaction(
        inputs=[TxIn(prev_txid=b"\x44" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    tx_b = Transaction(
        inputs=[TxIn(prev_txid=b"\x44" * 32, prev_vout=1)],
        outputs=[TxOut.p2pkh(1000, pkh)],
    )
    assert tx_a.txid() != tx_b.txid()


def test_changing_locktime_changes_txid():
    pkh = b"\x12" * 20
    tx_a = Transaction(
        inputs=[TxIn(prev_txid=b"\x55" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
        locktime=0,
    )
    tx_b = Transaction(
        inputs=[TxIn(prev_txid=b"\x55" * 32, prev_vout=0)],
        outputs=[TxOut.p2pkh(1000, pkh)],
        locktime=1,
    )
    assert tx_a.txid() != tx_b.txid()


def test_trailing_bytes_rejected():
    tx = _sample_tx()
    raw = serialize_transaction(tx) + b"\x00"
    with pytest.raises(SerializationError, match="trailing"):
        deserialize_transaction(raw)


def test_truncated_bytes_rejected():
    tx = _sample_tx()
    raw = serialize_transaction(tx)
    with pytest.raises(SerializationError):
        deserialize_transaction(raw[:10])


def test_varint_and_u32_helpers():
    assert write_varint(0) == b"\x00"
    assert write_varint(252) == b"\xfc"
    assert write_varint(253) == b"\xfd\xfd\x00"
    assert write_u32(1) == b"\x01\x00\x00\x00"


def test_empty_tx_serializes():
    tx = Transaction(inputs=[], outputs=[])
    raw = serialize_transaction(tx)
    assert deserialize_transaction(raw).serialize() == raw
    assert len(tx.txid()) == 32
