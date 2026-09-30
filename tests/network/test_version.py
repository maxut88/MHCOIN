"""VERSION payload encode/decode tests."""

from __future__ import annotations

import pytest

from mhcoin.network.messages import (
    VersionPayload,
    decode_version,
    encode_version,
)
from mhcoin.network.serialization import ProtocolError


def _sample(**kwargs) -> VersionPayload:
    base = dict(
        protocol_version=1,
        services=1,
        timestamp=1_700_000_000,
        nonce=0xABCDEF0123456789,
        start_height=0,
        network="localnet",
        listen_port=18444,
        software_version="0.2.0",
    )
    base.update(kwargs)
    return VersionPayload(**base)


def test_version_roundtrip():
    v = _sample()
    raw = encode_version(v)
    out = decode_version(raw)
    assert out == v


def test_version_fields_present():
    raw = encode_version(_sample(start_height=42, listen_port=18445))
    out = decode_version(raw)
    assert out.start_height == 42
    assert out.listen_port == 18445
    assert out.network == "localnet"


def test_malformed_version_truncated():
    with pytest.raises(ProtocolError):
        decode_version(b"\x00" * 10)


def test_malformed_version_trailing():
    raw = encode_version(_sample()) + b"\xff"
    with pytest.raises(ProtocolError):
        decode_version(raw)


def test_empty_network_rejected():
    with pytest.raises(ProtocolError):
        encode_version(_sample(network=""))


def test_unknown_network_encode_rejected():
    with pytest.raises(ProtocolError):
        encode_version(_sample(network="notanet"))


def test_unknown_network_decode_rejected():
    # craft valid layout with bogus network name
    from mhcoin.transaction.serialization import (
        write_i32,
        write_i64,
        write_u16,
        write_u32,
        write_u64,
        write_varint,
    )

    net = b"bogusnet"
    soft = b"0.2.0"
    payload = (
        write_u32(1)
        + write_u64(1)
        + write_i64(0)
        + write_u64(1)
        + write_i32(0)
        + write_varint(len(net))
        + net
        + write_u16(18444)
        + write_varint(len(soft))
        + soft
    )
    with pytest.raises(ProtocolError):
        decode_version(payload)


def test_oversized_software_rejected():
    with pytest.raises(ProtocolError):
        encode_version(_sample(software_version="x" * 200))
