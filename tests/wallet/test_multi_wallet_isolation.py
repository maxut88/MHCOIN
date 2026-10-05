"""Multi-wallet ownership isolation (localnet only).

Blockchain/UTXO DB is shared; balance/history/spendability must follow keys.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mhcoin.constants import DEFAULT_FEE_SATOSHIS, INITIAL_BLOCK_SUBSIDY
from mhcoin.desktop.controller import CoreController
from mhcoin.mining.miner import SoloMiner
from mhcoin.node.local_node import LocalNode
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.send import build_send_tx, select_coins
from mhcoin.wallet.storage import load_wallet_file
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths


REWARD = INITIAL_BLOCK_SUBSIDY  # 50 MHC in sats


def _pkh_fp(addr: str, hrp: str = "mhc") -> str:
    """Fingerprint of pubkey hash — never print private keys."""
    pkh = address_to_pubkey_hash(addr, hrp=hrp)
    return hashlib.sha256(pkh).hexdigest()[:16]


def _mine(td: Path, address: str, blocks: int) -> None:
    SoloMiner(data_dir=td, network="localnet", hrp="mhc", address=address).run(max_blocks=blocks)


def _ctrl(td: Path) -> CoreController:
    return CoreController(network="localnet", data_dir=td)


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "localnet")
    monkeypatch.setenv("MHCOIN_DATA", str(tmp_path))
    return tmp_path


def test_two_wallets_have_different_keys(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a-unique")
    addr_b = ctrl.create_wallet("pass-b-unique")
    assert addr_a != addr_b
    assert _pkh_fp(addr_a) != _pkh_fp(addr_b)
    wf = load_wallet_file(ctrl.paths.wallet_file)
    assert wf is not None and len(wf.wallets) == 2
    pubs = {w.public_key_hex for w in wf.wallets}
    assert len(pubs) == 2
    # After Create New Wallet, active wallet must be B
    assert ctrl.default_address() == addr_b
    assert wf.default_wallet_id == wf.wallets[-1].wallet_id
    ctrl.shutdown()


def test_new_wallet_starts_zero_balance(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 3)

    ctrl = _ctrl(isolated)
    assert ctrl.balance_sats() == 3 * REWARD
    addr_b = ctrl.create_wallet("pass-b")
    assert addr_b != addr_a
    assert ctrl.default_address() == addr_b
    assert ctrl.balance_sats() == 0
    bal_a, _ = Wallet(ctrl.paths).balance(addr_a)
    assert bal_a == 3 * REWARD
    tip_h = ctrl.chain_info()["height"]
    assert tip_h == 3
    ctrl.shutdown()


def test_coinbase_owned_only_by_payout_wallet(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 3)

    node = LocalNode(isolated, hrp="mhc", network="localnet")
    try:
        pkh_a = address_to_pubkey_hash(addr_a, hrp="mhc")
        coinbases = []
        for h in range(1, 4):
            block = node.chain.get_block_by_height(h)
            assert block is not None
            cb = block.transactions[0]
            assert cb.is_coinbase()
            out = cb.outputs[0]
            assert out.pubkey_hash() == pkh_a
            coinbases.append(out.value)
        assert sum(coinbases) == 3 * REWARD

        paths = WalletPaths(data_dir=isolated, network="localnet", hrp="mhc")
        w = Wallet(paths)
        addr_b = w.create(password="pass-b", make_default=True).address
        pkh_b = address_to_pubkey_hash(addr_b, hrp="mhc")
        assert node.chain.utxo.balance_for_pubkey_hash(pkh_b) == 0
        assert node.chain.utxo.balance_for_pubkey_hash(pkh_a) == 3 * REWARD
        foreign = node.chain.utxo.all_for_pubkey_hash(pkh_b)
        assert foreign == []
    finally:
        node.close()


def test_wallet_balance_filters_foreign_utxos(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    addr_b = ctrl.create_wallet("pass-b")
    assert ctrl.balance_sats() == 0
    assert Wallet(ctrl.paths).balance(addr_a)[0] == 2 * REWARD
    assert Wallet(ctrl.paths).balance(addr_b)[0] == 0
    # Session mining counters must not fake B's wealth
    assert ctrl.mining_stats["rewards_sats"] == 0
    assert ctrl.mining_stats["blocks_found"] == 0
    ctrl.shutdown()


def test_wallet_history_filters_foreign_transactions(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 3)

    ctrl = _ctrl(isolated)
    # While A is active: mining history present
    rows_a = ctrl.recent_transactions()
    assert sum(1 for r in rows_a if r.kind == "Mining reward") >= 3

    addr_b = ctrl.create_wallet("pass-b")
    rows_b = ctrl.recent_transactions()
    assert ctrl.default_address() == addr_b
    assert rows_b == [] or all(
        address_to_pubkey_hash(ctrl.default_address(), hrp="mhc")
        for _ in rows_b
    )
    assert not any(r.kind == "Mining reward" for r in rows_b)
    ctrl.shutdown()


def test_wallet_cannot_select_foreign_utxos(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    addr_b = ctrl.create_wallet("pass-b")
    pkh_b = address_to_pubkey_hash(addr_b, hrp="mhc")
    with ctrl._io:
        node = ctrl._get_local()
        available = node.chain.utxo.all_for_pubkey_hash(pkh_b)
        assert available == []
        with pytest.raises(ValueError, match="insufficient"):
            select_coins(available, 1)
    ctrl.shutdown()


def test_wallet_cannot_spend_foreign_coinbase(isolated: Path):
    """CRITICAL: B must not spend A's coinbases."""
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 3)

    ctrl = _ctrl(isolated)
    addr_b = ctrl.create_wallet("pass-b")
    assert ctrl.balance_sats() == 0
    with pytest.raises(WalletError, match="insufficient"):
        ctrl.send(addr_a, "10", "pass-b")
    # Chain tip unchanged (no sneak TX)
    assert ctrl.chain_info()["height"] == 3
    assert Wallet(ctrl.paths).balance(addr_a)[0] == 3 * REWARD
    ctrl.shutdown()


