"""secp256k1 key generation and public-key derivation (via cryptography library)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed


SECP256K1 = ec.SECP256K1()
PRIVATE_KEY_BYTES = 32
# secp256k1 curve order
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


@dataclass(frozen=True)
class KeyPair:
    private_key: bytes  # 32-byte secret; never transmitted on P2P
    public_key_compressed: bytes  # 33 bytes

    @property
    def public_key_uncompressed(self) -> bytes:
        return private_key_to_public_key(self.private_key, compressed=False)


def generate_private_key() -> bytes:
    """Cryptographically secure 32-byte private key in valid secp256k1 range."""
    while True:
        candidate = secrets.token_bytes(PRIVATE_KEY_BYTES)
        priv_int = int.from_bytes(candidate, "big")
        if priv_int == 0 or priv_int >= _SECP256K1_N:
            continue
        return candidate


def private_key_to_public_key(private_key: bytes, *, compressed: bool = True) -> bytes:
    if len(private_key) != PRIVATE_KEY_BYTES:
        raise ValueError("private key must be 32 bytes")
    priv_int = int.from_bytes(private_key, "big")
    private = ec.derive_private_key(priv_int, SECP256K1)
    public = private.public_key()
    numbers = public.public_numbers()
    x = numbers.x.to_bytes(32, "big")
    y = numbers.y
    if compressed:
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x
    return b"\x04" + x + y.to_bytes(32, "big")


def generate_keypair() -> KeyPair:
    priv = generate_private_key()
    pub = private_key_to_public_key(priv, compressed=True)
    return KeyPair(private_key=priv, public_key_compressed=pub)


def load_private_key(private_key: bytes) -> ec.EllipticCurvePrivateKey:
    if len(private_key) != PRIVATE_KEY_BYTES:
        raise ValueError("private key must be 32 bytes")
    return ec.derive_private_key(int.from_bytes(private_key, "big"), SECP256K1)


def load_public_key_from_compressed(public_key: bytes) -> ec.EllipticCurvePublicKey:
    if len(public_key) not in (33, 65):
        raise ValueError("unsupported public key length")
    return ec.EllipticCurvePublicKey.from_encoded_point(SECP256K1, public_key)


def assert_valid_private_key(private_key: bytes) -> None:
    load_private_key(private_key)


def assert_valid_public_key(public_key: bytes) -> None:
    load_public_key_from_compressed(public_key)
