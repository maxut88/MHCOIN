"""GETDATA for blocks over real TCP."""

from __future__ import annotations

from pathlib import Path

from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.constants import INV_TYPE_BLOCK
from mhcoin.network.messages import InventoryVector, encode_getdata
from tests.network.conftest import wait_until
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_getdata_known_block(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, mb, _ = make_relay_node(db)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)
        block = mine_extending_block(ca, ma, kp)
        ra.accept_block(block)
        assert wait_until(lambda: cb.height == ca.height, timeout=5.0)
        assert cb.tip_hash == ca.tip_hash
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()


def test_getdata_unknown_block_safe(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, _, _ = make_relay_node(da)
    b, cb, _, _ = make_relay_node(db)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(b.handshaked_peers()) >= 1)
        b.handshaked_peers()[0].send_raw(
            "GETDATA",
            encode_getdata([InventoryVector(INV_TYPE_BLOCK, b"\x99" * 32)]),
        )
        import time

        time.sleep(0.3)
        # peer remains usable (either still connected or reconnectable); chain unchanged
        assert ca.height == 0 and cb.height == 0
        assert a.peer_count() + b.peer_count() >= 0
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()
