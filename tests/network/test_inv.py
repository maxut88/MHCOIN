"""INV codec tests."""

from __future__ import annotations

import pytest

from mhcoin.network.constants import INV_TYPE_TX, MAX_INV_ITEMS
from mhcoin.network.messages import InventoryVector, decode_inv, encode_inv
from mhcoin.network.serialization import ProtocolError
from mhcoin.transaction.serialization import write_u32, write_varint


def test_inv_roundtrip_single():
    h = b"\x11" * 32
    raw = encode_inv([InventoryVector(INV_TYPE_TX, h)])
    out = decode_inv(raw)
    assert len(out) == 1
    assert out[0].type == INV_TYPE_TX
    assert out[0].hash == h


def test_inv_multiple():
    items = [InventoryVector(INV_TYPE_TX, bytes([i]) * 32) for i in range(5)]
    assert decode_inv(encode_inv(items)) == items


def test_inv_empty():
    assert decode_inv(encode_inv([])) == []


def test_inv_oversized():
    items = [(INV_TYPE_TX, bytes([i % 256]) * 32) for i in range(MAX_INV_ITEMS + 1)]
    with pytest.raises(ProtocolError):
        encode_inv(items)


def test_inv_malformed_truncated():
    with pytest.raises(ProtocolError):
        decode_inv(b"\x01")


def test_inv_trailing_bytes():
    raw = encode_inv([InventoryVector(INV_TYPE_TX, b"\xaa" * 32)]) + b"\x00"
    with pytest.raises(ProtocolError):
        decode_inv(raw)


def test_inv_invalid_type():
    payload = write_varint(1) + write_u32(99) + (b"\x00" * 32)
    with pytest.raises(ProtocolError):
        decode_inv(payload)


def test_inv_tuple_form():
    h = b"\x22" * 32
    raw = encode_inv([(INV_TYPE_TX, h)])
    assert decode_inv(raw)[0].hash == h
