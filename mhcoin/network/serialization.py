"""Canonical P2P message envelope serialization.

Wire layout (little-endian length; checksum = first 4 bytes of HASH256(payload)):

  Offset  Size  Field
  0       4     magic
  4       12    command (ASCII, NUL-padded)
  16      4     payload length (uint32 LE)
  20      4     checksum
  24      N     payload

No pickle / JSON / eval. All fields explicit.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from mhcoin.crypto.hashing import hash256
from mhcoin.network.constants import (
    COMMAND_SIZE,
    HEADER_SIZE,
    MAGIC_SIZE,
    MAX_PAYLOAD_SIZE,
    NETWORK_MAGIC,
)


class ProtocolError(ValueError):
    """Malformed, oversized, or checksum-invalid network message."""


@dataclass(frozen=True)
class NetMessage:
    command: str
    payload: bytes

    def __post_init__(self) -> None:
        if not self.command or not self.command.isascii():
            raise ProtocolError("command must be non-empty ASCII")
        if len(self.command.encode("ascii")) > COMMAND_SIZE:
            raise ProtocolError("command too long")
        if not isinstance(self.payload, (bytes, bytearray)):
            raise ProtocolError("payload must be bytes")


def payload_checksum(payload: bytes) -> bytes:
    return hash256(payload)[:4]


def _pad_command(cmd: str) -> bytes:
    raw = cmd.encode("ascii")
    if len(raw) > COMMAND_SIZE:
        raise ProtocolError("command too long")
    if not raw:
        raise ProtocolError("empty command")
    return raw.ljust(COMMAND_SIZE, b"\x00")


def encode_envelope(magic: bytes, command: str, payload: bytes = b"") -> bytes:
    """Serialize one MHCOIN P2P message."""
    if not isinstance(magic, (bytes, bytearray)) or len(magic) != MAGIC_SIZE:
        raise ProtocolError("magic must be exactly 4 bytes")
    if not isinstance(payload, (bytes, bytearray)):
        raise ProtocolError("payload must be bytes")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ProtocolError(f"payload exceeds MAX_PAYLOAD_SIZE ({MAX_PAYLOAD_SIZE})")
    cmd = _pad_command(command)
    length = struct.pack("<I", len(payload))
    checksum = payload_checksum(payload)
    return bytes(magic) + cmd + length + checksum + payload


def decode_envelope(magic: bytes, data: bytes) -> NetMessage:
    """Decode exactly one complete message (no trailing bytes allowed)."""
    msg, rest = try_decode_envelope(magic, data)
    if msg is None:
        raise ProtocolError("incomplete message")
    if rest:
        raise ProtocolError("trailing bytes after message")
    return msg


def try_decode_envelope(magic: bytes, buf: bytes) -> tuple[NetMessage | None, bytes]:
    """
    Try to parse one message from the front of buf.
    Returns (None, buf) if more bytes are needed.
    Raises ProtocolError on hard protocol violations.
    """
    if not isinstance(magic, (bytes, bytearray)) or len(magic) != MAGIC_SIZE:
        raise ProtocolError("magic must be exactly 4 bytes")
    if len(buf) < HEADER_SIZE:
        return None, buf
    if buf[:MAGIC_SIZE] != magic:
        raise ProtocolError(f"wrong network magic: got {buf[:MAGIC_SIZE].hex()}")
    cmd_raw = buf[MAGIC_SIZE : MAGIC_SIZE + COMMAND_SIZE]
    if b"\x00" in cmd_raw:
        command = cmd_raw.split(b"\x00", 1)[0].decode("ascii")
    else:
        command = cmd_raw.decode("ascii")
    if not command:
        raise ProtocolError("empty command")
    try:
        command.encode("ascii")
    except UnicodeError as e:
        raise ProtocolError("non-ASCII command") from e
    length = struct.unpack("<I", buf[16:20])[0]
    if length > MAX_PAYLOAD_SIZE:
        raise ProtocolError("payload length exceeds maximum")
    checksum = buf[20:24]
    total = HEADER_SIZE + length
    if len(buf) < total:
        return None, buf
    payload = bytes(buf[HEADER_SIZE:total])
    if payload_checksum(payload) != checksum:
        raise ProtocolError("checksum mismatch")
    return NetMessage(command=command, payload=payload), bytes(buf[total:])


def magic_for_network(network: str) -> bytes:
    try:
        return NETWORK_MAGIC[network]
    except KeyError as e:
        raise ProtocolError(f"unknown network: {network}") from e


# Aliases used by later stages / existing draft code
encode_message = encode_envelope
try_decode_message = try_decode_envelope
