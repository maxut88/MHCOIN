"""Desktop Import / Show WIF (controller bridge)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.crypto.keys import generate_keypair
from mhcoin.desktop.controller import CoreController
from mhcoin.wallet.wif import encode_wif
from mhcoin.wallet.wallet import WalletError


def test_import_and_export_wif_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    monkeypatch.setenv("MHCOIN_DATA_DIR", str(tmp_path / "data"))
    ctrl = CoreController(network="regtest", data_dir=tmp_path / "data")
    kp = generate_keypair()
    wif = encode_wif(kp.private_key, compressed=True)
    out = ctrl.import_wif(wif, "test-pass", label="from-wif")
    assert out["address"].startswith("mhc1") or out["address"].startswith("mht1")
    shown = ctrl.export_wif("test-pass")
    assert shown["wif"] == wif
    assert shown["address"] == out["address"]


def test_export_wif_rejects_wrong_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    monkeypatch.setenv("MHCOIN_DATA_DIR", str(tmp_path / "data"))
    ctrl = CoreController(network="regtest", data_dir=tmp_path / "data")
    kp = generate_keypair()
    ctrl.import_wif(encode_wif(kp.private_key), "good-pass")
    with pytest.raises(WalletError):
        ctrl.export_wif("bad-pass")
