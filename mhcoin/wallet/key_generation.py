"""Wallet key generation helpers."""

from __future__ import annotations

from mhcoin.crypto.keys import KeyPair, generate_keypair
from mhcoin.wallet.addresses import pubkey_to_address


def create_new_keypair(*, hrp: str = "mhc") -> tuple[KeyPair, str]:
    kp = generate_keypair()
    address = pubkey_to_address(kp.public_key_compressed, hrp=hrp)
    return kp, address
