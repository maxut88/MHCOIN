#!/usr/bin/env python3
"""Fail CI if Wallet/UTXO production fixes regress or maturity activates silently."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    from mhcoin.wallet.wallet import Wallet
    from mhcoin.blockchain.chain import Blockchain
    from mhcoin.consensus import params as P

    # 1) ensure_change must accept utxo= and must not construct UTXOSet
    src = inspect.getsource(Wallet.ensure_change_address)
    if "utxo" not in src:
        fail("ensure_change_address missing utxo parameter")
    # Prefer locked helper if present
    body = src
    if hasattr(Wallet, "_ensure_change_address_locked"):
        body = inspect.getsource(Wallet._ensure_change_address_locked)
    tree = ast.parse(textwrap.dedent(body))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id == "UTXOSet":
                fail("ensure_change opens UTXOSet() — second-handle regression")
            if isinstance(n.func, ast.Attribute) and n.func.attr == "UTXOSet":
                fail("ensure_change opens UTXOSet() — second-handle regression")

    # 2) send must pass live utxo into ensure_change
    send_src = inspect.getsource(Wallet.send)
    if "ensure_change_address(" not in send_src or "utxo=" not in send_src:
        fail("Wallet.send must call ensure_change_address(..., utxo=...)")
    if send_src.find("own_utxo") > send_src.find("ensure_change_address") and \
       "utxo=own_utxo" not in send_src:
        # allow either order if utxo=own_utxo present after acquire
        pass
    if "utxo=own_utxo" not in send_src:
        fail("Wallet.send must pass utxo=own_utxo into ensure_change_address")

    # 3) refresh_from_disk must prefer reload()
    ref = inspect.getsource(Blockchain.refresh_from_disk)
    if "self.utxo.reload()" not in ref:
        fail("Chain.refresh_from_disk missing self.utxo.reload() — stale-handle regression")
    head = ref.split("self.utxo.reload()")[0]
    if "self.utxo.close()" in head:
        fail("refresh_from_disk closes UTXO before reload — regression")

    # 4) maturity must stay inactive on mainnet if API exists
    fn = getattr(P, "get_coinbase_maturity_activation", None)
    if fn is not None and int(fn("mainnet")) != 0:
        fail(f"mainnet coinbase maturity activation is {fn('mainnet')} — refuse merge")

    # 5) core consensus constants
    if int(P.INITIAL_BLOCK_REWARD_COINS) != 50:
        fail("INITIAL_BLOCK_REWARD_COINS drift")
    if int(P.TARGET_BLOCK_TIME_SECONDS) != 600:
        fail("TARGET_BLOCK_TIME_SECONDS drift")
    if int(P.ASERT_HALF_LIFE_SECONDS) != 172_800:
        fail("ASERT_HALF_LIFE_SECONDS drift")
    if int(P.ASERT_ACTIVATION_HEIGHT) != 1:
        fail("ASERT_ACTIVATION_HEIGHT drift")

    print("PASS: utxo/wallet/consensus guards OK")


if __name__ == "__main__":
    main()
