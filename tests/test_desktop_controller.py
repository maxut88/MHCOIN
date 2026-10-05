"""Controller unit tests (no GUI display required)."""

from __future__ import annotations

from pathlib import Path

from mhcoin.desktop.controller import CoreController
from mhcoin.mining.miner import SoloMiner
from mhcoin.wallet.send import format_mhc
from mhcoin.wallet.wallet import Wallet


def test_desktop_controller_wallet_mine_send(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    assert ctrl.wallet_exists() is False
    addr = ctrl.create_wallet("desk-pass-123")
    assert addr.startswith("mhc1")
    assert ctrl.wallet_exists() is True
    assert ctrl.balance_sats() == 0
    # Release datadir so SoloMiner can open the same chain files.
    ctrl.shutdown()

    m = SoloMiner(data_dir=tmp_path, network="localnet", hrp="mhc", address=addr)
    r = m.run(max_blocks=1)
    assert r[0].reward_sats == 5_000_000_000

    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    assert ctrl.balance_sats() == 5_000_000_000
    assert "50.00000000" in ctrl.balance_text()

    petro = Wallet(ctrl.paths, password="desk-pass-123").create(
        label="petro", password="desk-pass-123", make_default=False
    ).address
    txid = ctrl.send(petro, "10", "desk-pass-123")
    assert len(txid) == 64
    ctrl.shutdown()

    SoloMiner(data_dir=tmp_path, network="localnet", hrp="mhc", address=addr).run(max_blocks=1)

    petro_bal, _ = Wallet(ctrl.paths).balance(petro)
    assert petro_bal == 1_000_000_000
    assert format_mhc(petro_bal) == "10.00000000"

    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    info = ctrl.chain_info()
    assert info["height"] >= 2
    rows = ctrl.recent_transactions()
    assert any(r.kind == "Mining reward" for r in rows)
    ctrl.shutdown()


def test_desktop_create_and_restore_seed(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path / "a")
    created = ctrl.create_hd_wallet("seed-pass-1")
    assert created["address"].startswith("mhc1")
    assert len(created["mnemonic"].split()) in (12, 24)
    seed = created["mnemonic"]
    addr = created["address"]
    ctrl.shutdown()

    ctrl2 = CoreController(network="localnet", data_dir=tmp_path / "b")
    restored = ctrl2.restore_wallet("seed-pass-2", seed)
    assert restored == addr
    assert ctrl2.export_seed("seed-pass-2") == seed
    ctrl2.shutdown()


def test_desktop_multi_receive_keeps_unlock(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    ctrl = CoreController(network="localnet", data_dir=tmp_path)
    created = ctrl.create_hd_wallet("multi-pass")
    root = created["address"]
    ctrl.unlock("multi-pass")
    nxt = ctrl.new_receive_address()
    assert nxt["path"] == "m/84'/0'/0'/0/1"
    assert nxt["address"] != root
    assert ctrl.default_address() == nxt["address"]
    rows = ctrl.list_receive_addresses()
    assert len(rows) == 2
    # Switch back to root without clearing session unlock.
    back = ctrl.select_wallet_by_address(root)
    assert back == root
    assert ctrl._password == "multi-pass"
    assert ctrl.default_address() == root
    ctrl.shutdown()
