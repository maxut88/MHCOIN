"""TCP listen server for MHCOIN P2P (Stage 2)."""

from __future__ import annotations

import logging
import socket
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mhcoin.network.p2p import P2PManager

logger = logging.getLogger("mhcoin.p2p")


class PeerServer:
    def __init__(
        self,
        manager: P2PManager,
        *,
        host: str,
        port: int,
    ):
        self.manager = manager
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._alive = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(64)
        sock.settimeout(1.0)
        self._sock = sock
        self._alive = True
        self._thread = threading.Thread(target=self._accept_loop, name="mhcoin-accept", daemon=True)
        self._thread.start()
        logger.info("Listening on %s:%s", self.host, self.port)

    def stop(self) -> None:
        self._alive = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while self._alive:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            logger.info("Incoming peer %s:%s", addr[0], addr[1])
            try:
                self.manager.accept_connection(conn)
            except Exception as e:
                logger.warning("reject inbound %s: %s", addr, e)
                try:
                    conn.close()
                except OSError:
                    pass
