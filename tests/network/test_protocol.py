"""Stage 1 — P2P message envelope tests."""

import pytest

from mhcoin.crypto.hashing import hash256
from mhcoin.network.constants import HEADER_SIZE, MAX_PAYLOAD_SIZE, NETWORK_MAGIC
from mhcoin.network.serialization import (
    NetMessage,
    ProtocolError,
    decode_envelope,
    encode_envelope,
    magic_for_network,
    payload_checksum,
    try_decode_envelope,
)


def test_magic_values_are_mhcoin_specific():
    # Must not equal Bitcoin mainnet magic F9BEB4D9 (LE wire often shown as D9B4BEF9)
    assert NETWORK_MAGIC["mainnet"] != bytes.fromhex("F9BEB4D9")
    assert NETWORK_MAGIC["mainnet"] != bytes.fromhex("D9B4BEF9")
    assert NETWORK_MAGIC["mainnet"] == b"MHCN"
    assert NETWORK_MAGIC["localnet"] == b"MHLN"
    assert NETWORK_MAGIC["testnet"] == b"MHTN"
    assert NETWORK_MAGIC["regtest"] == b"MHRN"
    for m in NETWORK_MAGIC.values():
        assert len(m) == 4


def test_encode_decode_roundtrip():
    magic = magic_for_network("localnet")
    payload = b"hello-mhcoin"
    raw = encode_envelope(magic, "ping", payload)
    assert len(raw) == HEADER_SIZE + len(payload)
    msg = decode_envelope(magic, raw)
    assert msg.command == "ping"
    assert msg.payload == payload


def test_deterministic_encoding():
    magic = magic_for_network("regtest")
    a = encode_envelope(magic, "verack", b"")
    b = encode_envelope(magic, "verack", b"")
    assert a == b


def test_checksum_is_hash256_prefix():
    payload = b"abc"
    assert payload_checksum(payload) == hash256(payload)[:4]
    magic = magic_for_network("localnet")
    raw = encode_envelope(magic, "tx", payload)
    assert raw[20:24] == payload_checksum(payload)


def test_wrong_magic_rejected():
    raw = encode_envelope(magic_for_network("localnet"), "ping", b"x")
    with pytest.raises(ProtocolError, match="magic"):
        decode_envelope(magic_for_network("testnet"), raw)


def test_bad_checksum_rejected():
    magic = magic_for_network("localnet")
    raw = bytearray(encode_envelope(magic, "ping", b"payload"))
    raw[20] ^= 0xFF
    with pytest.raises(ProtocolError, match="checksum"):
        decode_envelope(magic, bytes(raw))


def test_oversized_payload_rejected_on_encode():
    magic = magic_for_network("localnet")
    with pytest.raises(ProtocolError, match="MAX_PAYLOAD_SIZE"):
        encode_envelope(magic, "block", b"\x00" * (MAX_PAYLOAD_SIZE + 1))


def test_oversized_length_rejected_on_decode():
    magic = magic_for_network("localnet")
    # Craft header claiming huge length
    cmd = b"ping".ljust(12, b"\x00")
    length = (MAX_PAYLOAD_SIZE + 1).to_bytes(4, "little")
    checksum = b"\x00" * 4
    buf = magic + cmd + length + checksum
    with pytest.raises(ProtocolError, match="maximum"):
        try_decode_envelope(magic, buf)


def test_incomplete_buffer_returns_none():
    magic = magic_for_network("localnet")
    raw = encode_envelope(magic, "ping", b"12345")
    msg, rest = try_decode_envelope(magic, raw[:10])
    assert msg is None
    assert rest == raw[:10]


def test_streaming_two_messages():
    magic = magic_for_network("localnet")
    m1 = encode_envelope(magic, "ping", b"A")
    m2 = encode_envelope(magic, "pong", b"B")
    buf = m1 + m2
    msg1, buf = try_decode_envelope(magic, buf)
    msg2, buf = try_decode_envelope(magic, buf)
    assert msg1 == NetMessage("ping", b"A")
    assert msg2 == NetMessage("pong", b"B")
    assert buf == b""


def test_empty_command_rejected():
    magic = magic_for_network("localnet")
    with pytest.raises(ProtocolError):
        encode_envelope(magic, "", b"")


def test_command_too_long_rejected():
    magic = magic_for_network("localnet")
    with pytest.raises(ProtocolError, match="too long"):
        encode_envelope(magic, "x" * 13, b"")


def test_trailing_bytes_on_decode_envelope():
    magic = magic_for_network("localnet")
    raw = encode_envelope(magic, "ping", b"z") + b"\x00"
    with pytest.raises(ProtocolError, match="trailing"):
        decode_envelope(magic, raw)


def test_unknown_network():
    with pytest.raises(ProtocolError, match="unknown network"):
        magic_for_network("not-a-network")
