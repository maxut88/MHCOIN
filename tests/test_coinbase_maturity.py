"""Isolated coinbase maturity consensus tests (regtest only — no mainnet).

Semantics under test:
  coinbase at height H is spendable at spend_height >= H + COINBASE_MATURITY
  (H+100 OK, H+99 rejected) once activation_height > 0 and spend_height >= activation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.validation import ValidationError, validate_transaction
from mhcoin.consensus.params import (
    COINBASE_MATURITY,
    COINBASE_MATURITY_ACTIVATION,
    coinbase_maturity_enforced,
    is_coinbase_mature,
)
from mhcoin.crypto.hashing import hash160
from mhcoin.mempool import Mempool, MempoolError
from mhcoin.node.local_node import LocalNode
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.wallet.wallet import Wallet, WalletPaths


@pytest.fixture
def activate_regtest_maturity(monkeypatch):
    """Enable maturity from height 1 on regtest for this test module."""
    patched = dict(COINBASE_MATURITY_ACTIVATION)
    patched["regtest"] = 1
    monkeypatch.setattr(
        "mhcoin.consensus.params.COINBASE_MATURITY_ACTIVATION", patched
    )
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    return patched


def _mine_n(node: LocalNode, addr: str, n: int) -> None:
    for _ in range(n):
        node.mine_one(addr)


def _spend_genesis_coinbase(node: LocalNode, w: Wallet, password: str, dest: str) -> Transaction:
    from mhcoin.wallet.addresses import address_to_pubkey_hash

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


def test_maturity_helpers_semantics():
    assert COINBASE_MATURITY == 100
    assert is_coinbase_mature(coinbase_height=10, spend_height=109) is False  # H+99
    assert is_coinbase_mature(coinbase_height=10, spend_height=110) is True  # H+100
    assert coinbase_maturity_enforced(spend_height=50, activation_height=0) is False
    assert coinbase_maturity_enforced(spend_height=50, activation_height=100) is False
    assert coinbase_maturity_enforced(spend_height=100, activation_height=100) is True


def test_pre_activation_allows_early_spend(tmp_path: Path, monkeypatch):
    """Historical path: activation=0 → immature spend still validates (mainnet today)."""
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    # ensure inactive
    assert COINBASE_MATURITY_ACTIVATION.get("regtest", 0) == 0
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    tx = _spend_genesis_coinbase(node, w, "x", bob)
    # Spend at height 1 (age=1) must succeed when inactive
    fee = validate_transaction(tx, node.chain.utxo, height=1, coinbase_maturity_activation=0)
    assert fee >= 0
    node.close()


@pytest.mark.parametrize(
    "extra_blocks,expect_ok",
    [
        (1, False),  # spend height = 1 → age 1
        (20, False),  # age 20 (pool accounting depth)
        (99, False),  # H+99
        (100, True),  # H+100
        (101, True),  # H+101
    ],
)
def test_spend_ages_after_activation(
    tmp_path: Path, activate_regtest_maturity, extra_blocks: int, expect_ok: bool
):
    paths = WalletPaths(data_dir=tmp_path / f"a{extra_blocks}", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / f"n{extra_blocks}", hrp="mhc")
    node.bootstrap_genesis(alice)
    # Mine extra_blocks-1 more so tip = extra_blocks-1... wait
    # Genesis height 0. After mining k blocks, tip=k.
    # We want spend_height = extra_blocks (next block after tip = extra_blocks-1 when tip=extra_blocks-1)
    # Actually: mine (extra_blocks - 1) blocks after genesis so tip = extra_blocks - 1,
    # then validate spend at height=extra_blocks.
    if extra_blocks > 1:
        _mine_n(node, alice, extra_blocks - 1)
    assert node.chain.height == extra_blocks - 1
    tx = _spend_genesis_coinbase(node, w, "x", bob)
    if expect_ok:
        fee = validate_transaction(
            tx,
            node.chain.utxo,
            height=extra_blocks,
            coinbase_maturity_activation=1,
        )
        assert fee >= 0
        # Also accepted into mempool at tip (validates tip+1 == extra_blocks)
        node.submit_tx(tx)
    else:
        with pytest.raises(ValidationError, match="immature coinbase"):
            validate_transaction(
                tx,
                node.chain.utxo,
                height=extra_blocks,
                coinbase_maturity_activation=1,
            )
        with pytest.raises(MempoolError, match="immature coinbase"):
            node.submit_tx(tx)
    node.close()


def test_historical_block_before_activation_keeps_early_spend(
    tmp_path: Path, monkeypatch
):
    """Blocks at height < activation are not re-checked with maturity."""
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    tx = _spend_genesis_coinbase(node, w, "x", bob)
    # Simulate validating an old block at height 5 with activation=1000 (future)
    # Rule not yet active → early spend OK (preserves history)
    fee = validate_transaction(
        tx,
        node.chain.utxo,
        height=5,
        coinbase_maturity_activation=1000,
    )
    assert fee >= 0
    node.close()


def test_block_connect_rejects_immature_after_activation(
    tmp_path: Path, activate_regtest_maturity
):
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    _mine_n(node, alice, 10)  # tip=10; next block height 11 << 100
    tx = _spend_genesis_coinbase(node, w, "x", bob)
    with pytest.raises(MempoolError, match="immature coinbase"):
        node.submit_tx(tx)
    # Mine empty blocks to maturity
    _mine_n(node, alice, 89)  # tip = 10+89 = 99; next=100
    assert node.chain.height == 99
    node.submit_tx(tx)
    h, _ = node.mine_one(alice)
    assert h == 100
    assert node.chain.height == 100
    node.close()


def test_wallet_excludes_immature_coinbase(tmp_path: Path, activate_regtest_maturity):
    from mhcoin.wallet.send import build_account_send_tx, format_mhc
    from mhcoin.wallet.addresses import address_to_pubkey_hash
    from mhcoin.crypto.hashing import hash160 as h160

    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    _mine_n(node, alice, 5)
    kp = w.unlock_default("x")
    keys = {h160(kp.public_key_compressed): (kp.private_key, kp.public_key_compressed)}
    change = address_to_pubkey_hash(alice, hrp="mhc")
    with pytest.raises(ValueError, match="insufficient funds"):
        build_account_send_tx(
            utxo=node.chain.utxo,
            keys_by_pkh=keys,
            to_address=bob,
            amount_sats=1_000_000,
            change_pubkey_hash=change,
            tip_height=node.chain.height,
            coinbase_maturity_activation=1,
        )
    node.close()


def test_pool_20_conf_still_immature_under_consensus_100():
    """Pool mature_confirms=20 must NOT imply consensus-spendable."""
    # age 20 < 100
    assert is_coinbase_mature(coinbase_height=100, spend_height=120) is False
    assert is_coinbase_mature(coinbase_height=100, spend_height=200) is True


def test_mainnet_activation_still_zero():
    assert COINBASE_MATURITY_ACTIVATION["mainnet"] == 0


def test_mempool_rejects_immature_coinbase_spend(
    tmp_path: Path, activate_regtest_maturity
):
    """Mempool validates at tip+1 and must reject immature coinbase spends."""
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    _mine_n(node, alice, 50)  # tip=50; next spend height=51 << 100
    tx = _spend_genesis_coinbase(node, w, "x", bob)
    with pytest.raises(MempoolError, match="immature coinbase"):
        node.mempool.add(
            tx,
            node.chain.utxo,
            height=node.chain.height,
            network="regtest",
        )
    # Same tx accepted once tip+1 >= 100
    _mine_n(node, alice, 49)  # tip=99
    assert node.chain.height == 99
    hx = node.mempool.add(
        tx,
        node.chain.utxo,
        height=node.chain.height,
        network="regtest",
    )
    assert hx == tx.txid_hex()
    node.close()


def test_refresh_from_disk_still_prefers_reload():
    """Integration must not regress PR #1–3 UTXO reload fix."""
    from pathlib import Path as P

    src = (P(__file__).resolve().parents[1] / "mhcoin/blockchain/chain.py").read_text(
        encoding="utf-8"
    )
    fn = src.split("def refresh_from_disk", 1)[1].split("\ndef ", 1)[0]
    assert "self.utxo.reload()" in fn
    # Must not start with unconditional close()+UTXOSet recreate
    head = fn.split("self.utxo.reload()")[0]
    assert "self.utxo.close()" not in head or "try:" in head
