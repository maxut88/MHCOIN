"""Consensus / maturity guards — block accidental activation and param drift."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from mhcoin.consensus import params as P


ROOT = Path(__file__).resolve().parents[1]


def test_mainnet_reward_and_timing_unchanged():
    assert P.INITIAL_BLOCK_REWARD_COINS == 50
    assert P.TARGET_BLOCK_TIME_SECONDS == 600
    assert P.ASERT_HALF_LIFE_SECONDS == 172_800
    assert P.ASERT_ACTIVATION_HEIGHT == 1


def test_coinbase_maturity_activation_h_star_2000():
    """Activation RC: mainnet H*=2000; other nets remain inactive."""
    fn = getattr(P, "get_coinbase_maturity_activation", None)
    if fn is None:
        pytest.fail("activation API missing on activation RC")
    assert fn("mainnet") == 2000
    assert P.COINBASE_MATURITY == 100
    assert fn("testnet") == 0
    assert fn("regtest") == 0
    assert fn("localnet") == 0


def test_wallet_send_wires_maturity_with_mainnet_activation():
    """Behavioral: wallet send path must consult activation when API is present."""
    from mhcoin.wallet import wallet as wallet_mod

    src = inspect.getsource(wallet_mod.Wallet.send)
    if "get_coinbase_maturity_activation" not in src:
        pytest.fail("Wallet.send must consult maturity activation on this RC")
    assert hasattr(P, "get_coinbase_maturity_activation")
    assert P.get_coinbase_maturity_activation("mainnet") == 2000


def test_ensure_change_contract_no_second_utxoset_open():
    import textwrap

    from mhcoin.wallet import wallet as wallet_mod

    src = inspect.getsource(wallet_mod.Wallet.ensure_change_address)
    assert "utxo" in src
    # Prefer locked helper body (where UTXO work happens)
    if hasattr(wallet_mod.Wallet, "_ensure_change_address_locked"):
        src = inspect.getsource(wallet_mod.Wallet._ensure_change_address_locked)
    # Must not unconditionally construct UTXOSet inside ensure_change
    tree = ast.parse(textwrap.dedent(src))
    opens = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "UTXOSet")
            or (
                isinstance(n.func, ast.Attribute)
                and n.func.attr == "UTXOSet"
            )
        )
    ]
    assert opens == [], "ensure_change_address must not call UTXOSet(...)"


def test_pow_path_mentions_sha256_family():
    """SHA-256d remains the PoW family (search common modules)."""
    hits = 0
    for path in (ROOT / "mhcoin").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "sha256" in text.lower() or "hash256" in text.lower():
            # Prefer consensus/mining related files
            if any(x in str(path) for x in ("pow", "hashing", "mining", "consensus")):
                hits += 1
    assert hits >= 1


def test_halving_schedule_present():
    from mhcoin.constants import HALVING_INTERVAL, INITIAL_BLOCK_SUBSIDY
    from mhcoin.consensus.params import INITIAL_BLOCK_REWARD_COINS
    from mhcoin.consensus.block_reward import get_block_subsidy

    assert INITIAL_BLOCK_REWARD_COINS == 50
    assert get_block_subsidy(0) == INITIAL_BLOCK_SUBSIDY
    assert get_block_subsidy(HALVING_INTERVAL) == INITIAL_BLOCK_SUBSIDY // 2
