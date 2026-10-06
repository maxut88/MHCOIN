"""MHC address format: Bech32 with HRP ``mhc`` → ``mhc1…``."""

from __future__ import annotations

from mhcoin.constants import DEFAULT_ADDRESS_HRP, WITNESS_VERSION_PUBKEY_HASH
from mhcoin.crypto.bech32 import decode, encode
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import assert_valid_public_key


def pubkey_to_address(public_key_compressed: bytes, *, hrp: str = DEFAULT_ADDRESS_HRP) -> str:
    assert_valid_public_key(public_key_compressed)
    program = hash160(public_key_compressed)
    if len(program) != 20:
        raise ValueError("pubkey hash must be 20 bytes")
    return encode(hrp, WITNESS_VERSION_PUBKEY_HASH, program)


def pubkey_hash_to_address(pubkey_hash: bytes, *, hrp: str = DEFAULT_ADDRESS_HRP) -> str:
    if len(pubkey_hash) != 20:
        raise ValueError("pubkey hash must be 20 bytes")
    return encode(hrp, WITNESS_VERSION_PUBKEY_HASH, pubkey_hash)


def address_to_pubkey_hash(address: str, *, hrp: str = DEFAULT_ADDRESS_HRP) -> bytes:
    version, program = decode(address, expected_hrp=hrp)
    if version != WITNESS_VERSION_PUBKEY_HASH:
        raise ValueError("unsupported witness version")
    if len(program) != 20:
        raise ValueError("invalid pubkey hash length")
    return program


def validate_address(address: str, *, hrp: str = DEFAULT_ADDRESS_HRP) -> bool:
    try:
        address_to_pubkey_hash(address, hrp=hrp)
        return address.startswith(hrp + "1")
    except ValueError:
        return False
