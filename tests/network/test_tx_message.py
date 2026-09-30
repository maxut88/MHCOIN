"""TX wire codec (encode_tx / decode_tx)."""

from __future__ import annotations

import pytest

from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.constants import MAX_TX_SIZE
from mhcoin.network.messages import decode_tx, encode_tx
from mhcoin.network.serialization import ProtocolError
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import make_payment_tx


def test_tx_codec_valid():
    kp = generate_keypair()
    genesis = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    tx = make_payment_tx(genesis, kp)
    raw = encode_tx(tx)
    out = decode_tx(raw)
    assert out.txid() == tx.txid()


def test_txid_same_bytes_same_id():
    kp = generate_keypair()
    genesis = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    tx = make_payment_tx(genesis, kp)
    assert tx.txid() == Transaction.deserialize(tx.serialize()).txid()


def test_txid_different_tx_different_id():
    kp = generate_keypair()
    genesis = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    assert make_payment_tx(genesis, kp, amount=1_000_000).txid() != make_payment_tx(
        genesis, kp, amount=2_000_000
    ).txid()


def test_tx_invalid_serialization():
    with pytest.raises(ProtocolError):
        decode_tx(b"\x01\x02\x03")


def test_tx_truncated():
    kp = generate_keypair()
    genesis = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    raw = encode_tx(make_payment_tx(genesis, kp))
    with pytest.raises(ProtocolError):
        decode_tx(raw[:8])


def test_tx_trailing_bytes():
    kp = generate_keypair()
    genesis = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    with pytest.raises(ProtocolError):
        decode_tx(encode_tx(make_payment_tx(genesis, kp)) + b"\x00")


def test_tx_oversized():
    with pytest.raises(ProtocolError):
        decode_tx(b"\x00" * (MAX_TX_SIZE + 1))
