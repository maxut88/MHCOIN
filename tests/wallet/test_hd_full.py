"""Full BIP84 HD: account balance, gap-scan, change chain, xpub/watch-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.node.local_node import LocalNode
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_to_address
from mhcoin.wallet.hd import (
    DEFAULT_GAP_LIMIT,
    account_xpub_from_seed,
    change_path,
    derive_private_key,
    derive_pubkey_from_account_xpub,
    receive_path,
)
from mhcoin.wallet.bip39 import mnemonic_to_seed
from mhcoin.crypto.keys import private_key_to_public_key
from mhcoin.wallet.storage import load_wallet_file
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths


def _paths(tmp: Path) -> WalletPaths:
    d = tmp / "data"
    d.mkdir(parents=True, exist_ok=True)
    return WalletPaths(data_dir=d, network="regtest", hrp="mhc")


def test_account_balance_includes_change(tmp_path: Path):
    paths = _paths(tmp_path)
    w = Wallet(paths, password="pw")
    alice = w.create(password="pw").address
    bob = w.create(password="pw", label="bob", make_default=False).address
    node = LocalNode(paths.data_dir, hrp="mhc")
    node.bootstrap_genesis(alice)
    node.mine_one(alice)

    # Spend so change lands on …/1/0
    res = w.send(bob, "1", password="pw", utxo=node.chain.utxo)
    assert res.change > 0
    assert res.change_pubkey_hash is not None
    assert res.change_pubkey_hash != address_to_pubkey_hash(alice)
    node.submit_tx(res.tx)
    node.mine_one(alice)

    single, _ = w.balance(alice, account=False)
    account, _ = w.balance(alice, account=True)
    assert account > single
    assert account == single + node.chain.utxo.balance_for_pubkey_hash(res.change_pubkey_hash)
    node.close()


def test_gap_scan_restores_extra_receive(tmp_path: Path):
    a = _paths(tmp_path / "a")
    w = Wallet(a, password="pw")
    created = w.create(password="pw")
    second = w.new_receive_address(password="pw")
    assert second.derivation_path == receive_path(1)

    node = LocalNode(a.data_dir, hrp="mhc")
    node.bootstrap_genesis(created.address)
    # Mine to second address so UTXO marks it used
    node.mine_one(second.address)
    tip = node.chain.height
    assert tip >= 1
    node.close()

    b = _paths(tmp_path / "b")
    # Copy chainstate so restore can see UTXOs
    import shutil

    shutil.copytree(a.data_dir / "chainstate", b.data_dir / "chainstate")
    # Also need blocks for tip consistency — LocalNode may need genesis+blocks.
    for name in ("blocks",):
        src = a.data_dir / name
        if src.exists():
            shutil.copytree(src, b.data_dir / name)

    w2 = Wallet(b, password="pw2")
    restored = w2.restore_from_mnemonic(
        created.mnemonic, password="pw2", gap_limit=DEFAULT_GAP_LIMIT
    )
    assert restored.address == created.address
    rows = w2.list_receive_addresses()
    paths = {r["path"] for r in rows}
    assert receive_path(0) in paths
    assert receive_path(1) in paths
    assert len(rows) >= 2


def test_change_path_used_on_send(tmp_path: Path):
    paths = _paths(tmp_path)
    w = Wallet(paths, password="pw")
    alice = w.create(password="pw").address
    bob = w.create(password="pw", label="bob", make_default=False).address
    node = LocalNode(paths.data_dir, hrp="mhc")
    node.bootstrap_genesis(alice)
    res = w.send(bob, "10", password="pw", utxo=node.chain.utxo)
    assert res.change > 0
    wf = load_wallet_file(paths.wallet_file)
    assert wf is not None
    change_recs = [r for r in wf.wallets if r.derivation_path and r.derivation_path.startswith("m/84'/0'/0'/1/")]
    assert change_recs
    assert any(r.derivation_path == change_path(0) for r in change_recs)
    # Receive list hides change
    recv = w.list_receive_addresses()
    assert all(not (r.get("path") or "").startswith("m/84'/0'/0'/1/") for r in recv)
    node.close()


def test_xpub_roundtrip_and_watch_only_cannot_send(tmp_path: Path):
    paths = _paths(tmp_path)
    w = Wallet(paths, password="pw")
    created = w.create(password="pw")
    xpub = w.export_account_xpub(password="pw")
    assert xpub.startswith("xpub")
    seed = mnemonic_to_seed(created.mnemonic)
    assert xpub == account_xpub_from_seed(seed)

    watch_paths = _paths(tmp_path / "watch")
    ww = Wallet(watch_paths)
    imported = ww.import_account_xpub(xpub, label="watch")
    assert imported.address == created.address
    pub, path = derive_pubkey_from_account_xpub(xpub, change=False, index=0)
    assert path == receive_path(0)
    assert imported.derivation_path == path

    with pytest.raises(WalletError, match="watch-only"):
        ww.export_wif(password="anything")
    # Need a valid destination + UTXO path; watch-only must still refuse to sign.
    dest = pubkey_to_address(private_key_to_public_key(derive_private_key(seed, receive_path(9))))
    with pytest.raises(WalletError, match="watch-only"):
        ww.send(dest, "1", password="x")


def test_create_stores_account_xpub(tmp_path: Path):
    paths = _paths(tmp_path)
    w = Wallet(paths, password="pw")
    w.create(password="pw")
    wf = load_wallet_file(paths.wallet_file)
    assert wf is not None
    root = wf.wallets[0]
    assert root.account_xpub and root.account_xpub.startswith("xpub")
