"""ECDSA sign/verify abstraction over secp256k1 (consensus digests are pre-hashed)."""

from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from mhcoin.crypto.keys import load_private_key, load_public_key_from_compressed


class SignatureError(Exception):
    """Invalid or unverifiable signature."""


def sign_digest(private_key: bytes, digest32: bytes) -> bytes:
    """Sign a 32-byte digest; returns DER-encoded ECDSA signature."""
    if len(digest32) != 32:
        raise ValueError("digest must be 32 bytes")
    key = load_private_key(private_key)
    return key.sign(digest32, ec.ECDSA(utils.Prehashed(hashes.SHA256())))


def verify_digest(public_key_compressed: bytes, digest32: bytes, signature: bytes) -> bool:
    if len(digest32) != 32:
        return False
    try:
        pub = load_public_key_from_compressed(public_key_compressed)
        pub.verify(signature, digest32, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_digest_strict(public_key_compressed: bytes, digest32: bytes, signature: bytes) -> None:
    if not verify_digest(public_key_compressed, digest32, signature):
        raise SignatureError("signature verification failed")
