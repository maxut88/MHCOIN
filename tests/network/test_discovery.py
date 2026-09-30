"""Stage 7 — ADDR/GETADDR, AddrDB, bans, reconnect / discovery."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.network.addrdb import AddrDB
from mhcoin.network.ban import BanManager, MISBEHAVIOR_PROTOCOL
from mhcoin.network.constants import (
    BAN_SCORE_THRESHOLD,
    MAX_ADDR_ENTRIES,
    MAX_ADDR_HOST_LEN,
)
from mhcoin.network.messages import (
    NetAddress,
    decode_addr,
    decode_getaddr,
    encode_addr,
    encode_getaddr,
)
from mhcoin.network.p2p import P2PConfig, P2PManager
from mhcoin.network.serialization import ProtocolError
from tests.network.conftest import free_port, make_manager, wait_handshaked, wait_until


# --- codecs ----------------------------------------------------------------


def test_getaddr_empty_roundtrip():
    assert encode_getaddr() == b""
    decode_getaddr(b"")
    with pytest.raises(ProtocolError):
        decode_getaddr(b"\x01")


def test_addr_roundtrip():
    addrs = [
        NetAddress(timestamp=1_700_000_000, services=1, host="127.0.0.1", port=18444),
        NetAddress(timestamp=1_700_000_100, services=1, host="10.0.0.2", port=18445),
    ]
    raw = encode_addr(addrs)
    out = decode_addr(raw)
    assert len(out) == 2
    assert out[0].host == "127.0.0.1" and out[0].port == 18444
    assert out[1].key() == "10.0.0.2:18445"


def test_addr_rejects_oversized_count():
    with pytest.raises(ProtocolError):
        encode_addr(
            [
                NetAddress(1, 1, "127.0.0.1", 1 + i)
                for i in range(MAX_ADDR_ENTRIES + 1)
            ]
        )


def test_addr_rejects_bad_host():
    with pytest.raises(ProtocolError):
        encode_addr([NetAddress(1, 1, "a" * (MAX_ADDR_HOST_LEN + 1), 18444)])
    with pytest.raises(ProtocolError):
        decode_addr(encode_addr([NetAddress(1, 1, "ok", 18444)]) + b"\x00")


# --- AddrDB / BanManager ---------------------------------------------------


def test_addrdb_persist_restart(tmp_path: Path):
    path = tmp_path / "peers.sqlite"
    db = AddrDB(path)
    assert db.add("10.1.2.3", 18444, source="manual")
    assert db.count() == 1
    db.close()
    db2 = AddrDB(path)
    rec = db2.get("10.1.2.3", 18444)
    assert rec is not None
    assert rec.source == "manual"
    assert db2.count() == 1
    db2.close()


def test_addrdb_evicts_when_full(tmp_path: Path):
    db = AddrDB(tmp_path / "peers.sqlite", max_entries=5)
    for i in range(8):
        db.add(f"10.0.0.{i}", 18444, source="gossip")
        time.sleep(0.01)
    assert db.count() <= 5
    db.close()


def test_ban_manager_persist(tmp_path: Path):
    path = tmp_path / "bans.json"
    bans = BanManager(path, threshold=50, default_duration=3600)
    bans.ban("9.9.9.9", reason="test")
    assert bans.is_banned("9.9.9.9")
    bans2 = BanManager(path)
    assert bans2.is_banned("9.9.9.9")


def test_misbehavior_reaches_ban(tmp_path: Path):
    bans = BanManager(tmp_path / "bans.json", threshold=30, default_duration=60)
    assert not bans.misbehavior("1.2.3.4", 10, reason="a")
    assert not bans.is_banned("1.2.3.4")
    assert bans.misbehavior("1.2.3.4", 25, reason="b")
    assert bans.is_banned("1.2.3.4")


# --- Real TCP --------------------------------------------------------------


def test_getaddr_addr_exchange(tmp_path: Path):
    a = make_manager(tmp_path / "a")
    b = make_manager(tmp_path / "b")
    # Seed B's addrdb with a third address
    b.addrdb.add("10.9.8.7", 19999, source="manual")
    a.start()
    b.start()
    try:
        a.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(a, 1)
        assert wait_handshaked(b, 1)
        # After handshake, GETADDR/ADDR should store 10.9.8.7 on A
        assert wait_until(lambda: a.addrdb.get("10.9.8.7", 19999) is not None, timeout=5.0)
    finally:
        a.stop()
        b.stop()


def test_three_node_addr_gossip_enables_connect(tmp_path: Path):
    """
    A—B—C. A learns C via ADDR from B, then outbound connects to C.
    """
    a = make_manager(tmp_path / "a", outbound_target=2)
    a.config.reconnect_interval = 0.5
    a.discovery.reconnect_interval = 0.5
    b = make_manager(tmp_path / "b", outbound_target=0)
    c = make_manager(tmp_path / "c", outbound_target=0)
    a.start()
    b.start()
    c.start()
    try:
        a.connect_to_peer("127.0.0.1", b.config.port)
        c.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(b, 2, timeout=8.0)
        # Force A to refresh addresses from B
        for p in a.handshaked_peers():
            a.discovery.on_handshaked(p)
        assert wait_until(
            lambda: a.addrdb.get("127.0.0.1", c.config.port) is not None,
            timeout=8.0,
        )
        # Drive reconnect until A has path to C
        deadline = time.time() + 15.0
        while time.time() < deadline:
            a.discovery.tick()
            if any(
                (p.host == "127.0.0.1" and (p.remote_listen_port or p.port) == c.config.port)
                or (p.port == c.config.port)
                for p in a.handshaked_peers()
            ):
                break
            # Also accept inbound from C side if A connected
            if len(c.handshaked_peers()) >= 2 or len(a.handshaked_peers()) >= 2:
                break
            time.sleep(0.3)
        assert wait_until(
            lambda: len(a.handshaked_peers()) >= 2 or len(c.handshaked_peers()) >= 2,
            timeout=10.0,
        )
    finally:
        a.stop()
        b.stop()
        c.stop()


def test_banned_peer_rejected_on_connect(tmp_path: Path):
    a = make_manager(tmp_path / "a")
    b = make_manager(tmp_path / "b")
    a.bans.ban("127.0.0.1", reason="test-ban", duration=600)
    a.start()
    b.start()
    try:
        with pytest.raises(Exception):
            a.connect_to_peer("127.0.0.1", b.config.port)
    finally:
        a.stop()
        b.stop()


def test_banned_inbound_rejected(tmp_path: Path):
    a = make_manager(tmp_path / "a")
    b = make_manager(tmp_path / "b")
    b.bans.ban("127.0.0.1", reason="inbound-ban", duration=600)
    a.start()
    b.start()
    try:
        # A dials B; B should refuse accept due to ban
        try:
            a.connect_to_peer("127.0.0.1", b.config.port)
        except Exception:
            pass
        time.sleep(0.5)
        assert len(b.handshaked_peers()) == 0
    finally:
        a.stop()
        b.stop()


def test_reconnect_after_disconnect(tmp_path: Path):
    a = make_manager(tmp_path / "a", outbound_target=1)
    a.config.reconnect_interval = 0.4
    a.discovery.reconnect_interval = 0.4
    b = make_manager(tmp_path / "b", outbound_target=0)
    a.discovery.add_manual("127.0.0.1", b.config.port)
    a.start()
    b.start()
    try:
        a.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(a, 1)
        # Drop peer
        for p in list(a.handshaked_peers()):
            p.close()
        assert wait_until(lambda: len(a.handshaked_peers()) == 0, timeout=3.0)
        # Reconnect via discovery tick
        deadline = time.time() + 12.0
        while time.time() < deadline and len(a.handshaked_peers()) < 1:
            a.discovery.tick()
            time.sleep(0.3)
        assert len(a.handshaked_peers()) >= 1
    finally:
        a.stop()
        b.stop()


def test_oversized_addr_from_peer_closes(tmp_path: Path):
    a = make_manager(tmp_path / "a")
    b = make_manager(tmp_path / "b")
    a.start()
    b.start()
    try:
        a.connect_to_peer("127.0.0.1", b.config.port)
        assert wait_handshaked(a, 1)
        peer = a.handshaked_peers()[0]
        # Craft illegal payload: claim huge count via raw bytes
        from mhcoin.transaction.serialization import write_varint

        bad = write_varint(MAX_ADDR_ENTRIES + 50)
        peer.send_raw("ADDR", bad)
        assert wait_until(lambda: len(a.handshaked_peers()) == 0, timeout=5.0)
        # B should have misbehavior score or ban eventually
        assert b.bans.score_of("127.0.0.1") > 0 or b.bans.is_banned("127.0.0.1")
    finally:
        a.stop()
        b.stop()


def test_manual_seed_in_addrdb(tmp_path: Path):
    port = free_port()
    mgr = P2PManager(
        P2PConfig(
            network="localnet",
            host="127.0.0.1",
            port=port,
            data_dir=tmp_path / "n",
            connect_seeds=["10.0.0.5:18444"],
            outbound_target=0,
        )
    )
    rec = mgr.addrdb.get("10.0.0.5", 18444)
    assert rec is not None and rec.source == "manual"
    mgr.stop()


def test_no_dns_seed_constants():
    """Stage 7 must not introduce DNS seed lists."""
    import mhcoin.network.constants as c
    import mhcoin.network.discovery as d

    text = Path(c.__file__).read_text(encoding="utf-8") + Path(d.__file__).read_text(
        encoding="utf-8"
    )
    assert "dnsseed" not in text.lower()
    assert "DNS_SEED" not in text
    assert "seed.bitcoin" not in text.lower()


def test_discovery_status_in_p2p(tmp_path: Path):
    m = make_manager(tmp_path)
    st = m.status()
    assert "known_addrs" in st and "bans" in st and "outbound_target" in st
    m.stop()
