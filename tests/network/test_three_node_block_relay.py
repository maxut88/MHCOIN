"""Three-node BLOCK tip relay + restart."""

from __future__ import annotations

import time
from pathlib import Path

from mhcoin.crypto.keys import generate_keypair
from mhcoin.node.runtime import NodeRuntime
from tests.network.conftest import free_port, wait_handshaked, wait_until
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_three_node_block_relay(tmp_path: Path):
    kp = generate_keypair()
    da, db, dc = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    seed_spendable_chain(dc, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, mb, _ = make_relay_node(db)
    c, cc, mc, _ = make_relay_node(dc)
    a.start()
    b.start()
    c.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        c.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_until(lambda: len(b.handshaked_peers()) >= 2, timeout=5.0)
        block = mine_extending_block(ca, ma, kp)
        ra.accept_block(block)
        assert wait_until(
            lambda: ca.height == cb.height == cc.height == 1
            and ca.tip_hash == cb.tip_hash == cc.tip_hash,
            timeout=8.0,
        )
    finally:
        a.stop()
        b.stop()
        c.stop()
        ca.close()
        cb.close()
        cc.close()


def test_peer_disconnect_during_block_relay(tmp_path: Path):
    kp = generate_keypair()
    dirs = [tmp_path / x for x in ("a", "b", "c")]
    for d in dirs:
        seed_spendable_chain(d, kp)
    nodes = [make_relay_node(d) for d in dirs]
    for mgr, _, _, _ in nodes:
        mgr.start()
    try:
        nodes[1][0].connect_to_peer("127.0.0.1", nodes[0][0].config.port)
        nodes[2][0].connect_to_peer("127.0.0.1", nodes[1][0].config.port)
        assert wait_until(lambda: len(nodes[1][0].handshaked_peers()) >= 2)
        nodes[0][0].stop()
        assert wait_until(lambda: len(nodes[1][0].handshaked_peers()) == 1, timeout=3.0)
        block = mine_extending_block(nodes[1][1], nodes[1][2], kp)
        nodes[1][3].accept_block(block)
        assert wait_until(lambda: nodes[2][1].height == 1, timeout=5.0)
        assert nodes[1][1].tip_hash == nodes[2][1].tip_hash
    finally:
        for mgr, chain, _, _ in nodes[1:]:
            mgr.stop()
            chain.close()
        nodes[0][1].close()


def test_block_relay_after_restart(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    pa, pb = free_port(), free_port()
    rt_a = NodeRuntime(data_dir=da, network="localnet", host="127.0.0.1", port=pa)
    rt_b = NodeRuntime(data_dir=db, network="localnet", host="127.0.0.1", port=pb)
    rt_a.start(blocking=False)
    rt_b.start(blocking=False)
    try:
        rt_b.p2p.connect_to_peer("127.0.0.1", pa)
        assert wait_handshaked(rt_a.p2p, 1)
        block = mine_extending_block(rt_a.chain, rt_a.mempool, kp)
        rt_a.accept_block(block)
        assert wait_until(lambda: rt_b.chain.height == 1, timeout=5.0)

        rt_a.stop()
        assert wait_until(lambda: rt_b.p2p.peer_count() == 0, timeout=3.0)

        def port_free(port: int) -> bool:
            import socket

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False
            finally:
                s.close()

        assert wait_until(lambda: port_free(pa), timeout=5.0)
        rt_a = NodeRuntime(data_dir=da, network="localnet", host="127.0.0.1", port=pa)
        rt_a.start(blocking=False)
        time.sleep(0.2)
        rt_a.p2p.connect_to_peer("127.0.0.1", pb)
        assert wait_handshaked(rt_a.p2p, 1, timeout=5.0)
        assert rt_a.chain.height == 1 and rt_b.chain.height == 1
        block2 = mine_extending_block(rt_b.chain, rt_b.mempool, kp)
        rt_b.accept_block(block2)
        assert wait_until(lambda: rt_a.chain.height == 2, timeout=5.0)
        assert rt_a.chain.tip_hash == rt_b.chain.tip_hash
    finally:
        rt_a.stop()
        rt_b.stop()
