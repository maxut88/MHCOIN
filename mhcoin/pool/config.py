"""Pool configuration from env / CLI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    return float(raw)


@dataclass
class PoolConfig:
    network: str = "mainnet"
    # Dedicated chain datadir for the pool node (avoid sharing with Desktop miner).
    data_dir: Path = Path.home() / ".mhcoin" / "pool-node"
    pool_dir: Path = Path.home() / ".mhcoin" / "pool"
    pool_address: str = ""
    # Fee taken from block reward before PROP split (0..100).
    fee_percent: float = 1.0
    # Share target multiplier vs network (higher = easier shares).
    share_factor: int = 1024
    # Confirmations before immature credits mature.
    mature_confirms: int = 20
    # Auto-payout when matured balance >= this (sats). 0 = disable auto-payout.
    payout_threshold_sats: int = 10_000_000  # 0.1 MHC
    # JSON miner protocol
    listen_host: str = "0.0.0.0"
    listen_port: int = 3333
    # Stratum (phase 2)
    stratum_port: int = 3334
    # Stats HTTP
    web_host: str = "0.0.0.0"
    web_port: int = 8888
    # Wallet password for payouts (or MHCOIN_POOL_PASSWORD / MHCOIN_WALLET_PASSWORD)
    wallet_password: str = ""
    job_refresh_sec: float = 20.0
    hrp: str = "mhc"

    @property
    def db_path(self) -> Path:
        return self.pool_dir / "pool.sqlite"

    @classmethod
    def from_env(cls, **overrides: object) -> PoolConfig:
        home = Path.home()
        cfg = cls(
            network=_env("MHCOIN_NETWORK", "mainnet") or "mainnet",
            data_dir=Path(
                _env("MHCOIN_POOL_DATA", str(home / ".mhcoin" / "pool-node"))
            ).expanduser(),
            pool_dir=Path(
                _env("MHCOIN_POOL_DIR", str(home / ".mhcoin" / "pool"))
            ).expanduser(),
            pool_address=_env("MHCOIN_POOL_ADDRESS"),
            fee_percent=_env_float("MHCOIN_POOL_FEE_PERCENT", 1.0),
            share_factor=_env_int("MHCOIN_POOL_SHARE_FACTOR", 1024),
            mature_confirms=_env_int("MHCOIN_POOL_MATURE_CONFIRMS", 20),
            payout_threshold_sats=_env_int("MHCOIN_POOL_PAYOUT_THRESHOLD", 10_000_000),
            listen_host=_env("MHCOIN_POOL_LISTEN", "0.0.0.0") or "0.0.0.0",
            listen_port=_env_int("MHCOIN_POOL_PORT", 3333),
            stratum_port=_env_int("MHCOIN_POOL_STRATUM_PORT", 3334),
            web_host=_env("MHCOIN_POOL_WEB_HOST", "0.0.0.0") or "0.0.0.0",
            web_port=_env_int("MHCOIN_POOL_WEB_PORT", 8888),
            wallet_password=_env("MHCOIN_POOL_PASSWORD")
            or _env("MHCOIN_WALLET_PASSWORD"),
            job_refresh_sec=_env_float("MHCOIN_POOL_JOB_REFRESH", 20.0),
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        if isinstance(cfg.data_dir, str):
            cfg.data_dir = Path(cfg.data_dir).expanduser()
        if isinstance(cfg.pool_dir, str):
            cfg.pool_dir = Path(cfg.pool_dir).expanduser()
        return cfg
