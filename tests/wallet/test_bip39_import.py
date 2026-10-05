"""BIP39 restore + WIF/hex import (Bitcoin-style recovery)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.wallet.bip39 import generate_mnemonic, mnemonic_to_seed, validate_mnemonic
from mhcoin.wallet.hd import DEFAULT_DERIVATION_PATH, derive_private_key
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths
from mhcoin.wallet.wif import encode_wif, parse_private_key


def _paths(tmp: Path) -> WalletPaths:
    d = tmp / "data"
    d.mkdir(parents=True, exist_ok=True)
    return WalletPaths(data_dir=d, network="regtest", hrp="mhc")


def test_create_returns_mnemonic_and_restores_same_address(tmp_path: Path):
    w = Wallet(_paths(tmp_path), password="pw-create-1")
    created = w.create(password="pw-create-1", label="a")
    assert created.mnemonic and validate_mnemonic(created.mnemonic)
    assert created.derivation_path == DEFAULT_DERIVATION_PATH
    assert created.address.startswith("mhc1")

    w2 = Wallet(_paths(tmp_path / "other"), password="pw-restore-1")
    restored = w2.restore_from_mnemonic(
        created.mnemonic, password="pw-restore-1", label="b"
    )
    assert restored.address == created.address
    assert restored.derivation_path == DEFAULT_DERIVATION_PATH


def test_export_seed_roundtrip(tmp_path: Path):
    w = Wallet(_paths(tmp_path), password="pw-seed")
    created = w.create(password="pw-seed")
    assert w.export_mnemonic(password="pw-seed") == created.mnemonic


def test_import_hex_and_wif(tmp_path: Path):
    words = generate_mnemonic(strength=128)
    priv = derive_private_key(mnemonic_to_seed(words))
    hex_key = priv.hex()
    wif = encode_wif(priv, compressed=True)
    assert parse_private_key(wif) == priv
    assert parse_private_key(hex_key) == priv

    w = Wallet(_paths(tmp_path), password="pw-imp")
    a = w.import_private_key(hex_key, password="pw-imp", label="hex")
    with pytest.raises(WalletError, match="already"):
        w.import_private_key(wif, password="pw-imp", label="wif")

    w2 = Wallet(_paths(tmp_path / "b"), password="pw-imp2")
    b = w2.import_private_key(wif, password="pw-imp2", label="wif")
    assert a.address == b.address
    assert w2.export_wif(password="pw-imp2") == wif


def test_invalid_mnemonic_rejected(tmp_path: Path):
    w = Wallet(_paths(tmp_path), password="pw")
    with pytest.raises(WalletError, match="mnemonic"):
        w.restore_from_mnemonic(
            "notarealword zebra tiger lion cat dog bird fish mouse horse cow pig",
            password="pw",
        )


def test_imported_key_has_no_mnemonic_export(tmp_path: Path):
    words = generate_mnemonic(strength=128)
    priv = derive_private_key(mnemonic_to_seed(words))
    w = Wallet(_paths(tmp_path), password="pw")
    w.import_private_key(priv.hex(), password="pw")
    with pytest.raises(WalletError, match="no stored mnemonic"):
        w.export_mnemonic(password="pw")
