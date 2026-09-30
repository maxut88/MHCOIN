"""Outbound TCP client for MHCOIN P2P (Stage 2)."""

from __future__ import annotations

import logging
import socket
import time
from typing import TYPE_CHECKING

from mhcoin.network.constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_HANDSHAKE_TIMEOUT
from mhcoin.network.peer import Peer, PeerState
from mhcoin.network.serialization import ProtocolError

if TYPE_CHECKING:
    from mhcoin.network.p2p import P2PManager

logger = logging.getLogger("mhcoin.p2p")


def connect_peer(
    manager: P2PManager,
    host: str,
    port: int,
    *,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
) -> Peer:
    """Open outbound TCP connection and complete VERSION/VERACK handshake."""
    logger.info("Connecting to %s:%s", host, port)
    sock = socket.create_connection((host, port), timeout=connect_timeout)
    peer = manager.register_outbound(sock, handshake_timeout=handshake_timeout)
    peer.send_version()
    peer.start()

    deadline = time.time() + handshake_timeout
    while time.time() < deadline:
        if peer.state == PeerState.HANDSHAKED:
            return peer
        if peer.state in (PeerState.DISCONNECTED, PeerState.CLOSING):
            raise ProtocolError(f"peer closed during handshake: {host}:{port}")
        time.sleep(0.05)
    peer.close()
    raise ProtocolError(f"handshake timeout: {host}:{port}")
