"""Peer state machine unit tests."""

from __future__ import annotations

import socket

import pytest

from mhcoin.network.constants import NETWORK_MAGIC
from mhcoin.network.peer import Peer, PeerState
from mhcoin.network.serialization import ProtocolError


def _pair():
    a, b = socket.socketpair()
    return a, b


def test_peer_initial_state():
    a, b = _pair()
    try:
        p = Peer(
            a,
            magic=NETWORK_MAGIC["localnet"],
            network="localnet",
            inbound=True,
            our_nonce=1,
            our_listen_port=18444,
        )
        assert p.state == PeerState.CONNECTED
        assert p.inbound is True
        info = p.info()
        assert info.state == "CONNECTED"
    finally:
        a.close()
        b.close()


def test_invalid_transition_raises():
    a, b = _pair()
    try:
        p = Peer(
            a,
            magic=NETWORK_MAGIC["localnet"],
            network="localnet",
            inbound=False,
            our_nonce=1,
            our_listen_port=18444,
        )
        with pytest.raises(ProtocolError):
            p._set_state(PeerState.HANDSHAKED)
    finally:
        a.close()
        b.close()


def test_closing_always_allowed():
    a, b = _pair()
    try:
        p = Peer(
            a,
            magic=NETWORK_MAGIC["localnet"],
            network="localnet",
            inbound=False,
            our_nonce=1,
            our_listen_port=18444,
        )
        p._set_state(PeerState.VERSION_SENT)
        p.close()
        assert p.state == PeerState.DISCONNECTED
    finally:
        try:
            b.close()
        except OSError:
            pass


def test_peer_no_wallet_attrs():
    a, b = _pair()
    try:
        p = Peer(
            a,
            magic=NETWORK_MAGIC["localnet"],
            network="localnet",
            inbound=True,
            our_nonce=99,
            our_listen_port=18444,
        )
        assert not hasattr(p, "wallet")
        assert not hasattr(p, "private_key")
        assert "password" not in dir(p)
    finally:
        a.close()
        b.close()
