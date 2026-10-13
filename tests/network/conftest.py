"""Shared helpers for real localhost TCP P2P tests."""

from __future__ import annotations

import socket
import time
from pathlib import Path

from mhcoin.network.p2p import P2PConfig, P2PManager
from mhcoin.network.peer import PeerState


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def make_manager(
    tmp_path: Path | None = None,
    *,
    port: int | None = None,
    network: str = "localnet",
    max_peers: int = 32,
    handshake_timeout: float = 5.0,
    ping_interval: float = 60.0,
    outbound_target: int = 0,
    ban_threshold: int = 100,
) -> P2PManager:
    p = port if port is not None else free_port()
    data_dir = None
    if tmp_path is not None:
        data_dir = tmp_path / f"p2p-{p}"
        data_dir.mkdir(parents=True, exist_ok=True)
    cfg = P2PConfig(
        network=network,
        host="127.0.0.1",
        port=p,
        max_peers=max_peers,
        connect_timeout=3.0,
        handshake_timeout=handshake_timeout,
        ping_interval=ping_interval,
        start_height=0,
        data_dir=data_dir,
        outbound_target=outbound_target,
        reconnect_interval=3600.0,  # quiet in most tests unless overridden
        ban_threshold=ban_threshold,
    )
    return P2PManager(cfg)


def wait_handshaked(mgr: P2PManager, n: int = 1, timeout: float = 5.0) -> bool:
    return wait_until(lambda: len(mgr.handshaked_peers()) >= n, timeout=timeout)
