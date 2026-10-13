"""Merkle root validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.merkle import merkle_root, verify_merkle_root
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.keys import generate_keypair
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_merkle_recompute_matches(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, _ = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        assert block.recompute_merkle_root() == block.header.merkle_root
        assert verify_merkle_root([t.txid() for t in block.transactions], block.header.merkle_root)
    finally:
        chain.close()


def test_tampered_merkle_rejected(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, _ = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        block.header.merkle_root = merkle_root([b"\x00" * 32])
        mine_block(block, fix_merkle=False)
        with pytest.raises(ValidationError):
            validate_block(
                block,
                chain.utxo,
                height=chain.height + 1,
                expected_prev=chain.tip_hash,
                expected_bits=REGTEST_NBITS,
            )
    finally:
        chain.close()
