"""Persist Desktop preferences (separate from consensus)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def prefs_path() -> Path:
    return Path.home() / ".mhcoin" / "desktop_prefs.json"


def _read_prefs() -> dict:
    path = prefs_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_prefs(data: dict) -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_preferred_network() -> str | None:
    net = str(_read_prefs().get("network") or "").strip().lower()
    if net in ("mainnet", "localnet", "testnet", "regtest"):
        return net
    return None


def save_preferred_network(network: str) -> None:
    data = _read_prefs()
    data["network"] = network.strip().lower()
    _write_prefs(data)


def clamp_mine_intensity(value: object, default: int = 100) -> int:
    """CPU load percent for Desktop mining (10–100)."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = int(default)
    if n < 10:
        n = 10
    if n > 100:
        n = 100
    # Keep slider steps friendly
    return max(10, min(100, int(round(n / 5.0) * 5)))


def load_mine_intensity(default: int = 100) -> int:
    raw = _read_prefs().get("mine_intensity", default)
    return clamp_mine_intensity(raw, default=default)


def save_mine_intensity(value: object) -> int:
    n = clamp_mine_intensity(value)
    data = _read_prefs()
    data["mine_intensity"] = n
    _write_prefs(data)
    return n


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
