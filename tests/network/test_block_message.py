"""BLOCK message codec."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.constants import MAX_BLOCK_SIZE
from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.messages import decode_block, encode_block
from mhcoin.network.serialization import ProtocolError
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_block_roundtrip(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    _, chain, mp, _ = make_relay_node(tmp_path / "a")
    try:
        block = mine_extending_block(chain, mp, kp)
        raw = encode_block(block)
        out = decode_block(raw)
        assert out.block_hash() == block.block_hash()
        assert encode_block(out) == raw
    finally:
        chain.close()


def test_block_hash_deterministic(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    _, chain, mp, _ = make_relay_node(tmp_path / "a")
    try:
        block = mine_extending_block(chain, mp, kp)
        assert block.block_hash() == decode_block(block.serialize()).block_hash()
    finally:
        chain.close()


def test_block_truncated(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    _, chain, mp, _ = make_relay_node(tmp_path / "a")
    try:
        raw = encode_block(mine_extending_block(chain, mp, kp))
        with pytest.raises(ProtocolError):
            decode_block(raw[:20])
    finally:
        chain.close()


def test_block_trailing(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path / "a", kp)
    _, chain, mp, _ = make_relay_node(tmp_path / "a")
    try:
        with pytest.raises(ProtocolError):
            decode_block(encode_block(mine_extending_block(chain, mp, kp)) + b"\x00")
    finally:
        chain.close()


def test_block_oversized():
    with pytest.raises(ProtocolError):
        decode_block(b"\x00" * (MAX_BLOCK_SIZE + 1))
