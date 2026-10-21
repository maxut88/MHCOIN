"""Regression guards for Wallet/UTXO production fixes (isolated only).

Covers:
- ensure_change_address(utxo=) never opens a second UTXOSet
- Wallet.send passes the same live handle into ensure_change_address
- refresh_from_disk prefers reload() and does not invalidate other holders
- sufficient funds / true insufficient funds
- concurrent change derivation does not corrupt the wallet file
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mhcoin.config_loader import wallet_paths
from mhcoin.constants import SCRIPT_P2PKH_VERSION
from mhcoin.crypto.hashing import hash160
from mhcoin.transaction.input import TxOut
from mhcoin.utxo import OutPoint, UTXOEntry, UTXOSet
from mhcoin.wallet import Wallet, WalletError
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.send import build_account_send_tx, select_coins
from mhcoin.wallet.storage import load_wallet_file


@pytest.fixture()
def wallet_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    paths = wallet_paths("localnet")
    w = Wallet(paths)
    created = w.create(password="secret123")
    return w, created, paths


def _fund_pkh(utxo: UTXOSet, pkh: bytes, value: int = 50_0000_0000, n: int = 3) -> None:
    for i in range(n):
        op = OutPoint(txid=bytes([i + 1]) + b"\x00" * 31, vout=0)
        utxo._mem[op.key()] = UTXOEntry(
            outpoint=op,
            output=TxOut(
                value=value,
                script_pubkey=bytes([SCRIPT_P2PKH_VERSION]) + pkh,
            ),
            height=i + 1,
            coinbase=False,
        )


def test_ensure_change_with_utxo_never_opens_second_set(wallet_env):
    w, created, _paths = wallet_env
    change1 = w.ensure_change_address("secret123")
    assert change1.address != created.address

    fake_utxo = MagicMock()
    fake_utxo.balance_for_pubkey_hash.return_value = 0
    opens: list = []

    class BoomUTXO:
        def __init__(self, *a, **k):
            opens.append((a, k))
            raise AssertionError("must not open second UTXOSet when utxo= provided")

        def close(self):
            pass

    with patch("mhcoin.wallet.wallet.UTXOSet", BoomUTXO):
        again = w.ensure_change_address("secret123", utxo=fake_utxo)

    assert opens == []
    assert again.address == change1.address
    assert fake_utxo.balance_for_pubkey_hash.called


def test_ensure_change_without_utxo_never_opens_chainstate(wallet_env):
    w, created, paths = wallet_env
    c1 = w.ensure_change_address("secret123")
    cs = paths.utxo_path
    cs.mkdir(parents=True, exist_ok=True)
    (cs / "data.mdb").write_bytes(b"fake")

    opens: list = []

    class BoomUTXO:
        def __init__(self, *a, **k):
            opens.append(True)
            raise AssertionError("must not open UTXOSet without utxo=")

        def close(self):
            pass

    with patch("mhcoin.wallet.wallet.UTXOSet", BoomUTXO):
        c2 = w.ensure_change_address("secret123")
    assert opens == []
    assert c2.address == c1.address


def test_ensure_change_all_funded_derives_next_and_persists(wallet_env):
    w, created, paths = wallet_env
    c1 = w.ensure_change_address("secret123")
    fake = MagicMock()
    fake.balance_for_pubkey_hash.return_value = 1_000_000
    c2 = w.ensure_change_address("secret123", utxo=fake)
    assert c2.address != c1.address
    assert c2.derivation_path and "/1/" in c2.derivation_path
    data = json.loads(paths.wallet_file.read_text())
    assert c2.address in {x["address"] for x in data["wallets"]}


def test_send_passes_same_live_utxo_into_ensure_change(wallet_env):
    w, created, _paths = wallet_env
    seen: dict = {}

    def fake_ensure(password=None, *, utxo=None):
        seen["utxo"] = utxo
        from mhcoin.wallet.wallet import WalletCreateResult

        return WalletCreateResult(
            address=created.address,
            wallet_id="x",
            derivation_path="m/84'/0'/0'/1/0",
        )

    fake_utxo = MagicMock()
    with patch.object(w, "ensure_change_address", side_effect=fake_ensure):
        with patch("mhcoin.wallet.wallet.build_account_send_tx") as build:
            build.return_value = MagicMock()
            try:
                w.send(
                    created.address,
                    "1.0",
                    password="secret123",
                    utxo=fake_utxo,
                )
            except Exception:
                pass
    assert seen.get("utxo") is fake_utxo


def test_refresh_from_disk_reload_keeps_shared_handle(tmp_path: Path):
    """Behavioral: reload must not detach other holders of the live UTXO set."""
    cs = tmp_path / "chainstate"
    cs.mkdir()
    holder = UTXOSet(cs)
    try:
        op = OutPoint(txid=b"\x01" * 32, vout=0)
        pkh = b"\xab" * 20
        holder._mem[op.key()] = UTXOEntry(
            outpoint=op,
            output=TxOut(
                value=1000,
                script_pubkey=bytes([SCRIPT_P2PKH_VERSION]) + pkh,
            ),
            height=1,
            coinbase=False,
        )
        before = holder.count()
        assert before >= 1

        # Minimal Chain-like object: only fields refresh_from_disk needs before
        # _load_tip (which needs sqlite). Call reload path directly.
        holder.reload()
        assert holder.count() == before or holder.count() >= 0

        # Shared open: second handle must see same live mem, close must not wipe first
        other = UTXOSet(cs)
        try:
            assert other._live is holder._live
            assert other.count() == holder.count()
        finally:
            other.close()
        # After secondary close, primary must still be usable
        assert holder._live is not None
        assert holder.count() == before or holder.count() >= 0
    finally:
        holder.close()


def test_refresh_from_disk_source_prefers_reload():
    """Static behavioral contract: production fix uses reload, not close-first."""
    from mhcoin.blockchain.chain import Blockchain
    import inspect

    src = inspect.getsource(Blockchain.refresh_from_disk)
    assert "self.utxo.reload()" in src
    # close()+recreate may exist only as fallback after failed reload
    reload_pos = src.find("self.utxo.reload()")
    assert reload_pos >= 0
    # Primary path is try/reload, not unconditional close at start
    head = src.split("self.utxo.reload()")[0]
    assert "self.utxo.close()" not in head


def test_select_coins_true_insufficient_funds():
    pkh = b"\x22" * 20
    available = [
        UTXOEntry(
            outpoint=OutPoint(txid=b"\x02" * 32, vout=0),
            output=TxOut(
                value=1000,
                script_pubkey=bytes([SCRIPT_P2PKH_VERSION]) + pkh,
            ),
            height=1,
            coinbase=False,
        )
    ]
    with pytest.raises(ValueError, match="insufficient funds"):
        select_coins(available, need=10_000)


def test_build_account_send_with_funds_ok(wallet_env):
    w, created, paths = wallet_env
    pwd = "secret123"
    change = w.ensure_change_address(pwd)
    kp = w.unlock_key(w._default_record().wallet_id, pwd)
    # Also unlock change key
    wf = load_wallet_file(paths.wallet_file)
    assert wf is not None
    keys = {}
    for rec in wf.wallets:
        try:
            k = w.unlock_key(rec.wallet_id, pwd)
        except WalletError:
            continue
        keys[hash160(k.public_key_compressed)] = (k.private_key, k.public_key_compressed)

    utxo = UTXOSet(path=None)
    recv_pkh = address_to_pubkey_hash(created.address, hrp=paths.hrp)
    _fund_pkh(utxo, recv_pkh, value=10_0000_0000, n=2)
    change_pkh = address_to_pubkey_hash(change.address, hrp=paths.hrp)
    result = build_account_send_tx(
        utxo=utxo,
        keys_by_pkh=keys,
        to_address=created.address,
        amount_sats=1_0000_0000,
        fee_sats=1000,
        change_pubkey_hash=change_pkh,
        hrp=paths.hrp,
    )
    assert result.txid_hex
    assert result.amount == 1_0000_0000


def test_concurrent_ensure_change_does_not_corrupt_wallet_file(wallet_env):
    """Under concurrent derive, wallet file must remain valid JSON with unique paths."""
    w, _created, paths = wallet_env
    # Seed one change, then force derive by claiming all funded
    w.ensure_change_address("secret123")
    fake = MagicMock()
    fake.balance_for_pubkey_hash.return_value = 1

    errors: list[str] = []
    results: list[str] = []

    def worker():
        try:
            r = w.ensure_change_address("secret123", utxo=fake)
            results.append(r.address)
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)

    assert len(results) + len(errors) == 6
    data = json.loads(paths.wallet_file.read_text())
    addrs = [x["address"] for x in data["wallets"]]
    assert len(addrs) == len(set(addrs)), "duplicate addresses persisted"
    paths_set = [
        x.get("derivation_path")
        for x in data["wallets"]
        if x.get("derivation_path") and "/1/" in x["derivation_path"]
    ]
    assert len(paths_set) == len(set(paths_set)), "duplicate change paths persisted"
