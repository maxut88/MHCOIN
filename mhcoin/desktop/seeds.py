"""Non-consensus default P2P seeds for Desktop / casual operators.

Override with MHCOIN_CONNECT=host:port,host:port (comma-separated).
"""

from __future__ import annotations

import os

# Public solo seed after T=0 (this VPS). Add more as peers come online.
DEFAULT_SEEDS: dict[str, list[str]] = {
    "mainnet": ["176.38.3.168:8333"],
    "testnet": [],
    "regtest": [],
    "localnet": [],
}


def default_connect_peers(network: str) -> list[str]:
    """Return outbound dial targets for the given network."""
    override = os.environ.get("MHCOIN_CONNECT", "").strip()
    if override:
        return [p.strip() for p in override.split(",") if p.strip()]
    return list(DEFAULT_SEEDS.get(network.strip().lower(), []))
