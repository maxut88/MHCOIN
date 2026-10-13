"""BIP39 mnemonic helpers (standard English wordlist)."""

from __future__ import annotations

from mnemonic import Mnemonic

_MNEMO = Mnemonic("english")

# 12 words = 128-bit entropy (standard consumer wallets).
DEFAULT_STRENGTH = 128


def generate_mnemonic(*, strength: int = DEFAULT_STRENGTH) -> str:
    """Return a new BIP39 mnemonic (12 words for strength=128, 24 for 256)."""
    if strength not in (128, 160, 192, 224, 256):
        raise ValueError("strength must be one of 128,160,192,224,256")
    return _MNEMO.generate(strength=strength)


def normalize_mnemonic(words: str) -> str:
    """Collapse whitespace / case for BIP39."""
    return " ".join((words or "").strip().lower().split())


def validate_mnemonic(words: str) -> bool:
    return _MNEMO.check(normalize_mnemonic(words))


def mnemonic_to_seed(words: str, *, passphrase: str = "") -> bytes:
    """BIP39 PBKDF2 seed (64 bytes). Optional BIP39 passphrase (not wallet UI password)."""
    m = normalize_mnemonic(words)
    if not _MNEMO.check(m):
        raise ValueError("invalid BIP39 mnemonic checksum or words")
    return _MNEMO.to_seed(m, passphrase=passphrase or "")
