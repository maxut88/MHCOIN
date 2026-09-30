"""Coinbase rules."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import make_payment_tx, make_relay_node, seed_spendable_chain


def test_only_first_may_be_coinbase(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    _, chain, _, _ = make_relay_node(tmp_path)
    try:
        height = 1
        s = get_block_subsidy(height)
        cb1 = Transaction(
            inputs=[TxIn.coinbase(height)],
            outputs=[TxOut.p2pkh(s // 2, hash160(kp.public_key_compressed))],
        )
        cb2 = Transaction(
            inputs=[TxIn.coinbase(height, extra=b"2")],
            outputs=[TxOut.p2pkh(s // 2, hash160(kp.public_key_compressed))],
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
            transactions=[cb1, cb2],
        )
        block.set_merkle_root()
        mine_block(block)
        with pytest.raises(ValidationError):
            validate_block(
                block, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()


def test_coinbase_must_be_first(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    _, chain, _, _ = make_relay_node(tmp_path)
    try:
        tx = make_payment_tx(genesis, kp)
        height = 1
        s = get_block_subsidy(height)
        cb = Transaction(
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
            transactions=[tx, cb],
        )
        block.set_merkle_root()
        mine_block(block)
        with pytest.raises(ValidationError):
            validate_block(
                block, chain.utxo, height=height, expected_prev=chain.tip_hash, expected_bits=REGTEST_NBITS
            )
    finally:
        chain.close()
