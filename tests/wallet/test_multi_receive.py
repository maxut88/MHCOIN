"""HD multi-address: BIP84 external chain m/84'/0'/0'/0/n."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.wallet.hd import DEFAULT_DERIVATION_PATH, receive_path
from mhcoin.wallet.storage import load_wallet_file
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths


def _paths(tmp: Path) -> WalletPaths:
    d = tmp / "data"
    d.mkdir(parents=True, exist_ok=True)
    return WalletPaths(data_dir=d, network="regtest", hrp="mhc")


def test_receive_path_indices():
    assert receive_path(0) == DEFAULT_DERIVATION_PATH
    assert receive_path(1) == "m/84'/0'/0'/0/1"
    assert receive_path(7) == "m/84'/0'/0'/0/7"
    with pytest.raises(ValueError):
        receive_path(-1)


def test_new_receive_address_derives_next_index(tmp_path: Path):
    w = Wallet(_paths(tmp_path), password="pw-multi")
    root = w.create(password="pw-multi", label="root")
    assert root.derivation_path == "m/84'/0'/0'/0/0"
    assert root.address.startswith("mhc1")

    rows = w.list_receive_addresses()
    assert len(rows) == 1
    assert rows[0]["index"] == 0
    assert rows[0]["is_default"] is True
    assert rows[0]["has_seed"] is True

    a1 = w.new_receive_address(password="pw-multi")
    assert a1.derivation_path == "m/84'/0'/0'/0/1"
    assert a1.address != root.address
    assert a1.mnemonic is None

    a2 = w.new_receive_address(password="pw-multi")
    assert a2.derivation_path == "m/84'/0'/0'/0/2"
    assert a2.address not in {root.address, a1.address}

    rows = w.list_receive_addresses()
    assert [r["index"] for r in rows] == [0, 1, 2]
    assert sum(1 for r in rows if r["is_default"]) == 1
    assert rows[-1]["address"] == a2.address
    assert rows[-1]["is_default"] is True

    wf = load_wallet_file(_paths(tmp_path).wallet_file)
    assert wf is not None
    acc = {w.account_id for w in wf.wallets}
    assert len(acc) == 1
    assert None not in acc
    # Seed stays only on the account root.
    assert sum(1 for x in wf.wallets if x.encrypted_mnemonic) == 1
    assert w.export_mnemonic(password="pw-multi") == root.mnemonic


def test_restore_then_derive_same_addresses(tmp_path: Path):
    w = Wallet(_paths(tmp_path), password="pw-a")
    created = w.create(password="pw-a")
    second = w.new_receive_address(password="pw-a")

    w2 = Wallet(_paths(tmp_path / "other"), password="pw-b")
    restored = w2.restore_from_mnemonic(created.mnemonic, password="pw-b")
    assert restored.address == created.address
    again = w2.new_receive_address(password="pw-b")
    assert again.address == second.address
    assert again.derivation_path == "m/84'/0'/0'/0/1"


def test_imported_key_cannot_derive(tmp_path: Path):
    from mhcoin.wallet.bip39 import generate_mnemonic, mnemonic_to_seed
    from mhcoin.wallet.hd import derive_private_key

    words = generate_mnemonic(strength=128)
    priv = derive_private_key(mnemonic_to_seed(words))
    w = Wallet(_paths(tmp_path), password="pw")
    w.import_private_key(priv.hex(), password="pw")
    with pytest.raises(WalletError, match="no BIP39 seed"):
        w.new_receive_address(password="pw")
    rows = w.list_receive_addresses()
    assert len(rows) == 1
    assert rows[0]["path"] is None
    assert rows[0]["has_seed"] is False


def test_legacy_wallet_without_account_id_backfills(tmp_path: Path):
    """Pre-multi-address HD roots (account_id=None) still derive …/0/1."""
    w = Wallet(_paths(tmp_path), password="pw-leg")
    created = w.create(password="pw-leg")
    wf = load_wallet_file(_paths(tmp_path).wallet_file)
    assert wf is not None
    wf.wallets[0].account_id = None
    from mhcoin.wallet.storage import save_wallet_file

    save_wallet_file(_paths(tmp_path).wallet_file, wf)

    nxt = w.new_receive_address(password="pw-leg")
    assert nxt.derivation_path == "m/84'/0'/0'/0/1"
    assert nxt.address != created.address
    wf2 = load_wallet_file(_paths(tmp_path).wallet_file)
    assert wf2 is not None
    root = next(x for x in wf2.wallets if x.encrypted_mnemonic)
    assert root.account_id == root.wallet_id
    child = next(x for x in wf2.wallets if x.wallet_id == nxt.wallet_id)
    assert child.account_id == root.wallet_id
