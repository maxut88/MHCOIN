"""BIP32 hierarchical deterministic keys (secp256k1) for MHCOIN.

BIP84-style paths (Bitcoin-compatible tree; MHCOIN addresses remain ``mhc1…``):

- Account:  ``m/84'/0'/0'``
- Receive:  ``m/84'/0'/0'/0/n``  (external)
- Change:   ``m/84'/0'/0'/1/n``  (internal)

Also provides account-level **xpub** (BIP32 mainnet version bytes) for watch-only.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from mhcoin.crypto.keys import (
    PRIVATE_KEY_BYTES,
    assert_valid_private_key,
    private_key_to_public_key,
)
from mhcoin.wallet.wif import _base58check_decode, _base58check_encode

# secp256k1 curve order / field
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

DEFAULT_DERIVATION_PATH = "m/84'/0'/0'/0/0"
ACCOUNT_PATH = "m/84'/0'/0'"
EXTERNAL_RECEIVE_BASE = "m/84'/0'/0'/0"
INTERNAL_CHANGE_BASE = "m/84'/0'/0'/1"
DEFAULT_GAP_LIMIT = 20

HARDENED = 0x80000000
# BIP32 mainnet public version (zpub would be SLIP-132; we use classic xpub).
XPUB_VERSION = bytes.fromhex("0488B21E")


def receive_path(index: int = 0) -> str:
    """BIP84 external receive path ``m/84'/0'/0'/0/{index}``."""
    i = int(index)
    if i < 0 or i >= HARDENED:
        raise ValueError("receive index out of range")
    return f"{EXTERNAL_RECEIVE_BASE}/{i}"


def change_path(index: int = 0) -> str:
    """BIP84 internal change path ``m/84'/0'/0'/1/{index}``."""
    i = int(index)
    if i < 0 or i >= HARDENED:
        raise ValueError("change index out of range")
    return f"{INTERNAL_CHANGE_BASE}/{i}"


def is_receive_path(path: str | None) -> bool:
    return bool(path) and str(path).startswith(EXTERNAL_RECEIVE_BASE + "/")


def is_change_path(path: str | None) -> bool:
    return bool(path) and str(path).startswith(INTERNAL_CHANGE_BASE + "/")


def path_index(path: str | None) -> int | None:
    if not path:
        return None
    for base in (EXTERNAL_RECEIVE_BASE + "/", INTERNAL_CHANGE_BASE + "/"):
        if path.startswith(base):
            tail = path[len(base) :]
            if tail.isdigit():
                return int(tail)
    return None


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def master_key_from_seed(seed: bytes) -> tuple[bytes, bytes]:
    """BIP32 master private key + chain code from BIP39 seed (64 bytes)."""
    if len(seed) < 16:
        raise ValueError("seed too short")
    I = _hmac_sha512(b"Bitcoin seed", seed)
    il, ir = I[:32], I[32:]
    assert_valid_private_key(il)
    return il, ir


def _ser32(i: int) -> bytes:
    return struct.pack(">I", i & 0xFFFFFFFF)


def _hash160(data: bytes) -> bytes:
    # OpenSSL 3.x moved RIPEMD-160 into the "legacy" provider (often not
    # loaded), so ``hashlib.new("ripemd160", ...)`` can raise ValueError.
    # Reuse the fallback-aware implementation already used for addresses.
    from mhcoin.crypto.hashing import hash160 as _pubkey_hash160

    return _pubkey_hash160(data)


def _ckd_priv(k_par: bytes, c_par: bytes, index: int) -> tuple[bytes, bytes]:
    """BIP32 child key derivation (private → private)."""
    if index & HARDENED:
        data = b"\x00" + k_par + _ser32(index)
    else:
        data = private_key_to_public_key(k_par, compressed=True) + _ser32(index)
    I = _hmac_sha512(c_par, data)
    il, ir = I[:32], I[32:]
    il_int = int.from_bytes(il, "big")
    if il_int >= _N:
        raise ValueError("BIP32 invalid child (IL >= n)")
    ki = (il_int + int.from_bytes(k_par, "big")) % _N
    if ki == 0:
        raise ValueError("BIP32 invalid child (key=0)")
    child = ki.to_bytes(PRIVATE_KEY_BYTES, "big")
    assert_valid_private_key(child)
    return child, ir


def _decompress_pubkey(pub: bytes) -> tuple[int, int]:
    if len(pub) != 33 or pub[0] not in (2, 3):
        raise ValueError("compressed public key required")
    x = int.from_bytes(pub[1:], "big")
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if (y % 2 == 0 and pub[0] == 3) or (y % 2 == 1 and pub[0] == 2):
        y = _P - y
    return x, y


def _compress_point(x: int, y: int) -> bytes:
    prefix = b"\x02" if y % 2 == 0 else b"\x03"
    return prefix + x.to_bytes(32, "big")


