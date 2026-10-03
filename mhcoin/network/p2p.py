"""P2P manager — peers, listen, connect, relay, Stage 7 discovery/bans."""

from __future__ import annotations

import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from mhcoin.network.addrdb import AddrDB
from mhcoin.network.seeds import HARDCODED_SEEDS
from mhcoin.network.ban import (
    BanManager,
    MISBEHAVIOR_HANDSHAKE,
    MISBEHAVIOR_INVALID_BLOCK,
    MISBEHAVIOR_PROTOCOL,
)
from mhcoin.network.client import connect_peer
from mhcoin.network.constants import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_MAX_PEERS,
    DEFAULT_OUTBOUND_TARGET,
    DEFAULT_PING_INTERVAL,
    DEFAULT_RECONNECT_INTERVAL,
    BAN_SCORE_THRESHOLD,
    CONNECT_RATE_WINDOW,
    MAX_INBOUND_CONNECT_RATE,
    MAX_INBOUND_PER_HOST,
    NETWORK_MAGIC,
)
from mhcoin.network.discovery import DiscoveryManager
from mhcoin.network.peer import Peer, PeerState
from mhcoin.network.security import RateLimiter
from mhcoin.network.serialization import ProtocolError
from mhcoin.network.server import PeerServer

if TYPE_CHECKING:
    from mhcoin.network.relay import TxRelay
    from mhcoin.transaction.transaction import Transaction

logger = logging.getLogger("mhcoin.p2p")


@dataclass
class P2PConfig:
    network: str = "localnet"
    host: str = "127.0.0.1"
    port: int = 18444
    max_peers: int = DEFAULT_MAX_PEERS
    connect_timeout: float = 5.0
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    ping_interval: float = DEFAULT_PING_INTERVAL
    start_height: int = 0
    # If set, VERSION uses live height (process-start height goes stale and blocks IBD).
    get_start_height: Callable[[], int] | None = None
    data_dir: Path | None = None
    outbound_target: int = DEFAULT_OUTBOUND_TARGET
    reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL
    ban_threshold: int = BAN_SCORE_THRESHOLD
    # Seeded connect targets (manual / DNS / hardcoded); gossip fills the rest
    connect_seeds: list[str] = field(default_factory=list)
    # Desktop wallets: outbound-only (no listen) avoids port clashes / inbound bans.
    enable_listen: bool = True


