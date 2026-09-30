"""Persist Desktop network preference (separate from consensus)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def prefs_path() -> Path:
    return Path.home() / ".mhcoin" / "desktop_prefs.json"


def load_preferred_network() -> str | None:
    path = prefs_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        net = str(data.get("network") or "").strip().lower()
        if net in ("mainnet", "localnet", "testnet", "regtest"):
            return net
    except Exception:
        return None
    return None


def save_preferred_network(network: str) -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"network": network.strip().lower()}, indent=2) + "\n",
        encoding="utf-8",
    )


def resolve_launch_network(
    explicit: str | None = None,
    argv: list[str] | None = None,
) -> str:
    """Resolve Desktop network.

    Priority: --network CLI → MHCOIN_NETWORK env → saved prefs →
    mainnet when frozen (release) else localnet (dev/RC).
    """
    args = list(argv or sys.argv[1:])
    if explicit:
        return explicit.strip().lower()
    if "--network" in args:
        i = args.index("--network")
        if i + 1 < len(args):
            return args[i + 1].strip().lower()
    env = os.environ.get("MHCOIN_NETWORK", "").strip().lower()
    if env:
        return env
    pref = load_preferred_network()
    if pref:
        return pref
    if getattr(sys, "frozen", False):
        return "mainnet"
    return "localnet"
