"""Canonical binary encoding helpers (MHCOIN wire format).

Byte order: little-endian for all multi-byte integers.
Varints: compact size encoding (MHCOIN-owned; not Bitcoin Core source).

Transaction layout:

  version:        uint32 LE
  input_count:    varint
  inputs[]:
    prev_txid:    32 bytes (HASH256 of previous tx)
    prev_vout:    uint32 LE
    script_sig:   varint length + bytes
    sequence:     uint32 LE
  output_count:   varint
  outputs[]:
    value:        uint64 LE  (smallest MHC units; never float)
    script_pubkey: varint length + bytes
  locktime:       uint32 LE
"""

from __future__ import annotations


class SerializationError(ValueError):
    """Malformed or out-of-range binary payload."""


def write_u8(n: int) -> bytes:
    if not 0 <= n <= 0xFF:
        raise SerializationError("u8 out of range")
    return bytes([n])


def write_u16(n: int) -> bytes:
    if not 0 <= n <= 0xFFFF:
        raise SerializationError("u16 out of range")
    return int(n).to_bytes(2, "little")


def write_u32(n: int) -> bytes:
    if not 0 <= n <= 0xFFFFFFFF:
        raise SerializationError("u32 out of range")
    return int(n).to_bytes(4, "little")


def write_i32(n: int) -> bytes:
    if not -0x80000000 <= n <= 0x7FFFFFFF:
        raise SerializationError("i32 out of range")
    return int(n).to_bytes(4, "little", signed=True)


def write_i64(n: int) -> bytes:
    if not -0x8000000000000000 <= n <= 0x7FFFFFFFFFFFFFFF:
        raise SerializationError("i64 out of range")
    return int(n).to_bytes(8, "little", signed=True)


def write_u64(n: int) -> bytes:
    if not 0 <= n <= 0xFFFFFFFFFFFFFFFF:
        raise SerializationError("u64 out of range")
    return int(n).to_bytes(8, "little")


def write_varint(n: int) -> bytes:
    if n < 0:
        raise SerializationError("negative varint")
    if n < 0xFD:
        return write_u8(n)
    if n <= 0xFFFF:
        return b"\xfd" + int(n).to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + int(n).to_bytes(4, "little")
    return b"\xff" + int(n).to_bytes(8, "little")


def write_bytes(data: bytes) -> bytes:
    return write_varint(len(data)) + data


def write_hash32(h: bytes) -> bytes:
    if len(h) != 32:
        raise SerializationError("hash must be 32 bytes")
    return h


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0

    def remaining(self) -> int:
        return len(self.data) - self.i

    def read(self, n: int) -> bytes:
        if self.i + n > len(self.data):
            raise SerializationError("unexpected EOF")
        out = self.data[self.i : self.i + n]
        self.i += n
        return out

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.read(2), "little")

    def u32(self) -> int:
        return int.from_bytes(self.read(4), "little")

    def i32(self) -> int:
        return int.from_bytes(self.read(4), "little", signed=True)

    def i64(self) -> int:
        return int.from_bytes(self.read(8), "little", signed=True)

    def u64(self) -> int:
        return int.from_bytes(self.read(8), "little")

    def varint(self) -> int:
        first = self.u8()
        if first < 0xFD:
            return first
        if first == 0xFD:
            return int.from_bytes(self.read(2), "little")
        if first == 0xFE:
            return int.from_bytes(self.read(4), "little")
        return int.from_bytes(self.read(8), "little")

    def bytes_blob(self) -> bytes:
        return self.read(self.varint())

    def hash32(self) -> bytes:
        return self.read(32)
