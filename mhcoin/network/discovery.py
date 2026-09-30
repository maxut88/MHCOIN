"""Stage 7 — ADDR/GETADDR gossip, outbound selection, reconnect.

Bootstrap seeds (hardcoded / DNS) are handled in mhcoin.network.seeds; this
module maintains the mesh after first contact (Bitcoin-style ADDR gossip).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import TYPE_CHECKING, Callable

from mhcoin.network.addrdb import AddrDB
from mhcoin.network.ban import BanManager, MISBEHAVIOR_FLOOD, MISBEHAVIOR_PROTOCOL
from mhcoin.network.constants import (
    ADDR_RATE_WINDOW,
    DEFAULT_ADDR_BROADCAST_INTERVAL,
    DEFAULT_OUTBOUND_TARGET,
    DEFAULT_RECONNECT_INTERVAL,
    MAX_ADDR_RATE_PER_PEER,
    MAX_GETADDR_RESPONSE,
    SERVICES_NODE_NETWORK,
)
from mhcoin.network.messages import (
    NetAddress,
    decode_addr,
    decode_getaddr,
    encode_addr,
    encode_getaddr,
)
from mhcoin.network.serialization import ProtocolError

if TYPE_CHECKING:
    from mhcoin.network.peer import Peer
    from mhcoin.network.p2p import P2PManager

logger = logging.getLogger("mhcoin.p2p")


class DiscoveryManager:
    """Peer address gossip + outbound maintenance (post-bootstrap mesh)."""

    def __init__(
        self,
        *,
        manager: P2PManager,
        addrdb: AddrDB,
        bans: BanManager,
        our_host: str,
        our_port: int,
        outbound_target: int = DEFAULT_OUTBOUND_TARGET,
        reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL,
        addr_broadcast_interval: float = DEFAULT_ADDR_BROADCAST_INTERVAL,
        connect_fn: Callable[[str, int], None] | None = None,
    ):
        self.manager = manager
        self.addrdb = addrdb
        self.bans = bans
        self.our_host = our_host
        self.our_port = our_port
        self.outbound_target = outbound_target
        self.reconnect_interval = reconnect_interval
        self.addr_broadcast_interval = addr_broadcast_interval
        self._connect_fn = connect_fn
        self._lock = threading.Lock()
        self._addr_rate: dict[str, list[float]] = {}  # peer.addr -> timestamps
        self._last_reconnect = 0.0
        self._last_addr_share = 0.0
        self._getaddr_sent: set[str] = set()

    def is_self(self, host: str, port: int) -> bool:
        if port != self.our_port:
            return False
        if host in (self.our_host, "127.0.0.1", "localhost", "::1", "0.0.0.0"):
            # Treat loopback + our bind host as self when ports match
            if self.our_host in ("127.0.0.1", "0.0.0.0", "localhost", "::1"):
                return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", self.our_host)
            return host == self.our_host
        return False

    def add_manual(self, host: str, port: int) -> None:
        if self.is_self(host, port):
            return
        self.addrdb.add(host, port, source="manual")

    def on_handshaked(self, peer: Peer) -> None:
        # Record peer's advertised listen address when available
        listen_port = peer.remote_listen_port or peer.port
        if peer.host and listen_port:
            if not self.is_self(peer.host, listen_port) and not self.bans.is_banned(peer.host):
                self.addrdb.add(
                    peer.host,
                    listen_port,
                    services=peer.services or SERVICES_NODE_NETWORK,
                    source="inbound" if peer.inbound else "gossip",
                )
                self.addrdb.mark_success(peer.host, listen_port)
        # Stage 8: do NOT clear misbehavior scores on handshake (prevents ban bypass)

        # Request addresses once per peer
        with self._lock:
            if peer.addr in self._getaddr_sent:
                return
            self._getaddr_sent.add(peer.addr)
        try:
            peer.send_raw("GETADDR", encode_getaddr())
            logger.info("GETADDR sent to %s", peer.addr)
        except Exception:
            logger.debug("GETADDR send failed to %s", peer.addr, exc_info=True)

        # Opportunistically share a few known addrs
        self._maybe_push_addr(peer)

    def on_getaddr(self, peer: Peer, payload: bytes) -> None:
        if not getattr(self.manager, "_alive", False):
            return
        try:
            decode_getaddr(payload)
        except ProtocolError as e:
            logger.warning("malformed GETADDR from %s: %s", peer.addr, e)
            self.bans.misbehavior(peer.host, MISBEHAVIOR_PROTOCOL, reason="bad GETADDR")
            peer.close()
            return
        try:
            addrs = self._select_addrs_for_peer(peer, limit=MAX_GETADDR_RESPONSE)
            peer.send_raw("ADDR", encode_addr(addrs))
            logger.info("ADDR sent to %s count=%s", peer.addr, len(addrs))
        except Exception:
            logger.debug("ADDR reply failed", exc_info=True)

    def on_addr(self, peer: Peer, payload: bytes) -> None:
        if not getattr(self.manager, "_alive", False):
            return
        try:
            addrs = decode_addr(payload)
        except ProtocolError as e:
            logger.warning("malformed ADDR from %s: %s", peer.addr, e)
            self.bans.misbehavior(peer.host, MISBEHAVIOR_PROTOCOL, reason="bad ADDR")
            peer.close()
            return

        if not self._rate_allow(peer.addr):
            logger.info("ADDR rate-limited from %s", peer.addr)
            if self.bans.misbehavior(peer.host, MISBEHAVIOR_FLOOD, reason="ADDR flood"):
                peer.close()
            return

        now = int(time.time())
        stored = 0
        for a in addrs:
            # Reject absurd timestamps (too far future / ancient)
            if a.timestamp > now + 10 * 60:
                continue
            if a.timestamp < now - 30 * 24 * 3600:
                continue
            if self.is_self(a.host, a.port):
                continue
            if self.bans.is_banned(a.host):
                continue
            if self.addrdb.add_net_address(a, source="gossip"):
                stored += 1
        logger.info("ADDR from %s accepted=%s/%s", peer.addr, stored, len(addrs))

    def tick(self) -> None:
        """Periodic reconnect + occasional address share."""
        if not getattr(self.manager, "_alive", False):
            return
        now = time.time()
        if now - self._last_reconnect >= self.reconnect_interval:
            self._last_reconnect = now
            self._try_outbound()
        if now - self._last_addr_share >= self.addr_broadcast_interval:
            self._last_addr_share = now
            for p in self.manager.handshaked_peers():
                self._maybe_push_addr(p)

    def _try_outbound(self) -> None:
        if self._connect_fn is None:
            return
        peers = self.manager.handshaked_peers()
        outbound = [p for p in peers if not p.inbound]
        need = self.outbound_target - len(outbound)
        if need <= 0:
            return
        connected = {p.addr for p in self.manager.get_peers_objects()}
        # Also exclude by listen keys
        for p in self.manager.get_peers_objects():
            lp = p.remote_listen_port or p.port
            connected.add(f"{p.host}:{lp}")
        connected.add(f"{self.our_host}:{self.our_port}")
        connected.add(f"127.0.0.1:{self.our_port}")

        candidates = self.addrdb.candidates_for_outbound(exclude=connected, limit=need * 3)
        random.shuffle(candidates)
        for rec in candidates[:need]:
            if self.bans.is_banned(rec.host):
                continue
            if self.is_self(rec.host, rec.port):
                continue
            self.addrdb.mark_attempt(rec.host, rec.port)
            logger.info("Outbound attempt %s:%s", rec.host, rec.port)
            try:
                self._connect_fn(rec.host, rec.port)
            except Exception as e:
                logger.info("Outbound connect failed %s:%s: %s", rec.host, rec.port, e)

    def _select_addrs_for_peer(self, peer: Peer, *, limit: int) -> list[NetAddress]:
        if not getattr(self.manager, "_alive", False):
            return []
        try:
            records = self.addrdb.list_recent(limit=limit * 2)
        except Exception:
            return []
        out: list[NetAddress] = []
        # Include our listen address so peers can dial us back (if not 0.0.0.0)
        advertise_host = self.our_host
        if advertise_host in ("0.0.0.0", "::"):
            advertise_host = "127.0.0.1"
        our = NetAddress(
            timestamp=int(time.time()),
            services=SERVICES_NODE_NETWORK,
            host=advertise_host,
            port=self.our_port,
        )
        out.append(our)
        for rec in records:
            if rec.host == peer.host and rec.port == (peer.remote_listen_port or peer.port):
                continue
            if self.bans.is_banned(rec.host):
                continue
            out.append(rec.to_net_address())
            if len(out) >= limit:
                break
        return out[:limit]

    def _maybe_push_addr(self, peer: Peer) -> None:
        addrs = self._select_addrs_for_peer(peer, limit=min(8, MAX_GETADDR_RESPONSE))
        if len(addrs) <= 1:
            return
        try:
            peer.send_raw("ADDR", encode_addr(addrs))
        except Exception:
            pass

    def _rate_allow(self, peer_addr: str) -> bool:
        now = time.time()
        with self._lock:
            stamps = self._addr_rate.setdefault(peer_addr, [])
            stamps[:] = [t for t in stamps if now - t < ADDR_RATE_WINDOW]
            if len(stamps) >= MAX_ADDR_RATE_PER_PEER:
                return False
            stamps.append(now)
            return True
