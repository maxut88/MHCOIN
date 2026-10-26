"""Exact H*=2000 activation-boundary tests (isolated — no mainnet I/O).

Height rule (owner lock 2026-10-26):
  - spend_height <= 1999 → legacy (maturity NOT enforced)
  - spend_height >= 2000 → maturity enforced (spendable iff spend >= cb + 100)
Applies to all coinbase UTXOs regardless of creation height once spend is at/after H*.
Historical spends confirmed at height < 2000 remain valid (not re-litigated).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.fingerprint import utxo_fingerprint
from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.blockchain.validation import ValidationError, validate_transaction
from mhcoin.consensus.params import (
    COINBASE_MATURITY,
    COINBASE_MATURITY_ACTIVATION,
    coinbase_maturity_enforced,
    get_coinbase_maturity_activation,
    is_coinbase_mature,
)
from mhcoin.explorer.decode import coinbase_maturity_label
from mhcoin.mempool import MempoolError
from mhcoin.node.local_node import LocalNode
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.utxo import OutPoint, UTXOEntry
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.send import spendable_total
from mhcoin.wallet.wallet import Wallet, WalletPaths


H_STAR = 2000


@pytest.fixture
def activate_h_star(monkeypatch):
    patched = dict(COINBASE_MATURITY_ACTIVATION)
    patched["regtest"] = H_STAR
    patched["localnet"] = H_STAR
    monkeypatch.setattr(
        "mhcoin.consensus.params.COINBASE_MATURITY_ACTIVATION", patched
    )
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    return patched


def _spend_genesis_cb(node: LocalNode, w: Wallet, password: str, dest: str) -> Transaction:
    kp = w.unlock_default(password)
    genesis = node.chain.get_block_by_height(0)
    assert genesis is not None
    cb = genesis.transactions[0]
    amount = cb.outputs[0].value - 1000
    dest_pkh = address_to_pubkey_hash(dest, hrp="mhc")
    tx = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(amount, dest_pkh)],
    )
    sign_input(
        tx,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    return tx


def _inject_cb_height(node: LocalNode, height: int) -> None:
    """Rewrite genesis CB UTXO height for boundary simulation (isolated test only)."""
    genesis = node.chain.get_block_by_height(0)
    assert genesis is not None
    cb = genesis.transactions[0]
    op = OutPoint(txid=cb.txid(), vout=0)
    entry = node.chain.utxo.get(op)
    assert entry is not None
    node.chain.utxo._mem[op.key()] = UTXOEntry(
        outpoint=op,
        output=entry.output,
        height=int(height),
        coinbase=True,
    )


def test_params_lock_mainnet_h_star_2000_only():
    assert COINBASE_MATURITY == 100
    assert COINBASE_MATURITY_ACTIVATION["mainnet"] == 2000
    assert get_coinbase_maturity_activation("mainnet") == 2000
    for net in ("testnet", "regtest", "localnet"):
        assert get_coinbase_maturity_activation(net) == 0


@pytest.mark.parametrize(
    "spend_h,expect_enforced",
    [
        (1998, False),
        (1999, False),  # last legacy height
        (2000, True),  # first enforced height
        (2001, True),
        (2100, True),
    ],
)
def test_enforcement_boundary_1999_2000_2001(spend_h: int, expect_enforced: bool):
    assert (
        coinbase_maturity_enforced(spend_height=spend_h, activation_height=H_STAR)
        is expect_enforced
    )


@pytest.mark.parametrize(
    "cb_h,spend_h,ok",
    [
        (1900, 1999, True),  # H+99 but pre-activation → not checked
        (1900, 2000, True),  # age 100 exactly at H*
        (1901, 2000, False),  # age 99 at H* → reject
        (1900, 2001, True),  # H+101
        (0, 2000, True),  # old CB spent after H* (age >> 100)
        (1950, 2049, False),  # H+99 post-activation
        (1950, 2050, True),  # H+100
        (1950, 2051, True),  # H+101
    ],
)
def test_validation_off_by_one_at_exact_h_star(
    tmp_path: Path, activate_h_star, cb_h: int, spend_h: int, ok: bool
):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    _inject_cb_height(node, cb_h)
    tx = _spend_genesis_cb(node, w, "x", bob)
    if ok:
        fee = validate_transaction(
            tx,
            node.chain.utxo,
            height=spend_h,
            coinbase_maturity_activation=H_STAR,
        )
        assert fee >= 0
    else:
        with pytest.raises(ValidationError, match="immature coinbase"):
            validate_transaction(
                tx,
                node.chain.utxo,
                height=spend_h,
                coinbase_maturity_activation=H_STAR,
            )
    node.close()


def test_historical_spend_before_h_star_remains_valid(
    tmp_path: Path, activate_h_star
):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    tx = _spend_genesis_cb(node, w, "x", bob)
    fee = validate_transaction(
        tx, node.chain.utxo, height=5, coinbase_maturity_activation=H_STAR
    )
    assert fee >= 0
    node.close()


def test_cb_created_before_h_star_spent_after_requires_maturity(
    tmp_path: Path, activate_h_star
):
    """CB at 1950 (pre-H*) spent at 2000 → age 50 → reject; at 2050 → accept."""
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    _inject_cb_height(node, 1950)
    tx = _spend_genesis_cb(node, w, "x", bob)
    with pytest.raises(ValidationError, match="immature coinbase"):
        validate_transaction(
            tx, node.chain.utxo, height=2000, coinbase_maturity_activation=H_STAR
        )
    fee = validate_transaction(
        tx, node.chain.utxo, height=2050, coinbase_maturity_activation=H_STAR
    )
    assert fee >= 0
    node.close()


def test_mempool_legacy_accept_then_post_h_star_reject(
    tmp_path: Path, activate_h_star
):
    """Immature CB spend accepted at tip+1 < H*; rejected when tip+1 >= H*."""
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    for _ in range(50):
        node.mine_one(alice)
    assert node.chain.height == 50
    # CB "created" at 1950: age at tip+1=51 is N/A for enforcement (<H*);
    # at spend 2000 age=50 → immature.
    _inject_cb_height(node, 1950)
    tx = _spend_genesis_cb(node, w, "x", bob)
    # tip+1=51 < H* → legacy accept into mempool
    node.submit_tx(tx)
    node.mempool.remove(tx.txid())
    with pytest.raises(ValidationError, match="immature coinbase"):
        validate_transaction(
            tx, node.chain.utxo, height=H_STAR, coinbase_maturity_activation=H_STAR
        )
    from mhcoin.mempool import Mempool

    mp = Mempool()
    # height=tip; mempool validates at tip+1 → 2000
    with pytest.raises(MempoolError, match="immature coinbase"):
        mp.add(
            tx,
            node.chain.utxo,
            height=1999,
            coinbase_maturity_activation=H_STAR,
        )
    node.close()


def test_explorer_labels_mainnet_h_star():
    assert (
        coinbase_maturity_label(
            coinbase_height=1900, tip_height=1998, network="mainnet"
        )
        == "maturity_inactive"
    )
    assert (
        coinbase_maturity_label(
            coinbase_height=1900, tip_height=1999, network="mainnet"
        )
        == "mature"
    )
    assert (
        coinbase_maturity_label(
            coinbase_height=1901, tip_height=1999, network="mainnet"
        )
        == "immature"
    )
    assert (
        coinbase_maturity_label(
            coinbase_height=0, tip_height=5000, network="regtest"
        )
        == "maturity_inactive"
    )


def test_wallet_spendable_total_guards_after_h_star(tmp_path: Path, activate_h_star):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    for _ in range(5):
        node.mine_one(alice)
    # Mark every coinbase UTXO as created at 1950 (young vs spend at 2001)
    for key, entry in list(node.chain.utxo._mem.items()):
        if entry.coinbase:
            node.chain.utxo._mem[key] = UTXOEntry(
                outpoint=entry.outpoint,
                output=entry.output,
                height=1950,
                coinbase=True,
            )
    kp = w.unlock_default("x")
    from mhcoin.crypto.hashing import hash160

    pkh = hash160(kp.public_key_compressed)
    keys = {pkh: (kp.private_key, kp.public_key_compressed)}
    # Pre-H*: tip+1 < 2000 → immature CB still counted
    pre = spendable_total(
        node.chain.utxo,
        keys,
        tip_height=10,
        coinbase_maturity_activation=H_STAR,
    )
    assert pre > 0
    # Post-H*: young CBs excluded
    post = spendable_total(
        node.chain.utxo,
        keys,
        tip_height=2000,
        coinbase_maturity_activation=H_STAR,
    )
    assert post == 0
    node.close()


def test_three_node_sync_through_scaled_h_star_plus_legacy_split(
    tmp_path: Path, monkeypatch
):
    """Three upgraded nodes sync past scaled H*; legacy (act=0) would accept immature spend."""
    scaled = 30
    patched = dict(COINBASE_MATURITY_ACTIVATION)
    patched["regtest"] = scaled
    monkeypatch.setattr(
        "mhcoin.consensus.params.COINBASE_MATURITY_ACTIVATION", patched
    )
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")

    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    pkh = address_to_pubkey_hash(alice, hrp="mhc")
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    nodes = {}
    for name in ("a", "b", "c"):
        d = tmp_path / name
        d.mkdir()
        n = LocalNode(d, hrp="mhc", network="regtest")
        n.chain.init_with_genesis(genesis)
        nodes[name] = n
    a, b, c = nodes["a"], nodes["b"], nodes["c"]

    def sync(src, dst):
        while dst.chain.height < src.chain.height:
            blk = src.chain.get_block_by_height(dst.chain.height + 1)
            assert blk is not None
            dst.chain.accept_block(blk, mempool=dst.mempool)

    for _ in range(scaled + 5):
        a.mine_one(alice)
    sync(a, b)
    sync(a, c)
    assert a.chain.height == b.chain.height == c.chain.height == scaled + 5
    assert a.chain.tip_hash == c.chain.tip_hash
    assert utxo_fingerprint(a.chain.utxo) == utxo_fingerprint(c.chain.utxo)

    tip = a.chain.height
    blk = a.chain.get_block_by_height(tip)
    assert blk is not None
    cb = blk.transactions[0]
    kp = w.unlock_default("x")
    dest_pkh = address_to_pubkey_hash(bob, hrp="mhc")
    young = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(cb.outputs[0].value - 1000, dest_pkh)],
    )
    sign_input(
        young,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    for n in (a, b, c):
        with pytest.raises(ValidationError, match="immature coinbase"):
            validate_transaction(
                young,
                n.chain.utxo,
                height=tip + 1,
                coinbase_maturity_activation=scaled,
            )
        with pytest.raises(MempoolError, match="immature coinbase"):
            n.submit_tx(young)
    # Legacy node risk: activation=0 accepts → potential chain split
    fee = validate_transaction(
        young, a.chain.utxo, height=tip + 1, coinbase_maturity_activation=0
    )
    assert fee >= 0

    for n in (a, b, c):
        n.close()


def test_pool_mature_confirms_independent_of_consensus_activation(tmp_path: Path):
    """Pool MATURE_CONFIRMS=100 is orthogonal to COINBASE_MATURITY_ACTIVATION=2000."""
    import time

    from mhcoin.pool.db import PoolDB

    MATURE_CONFIRMS = 100
    ADDR = "mhc1qnkfc9zcfpun8pkud45gz9qutv6xtrmrun9p07r"
    assert COINBASE_MATURITY_ACTIVATION["mainnet"] == 2000
    db = PoolDB(tmp_path / "pool.sqlite")
    bh = "ab" * 32
    db.add_share(
        address=ADDR,
        worker="t",
        job_id="j100",
        height=100,
        difficulty=1.0,
        is_block=True,
        block_hash=bh,
    )
    db.close_round_with_block(
        height=100,
        block_hash=bh,
        reward_sats=5_000_000_000,
        fee_percent=0.0,
        found_ts=time.time(),
    )
    assert db.mature_rounds(199, MATURE_CONFIRMS, canonical_hash_at=lambda _h: bh) == 0
    assert db.mature_rounds(200, MATURE_CONFIRMS, canonical_hash_at=lambda _h: bh) == 1
    db.close()
