"""Bech32 encoding (BIP-173 style) with MHCOIN-specific HRP — not Bitcoin."""

from __future__ import annotations

# Charset and generator for Bech32 checksum
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
GENERATOR = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= GENERATOR[i]
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _hrp_expand(hrp) + data
    polymod = _polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _verify_checksum(hrp: str, data: list[int]) -> bool:
    return _polymod(_hrp_expand(hrp) + data) == 1


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for b in data:
        acc = (acc << frombits) | b
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("invalid padding")
    return ret


def encode(hrp: str, witness_version: int, witness_program: bytes) -> str:
    if witness_version < 0 or witness_version > 16:
        raise ValueError("invalid witness version")
    if len(witness_program) < 2 or len(witness_program) > 40:
        raise ValueError("invalid witness program length")
    hrp = hrp.lower()
    data = [witness_version] + _convertbits(witness_program, 8, 5)
    combined = data + _create_checksum(hrp, data)
    return hrp + "1" + "".join(CHARSET[d] for d in combined)


def decode(addr: str, *, expected_hrp: str | None = None) -> tuple[int, bytes]:
    if any(ord(x) < 33 or ord(x) > 126 for x in addr):
        raise ValueError("invalid character")
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        raise ValueError("invalid bech32 address")
    hrp = addr[:pos]
    if expected_hrp and hrp != expected_hrp.lower():
        raise ValueError("wrong network prefix")
    data = [CHARSET.find(c) for c in addr[pos + 1 :]]
    if -1 in data or not _verify_checksum(hrp, data):
        raise ValueError("checksum failure")
    data = data[:-6]
    if len(data) < 1:
        raise ValueError("empty witness program")
    witness_version = data[0]
    program = bytes(_convertbits(data[1:], 5, 8, pad=False))
    return witness_version, program
