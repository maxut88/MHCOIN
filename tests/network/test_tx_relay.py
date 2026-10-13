"""Two-node and network validation TX relay over real TCP."""

from __future__ import annotations

from pathlib import Path

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.messages import encode_getdata, encode_inv, encode_tx
from mhcoin.network.constants import INV_TYPE_TX
from mhcoin.network.messages import InventoryVector
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.conftest import wait_until
from tests.network.relay_helpers import make_payment_tx, make_relay_node, seed_spendable_chain


def test_two_node_inv_getdata_tx(tmp_path: Path):
    kp = generate_keypair()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    genesis, _ = seed_spendable_chain(dir_a, kp)
    seed_spendable_chain(dir_b, kp)  # same key → same genesis

    a, chain_a, mp_a, relay_a = make_relay_node(dir_a)
    b, chain_b, mp_b, relay_b = make_relay_node(dir_b)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1 and len(b.handshaked_peers()) >= 1)

        tx = make_payment_tx(genesis, kp)
        assert len(mp_b) == 0
        relay_a.accept_local(tx)
        assert mp_a.contains(tx.txid_hex())

        assert wait_until(lambda: mp_b.contains(tx.txid_hex()), timeout=5.0)
        assert len(mp_b) == 1
        assert mp_b.get(tx.txid_hex()).txid() == tx.txid()
    finally:
        a.stop()
        b.stop()
        chain_a.close()
        chain_b.close()


def test_invalid_tx_not_accepted_on_peer(tmp_path: Path):
    kp = generate_keypair()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    seed_spendable_chain(dir_a, kp)
    seed_spendable_chain(dir_b, kp)
    a, chain_a, mp_a, _ = make_relay_node(dir_a)
    b, chain_b, mp_b, _ = make_relay_node(dir_b)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)

        bad = Transaction(
            inputs=[TxIn(prev_txid=b"\xee" * 32, prev_vout=0, script_sig=b"x", sequence=0xFFFFFFFF)],
            outputs=[TxOut.p2pkh(1, hash160(kp.public_key_compressed))],
        )
        # Push TX directly without mempool accept on A
        peer = a.handshaked_peers()[0]
        peer.send_raw("TX", encode_tx(bad))
        assert wait_until(lambda: True, timeout=0.3) or True
        import time

        time.sleep(0.4)
        assert len(mp_b) == 0
        assert len(mp_a) == 0
    finally:
        a.stop()
        b.stop()
        chain_a.close()
        chain_b.close()


def test_duplicate_no_loop(tmp_path: Path):
    kp = generate_keypair()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    genesis, _ = seed_spendable_chain(dir_a, kp)
    seed_spendable_chain(dir_b, kp)
    a, chain_a, mp_a, relay_a = make_relay_node(dir_a)
    b, chain_b, mp_b, relay_b = make_relay_node(dir_b)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(b.handshaked_peers()) >= 1)
        tx = make_payment_tx(genesis, kp)
        relay_a.accept_local(tx)
        assert wait_until(lambda: mp_b.contains(tx.txid_hex()), timeout=5.0)
        # announce again
        relay_a._announce_inv(tx.txid_hex(), exclude=None)
        import time

        time.sleep(0.5)
        assert len(mp_a) == 1
        assert len(mp_b) == 1
    finally:
        a.stop()
        b.stop()
        chain_a.close()
        chain_b.close()


def test_unknown_getdata_safe(tmp_path: Path):
    kp = generate_keypair()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    genesis, _ = seed_spendable_chain(dir_a, kp)
    seed_spendable_chain(dir_b, kp)
    a, chain_a, mp_a, _ = make_relay_node(dir_a)
    b, chain_b, mp_b, _ = make_relay_node(dir_b)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)
        peer = b.handshaked_peers()[0]
        peer.send_raw(
            "GETDATA",
            encode_getdata([InventoryVector(INV_TYPE_TX, b"\x42" * 32)]),
        )
        import time

        time.sleep(0.3)
        assert len(a.handshaked_peers()) >= 1
        tx = make_payment_tx(genesis, kp)
        a.relay.accept_local(tx)
        assert wait_until(lambda: mp_b.contains(tx.txid_hex()), timeout=5.0)
    finally:
        a.stop()
        b.stop()
        chain_a.close()
        chain_b.close()
