"""Real multi-node localhost TCP scenario (Stage 2)."""

from __future__ import annotations

import socket
import time
from pathlib import Path

from mhcoin.network.peer import PeerState
from mhcoin.node.runtime import NodeRuntime
from tests.network.conftest import free_port, make_manager, wait_handshaked, wait_until


def test_three_node_handshake_topology():
    """
    A listen; B->A; C->B.
    Verify A<->B and B<->C (C not required to see A).
    """
    a = make_manager()
    b = make_manager()
    c = make_manager()
    a.start()
    b.start()
    c.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(a, 1)
        assert wait_handshaked(b, 1)

        c.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(b, 2)
        assert wait_handshaked(c, 1)

        assert len(a.handshaked_peers()) == 1
        assert len(b.handshaked_peers()) == 2
        assert len(c.handshaked_peers()) == 1

        # PING A -> B
        peer_on_a = a.handshaked_peers()[0]
        peer_on_a.send_ping(0x1234567890ABCDEF)
        assert wait_until(lambda: peer_on_a._pending_ping is None, timeout=3.0)
    finally:
        a.stop()
        b.stop()
        c.stop()


def test_stop_one_node_others_remain():
    a = make_manager()
    b = make_manager()
    c = make_manager()
    a.start()
    b.start()
    c.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        c.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(b, 2)
        a.stop()
        assert wait_until(lambda: len(b.handshaked_peers()) == 1, timeout=3.0)
        # B-C still up
        assert len(c.handshaked_peers()) == 1
        assert c.handshaked_peers()[0].state == PeerState.HANDSHAKED
        peer_b = b.handshaked_peers()[0]
        peer_b.send_ping(55)
        assert wait_until(lambda: peer_b._pending_ping is None, timeout=3.0)
    finally:
        b.stop()
        c.stop()


def test_restart_and_reconnect(tmp_path: Path):
    """Restart node A and reconnect A->B."""
    port_a = free_port()
    port_b = free_port()
    dir_a = tmp_path / "node_a"
    dir_b = tmp_path / "node_b"

    def port_free(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    rt_a = NodeRuntime(
        data_dir=dir_a, network="localnet", host="127.0.0.1", port=port_a, max_peers=8
    )
    rt_b = NodeRuntime(
        data_dir=dir_b, network="localnet", host="127.0.0.1", port=port_b, max_peers=8
    )
    rt_a.start(blocking=False)
    rt_b.start(blocking=False)
    try:
        rt_b.p2p.connect_to_peer("127.0.0.1", port_a)
        assert wait_handshaked(rt_a.p2p, 1)
        assert wait_handshaked(rt_b.p2p, 1)

        rt_a.stop()
        assert wait_until(lambda: rt_b.p2p.peer_count() == 0, timeout=3.0)
        assert wait_until(lambda: port_free(port_a), timeout=5.0)

        # restart A
        rt_a = NodeRuntime(
            data_dir=dir_a, network="localnet", host="127.0.0.1", port=port_a, max_peers=8
        )
        rt_a.start(blocking=False)
        time.sleep(0.15)
        rt_a.p2p.connect_to_peer("127.0.0.1", port_b)
        assert wait_handshaked(rt_a.p2p, 1, timeout=5.0)
        assert wait_handshaked(rt_b.p2p, 1, timeout=5.0)
    finally:
        rt_a.stop()
        rt_b.stop()


def test_fixed_ports_scenario_18444_family():
    """Prefer classic localnet ports when free; else skip-equivalent via free ports."""
    import socket

    def try_bind(port: int) -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            s.close()

    ports = (18444, 18445, 18446)
    if all(try_bind(p) for p in ports):
        pa, pb, pc = ports
    else:
        pa, pb, pc = free_port(), free_port(), free_port()

    a = make_manager(port=pa)
    b = make_manager(port=pb)
    c = make_manager(port=pc)
    a.start()
    b.start()
    c.start()
    try:
        b.connect_to_peer("127.0.0.1", pa)
        assert wait_handshaked(a, 1) and wait_handshaked(b, 1)
        c.connect_to_peer("127.0.0.1", pb)
        assert wait_handshaked(b, 2) and wait_handshaked(c, 1)
        assert a.peer_count() == 1
        assert b.peer_count() == 2
        assert c.peer_count() == 1
    finally:
        a.stop()
        b.stop()
        c.stop()
