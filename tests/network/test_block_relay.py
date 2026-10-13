"""Two-node BLOCK relay over real TCP."""

from __future__ import annotations

from pathlib import Path

from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.messages import encode_block
from tests.network.conftest import wait_until
from tests.network.relay_helpers import make_relay_node, mine_extending_block, seed_spendable_chain


def test_two_node_block_relay(tmp_path: Path):
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
        assert ca.height == cb.height == 0
        block = mine_extending_block(ca, ma, kp)
        ra.accept_block(block)
        assert wait_until(lambda: cb.height == 1 and cb.tip_hash == ca.tip_hash, timeout=5.0)
        assert ca.tip_hash == block.block_hash()
        assert cb.get_block_by_hash(block.block_hash()) is not None
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()


def test_duplicate_block_no_double_apply(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, mb, rb = make_relay_node(db)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)
        block = mine_extending_block(ca, ma, kp)
        ra.accept_block(block)
        assert wait_until(lambda: cb.height == 1, timeout=5.0)
        # send again
        a.handshaked_peers()[0].send_raw("BLOCK", encode_block(block))
        import time

        time.sleep(0.4)
        assert ca.height == 1 and cb.height == 1
        assert ca.utxo.count() == cb.utxo.count()
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()


def test_malformed_block_peer_survives(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, _, _ = make_relay_node(db)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)
        # corrupt payload with valid envelope via send_raw (checksum of junk)
        a.handshaked_peers()[0].send_raw("BLOCK", b"\x00\x01\x02")
        import time

        time.sleep(0.3)
        # peer may disconnect; node A still up — mine locally
        block = mine_extending_block(ca, ma, kp)
        ra.accept_block(block)
        assert ca.height == 1
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()
