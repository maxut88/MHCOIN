"""GETDATA codec tests."""

from __future__ import annotations

import pytest

from mhcoin.network.constants import INV_TYPE_TX, MAX_GETDATA_ITEMS
from mhcoin.network.messages import InventoryVector, decode_getdata, encode_getdata
from mhcoin.network.serialization import ProtocolError
from mhcoin.transaction.serialization import write_u32, write_varint


def test_getdata_roundtrip():
    items = [InventoryVector(INV_TYPE_TX, b"\xab" * 32), InventoryVector(INV_TYPE_TX, b"\xcd" * 32)]
    assert decode_getdata(encode_getdata(items)) == items


def test_getdata_multiple():
    items = [InventoryVector(INV_TYPE_TX, bytes([i]) * 32) for i in range(10)]
    assert len(decode_getdata(encode_getdata(items))) == 10


def test_getdata_malformed():
    with pytest.raises(ProtocolError):
        decode_getdata(b"\xfd")


def test_getdata_oversized():
    items = [(INV_TYPE_TX, b"\x01" * 32) for _ in range(MAX_GETDATA_ITEMS + 1)]
    with pytest.raises(ProtocolError):
        encode_getdata(items)


def test_getdata_invalid_type():
    payload = write_varint(1) + write_u32(99) + (b"\xff" * 32)
    with pytest.raises(ProtocolError):
        decode_getdata(payload)


def test_getdata_trailing():
    raw = encode_getdata([InventoryVector(INV_TYPE_TX, b"\x00" * 32)]) + b"\x01"
    with pytest.raises(ProtocolError):
        decode_getdata(raw)
