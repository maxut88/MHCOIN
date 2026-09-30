"""Block inventory INV codec."""

from __future__ import annotations

import pytest

from mhcoin.network.constants import INV_TYPE_BLOCK, INV_TYPE_TX
from mhcoin.network.messages import InventoryVector, decode_inv, encode_inv
from mhcoin.network.serialization import ProtocolError


def test_block_inv_roundtrip():
    h = b"\xab" * 32
    items = [InventoryVector(INV_TYPE_BLOCK, h)]
    assert decode_inv(encode_inv(items)) == items


def test_mixed_tx_block_inv():
    items = [
        InventoryVector(INV_TYPE_TX, b"\x01" * 32),
        InventoryVector(INV_TYPE_BLOCK, b"\x02" * 32),
    ]
    assert decode_inv(encode_inv(items)) == items


def test_unknown_inv_type_rejected():
    from mhcoin.transaction.serialization import write_u32, write_varint

    payload = write_varint(1) + write_u32(99) + (b"\x00" * 32)
    with pytest.raises(ProtocolError):
        decode_inv(payload)
