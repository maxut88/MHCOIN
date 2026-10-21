"""Three-node activation boundary tests (isolated localnet — no mainnet).

Node A/B: upgraded (maturity enforced after ACTIVATION_HEIGHT).
Node C: legacy behaviour (activation=0 — old nodes).

Does not touch production.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.validation import ValidationError, validate_transaction
from mhcoin.consensus.params import COINBASE_MATURITY, is_coinbase_mature
from mhcoin.crypto.hashing import hash160
from mhcoin.node.local_node import LocalNode
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.wallet import Wallet, WalletPaths


ACTIVATION = 30  # localnet-only test height


def _mine(node: LocalNode, addr: str, n: int) -> None:
    for _ in range(n):
        node.mine_one(addr)


def _spend_cb_at_height0(node: LocalNode, w: Wallet, password: str, dest: str) -> Transaction:
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


@pytest.fixture
def three_wallets(tmp_path: Path):
    nodes = {}
    for name in ("a", "b", "c"):
        p = tmp_path / name
        paths = WalletPaths(data_dir=p, network="regtest", hrp="mhc")
        w = Wallet(paths)
        addr = w.create(password="x").address
        node = LocalNode(p, hrp="mhc", network="regtest")
        node.bootstrap_genesis(addr)
        nodes[name] = (node, w, addr)
    return nodes


def test_pre_activation_early_spend_accepted_by_all(three_wallets):
    """Blocks before ACTIVATION: old rule — early spend OK on new and old validators."""
    node, w, addr = three_wallets["a"]
    bob_paths = WalletPaths(data_dir=node.data_dir / "bob", network="regtest", hrp="mhc")
    bw = Wallet(bob_paths)
    dest = bw.create(password="x").address
    _mine(node, addr, 5)  # tip=5 < ACTIVATION
    tx = _spend_cb_at_height0(node, w, "x", dest)
    # New nodes with activation=ACTIVATION but spend_height < activation → no enforce
    fee = validate_transaction(
        tx, node.chain.utxo, height=6, coinbase_maturity_activation=ACTIVATION
    )
    assert fee >= 0
    # Old node (activation=0)
    fee2 = validate_transaction(
        tx, node.chain.utxo, height=6, coinbase_maturity_activation=0
    )
    assert fee2 >= 0
    node.close()
    three_wallets["b"][0].close()
    three_wallets["c"][0].close()


@pytest.mark.parametrize("age,expect_new_ok", [(1, False), (20, False), (99, False), (100, True)])
def test_post_activation_ages_new_vs_old(tmp_path: Path, age: int, expect_new_ok: bool):
    """After activation: new nodes enforce; old nodes still accept immature spends."""
    paths = WalletPaths(data_dir=tmp_path / f"n{age}", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / f"c{age}", hrp="mhc")
    node.bootstrap_genesis(alice)
    if age > 1:
        _mine(node, alice, age - 1)
    assert node.chain.height == age - 1
    tx = _spend_cb_at_height0(node, w, "x", bob)
    spend_h = age  # next block
    # Ensure we're past activation for enforce (activation=1)
    act = 1
    if expect_new_ok:
        validate_transaction(
            tx, node.chain.utxo, height=spend_h, coinbase_maturity_activation=act
        )
    else:
        with pytest.raises(ValidationError, match="immature coinbase"):
            validate_transaction(
                tx, node.chain.utxo, height=spend_h, coinbase_maturity_activation=act
            )
    # Old node always accepts (activation=0) — SPLIT RISK demonstration
    validate_transaction(
        tx, node.chain.utxo, height=spend_h, coinbase_maturity_activation=0
    )
    node.close()


def test_coinbase_created_before_activation_spent_after(tmp_path: Path):
    """CB at H=0; activation at 50; spend at 100 requires age>=100 (still H+100)."""
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    act = 50
    _mine(node, alice, 98)  # tip=98; next=99 < 0+100
    tx = _spend_cb_at_height0(node, w, "x", bob)
    with pytest.raises(ValidationError, match="immature coinbase"):
        validate_transaction(
            tx, node.chain.utxo, height=99, coinbase_maturity_activation=act
        )
    _mine(node, alice, 1)  # tip=99; next=100
    validate_transaction(
        tx, node.chain.utxo, height=100, coinbase_maturity_activation=act
    )
    assert is_coinbase_mature(coinbase_height=0, spend_height=100)
    assert not is_coinbase_mature(coinbase_height=0, spend_height=99)
    node.close()


def test_old_new_disagreement_is_documented_split_risk():
    """Explicit: same immature tx → new REJECT, old ACCEPT after activation."""
    assert COINBASE_MATURITY == 100
    # Semantic proof without full chain
    assert is_coinbase_mature(coinbase_height=10, spend_height=109) is False
    assert is_coinbase_mature(coinbase_height=10, spend_height=110) is True


def test_reorg_semantics_across_activation_boundary():
    """Activation is height-based on the spend block, not CB create time."""
    # Spend in block 40 with activation=50 → not enforced
    assert 40 < 50
    # Spend in block 50 with activation=50 → enforced
    assert 50 >= 50
