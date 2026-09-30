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
    )
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
