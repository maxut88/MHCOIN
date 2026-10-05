"""Encrypted local wallet storage (private keys never leave disk unencrypted)."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


WALLET_VERSION = 1
DEFAULT_SCRYPT_N = 2**14
DEFAULT_SCRYPT_R = 8
DEFAULT_SCRYPT_P = 1


@dataclass
class WalletRecord:
    wallet_id: str
    label: str
    address: str
    public_key_hex: str
    encrypted_private_key: str  # base64 payload
    created_at: str
    # Optional Bitcoin-style recovery metadata (older wallet.json omit these).
    derivation_path: str | None = None
    encrypted_mnemonic: str | None = None
    # HD account grouping: root wallet_id shared by receive addresses.
    account_id: str | None = None


@dataclass
class WalletFile:
    version: int
    network: str
    default_wallet_id: str | None
    wallets: list[WalletRecord]
    kdf: dict[str, int]
    salt_hex: str  # file-level salt for KDF when unlocking


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def encrypt_private_key(private_key: bytes, password: str, *, n: int, r: int, p: int) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    key = _derive_key(password, salt, n, r, p)
    nonce = secrets.token_bytes(12)
    aes = AESGCM(key)
    ciphertext = aes.encrypt(nonce, private_key, None)
    payload = {
        "salt_hex": salt.hex(),
        "nonce_hex": nonce.hex(),
        "ciphertext_hex": ciphertext.hex(),
    }
    return json.dumps(payload, separators=(",", ":")), salt.hex()


def decrypt_private_key(encrypted_json: str, password: str, *, n: int, r: int, p: int) -> bytes:
    payload = json.loads(encrypted_json)
    salt = bytes.fromhex(payload["salt_hex"])
    nonce = bytes.fromhex(payload["nonce_hex"])
    ciphertext = bytes.fromhex(payload["ciphertext_hex"])
    key = _derive_key(password, salt, n, r, p)
    aes = AESGCM(key)
    return aes.decrypt(nonce, ciphertext, None)


def default_wallet_path(data_dir: Path) -> Path:
    return data_dir / "wallet.json"


def load_wallet_file(path: Path) -> WalletFile | None:
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    wallets = []
    for w in raw.get("wallets", []):
        if not isinstance(w, dict):
            continue
        # Ignore unknown forward-compat keys; keep known fields only.
        allowed = {f.name for f in fields(WalletRecord)}
        wallets.append(WalletRecord(**{k: v for k, v in w.items() if k in allowed}))
    return WalletFile(
        version=int(raw.get("version", 1)),
        network=str(raw.get("network", "localnet")),
        default_wallet_id=raw.get("default_wallet_id"),
        wallets=wallets,
        kdf=raw.get("kdf", {"n": DEFAULT_SCRYPT_N, "r": DEFAULT_SCRYPT_R, "p": DEFAULT_SCRYPT_P}),
        salt_hex=str(raw.get("salt_hex", "")),
    )


def save_wallet_file(path: Path, wf: WalletFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    data = {
        "version": wf.version,
        "network": wf.network,
        "default_wallet_id": wf.default_wallet_id,
        "kdf": wf.kdf,
        "salt_hex": wf.salt_hex,
        "wallets": [asdict(w) for w in wf.wallets],
    }
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
