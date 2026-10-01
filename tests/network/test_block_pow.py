"""Block PoW / merkle / coinbase focused tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.proof_of_work import mine_block, verify_proof_of_work
from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def _empty_block(chain, kp, *, merkle=None, prev=None, reward=None):
    height = chain.height + 1
    subsidy = get_block_subsidy(height)
    amt = subsidy if reward is None else reward
    cb = Transaction(
        inputs=[TxIn.coinbase(height)],
        outputs=[TxOut.p2pkh(amt, hash160(kp.public_key_compressed))],
    )
    block = Block(
        header=BlockHeader(
            version=1,
            previous_block_hash=prev or chain.tip_hash,
            merkle_root=b"\x00" * 32,
            timestamp=int(time.time()),
            bits=REGTEST_NBITS,
            nonce=0,
        ),
        transactions=[cb],
    )
    if merkle is not None:
        block.header.merkle_root = merkle
    else:
        block.set_merkle_root()
    mine_block(block)
    return block, height


def test_pow_required(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, _ = make_relay_node(tmp_path)
    try:
        block, height = _empty_block(chain, kp)
        while verify_proof_of_work(block.header):
            block.header.nonce = (block.header.nonce + 1) & 0xFFFFFFFF
            if block.header.nonce == 0:
                break
        # Ensure invalid
        if verify_proof_of_work(block.header):
            # Flip a hash byte via nonce search failure path: bump merkle then leave unmined
            block.header.merkle_root = bytes(b ^ 0xFF for b in block.header.merkle_root)
            assert not verify_proof_of_work(block.header)
        with pytest.raises(ValidationError, match="Proof-of-Work"):
            validate_block(
                block, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()


def test_merkle_mismatch(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, _ = make_relay_node(tmp_path)
    try:
        block, height = _empty_block(chain, kp)
        block.header.merkle_root = b"\xff" * 32
        mine_block(block, fix_merkle=False)
        with pytest.raises(ValidationError, match="merkle"):
            validate_block(
                block, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()


def test_coinbase_reward_and_fees(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        relay.accept_block(block)
        assert chain.height == 1
        # excessive
        bad, height = _empty_block(chain, kp, reward=get_block_subsidy(2) + 1)
        with pytest.raises(ValidationError, match="excessive"):
            validate_block(
                bad, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()
