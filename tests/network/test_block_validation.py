"""Block validation coverage (Stage 4)."""

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
from mhcoin.transaction.signing import sign_input
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import (
    make_payment_tx,
    make_relay_node,
    mine_extending_block,
    seed_spendable_chain,
)


def test_valid_block_accepted(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        assert relay.accept_block(block) == 1
        assert chain.tip_hash == block.block_hash()
    finally:
        chain.close()


def test_invalid_pow_rejected(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        tip = chain.tip_hash
        for _ in range(10_000):
            block.header.nonce = (block.header.nonce + 1) & 0xFFFFFFFF
            if not verify_proof_of_work(block.header):
                break
        with pytest.raises((ValidationError, Exception)):
            relay.accept_block(block)
        assert chain.tip_hash == tip
    finally:
        chain.close()


def test_orphan_unknown_prev_stored(tmp_path: Path):
    """Unknown previous hash → orphan pool (Stage 6), tip unchanged."""
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        block = mine_extending_block(chain, mp, kp)
        tip = chain.tip_hash
        block.header.previous_block_hash = b"\x33" * 32
        mine_block(block)
        height = relay.accept_block(block)
        assert height == 0
        assert chain.tip_hash == tip
        assert chain.height == 0
        assert chain.orphans.contains(block.block_hash())
    finally:
        chain.close()


def test_bad_signature_rejected(tmp_path: Path):
    kp = generate_keypair()
    other = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    _, chain, _, _ = make_relay_node(tmp_path)
    try:
        cb = genesis.transactions[0]
        tx = make_payment_tx(genesis, kp)
        sign_input(
            tx,
            0,
            other.private_key,
            other.public_key_compressed,
            cb.outputs[0].script_pubkey,
            cb.outputs[0].value,
        )
        height = 1
        s = get_block_subsidy(height)
        coinbase = Transaction(
            inputs=[TxIn.coinbase(height)],
            outputs=[TxOut.p2pkh(s + 1000, hash160(kp.public_key_compressed))],
        )
        block = Block(
            header=BlockHeader(
                version=1,
                previous_block_hash=chain.tip_hash,
                merkle_root=b"\x00" * 32,
                timestamp=int(time.time()),
                bits=REGTEST_NBITS,
                nonce=0,
            ),
            transactions=[coinbase, tx],
        )
        block.set_merkle_root()
        mine_block(block)
        with pytest.raises(ValidationError):
            validate_block(
                block, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()


def test_mempool_tx_removed_after_block(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        tx = make_payment_tx(genesis, kp)
        mp.add(tx, chain.utxo, height=0)
        assert len(mp) == 1
        block = mine_extending_block(chain, mp, kp)
        relay.accept_block(block)
        assert mp.get(tx.txid_hex()) is None
    finally:
        chain.close()