def _point_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int]:
    if p1 is None:
        assert p2 is not None
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        raise ValueError("BIP32 point at infinity")
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow((x2 - x1) % _P, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return x3, y3


def _point_mul_g(k: int) -> tuple[int, int]:
    """Naive double-and-add scalar mul of G (small IL only — ok for CKD)."""
    if k % _N == 0:
        raise ValueError("BIP32 invalid IL")
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = (_GX, _GY)
    kk = k % _N
    while kk:
        if kk & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        kk >>= 1
    assert result is not None
    return result


def _ckd_pub(k_par: bytes, c_par: bytes, index: int) -> tuple[bytes, bytes]:
    """BIP32 child key derivation (public → public). Non-hardened only."""
    if index & HARDENED:
        raise ValueError("cannot derive hardened child from public key")
    data = k_par + _ser32(index)
    I = _hmac_sha512(c_par, data)
    il, ir = I[:32], I[32:]
    il_int = int.from_bytes(il, "big")
    if il_int >= _N:
        raise ValueError("BIP32 invalid child (IL >= n)")
    parent = _decompress_pubkey(k_par)
    child_pt = _point_add(_point_mul_g(il_int), parent)
    return _compress_point(*child_pt), ir


def parse_path(path: str) -> list[int]:
    """Parse ``m/84'/0'/0'/0/0`` into BIP32 index list."""
    s = (path or "").strip()
    if not s.startswith("m"):
        raise ValueError("derivation path must start with m")
    parts = s.split("/")
    if parts[0] != "m":
        raise ValueError("invalid derivation path")
    out: list[int] = []
    for p in parts[1:]:
        if not p:
            raise ValueError("empty path component")
        hardened = p.endswith("'") or p.endswith("h") or p.endswith("H")
        num_s = p[:-1] if hardened else p
        if not num_s.isdigit():
            raise ValueError(f"invalid path component {p!r}")
        idx = int(num_s)
        if idx < 0 or idx >= HARDENED:
            raise ValueError(f"path index out of range: {p}")
        if hardened:
            idx |= HARDENED
        out.append(idx)
    return out


def derive_private_key(seed: bytes, path: str = DEFAULT_DERIVATION_PATH) -> bytes:
    """Derive secp256k1 private key from BIP39 seed + BIP32 path."""
    key, chain = master_key_from_seed(seed)
    for index in parse_path(path):
        key, chain = _ckd_priv(key, chain, index)
    return key


def derive_private_key_and_chain(seed: bytes, path: str) -> tuple[bytes, bytes]:
    key, chain = master_key_from_seed(seed)
    for index in parse_path(path):
        key, chain = _ckd_priv(key, chain, index)
    return key, chain


@dataclass(frozen=True)
class ExtendedPublicKey:
    """BIP32 extended public key (account or deeper non-hardened node)."""

    depth: int
    fingerprint: bytes  # 4 bytes parent fingerprint
    child_number: int
    chain_code: bytes  # 32 bytes
    public_key: bytes  # 33 bytes compressed


def encode_xpub(node: ExtendedPublicKey) -> str:
    if len(node.fingerprint) != 4:
        raise ValueError("fingerprint must be 4 bytes")
    if len(node.chain_code) != 32:
        raise ValueError("chain code must be 32 bytes")
    if len(node.public_key) != 33:
        raise ValueError("public key must be compressed 33 bytes")
    payload = (
        XPUB_VERSION
        + bytes([node.depth & 0xFF])
        + node.fingerprint
        + _ser32(node.child_number)
        + node.chain_code
        + node.public_key
    )
    return _base58check_encode(payload)


def decode_xpub(text: str) -> ExtendedPublicKey:
    raw = _base58check_decode(text.strip())
    if len(raw) != 78:
        raise ValueError("invalid xpub length")
    if raw[:4] != XPUB_VERSION:
        raise ValueError("unsupported xpub version (expected BIP32 mainnet public)")
    depth = raw[4]
    fingerprint = raw[5:9]
    child = struct.unpack(">I", raw[9:13])[0]
    chain = raw[13:45]
    pub = raw[45:78]
    if pub[0] not in (2, 3):
        raise ValueError("xpub public key must be compressed")
    return ExtendedPublicKey(
        depth=depth,
        fingerprint=fingerprint,
        child_number=child,
        chain_code=chain,
        public_key=pub,
    )


def account_xpub_from_seed(seed: bytes) -> str:
    """Derive BIP84 account ``m/84'/0'/0'`` and encode as xpub."""
    key, chain = master_key_from_seed(seed)
    # parent fingerprint at account depth: hash160(pubkey of m/84'/0') first 4 bytes
    parent_pub = None
    depth = 0
    fingerprint = b"\x00\x00\x00\x00"
    child_number = 0
    for index in parse_path(ACCOUNT_PATH):
        parent_pub = private_key_to_public_key(key, compressed=True)
        fingerprint = _hash160(parent_pub)[:4]
        key, chain = _ckd_priv(key, chain, index)
        child_number = index
        depth += 1
    pub = private_key_to_public_key(key, compressed=True)
    return encode_xpub(
        ExtendedPublicKey(
            depth=depth,
            fingerprint=fingerprint,
            child_number=child_number,
            chain_code=chain,
            public_key=pub,
        )
    )


def derive_pubkey_from_account_xpub(xpub: str, *, change: bool, index: int) -> tuple[bytes, str]:
    """Derive compressed pubkey + path from account xpub (non-hardened)."""
    node = decode_xpub(xpub)
    if int(index) < 0 or int(index) >= HARDENED:
        raise ValueError("index out of range")
    chain_i = 1 if change else 0
    path = change_path(index) if change else receive_path(index)
    # account → chain
    pub, c = _ckd_pub(node.public_key, node.chain_code, chain_i)
    # chain → address index
    pub, _c2 = _ckd_pub(pub, c, int(index))
    return pub, path
