"""MHCOIN TCP peer with explicit handshake state machine."""

from __future__ import annotations

import enum
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from mhcoin.network.constants import (
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_PING_TIMEOUT,
    MAX_PROTOCOL_VERSION,
    MIN_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SERVICES_NODE_NETWORK,
    SOFTWARE_VERSION,
)
from mhcoin.network.messages import (
    VersionPayload,
    decode_ping,
    decode_pong,
    decode_status,
    decode_verack,
    decode_version,
    encode_ping,
    encode_pong,
    encode_verack,
    encode_version,
)
from mhcoin.network.serialization import (
    NetMessage,
    ProtocolError,
    encode_envelope,
    try_decode_envelope,
)

logger = logging.getLogger("mhcoin.p2p")


class PeerState(enum.Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    VERSION_SENT = "VERSION_SENT"
    VERSION_RECEIVED = "VERSION_RECEIVED"
    HANDSHAKED = "HANDSHAKED"
    CLOSING = "CLOSING"


# Valid transitions
_TRANSITIONS: dict[PeerState, set[PeerState]] = {
    PeerState.DISCONNECTED: {PeerState.CONNECTING, PeerState.CONNECTED},
    PeerState.CONNECTING: {PeerState.CONNECTED, PeerState.CLOSING, PeerState.DISCONNECTED},
    PeerState.CONNECTED: {PeerState.VERSION_SENT, PeerState.VERSION_RECEIVED, PeerState.CLOSING},
    PeerState.VERSION_SENT: {PeerState.VERSION_RECEIVED, PeerState.HANDSHAKED, PeerState.CLOSING},
    PeerState.VERSION_RECEIVED: {PeerState.VERSION_SENT, PeerState.HANDSHAKED, PeerState.CLOSING},
    PeerState.HANDSHAKED: {PeerState.CLOSING, PeerState.DISCONNECTED},
    PeerState.CLOSING: {PeerState.DISCONNECTED},
}

# Commands allowed before HANDSHAKED
_PRE_HANDSHAKE_CMDS = frozenset({"VERSION", "VERACK", "PING", "PONG"})


@dataclass
class PeerInfo:
    addr: str
    host: str
    port: int
    inbound: bool
    state: str
    protocol_version: int | None = None
    services: int | None = None
    remote_nonce: int | None = None
    software_version: str | None = None
    start_height: int | None = None
    remote_network: str | None = None
    remote_listen_port: int | None = None


class Peer:
    def __init__(
        self,
        sock: socket.socket,
        *,
        magic: bytes,
        network: str,
        inbound: bool,
        our_nonce: int,
        our_listen_port: int,
        start_height: int = 0,
        get_start_height: Callable[[], int] | None = None,
        on_handshaked: Callable[[Peer], None] | None = None,
        on_close: Callable[[Peer], None] | None = None,
        handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    ):
        self.sock = sock
        self.magic = magic
        self.network = network
        self.inbound = inbound
        self.our_nonce = our_nonce
        self.our_listen_port = our_listen_port
        self.start_height = start_height
        self.get_start_height = get_start_height
        self.on_handshaked = on_handshaked
        self.on_close = on_close
        self.handshake_timeout = handshake_timeout
        self.relay = None  # set by P2PManager when TxRelay is attached
        self.discovery = None  # DiscoveryManager

        try:
            peername = sock.getpeername()
        except OSError:
            peername = ("0.0.0.0", 0)
        if isinstance(peername, tuple) and len(peername) >= 2 and not isinstance(peername[0], int):
            self.host = str(peername[0])
            self.port = int(peername[1])
        else:
            # AF_UNIX socketpair etc.
            self.host = "local"
            self.port = 0
        self.addr = f"{self.host}:{self.port}"

        self.state = PeerState.CONNECTED
        self.protocol_version: int | None = None
        self.services: int | None = None
        self.remote_nonce: int | None = None
        self.software_version: str | None = None
        self.remote_start_height: int | None = None
        self.remote_network: str | None = None
        self.remote_listen_port: int | None = None
        # Best tip height we have heard from this peer (VERSION / STATUS).
        # remote_start_height alone stays frozen at handshake time.
        self.best_height: int = 0
        # Advisory STATUS from peer (live mining hashrate) — not consensus.
        self.reported_hps: int = 0
        self.reported_mining: bool = False
        self.status_recv_at: float = 0.0
        self._status_last_accept: float = 0.0
        self._catchup_last_offer: float = 0.0

        self.last_recv = time.time()
        self.last_send = time.time()
        self.created_at = time.time()
        self._pending_ping: int | None = None
        self._ping_sent_at: float | None = None
        self._got_version = False
        self._got_verack = False
        self._sent_verack = False
        self._sent_version = False
        self._alive = True
        self._buf = b""
        self._send_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def info(self) -> PeerInfo:
        return PeerInfo(
            addr=self.addr,
            host=self.host,
            port=self.port,
            inbound=self.inbound,
            state=self.state.value,
            protocol_version=self.protocol_version,
            services=self.services,
            remote_nonce=self.remote_nonce,
            software_version=self.software_version,
            start_height=self.remote_start_height,
            remote_network=self.remote_network,
            remote_listen_port=self.remote_listen_port,
        )

    def _set_state(self, new: PeerState) -> None:
        if new == self.state:
            return
        allowed = _TRANSITIONS.get(self.state, set())
        if new not in allowed and new != PeerState.DISCONNECTED:
            # Allow force to CLOSING/DISCONNECTED from any state for errors
            if new not in (PeerState.CLOSING, PeerState.DISCONNECTED):
                raise ProtocolError(f"invalid peer transition {self.state} -> {new}")
        old = self.state
        self.state = new
        logger.debug("peer %s state %s -> %s", self.addr, old.value, new.value)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._read_loop, name=f"peer-{self.addr}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self.state == PeerState.DISCONNECTED:
            return
        self._set_state(PeerState.CLOSING)
        self._alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._set_state(PeerState.DISCONNECTED)
        if self.on_close:
            try:
                self.on_close(self)
            except Exception:
                logger.exception("on_close error")

    def send_raw(self, command: str, payload: bytes = b"") -> None:
        data = encode_envelope(self.magic, command, payload)
        with self._send_lock:
            try:
                self.sock.sendall(data)
                self.last_send = time.time()
            except OSError:
                self.close()

    def send_version(self) -> None:
        height = self.start_height
        if self.get_start_height is not None:
            try:
                height = max(0, int(self.get_start_height()))
            except Exception:
                height = self.start_height
        vp = VersionPayload(
            protocol_version=PROTOCOL_VERSION,
            services=SERVICES_NODE_NETWORK,
            timestamp=int(time.time()),
            nonce=self.our_nonce,
            start_height=height,
            network=self.network,
            listen_port=self.our_listen_port,
            software_version=SOFTWARE_VERSION,
        )
        self.send_raw("VERSION", encode_version(vp))
        self._sent_version = True
        if self.state == PeerState.CONNECTED:
            self._set_state(PeerState.VERSION_SENT)
        elif self.state == PeerState.VERSION_RECEIVED:
            pass  # already have remote VERSION
        logger.info("VERSION sent to %s (height=%s)", self.addr, height)

    def send_verack(self) -> None:
        self.send_raw("VERACK", encode_verack())
        self._sent_verack = True
        self._maybe_handshaked()

    def send_ping(self, nonce: int | None = None) -> int:
        if self.state != PeerState.HANDSHAKED:
            raise ProtocolError("cannot PING before handshake")
        n = nonce if nonce is not None else secrets.randbits(64)
        self._pending_ping = n
        self._ping_sent_at = time.time()
        self.send_raw("PING", encode_ping(n))
        logger.info("PING sent to %s", self.addr)
        return n

    def check_timeouts(self) -> None:
        now = time.time()
        if self.state != PeerState.HANDSHAKED:
            if now - self.created_at > self.handshake_timeout:
                logger.warning("handshake timeout with %s", self.addr)
                self.close()
            return
        if self._pending_ping is not None and self._ping_sent_at is not None:
            if now - self._ping_sent_at > DEFAULT_PING_TIMEOUT:
                logger.warning("ping timeout with %s", self.addr)
                self.close()

    def _maybe_handshaked(self) -> None:
        if self._got_version and self._got_verack and self._sent_verack:
            if self.state != PeerState.HANDSHAKED:
                self._set_state(PeerState.HANDSHAKED)
                logger.info("Handshake completed with %s", self.addr)
                if self.on_handshaked:
                    # NEVER run sync/IBD on the read thread — it blocked VERSION/VERACK
                    # for other peers and left sockets in CLOSE-WAIT (Mac handshake timeouts).
                    cb = self.on_handshaked

                    def _run() -> None:
                        try:
                            cb(self)
                        except Exception:
                            logger.exception("on_handshaked")

                    threading.Thread(
                        target=_run, name=f"hs-{self.addr}", daemon=True
                    ).start()

    def _read_loop(self) -> None:
        try:
            while self._alive:
                try:
                    self.sock.settimeout(1.0)
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    self.check_timeouts()
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                self.last_recv = time.time()
                self._buf += chunk
                while True:
                    try:
                        msg, self._buf = try_decode_envelope(self.magic, self._buf)
                    except ProtocolError as e:
                        logger.warning("protocol error from %s: %s", self.addr, e)
                        self.close()
                        return
                    if msg is None:
                        break
                    self._dispatch(msg)
                self.check_timeouts()
        finally:
            if self.state != PeerState.DISCONNECTED:
                self.close()

    def _dispatch(self, msg: NetMessage) -> None:
        cmd = msg.command.upper()
        if self.state != PeerState.HANDSHAKED and cmd not in _PRE_HANDSHAKE_CMDS:
            n = int(getattr(self, "_pre_hs_ignored", 0)) + 1
            self._pre_hs_ignored = n
            # Seed may flood BLOCKs before VERACK; avoid log spam (segfault-unrelated).
            if n <= 2 or n == 10 or n % 100 == 0:
                logger.warning(
                    "ignoring %s before handshake from %s (×%s)", cmd, self.addr, n
                )
            return
        if cmd == "VERSION":
            self._on_version(msg.payload)
        elif cmd == "VERACK":
            self._on_verack(msg.payload)
        elif cmd == "PING":
            self._on_ping(msg.payload)
        elif cmd == "PONG":
            self._on_pong(msg.payload)
        elif cmd == "STATUS":
            self._on_status(msg.payload)
        elif cmd == "INV":
            if self.relay is not None:
                self.relay.on_inv(self, msg.payload)
        elif cmd == "GETDATA":
            if self.relay is not None:
                try:
                    self.relay.on_getdata(self, msg.payload)
                except Exception:
                    logger.debug("GETDATA handler failed from %s", self.addr, exc_info=True)
        elif cmd == "TX":
            if self.relay is not None:
                try:
                    self.relay.on_tx(self, msg.payload)
                except Exception:
                    logger.debug("TX handler failed from %s", self.addr, exc_info=True)
        elif cmd == "BLOCK":
            if self.relay is not None:
                try:
                    self.relay.on_block(self, msg.payload)
                except Exception:
                    logger.debug("BLOCK handler failed from %s", self.addr, exc_info=True)
        elif cmd == "GETHEADERS":
            if self.relay is not None and self.relay.sync is not None:
                self.relay.sync.on_getheaders(self, msg.payload)
        elif cmd == "HEADERS":
            if self.relay is not None and self.relay.sync is not None:
                self.relay.sync.on_headers(self, msg.payload)
        elif cmd == "GETADDR":
            if self.discovery is not None:
                try:
                    self.discovery.on_getaddr(self, msg.payload)
                except Exception:
                    logger.debug("GETADDR handler failed from %s", self.addr, exc_info=True)
        elif cmd == "ADDR":
            if self.discovery is not None:
                try:
                    self.discovery.on_addr(self, msg.payload)
                except Exception:
                    logger.debug("ADDR handler failed from %s", self.addr, exc_info=True)
        else:
            logger.debug("ignored command %s from %s", cmd, self.addr)

    def _on_version(self, payload: bytes) -> None:
        if self._got_version:
            logger.warning("duplicate VERSION from %s", self.addr)
            self.close()
            return
        try:
            vp = decode_version(payload)
        except ProtocolError as e:
            logger.warning("bad VERSION from %s: %s", self.addr, e)
            self.close()
            return
        if not (MIN_PROTOCOL_VERSION <= vp.protocol_version <= MAX_PROTOCOL_VERSION):
            logger.warning("incompatible protocol from %s: %s", self.addr, vp.protocol_version)
            self.close()
            return
        if vp.network != self.network:
            logger.warning("wrong network from %s: %s", self.addr, vp.network)
            self.close()
            return
        if vp.nonce == self.our_nonce:
            logger.warning("self-connection detected with %s", self.addr)
            self.close()
            return

        self.protocol_version = vp.protocol_version
        self.services = vp.services
        self.remote_nonce = vp.nonce
        self.software_version = vp.software_version
        self.remote_start_height = vp.start_height
        self.note_height(vp.start_height)
        self.remote_network = vp.network
        self.remote_listen_port = vp.listen_port
        self._got_version = True
        logger.info("VERSION received from %s (height=%s)", self.addr, vp.start_height)

        if self.state == PeerState.CONNECTED:
            self._set_state(PeerState.VERSION_RECEIVED)
        elif self.state == PeerState.VERSION_SENT:
            # keep VERSION_SENT until we also note received; transition to VERSION_RECEIVED
            self._set_state(PeerState.VERSION_RECEIVED)

        # Inbound peers: reply with our VERSION before VERACK
        if not self._sent_version:
            self.send_version()
        self.send_verack()

    def _on_verack(self, payload: bytes) -> None:
        try:
            decode_verack(payload)
        except ProtocolError as e:
            logger.warning("bad VERACK from %s: %s", self.addr, e)
            self.close()
            return
        if not self._got_version:
            logger.warning("VERACK before VERSION from %s", self.addr)
            self.close()
            return
        self._got_verack = True
        logger.info("VERACK received from %s", self.addr)
        self._maybe_handshaked()

    def _on_ping(self, payload: bytes) -> None:
        try:
            nonce = decode_ping(payload)
        except ProtocolError as e:
            logger.warning("bad PING from %s: %s", self.addr, e)
            self.close()
            return
        logger.info("PING received from %s", self.addr)
        self.send_raw("PONG", encode_pong(nonce))

    def _on_pong(self, payload: bytes) -> None:
        try:
            nonce = decode_pong(payload)
        except ProtocolError as e:
            logger.warning("bad PONG from %s: %s", self.addr, e)
            self.close()
            return
        if self._pending_ping is None or nonce != self._pending_ping:
            logger.warning("unexpected PONG nonce from %s", self.addr)
            # Do not disconnect on mismatch — log only (could be late/stale)
            return
        self._pending_ping = None
        self._ping_sent_at = None
        logger.info("PONG received from %s", self.addr)

    def note_height(self, height: int | None) -> None:
        """Raise best-known advisory tip for this peer (never lowers)."""
        if height is None:
            return
        try:
            h = int(height)
        except (TypeError, ValueError):
            return
        if h < 0:
            return
        if h > self.best_height:
            self.best_height = h

    def _on_status(self, payload: bytes) -> None:
        """Advisory mining status — rate-limited, never disconnects on bad payload."""
        now = time.time()
        if now - self._status_last_accept < 3.0:
            return
        try:
            st = decode_status(payload)
        except ProtocolError as e:
            logger.debug("bad STATUS from %s: %s", self.addr, e)
            return
        self._status_last_accept = now
        self.status_recv_at = now
        self.reported_mining = bool(st.mining)
        self.reported_hps = int(st.hps) if st.mining else 0
        if int(st.height) >= 0:
            self.note_height(st.height)
        logger.debug(
            "STATUS from %s mining=%s hps=%s height=%s",
            self.addr,
            self.reported_mining,
            self.reported_hps,
            st.height,
        )
