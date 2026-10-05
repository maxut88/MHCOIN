"""Minimal BIP32 hierarchical deterministic keys (secp256k1).

Default path matches Bitcoin native-segwit account style (BIP84):
``m/84'/0'/0'/0/0`` — first receiving address. MHCOIN addresses are still
``mhc1…`` (witness v0 pubkey-hash); only the key tree is BIP-compatible.
"""

from __future__ import annotations

import hashlib
import hmac
import struct

from mhcoin.crypto.keys import (
    PRIVATE_KEY_BYTES,
    assert_valid_private_key,
    private_key_to_public_key,
)

# secp256k1 curve order
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# BIP84-style path for the first external address (coin_type 0' = BTC mainnet
# slot; MHCOIN reuses the path convention so tools/docs stay familiar).
DEFAULT_DERIVATION_PATH = "m/84'/0'/0'/0/0"
# External (receive) chain base — index N → m/84'/0'/0'/0/N
EXTERNAL_RECEIVE_BASE = "m/84'/0'/0'/0"

HARDENED = 0x80000000


def receive_path(index: int = 0) -> str:
    """BIP84 external receive path ``m/84'/0'/0'/0/{index}``."""
    i = int(index)
    if i < 0 or i >= HARDENED:
        raise ValueError("receive index out of range")
    return f"{EXTERNAL_RECEIVE_BASE}/{i}"


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
