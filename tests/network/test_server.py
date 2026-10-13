"""TCP server limits and peer management."""

from __future__ import annotations

import time

import pytest

from mhcoin.network.serialization import ProtocolError
from tests.network.conftest import make_manager, wait_handshaked, wait_until


def test_server_listen_and_accept():
    a = make_manager()
    b = make_manager()
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        assert a.peer_count() == 1
        assert b.peer_count() == 1
    finally:
        a.stop()
        b.stop()


def test_maximum_peers():
    a = make_manager(max_peers=1)
    b = make_manager()
    c = make_manager()
    a.start()
    b.start()
    c.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        with pytest.raises(ProtocolError):
            c.connect_to_peer("127.0.0.1", a.config.port)
        assert a.peer_count() == 1
    finally:
        a.stop()
        b.stop()
        c.stop()


def test_duplicate_outbound_rejected():
    a = make_manager()
    b = make_manager()
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(b, 1)
        with pytest.raises(ProtocolError):
            b.connect_to_peer("127.0.0.1", a.config.port)
    finally:
        a.stop()
        b.stop()


def test_peer_disconnect():
    a = make_manager()
    b = make_manager()
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        # stop B — A should drop peer
        b.stop()
        assert wait_until(lambda: a.peer_count() == 0, timeout=3.0)
        assert a.peer_count() == 0
    finally:
        a.stop()
