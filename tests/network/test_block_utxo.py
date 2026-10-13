"""UTXO atomicity on block connect."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.validation import ValidationError
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.relay_helpers import make_payment_tx, make_relay_node, seed_spendable_chain


def test_failed_block_leaves_utxo_unchanged(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    _, chain, mp, relay = make_relay_node(tmp_path)
    try:
        t1 = make_payment_tx(genesis, kp)
        bad = Transaction(
            inputs=[TxIn(prev_txid=b"\xab" * 32, prev_vout=0, script_sig=b"no", sequence=0xFFFFFFFF)],
            outputs=[TxOut.p2pkh(1, hash160(kp.public_key_compressed))],
        )
        height = 1
        s = get_block_subsidy(height)
        cb = Transaction(
            inputs=[TxIn.coinbase(height)],
            outputs=[TxOut.p2pkh(s + 1000, hash160(kp.public_key_compressed))],
        )
        tip = chain.tip_hash
        snap = dict(chain.utxo._mem)
        block = Block(
            header=BlockHeader(
                version=1,
                previous_block_hash=chain.tip_hash,
                merkle_root=b"\x00" * 32,
                timestamp=int(time.time()),
                bits=REGTEST_NBITS,
                nonce=0,
            ),
            transactions=[cb, t1, bad],
        )
        block.set_merkle_root()
        mine_block(block)
        with pytest.raises((ValidationError, Exception)):
            relay.accept_block(block)
        assert chain.tip_hash == tip
        assert chain.utxo._mem.keys() == snap.keys()
    finally:
        chain.close()
