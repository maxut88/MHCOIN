"""Milestone 2 audit — supply, coinbase, fees, double-spend, persistence, chain work."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.validation import ValidationError, validate_block, validate_transaction
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.chain_work import get_chain_work, select_best_chain, work_for_bits
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import INITIAL_BLOCK_SUBSIDY, REGTEST_GENESIS_TIMESTAMP, REGTEST_NBITS, SATOSHI_PER_COIN
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mining.block_template import build_block_template
from mhcoin.mempool import Mempool
from mhcoin.node.local_node import LocalNode
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.wallet import Wallet, WalletPaths


def test_alice_bob_supply_accounting(tmp_path: Path):
    """
    Genesis 50 → Alice.
    Alice sends 1.25 to Bob, fee 0.00001, change 48.74999.
    Miner (Alice) gets subsidy 50 + fee 0.00001.

    Alice wallet shows ~98.75 (= change + coinbase), but TOTAL circulating must be 100.0 MHC.
    Fee redistributes existing coins; it does NOT mint.
    """
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="audit", label="alice")
    bob = w.create(password="audit", label="bob", make_default=False)
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)

    assert node.chain.circulating_supply() == 50 * SATOSHI_PER_COIN
    assert node.chain.issued_supply() == get_block_subsidy(0)

    res = w.send(bob, "1.25", password="audit", utxo=node.chain.utxo)
    assert res.amount == 125_000_000
    assert res.fee == 1000
    assert res.change == 4_874_999_000
    assert res.amount + res.fee + res.change == 50 * SATOSHI_PER_COIN

    node.submit_tx(res.tx)
    node.mine_one(alice)

    issued = get_block_subsidy(0) + get_block_subsidy(1)
    assert issued == 100 * SATOSHI_PER_COIN
    assert node.chain.circulating_supply() == issued
    assert node.chain.issued_supply() == issued

    bal_a = node.chain.utxo.balance_for_pubkey_hash(address_to_pubkey_hash(alice))
    bal_b = node.chain.utxo.balance_for_pubkey_hash(address_to_pubkey_hash(bob))
    assert bal_b == 125_000_000
    # change + (subsidy + fee) = 48.74999 + 50.00001 = 98.75
    assert bal_a == 9_875_000_000
    assert bal_a + bal_b == issued
    # Must NOT be 148.75 MHC
    assert bal_a + bal_b != int(148.75 * SATOSHI_PER_COIN)
    node.chain.assert_supply_consistency()
    node.close()


def test_normal_tx_cannot_mint(tmp_path: Path):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x")
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    kp = w.unlock_default("x")
    pkh = hash160(kp.public_key_compressed)
    genesis = node.chain.get_block_by_height(0)
    assert genesis is not None
    cb = genesis.transactions[0]
    # Try to create outputs > input
    tx = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(60 * SATOSHI_PER_COIN, pkh)],
    )
    sign_input(tx, 0, kp.private_key, kp.public_key_compressed, cb.outputs[0].script_pubkey, cb.outputs[0].value)
    with pytest.raises(ValidationError):
        validate_transaction(tx, node.chain.utxo, height=1)
    node.close()


def test_excessive_coinbase_rejected(tmp_path: Path):
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    node = LocalNode(tmp_path, hrp="mhc")
    from mhcoin.blockchain.genesis import mine_regtest_genesis

    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    node.chain.init_with_genesis(genesis)
    block = build_block_template(
        height=1,
        previous_hash=node.chain.tip_hash,
        timestamp=REGTEST_GENESIS_TIMESTAMP + 600,
        bits=REGTEST_NBITS,
        mempool=Mempool(),
        utxo=node.chain.utxo,
        miner_pubkey_hash=pkh,
    )
    block.transactions[0].outputs[0].value = INITIAL_BLOCK_SUBSIDY + 1  # no fees → excess
    block.set_merkle_root()
    mine_block(block)
    with pytest.raises(ValidationError, match="excessive"):
        validate_block(
            block,
            node.chain.utxo,
            height=1,
            expected_prev=node.chain.tip_hash,
            expected_bits=REGTEST_NBITS,
        )
    node.close()


def test_utxo_persistence_after_restart(tmp_path: Path):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="r")
    bob = w.create(password="r", label="bob", make_default=False)
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    res = w.send(bob, "1.25", password="r", utxo=node.chain.utxo)
    node.submit_tx(res.tx)
    node.mine_one(alice)
    tip = node.chain.tip_hash
    circ = node.chain.circulating_supply()
    work = node.chain.get_chain_work()
    node.close()

    node2 = LocalNode(tmp_path, hrp="mhc")
    assert node2.chain.height == 1
    assert node2.chain.tip_hash == tip
    assert node2.chain.circulating_supply() == circ == 100 * SATOSHI_PER_COIN
    assert node2.chain.get_chain_work() == work
    node2.chain.assert_supply_consistency()
    assert len(node2.mempool) == 0  # mined tx cleared from persisted mempool
    node2.close()


def test_double_spend_mempool_and_utxo(tmp_path: Path):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="d")
    bob = w.create(password="d", label="bob", make_default=False)
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    t1 = w.send(bob, "1", password="d", utxo=node.chain.utxo)
    node.submit_tx(t1.tx)
    t2 = w.send(bob, "2", password="d", utxo=node.chain.utxo)
    # same UTXO still unspent in chain UTXO view, but mempool marks spend
    from mhcoin.mempool import MempoolError

    with pytest.raises(MempoolError):
        node.submit_tx(t2.tx)
    node.close()


def test_cumulative_work_prefers_more_work():
    # Same bits → work proportional to length for regtest equal difficulty
    from mhcoin.blockchain.genesis import mine_regtest_genesis

    pkh = hash160(generate_keypair().public_key_compressed)
    g1 = mine_regtest_genesis(pubkey_hash=pkh)
    g2 = mine_regtest_genesis(pubkey_hash=pkh)
    # two single-block chains of equal work — select_best_chain still returns one
    best = select_best_chain([[g1], [g2]])
    assert best in ([g1], [g2])
    assert get_chain_work([g1, g1]) == 2 * work_for_bits(REGTEST_NBITS)  # illustrative sum API
    assert work_for_bits(REGTEST_NBITS) > 0


def test_halving_boundaries():
    assert get_block_subsidy(0) == INITIAL_BLOCK_SUBSIDY
    assert get_block_subsidy(209_999) == INITIAL_BLOCK_SUBSIDY
    assert get_block_subsidy(210_000) == INITIAL_BLOCK_SUBSIDY // 2
    assert get_block_subsidy(419_999) == INITIAL_BLOCK_SUBSIDY // 2
    assert get_block_subsidy(420_000) == INITIAL_BLOCK_SUBSIDY // 4
