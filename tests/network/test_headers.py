"""GETHEADERS / HEADERS codecs and locator."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.constants import MAX_HEADERS, MAX_LOCATOR_HASHES, ZERO_HASH
from mhcoin.network.messages import (
    decode_getheaders,
    decode_headers,
    encode_getheaders,
    encode_headers,
)
from mhcoin.network.serialization import ProtocolError
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_getheaders_roundtrip():
    loc = [b"\xaa" * 32, b"\xbb" * 32]
    raw = encode_getheaders(loc, ZERO_HASH)
    out_loc, stop = decode_getheaders(raw)
    assert out_loc == loc
    assert stop == ZERO_HASH


def test_getheaders_oversized_locator():
    with pytest.raises(ProtocolError):
        encode_getheaders([b"\x00" * 32] * (MAX_LOCATOR_HASHES + 1))


def test_headers_roundtrip(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        b = mine_extending_block(chain, mp, kp)
        relay.accept_block(b)
        headers = [b.header]
        raw = encode_headers(headers)
        assert decode_headers(raw)[0].block_hash() == b.block_hash()
    finally:
        chain.close()


def test_headers_oversized():
    from mhcoin.blockchain.block import BlockHeader

    with pytest.raises(ProtocolError):
        encode_headers([BlockHeader()] * (MAX_HEADERS + 1))


def test_headers_trailing():
    from mhcoin.blockchain.block import BlockHeader

    raw = encode_headers([BlockHeader(version=1)]) + b"\x00"
    with pytest.raises(ProtocolError):
        decode_headers(raw)


def test_build_locator(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        for _ in range(5):
            relay.accept_block(mine_extending_block(chain, mp, kp))
        loc = chain.build_locator()
        assert loc[0] == chain.tip_hash
        assert loc[-1] == chain.get_block_by_height(0).block_hash()
        assert len(loc) <= MAX_LOCATOR_HASHES
    finally:
        chain.close()


def test_headers_after_locator(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        for _ in range(4):
            relay.accept_block(mine_extending_block(chain, mp, kp))
        genesis_h = chain.get_block_by_height(0).block_hash()
        headers = chain.headers_after_locator([genesis_h], hash_stop=ZERO_HASH, limit=10)
        assert len(headers) == 4
        assert headers[0].previous_block_hash == genesis_h
    finally:
        chain.close()
