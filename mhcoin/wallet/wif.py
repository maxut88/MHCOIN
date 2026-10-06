"""WIF (Wallet Import Format) encode/decode for secp256k1 keys.

MHCOIN uses secp256k1 key material. Compressed
mainnet-style WIF (version ``0x80``) is accepted so keys can move between tools
that speak WIF; hex (64 nybbles) is also accepted.
"""

from __future__ import annotations

import hashlib

# WIF version byte 0x80 (compressed keys append 0x01 before checksum).
WIF_VERSION_MAINNET = 0x80

_B58_ALPHABET = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _base58check_encode(payload: bytes) -> str:
    checksum = _sha256(_sha256(payload))[:4]
    data = payload + checksum
    # Count leading zeros
    n_pad = 0
    for b in data:
        if b == 0:
            n_pad += 1
        else:
            break
    num = int.from_bytes(data, "big")
    enc = bytearray()
    while num > 0:
        num, rem = divmod(num, 58)
        enc.append(_B58_ALPHABET[rem])
    return (b"1" * n_pad + enc[::-1]).decode("ascii")


def _base58check_decode(text: str) -> bytes:
    s = text.strip()
    if not s:
        raise ValueError("empty WIF")
    num = 0
    for ch in s.encode("ascii"):
        if ch not in _B58_INDEX:
            raise ValueError("invalid Base58 character in WIF")
        num = num * 58 + _B58_INDEX[ch]
    # Leading ones → leading zero bytes
    n_pad = 0
    for ch in s:
        if ch == "1":
            n_pad += 1
        else:
            break
    full = num.to_bytes((num.bit_length() + 7) // 8 or 1, "big")
    full = b"\x00" * n_pad + full
    if len(full) < 5:
        raise ValueError("WIF too short")
    payload, checksum = full[:-4], full[-4:]
    if _sha256(_sha256(payload))[:4] != checksum:
        raise ValueError("invalid WIF checksum")
    return payload


def encode_wif(private_key: bytes, *, compressed: bool = True) -> str:
    """Encode 32-byte private key as compressed (default) WIF."""
    if len(private_key) != 32:
        raise ValueError("private key must be 32 bytes")
    payload = bytes([WIF_VERSION_MAINNET]) + private_key
    if compressed:
        payload += b"\x01"
    return _base58check_encode(payload)


def decode_wif(wif: str) -> tuple[bytes, bool]:
    """Decode WIF → (32-byte private key, compressed flag)."""
    payload = _base58check_decode(wif)
    if len(payload) == 33 and payload[0] == WIF_VERSION_MAINNET:
        return payload[1:], False
    if len(payload) == 34 and payload[0] == WIF_VERSION_MAINNET and payload[-1] == 0x01:
        return payload[1:-1], True
    raise ValueError("unsupported WIF version or length")


def parse_private_key(text: str) -> bytes:
    """Parse hex (64 chars) or WIF into 32 raw private-key bytes."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty private key")
    # Hex
    hex_candidate = raw.lower().removeprefix("0x")
    if all(c in "0123456789abcdef" for c in hex_candidate) and len(hex_candidate) == 64:
        key = bytes.fromhex(hex_candidate)
        _assert_key_range(key)
        return key
    # WIF
    key, _compressed = decode_wif(raw)
    _assert_key_range(key)
    return key


def _assert_key_range(key: bytes) -> None:
    from mhcoin.crypto.keys import assert_valid_private_key

    assert_valid_private_key(key)
