"""Wallet/Mining UX: create → mine → send → balance (Vasya → Petro)."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from mhcoin.cli.main import cli


def test_ux_vasya_sends_10_mhc_to_petro(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_WALLET_PASSWORD", "ux-secret-pass")
    runner = CliRunner()

    r = runner.invoke(cli, ["wallet", "create", "--label", "vasya"])
    assert r.exit_code == 0, r.output
    assert "Wallet created" in r.output
    assert "Address: mhc1" in r.output
    assert "RECOVERY SEED" in r.output

    r = runner.invoke(cli, ["wallet", "address"])
    assert r.exit_code == 0
    vasya = r.output.strip()
    assert vasya.startswith("mhc1")

    r = runner.invoke(cli, ["wallet", "create", "--label", "petro"])
    assert r.exit_code == 0
    petro = [ln for ln in r.output.splitlines() if ln.startswith("Address: ")][0].split(" ", 1)[1]
    assert petro.startswith("mhc1")
    assert petro != vasya

    r = runner.invoke(cli, ["mining", "start", "--address", vasya, "--blocks", "2"])
    assert r.exit_code == 0, r.output
    assert "50.00000000 MHC" in r.output

    r = runner.invoke(cli, ["wallet", "balance"])
    assert r.exit_code == 0, r.output
    assert "Balance: 100.00000000 MHC" in r.output  # two block rewards

    r = runner.invoke(cli, ["wallet", "send", petro, "10"])
    assert r.exit_code == 0, r.output
    assert "Transaction sent to local mempool." in r.output
    assert "Amount: 10.00000000 MHC" in r.output

    # Confirm in a block (miner needs no password)
    r = runner.invoke(cli, ["mining", "start", "--address", vasya, "--blocks", "1"])
    assert r.exit_code == 0, r.output

    r = runner.invoke(cli, ["wallet", "balance", "--address", petro])
    assert r.exit_code == 0, r.output
    assert "Balance: 10.00000000 MHC" in r.output

    r = runner.invoke(cli, ["wallet", "balance", "--label", "vasya"])
    assert r.exit_code == 0, r.output
    # 100 - 10 - fee(0.00001) + 50 coinbase ≈ 139.99999
    assert "Balance:" in r.output
    assert "MHC" in r.output


def test_address_and_balance_need_no_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_WALLET_PASSWORD", "secret")
    runner = CliRunner()
    assert runner.invoke(cli, ["wallet", "create"]).exit_code == 0
    monkeypatch.delenv("MHCOIN_WALLET_PASSWORD", raising=False)
    # No password in env — address/balance must still work
    r = runner.invoke(cli, ["wallet", "address"])
    assert r.exit_code == 0
    assert r.output.strip().startswith("mhc1")
    r = runner.invoke(cli, ["wallet", "balance"])
    assert r.exit_code == 0
    assert "Balance: 0.00000000 MHC" in r.output
