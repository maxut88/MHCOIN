import json
from pathlib import Path

import pytest

from mhcoin.config_loader import wallet_paths
from mhcoin.wallet import Wallet, WalletError
from mhcoin.wallet.storage import decrypt_private_key, encrypt_private_key


def test_encrypt_decrypt_private_key():
    key = b"\x01" * 32
    enc, _ = encrypt_private_key(key, "test-pass", n=2**14, r=8, p=1)
    out = decrypt_private_key(enc, "test-pass", n=2**14, r=8, p=1)
    assert out == key


def test_wallet_create_and_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    paths = wallet_paths("localnet")
    w = Wallet(paths)
    addr = w.create(password="secret123").address
    assert addr.startswith("mhc1")
    assert w.default_address() == addr
    wf = paths.wallet_file
    assert wf.is_file()
    data = json.loads(wf.read_text())
    assert "wallets" in data
    assert data["wallets"][0]["address"] == addr
    # private key must not appear in plaintext
    assert "010101" not in wf.read_text()


def test_wallet_address_without_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    w = Wallet(wallet_paths("localnet"))
    with pytest.raises(WalletError):
        w.default_address()


def test_ensure_change_fallback_without_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Imported / seedless keys must still Send — change returns to self."""
    from mhcoin.wallet.storage import load_wallet_file, save_wallet_file

    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    paths = wallet_paths("localnet")
    w = Wallet(paths)
    created = w.create(password="secret123")
    wf = load_wallet_file(paths.wallet_file)
    assert wf is not None
    wf.wallets[0].encrypted_mnemonic = None
    wf.wallets[0].derivation_path = None
    wf.wallets[0].account_xpub = None
    save_wallet_file(paths.wallet_file, wf)
    w2 = Wallet(paths, password="secret123")
    change = w2.ensure_change_address("secret123")
    assert change.address == created.address


def test_ensure_change_derives_from_hd_seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    paths = wallet_paths("localnet")
    w = Wallet(paths)
    created = w.create(password="secret123")
    assert created.mnemonic
    change = w.ensure_change_address("secret123")
    assert change.address != created.address
    assert change.derivation_path and "/1/" in change.derivation_path