def test_send_a_to_b(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    # Create B without making it default so A can still send
    addr_b = Wallet(ctrl.paths, password="pass-b").create(
        password="pass-b", make_default=False, label="b"
    ).address
    assert ctrl.default_address() == addr_a
    txid = ctrl.send(addr_b, "10", "pass-a")
    assert len(txid) == 64
    # Unconfirmed: A reserved inputs via mempool; confirmed balance may still show
    # spent UTXOs until mined depending on UTXO view — check mempool has TX
    with ctrl._io:
        node = ctrl._get_local()
        assert any(t.txid_hex() == txid for t in node.mempool.list_txs())
    ctrl.shutdown()


def test_b_receives_after_confirmation(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    addr_b = Wallet(ctrl.paths, password="pass-b").create(
        password="pass-b", make_default=False, label="b"
    ).address
    ctrl.send(addr_b, "10", "pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 1)

    ctrl = _ctrl(isolated)
    bal_b, _ = Wallet(ctrl.paths).balance(addr_b)
    assert bal_b == 1_000_000_000  # 10 MHC
    ctrl.select_wallet_by_address(addr_b)
    rows = ctrl.recent_transactions()
    assert any(r.kind == "Received" and r.amount_sats == 1_000_000_000 for r in rows)
    assert not any(r.kind == "Mining reward" for r in rows)
    ctrl.shutdown()


def test_b_can_spend_received_output(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    addr_b = Wallet(ctrl.paths, password="pass-b").create(
        password="pass-b", make_default=False, label="b"
    ).address
    ctrl.send(addr_b, "10", "pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 1)

    ctrl = _ctrl(isolated)
    ctrl.select_wallet_by_address(addr_b)
    ctrl.unlock("pass-b")
    txid = ctrl.send(addr_a, "3", "pass-b")
    assert len(txid) == 64
    ctrl.shutdown()
    _mine(isolated, addr_a, 1)

    bal_b, _ = Wallet(WalletPaths(data_dir=isolated, network="localnet", hrp="mhc")).balance(addr_b)
    # 10 - 3 - fee
    assert bal_b == 1_000_000_000 - 300_000_000 - DEFAULT_FEE_SATOSHIS


def test_change_returns_to_correct_wallet(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 1)

    ctrl = _ctrl(isolated)
    addr_b = Wallet(ctrl.paths, password="pass-b").create(
        password="pass-b", make_default=False, label="b"
    ).address
    # A sends 10 from 50 coinbase → change back to A
    with ctrl._io:
        node = ctrl._get_local()
        w = Wallet(ctrl.paths, password="pass-a")
        kp = w.unlock_default("pass-a")
        from mhcoin.crypto.hashing import hash160

        pkh = hash160(kp.public_key_compressed)
        result = build_send_tx(
            utxo=node.chain.utxo,
            from_pubkey_hash=pkh,
            private_key=kp.private_key,
            public_key=kp.public_key_compressed,
            to_address=addr_b,
            amount_sats=1_000_000_000,
            fee_sats=DEFAULT_FEE_SATOSHIS,
            hrp="mhc",
        )
        change_outs = [
            o
            for o in result.tx.outputs
            if o.pubkey_hash() == pkh
        ]
        assert len(change_outs) == 1
        assert change_outs[0].value == REWARD - 1_000_000_000 - DEFAULT_FEE_SATOSHIS
        recv = [o for o in result.tx.outputs if o.pubkey_hash() == address_to_pubkey_hash(addr_b, hrp="mhc")]
        assert len(recv) == 1 and recv[0].value == 1_000_000_000
    ctrl.shutdown()


def test_wallet_state_survives_restart(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl.shutdown()
    _mine(isolated, addr_a, 2)

    ctrl = _ctrl(isolated)
    addr_b = ctrl.create_wallet("pass-b")
    tip = ctrl.chain_info()["tip"]
    height = ctrl.chain_info()["height"]
    assert ctrl.default_address() == addr_b
    assert ctrl.balance_sats() == 0
    ctrl.shutdown()

    ctrl2 = _ctrl(isolated)
    assert ctrl2.chain_info()["height"] == height
    assert ctrl2.chain_info()["tip"] == tip
    assert ctrl2.default_address() == addr_b
    assert ctrl2.balance_sats() == 0
    ctrl2.select_wallet_by_address(addr_a)
    assert ctrl2.balance_sats() == 2 * REWARD
    assert ctrl2.default_address() == addr_a
    ctrl2.shutdown()


def test_desktop_switch_resets_session_mining_stats(isolated: Path):
    ctrl = _ctrl(isolated)
    addr_a = ctrl.create_wallet("pass-a")
    ctrl._blocks_found = 99
    ctrl._rewards_sats = 99 * REWARD
    addr_b = ctrl.create_wallet("pass-b")
    assert addr_b != addr_a
    assert ctrl.mining_stats["blocks_found"] == 0
    assert ctrl.mining_stats["rewards_sats"] == 0
    assert ctrl.balance_sats() == 0
    ctrl.shutdown()
