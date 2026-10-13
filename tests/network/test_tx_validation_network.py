"""Network TX validation edge cases over TCP."""

from __future__ import annotations

from pathlib import Path

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.network.messages import encode_tx
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from tests.network.conftest import wait_until
from tests.network.relay_helpers import make_payment_tx, make_relay_node, seed_spendable_chain


def test_bad_signature_rejected(tmp_path: Path):
    kp = generate_keypair()
    other = generate_keypair()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    genesis, _ = seed_spendable_chain(dir_a, kp)
    seed_spendable_chain(dir_b, kp)

    a, ca, mpa, _ = make_relay_node(dir_a)
    b, cb, mpb, _ = make_relay_node(dir_b)
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_until(lambda: len(a.handshaked_peers()) >= 1)

        # Sign with wrong key
        from mhcoin.transaction.signing import sign_input

        cb_tx = genesis.transactions[0]
        tx = Transaction(
            inputs=[TxIn(prev_txid=cb_tx.txid(), prev_vout=0, script_sig=b"", sequence=0xFFFFFFFF)],
            outputs=[TxOut.p2pkh(1_000_000, hash160(kp.public_key_compressed))],
        )
        sign_input(tx, 0, other.private_key, other.public_key_compressed, cb_tx.outputs[0].script_pubkey, cb_tx.outputs[0].value)
        a.handshaked_peers()[0].send_raw("TX", encode_tx(tx))
        import time

        time.sleep(0.5)
        assert len(mpa) == 0
        assert len(mpb) == 0
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()


def test_peer_disconnect_during_relay(tmp_path: Path):
    kp = generate_keypair()
    dirs = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    genesis, _ = seed_spendable_chain(dirs[0], kp)
    for d in dirs[1:]:
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

        tx = make_payment_tx(genesis, kp)
        nodes[1][3].accept_local(tx)
        assert wait_until(lambda: nodes[2][2].contains(tx.txid_hex()), timeout=5.0)
        assert nodes[1][2].contains(tx.txid_hex())
    finally:
        for mgr, chain, _, _ in nodes[1:]:
            mgr.stop()
            chain.close()
        nodes[0][1].close()
