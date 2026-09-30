"""PING/PONG over real TCP after handshake."""

from __future__ import annotations

import time

from mhcoin.network.peer import PeerState
from tests.network.conftest import make_manager, wait_handshaked, wait_until


def test_ping_pong():
    a = make_manager(ping_interval=3600)
    b = make_manager(ping_interval=3600)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        peer_a = a.handshaked_peers()[0]
        nonce = peer_a.send_ping(0xDEADBEEFCAFEBABE)
        assert nonce == 0xDEADBEEFCAFEBABE
        assert wait_until(lambda: peer_a._pending_ping is None, timeout=3.0)
    finally:
        a.stop()
        b.stop()


def test_wrong_pong_nonce_logged_not_cleared():
    """Unexpected PONG does not clear outstanding ping."""
    a = make_manager(ping_interval=3600)
    b = make_manager(ping_interval=3600)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        peer_a = a.handshaked_peers()[0]
        peer_a.send_ping(111)
        # inject wrong pong via raw
        from mhcoin.network.messages import encode_pong

        peer_a.send_raw("PONG", encode_pong(222))  # we send to remote — wrong direction
        # instead simulate local receive of wrong pong
        peer_a._on_pong(encode_pong(222))
        assert peer_a._pending_ping == 111
        # correct pong clears
        peer_a._on_pong(encode_pong(111))
        assert peer_a._pending_ping is None
    finally:
        a.stop()
        b.stop()


def test_ping_before_handshake_rejected():
    import pytest
    from mhcoin.network.serialization import ProtocolError
    from mhcoin.network.constants import NETWORK_MAGIC
    import socket

    a, b = socket.socketpair()
    try:
        from mhcoin.network.peer import Peer

        p = Peer(
            a,
            magic=NETWORK_MAGIC["localnet"],
            network="localnet",
            inbound=False,
            our_nonce=1,
            our_listen_port=1,
        )
        with pytest.raises(ProtocolError):
            p.send_ping()
    finally:
        a.close()
        b.close()
