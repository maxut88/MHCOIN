"""Load data directory and network settings."""

from __future__ import annotations

import os
from pathlib import Path

from mhcoin.consensus.params import get_network_params
from mhcoin.wallet.wallet import WalletPaths


def resolve_data_dir(network: str | None = None) -> Path:
    net = (network or os.environ.get("MHCOIN_NETWORK", "localnet")).strip().lower()
    # Validate network name early (raises on unknown)
    get_network_params(net)
    override = os.environ.get("MHCOIN_DATA")
    if override:
        return Path(override).expanduser().resolve()
    # Separate data directories per network — prevents accidental mainnet↔testnet reuse
    return Path.home() / ".mhcoin" / net


def wallet_paths(network: str | None = None) -> WalletPaths:
    net = (network or os.environ.get("MHCOIN_NETWORK", "localnet")).strip().lower()
    params = get_network_params(net)
    return WalletPaths(
        data_dir=resolve_data_dir(net),
        network=params.name,
        hrp=params.address_hrp,
    )