class P2PManager:
    def __init__(self, config: P2PConfig):
        if config.network not in NETWORK_MAGIC:
            raise ValueError(f"unknown network {config.network}")
        self.config = config
        self.magic = NETWORK_MAGIC[config.network]
        self.our_nonce = secrets.randbits(64)
        self._peers: dict[str, Peer] = {}
        self._lock = threading.RLock()
        self._server: PeerServer | None = None
        self._alive = False
        self._ping_thread: threading.Thread | None = None
        self.relay: TxRelay | None = None

        if config.data_dir is not None:
            self.addrdb = AddrDB(config.data_dir / "peers.sqlite")
            self.bans = BanManager(
                config.data_dir / "bans.json",
                threshold=config.ban_threshold,
                on_ban=self._kick_banned_host,
            )
        else:
            import tempfile

            td = Path(tempfile.mkdtemp(prefix="mhcoin-p2p-"))
            self.addrdb = AddrDB(td / "peers.sqlite")
            self.bans = BanManager(
                td / "bans.json",
                threshold=config.ban_threshold,
                on_ban=self._kick_banned_host,
            )

        self._inbound_rate = RateLimiter(
            limit=MAX_INBOUND_CONNECT_RATE, window=CONNECT_RATE_WINDOW
        )
        self._inbound_counts: dict[str, int] = {}

        self.discovery = DiscoveryManager(
            manager=self,
            addrdb=self.addrdb,
            bans=self.bans,
            our_host=config.host,
            our_port=config.port,
            outbound_target=config.outbound_target,
            reconnect_interval=config.reconnect_interval,
            connect_fn=self._discovery_connect,
        )
        # Always protect hardcoded seeds (even with --no-seed): NAT hairpin
        # inbound can appear as the public seed IP; do not ban our own seed.
        net = str(getattr(config, "network", "") or "")
        for seed in list(HARDCODED_SEEDS.get(net, []) or []) + list(config.connect_seeds or []):
            host, _, port_s = seed.rpartition(":")
            if not host:
                continue
            self.bans.protect(host)
        for seed in config.connect_seeds:
            host, _, port_s = seed.rpartition(":")
            try:
                port = int(port_s)
            except ValueError:
                logger.warning("invalid connect seed %s", seed)
                continue
            if not host:
                continue
            self.discovery.add_manual(host, port)

    def _kick_banned_host(self, host: str) -> None:
        for p in self.get_peers_objects():
            if p.host == host:
                logger.info("Kicking banned peer %s", p.addr)
                try:
                    p.close()
                except Exception:
                    pass

    def _discovery_connect(self, host: str, port: int) -> None:
        self.connect_to_peer(host, port)

    def attach_relay(self, relay: TxRelay) -> None:
        self.relay = relay
        relay.attach_ban_manager(self.bans)
        with self._lock:
            for p in self._peers.values():
                p.relay = relay
                p.discovery = self.discovery

    @property
    def listen_addr(self) -> str:
        return f"{self.config.host}:{self.config.port}"

    def start(self) -> None:
        self._alive = True
        if self.config.enable_listen:
            self._server = PeerServer(self, host=self.config.host, port=self.config.port)
            self._server.start()
        else:
            self._server = None
            logger.info("P2P outbound-only (listen disabled)")
        self._ping_thread = threading.Thread(target=self._ping_loop, name="mhcoin-ping", daemon=True)
        self._ping_thread.start()

    def stop(self) -> None:
        # Mark dead first so peer/discovery threads stop touching AddrDB.
        self._alive = False
        if self._server:
            self._server.stop()
            self._server = None
        with self._lock:
            peers = list(self._peers.values())
            self._peers.clear()
        for p in peers:
            try:
                p.close()
            except Exception:
                pass
        # Let peer reader threads exit before closing AddrDB.
        time.sleep(0.35)
        try:
            self.addrdb.close()
        except Exception:
            pass

    def peer_count(self) -> int:
        with self._lock:
            return len(self._peers)

    def get_peers(self) -> list[dict]:
        now = time.time()
        stale_after = 60.0
        with self._lock:
            out = []
            for p in self._peers.values():
                fresh = (
                    p.status_recv_at > 0
                    and (now - p.status_recv_at) <= stale_after
                )
                mining = bool(p.reported_mining) if fresh else False
                hps = int(p.reported_hps) if fresh and mining else 0
                out.append(
                    {
                        "addr": p.addr,
                        "inbound": p.inbound,
                        "state": p.state.value,
                        "protocol": p.protocol_version,
                        "height": max(
                            int(p.best_height or 0),
                            int(p.remote_start_height or 0),
                        ),
                        "agent": p.software_version,
                        "network": p.remote_network,
                        "mining": mining,
                        "hps": hps,
                        "status_age": (
                            round(now - p.status_recv_at, 1)
                            if p.status_recv_at > 0
                            else None
                        ),
                    }
                )
            return out

    def get_peers_objects(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def handshaked_peers(self) -> list[Peer]:
        with self._lock:
            return [p for p in self._peers.values() if p.state == PeerState.HANDSHAKED]

    def get_peer(self, addr: str) -> Peer | None:
        with self._lock:
            return self._peers.get(addr)

    def add_peer(self, peer: Peer) -> None:
        self._add(peer)

    def remove_peer(self, addr: str) -> None:
        with self._lock:
            peer = self._peers.pop(addr, None)
        if peer is not None:
            peer.close()

    def broadcast(self, command: str, payload: bytes = b"") -> int:
        n = 0
        for p in self.handshaked_peers():
            try:
                p.send_raw(command, payload)
                n += 1
            except Exception:
                logger.debug("broadcast failed to %s", p.addr)
        return n

    def broadcast_transaction(self, tx: Transaction, *, source_peer: Peer | None = None) -> str:
        if self.relay is None:
            raise ProtocolError("TX relay not configured")
        return self.relay.broadcast_transaction(tx, source_peer=source_peer)

    def accept_connection(self, sock: socket.socket) -> Peer:
        try:
            peername = sock.getpeername()
            host = str(peername[0])
        except OSError:
            host = ""
        if host and self.bans.is_banned(host):
            sock.close()
            raise ProtocolError(f"banned peer {host}")
        if host:
            # Concurrent inbound slots from same host
            with self._lock:
                n = sum(1 for p in self._peers.values() if p.host == host and p.inbound)
            if n >= MAX_INBOUND_PER_HOST:
                sock.close()
                raise ProtocolError(f"too many inbound from {host}")
            if not self._inbound_rate.allow(host):
                self.bans.misbehavior(host, MISBEHAVIOR_HANDSHAKE, reason="connect flood")
                sock.close()
                raise ProtocolError(f"inbound rate exceeded for {host}")
        with self._lock:
            if len(self._peers) >= self.config.max_peers:
                try:
                    sock.close()
                except OSError:
                    pass
                raise ProtocolError("max peers reached")
        peer = self._make_peer(sock, inbound=True, handshake_timeout=self.config.handshake_timeout)
        self._add(peer)
        peer.start()
        return peer

    def register_outbound(self, sock: socket.socket, *, handshake_timeout: float) -> Peer:
        with self._lock:
            if len(self._peers) >= self.config.max_peers:
                sock.close()
                raise ProtocolError("max peers reached")
            host, port = sock.getpeername()[:2]
            key = f"{host}:{port}"
            if key in self._peers:
                sock.close()
                raise ProtocolError(f"duplicate peer {key}")
            if self.bans.is_banned(str(host)):
                sock.close()
                raise ProtocolError(f"banned peer {host}")
        peer = self._make_peer(sock, inbound=False, handshake_timeout=handshake_timeout)
        self._add(peer)
        return peer

    def connect_to_peer(self, host: str, port: int) -> Peer:
        if host in ("127.0.0.1", "localhost", "::1") and port == self.config.port:
            raise ProtocolError("refusing self-connection to own listen port")
        if self.discovery is not None and self.discovery.is_self(host, port):
            raise ProtocolError(f"refusing self-connection to {host}:{port}")
        if self.bans.is_banned(host):
            raise ProtocolError(f"banned peer {host}")
        with self._lock:
            key = f"{host}:{port}"
            if key in self._peers:
                raise ProtocolError(f"already connected to {key}")
        self.discovery.add_manual(host, port)
        return connect_peer(
            self,
            host,
            port,
            connect_timeout=self.config.connect_timeout,
            handshake_timeout=self.config.handshake_timeout,
        )

    def report_invalid_block(self, peer: Peer | None, reason: str) -> None:
        if peer is None:
            return
        self.bans.misbehavior(
            peer.host, MISBEHAVIOR_INVALID_BLOCK, reason=f"invalid block: {reason}"
        )

    def report_protocol_abuse(self, peer: Peer | None, reason: str) -> None:
        if peer is None:
            return
        banned = self.bans.misbehavior(
            peer.host, MISBEHAVIOR_PROTOCOL, reason=reason
        )
        if banned:
            peer.close()

    def _make_peer(self, sock: socket.socket, *, inbound: bool, handshake_timeout: float) -> Peer:
        peer = Peer(
            sock,
            magic=self.magic,
            network=self.config.network,
            inbound=inbound,
            our_nonce=self.our_nonce,
            our_listen_port=self.config.port,
            start_height=self.config.start_height,
            get_start_height=self.config.get_start_height,
            on_handshaked=self._on_handshaked,
            on_close=self._on_close,
            handshake_timeout=handshake_timeout,
        )
        peer.relay = self.relay
        peer.discovery = self.discovery
        return peer

    def _add(self, peer: Peer) -> None:
        with self._lock:
            self._peers[peer.addr] = peer
            if self.relay is not None:
                peer.relay = self.relay
            peer.discovery = self.discovery

    def _on_handshaked(self, peer: Peer) -> None:
        # IMPORTANT: never call peer.close() while holding self._lock.
        # peer.close() -> on_close -> _on_close acquires the same lock; with
        # threading.Lock that deadlocks the accept loop (CLOSE-WAIT backlog,
        # clients see "peer closed during handshake").
        drop_peer = None
        with self._lock:
            for other in self._peers.values():
                if other is peer:
                    continue
                if peer.remote_nonce is not None and other.remote_nonce == peer.remote_nonce:
                    drop, keep = peer, other

                    def _is_lan(host: str) -> bool:
                        return host.startswith(("192.168.", "10.", "172.")) or host in (
                            "127.0.0.1",
                            "localhost",
                        )

                    if _is_lan(peer.host) and not _is_lan(other.host):
                        drop, keep = other, peer
                    logger.info(
                        "duplicate peer nonce %s — closing extra path %s (kept %s)",
                        peer.remote_nonce,
                        drop.addr,
                        keep.addr,
                    )
                    drop_peer = drop
                    break
        if drop_peer is not None:
            try:
                drop_peer.close()
            except Exception:
                logger.debug("duplicate path close failed", exc_info=True)
            if drop_peer is peer:
                return
        try:
            self.discovery.on_handshaked(peer)
        except Exception:
            logger.debug("discovery on_handshaked failed", exc_info=True)
        if self.relay is not None and self.relay.sync is not None:
            sync = self.relay.sync

            def _kick() -> None:
                try:
                    sync.maybe_start(peer)
                except Exception:
                    logger.debug("sync start failed", exc_info=True)

            threading.Thread(target=_kick, name="mhcoin-sync-kick", daemon=True).start()

    def _on_close(self, peer: Peer) -> None:
        with self._lock:
            self._peers.pop(peer.addr, None)
        logger.info("Peer disconnected %s", peer.addr)
        if self.relay is not None and self.relay.sync is not None:
            sync = self.relay.sync
            try:
                sync.on_peer_disconnected(peer)
            except Exception:
                logger.debug("sync on_peer_disconnected failed", exc_info=True)
            # Retry IBD / catch-up with any remaining ahead/behind peer.
            for other in self.handshaked_peers():
                if other is peer:
                    continue
                try:
                    sync.maybe_start(other)
                except Exception:
                    logger.debug("sync retry after disconnect failed", exc_info=True)
                break

    def status(self) -> dict:
        return {
            "peer_count": self.peer_count(),
            "handshaked": len(self.handshaked_peers()),
            "known_addrs": self.addrdb.count(),
            "bans": len(self.bans.list_bans()),
            "outbound_target": self.config.outbound_target,
        }

    def _ping_loop(self) -> None:
        while self._alive:
            time.sleep(1.0)
            if not self._alive:
                break
            if self.relay is not None:
                try:
                    self.relay.sync_from_disk()
                except Exception:
                    logger.debug("mempool disk sync failed", exc_info=True)
                if self.relay.sync is not None:
                    try:
                        self.relay.sync.tick()
                    except Exception:
                        logger.debug("sync tick failed", exc_info=True)
            try:
                self.discovery.tick()
            except Exception:
                logger.debug("discovery tick failed", exc_info=True)
            for p in self.handshaked_peers():
                p.check_timeouts()
                if p._pending_ping is None and (time.time() - p.last_send) >= self.config.ping_interval:
                    try:
                        p.send_ping()
                    except Exception:
                        logger.debug("ping failed to %s", p.addr)
