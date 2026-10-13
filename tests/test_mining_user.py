"""User-path mining: wallet → address → mining start → balance."""

from __future__ import annotations

from pathlib import Path

from mhcoin.mining.miner import SoloMiner
from mhcoin.wallet.wallet import Wallet, WalletPaths
from mhcoin.wallet.send import format_mhc, parse_amount_mhc


def test_solo_mining_start_pays_address(tmp_path: Path):
    paths = WalletPaths(data_dir=tmp_path, network="localnet", hrp="mhc")
    w = Wallet(paths, password="test-pass-123")
    addr = w.create(password="test-pass-123").address
    assert addr.startswith("mhc1")

    miner = SoloMiner(
        data_dir=tmp_path,
        network="localnet",
        hrp="mhc",
        address=addr,
    )
    results = miner.run(max_blocks=1)
    assert len(results) == 1
    assert results[0].height == 1  # height 0 = frozen genesis (burn); reward at 1
    assert results[0].reward_sats == 5_000_000_000  # 50 MHC
    assert results[0].address == addr

    confirmed, _ = w.balance(addr)
    assert confirmed == 5_000_000_000
    assert format_mhc(confirmed) == "50.00000000"


def test_mining_rejects_bad_address(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError):
        SoloMiner(
            data_dir=tmp_path,
            network="localnet",
            hrp="mhc",
            address="not-an-address",
        )
