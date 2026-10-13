"""VERSION/VERACK handshake over real TCP."""

from __future__ import annotations

import time

import pytest

from mhcoin.network.messages import encode_verack, encode_version, VersionPayload
from mhcoin.network.peer import PeerState
from mhcoin.network.serialization import ProtocolError, encode_envelope
from mhcoin.network.constants import NETWORK_MAGIC, PROTOCOL_VERSION, SERVICES_NODE_NETWORK
from tests.network.conftest import make_manager, wait_handshaked, wait_until


def test_two_node_handshake():
    a = make_manager()
    b = make_manager()
    a.start()
    b.start()
    try:
        peer = b.connect_to_peer("127.0.0.1", a.config.port)
        assert peer.state == PeerState.HANDSHAKED
        assert wait_handshaked(a, 1)
        assert a.handshaked_peers()[0].state == PeerState.HANDSHAKED
        assert b.handshaked_peers()[0].state == PeerState.HANDSHAKED
    finally:
        a.stop()
        b.stop()


def test_wrong_network_rejected():
    a = make_manager(network="localnet")
    b = make_manager(network="testnet")
    a.start()
    b.start()
    try:
        with pytest.raises(ProtocolError):
            b.connect_to_peer("127.0.0.1", a.config.port)
        # A may briefly see a peer then drop it
        time.sleep(0.3)
        assert len(a.handshaked_peers()) == 0
    finally:
        a.stop()
        b.stop()


def test_self_connection_own_port_rejected():
    a = make_manager()
    a.start()
    try:
        with pytest.raises(ProtocolError):
            a.connect_to_peer("127.0.0.1", a.config.port)
    finally:
        a.stop()


def test_self_connection_same_nonce():
    """Two managers forced to share nonce — handshake must fail."""
    a = make_manager()
    b = make_manager()
    a.our_nonce = 0x1111222233334444
    b.our_nonce = 0x1111222233334444
    a.start()
    b.start()
    try:
        with pytest.raises(ProtocolError):
            b.connect_to_peer("127.0.0.1", a.config.port)
    finally:
        a.stop()
        b.stop()


def test_handshake_timeout():
    """Connect TCP but never send VERSION — peer should time out."""
    import socket

    a = make_manager(handshake_timeout=0.5)
    a.start()
    try:
        sock = socket.create_connection(("127.0.0.1", a.config.port), timeout=2)
        # do not send VERSION; wait for server to drop
        time.sleep(1.2)
        # peer should be gone
        assert wait_until(lambda: a.peer_count() == 0, timeout=2.0)
        sock.close()
    finally:
        a.stop()


def test_malformed_verack_disconnects():
    import socket
    import threading

    a = make_manager()
    a.start()
    try:
        sock = socket.create_connection(("127.0.0.1", a.config.port), timeout=2)
        magic = NETWORK_MAGIC["localnet"]
        vp = VersionPayload(
            protocol_version=PROTOCOL_VERSION,
            services=SERVICES_NODE_NETWORK,
            timestamp=int(time.time()),
            nonce=0x9999888877776666,
            start_height=0,
            network="localnet",
            listen_port=9999,
            software_version="test",
        )
        sock.sendall(encode_envelope(magic, "VERSION", encode_version(vp)))
        # wait for their VERSION + VERACK
        time.sleep(0.2)
        # send bad VERACK (non-empty payload)
        sock.sendall(encode_envelope(magic, "VERACK", b"nope"))
        time.sleep(0.5)
        assert wait_until(lambda: a.peer_count() == 0, timeout=3.0)
        sock.close()
    finally:
        a.stop()


def test_wrong_protocol_rejected():
    import socket

    a = make_manager()
    a.start()
    try:
        sock = socket.create_connection(("127.0.0.1", a.config.port), timeout=2)
        magic = NETWORK_MAGIC["localnet"]
        vp = VersionPayload(
            protocol_version=99,
            services=1,
            timestamp=int(time.time()),
            nonce=0xAABBCCDDEEFF0011,
            start_height=0,
            network="localnet",
            listen_port=1,
            software_version="x",
        )
        sock.sendall(encode_envelope(magic, "VERSION", encode_version(vp)))
        time.sleep(0.5)
        assert wait_until(lambda: a.peer_count() == 0, timeout=3.0)
        sock.close()
    finally:
        a.stop()
