"""MHCOIN wallet: create keys, addresses, encrypted persistence, send.

Bitcoin-style recovery:
- BIP39 mnemonic (12/24 words) + BIP32 path ``m/84'/0'/0'/0/0``
- Import raw hex or WIF private key
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import KeyPair, private_key_to_public_key
from mhcoin.utxo import UTXOSet
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_to_address, validate_address
from mhcoin.wallet.bip39 import (
    generate_mnemonic,
    mnemonic_to_seed,
    normalize_mnemonic,
    validate_mnemonic,
)
from mhcoin.wallet.hd import DEFAULT_DERIVATION_PATH, derive_private_key
from mhcoin.wallet.send import SendResult, build_send_tx, parse_amount_mhc
from mhcoin.wallet.storage import (
    DEFAULT_SCRYPT_N,
    DEFAULT_SCRYPT_P,
    DEFAULT_SCRYPT_R,
    WalletFile,
    WalletRecord,
    decrypt_private_key,
    default_wallet_path,
    encrypt_private_key,
    load_wallet_file,
    save_wallet_file,
)
from mhcoin.wallet.wif import encode_wif, parse_private_key


class WalletError(Exception):
    pass


@dataclass(frozen=True)
class WalletCreateResult:
    """Result of creating / restoring / importing a wallet key."""

    address: str
    wallet_id: str
    mnemonic: str | None = None
    derivation_path: str | None = None
    wif: str | None = None


@dataclass
class WalletPaths:
    data_dir: Path
    network: str = "localnet"
    hrp: str = "mhc"

    @property
    def wallet_file(self) -> Path:
        return default_wallet_path(self.data_dir)

    @property
    def utxo_path(self) -> Path:
        # LMDB chainstate dir (legacy utxo.sqlite is migrated by UTXOSet).
        return self.data_dir / "chainstate"


class Wallet:
    def __init__(self, paths: WalletPaths, password: str | None = None):
        self.paths = paths
        self._password = password
        self._unlocked_keys: dict[str, bytes] = {}

    def create(
        self,
        *,
        label: str = "default",
        password: str | None = None,
        make_default: bool = True,
        strength: int = 128,
    ) -> WalletCreateResult:
        """Create a new HD wallet from a BIP39 mnemonic (Bitcoin-style).

        Writes encrypted private key (+ encrypted mnemonic) into wallet.json.
        Returns the mnemonic **once** — store it offline; it is the recovery seed.
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("wallet password required for encrypted storage")
        try:
            mnemonic = generate_mnemonic(strength=strength)
            seed = mnemonic_to_seed(mnemonic)
            priv = derive_private_key(seed, DEFAULT_DERIVATION_PATH)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=label,
            make_default=make_default,
            mnemonic=mnemonic,
            derivation_path=DEFAULT_DERIVATION_PATH,
        )

    def restore_from_mnemonic(
        self,
        mnemonic: str,
        *,
        password: str | None = None,
        label: str = "restored",
        make_default: bool = True,
        passphrase: str = "",
        path: str = DEFAULT_DERIVATION_PATH,
    ) -> WalletCreateResult:
        """Restore wallet from BIP39 words (optional BIP39 passphrase)."""
        pwd = password or self._password
        if not pwd:
            raise WalletError("wallet password required for encrypted storage")
        words = normalize_mnemonic(mnemonic)
        if not validate_mnemonic(words):
            raise WalletError("invalid BIP39 mnemonic (bad words or checksum)")
        try:
            seed = mnemonic_to_seed(words, passphrase=passphrase)
            priv = derive_private_key(seed, path)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=label,
            make_default=make_default,
            mnemonic=words,
            derivation_path=path,
        )

    def import_private_key(
        self,
        key_text: str,
        *,
        password: str | None = None,
        label: str = "imported",
        make_default: bool = True,
    ) -> WalletCreateResult:
        """Import a hex or WIF private key (Bitcoin-compatible WIF)."""
        pwd = password or self._password
        if not pwd:
            raise WalletError("wallet password required for encrypted storage")
        try:
            priv = parse_private_key(key_text)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=label,
            make_default=make_default,
            mnemonic=None,
            derivation_path=None,
        )

    def export_wif(self, *, password: str | None = None, wallet_id: str | None = None) -> str:
        """Export the active (or specified) key as compressed WIF."""
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to export key")
        if wallet_id:
            kp = self.unlock_key(wallet_id, pwd)
        else:
            kp = self.unlock_default(pwd)
        return encode_wif(kp.private_key, compressed=True)

    def export_mnemonic(self, *, password: str | None = None, wallet_id: str | None = None) -> str:
        """Decrypt stored BIP39 mnemonic (only if wallet was created/restored from seed)."""
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to export mnemonic")
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found")
        if wallet_id:
            rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
        else:
            rec = self._default_record()
        if rec is None:
            raise WalletError("unknown wallet id")
        if not rec.encrypted_mnemonic:
            raise WalletError("this wallet has no stored mnemonic (imported key only)")
        kdf = wf.kdf
        try:
            raw = decrypt_private_key(
                rec.encrypted_mnemonic,
                pwd,
                n=int(kdf["n"]),
                r=int(kdf["r"]),
                p=int(kdf["p"]),
            )
        except Exception as e:
            raise WalletError("wrong password or corrupt wallet") from e
        words = raw.decode("utf-8")
        if not validate_mnemonic(words):
            raise WalletError("stored mnemonic is corrupt")
        return normalize_mnemonic(words)

    def _persist_key(
        self,
        *,
        private_key: bytes,
        password: str,
        label: str,
        make_default: bool,
        mnemonic: str | None,
        derivation_path: str | None,
    ) -> WalletCreateResult:
        pub = private_key_to_public_key(private_key, compressed=True)
        address = pubkey_to_address(pub, hrp=self.paths.hrp)
        # Reject duplicate addresses in the same wallet file.
        existing = load_wallet_file(self.paths.wallet_file)
        if existing:
            for w in existing.wallets:
                if w.address == address or w.public_key_hex == pub.hex():
                    raise WalletError(f"key already in wallet ({address})")
        wallet_id = uuid.uuid4().hex[:16]
        n, r, p = DEFAULT_SCRYPT_N, DEFAULT_SCRYPT_R, DEFAULT_SCRYPT_P
        enc, _ = encrypt_private_key(private_key, password, n=n, r=r, p=p)
        enc_mnemonic = None
        if mnemonic:
            enc_mnemonic, _ = encrypt_private_key(
                normalize_mnemonic(mnemonic).encode("utf-8"),
                password,
                n=n,
                r=r,
                p=p,
            )
        record = WalletRecord(
            wallet_id=wallet_id,
            label=label,
            address=address,
            public_key_hex=pub.hex(),
            encrypted_private_key=enc,
            created_at=datetime.now(timezone.utc).isoformat(),
            derivation_path=derivation_path,
            encrypted_mnemonic=enc_mnemonic,
        )
        wf = existing
        if wf is None:
            wf = WalletFile(
                version=1,
                network=self.paths.network,
                default_wallet_id=wallet_id,
                wallets=[record],
                kdf={"n": n, "r": r, "p": p},
                salt_hex=secrets.token_hex(16),
            )
        else:
            wf.wallets.append(record)
            if make_default or not wf.default_wallet_id:
                wf.default_wallet_id = wallet_id
        save_wallet_file(self.paths.wallet_file, wf)
        self._password = password
        if make_default:
            self._unlocked_keys = {wallet_id: private_key}
        else:
            self._unlocked_keys[wallet_id] = private_key
        return WalletCreateResult(
            address=address,
            wallet_id=wallet_id,
            mnemonic=normalize_mnemonic(mnemonic) if mnemonic else None,
            derivation_path=derivation_path,
            wif=encode_wif(private_key, compressed=True),
        )

    def set_default_wallet_id(self, wallet_id: str) -> None:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found; run: mhcoin wallet create")
        if not any(w.wallet_id == wallet_id for w in wf.wallets):
            raise WalletError("unknown wallet id")
        wf.default_wallet_id = wallet_id
        save_wallet_file(self.paths.wallet_file, wf)
        # Active key must match the selected wallet.
        self._unlocked_keys = {
            k: v for k, v in self._unlocked_keys.items() if k == wallet_id
        }

    def set_default_by_address(self, address: str) -> str:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found; run: mhcoin wallet create")
        rec = next((w for w in wf.wallets if w.address == address), None)
        if rec is None:
            raise WalletError("address not in wallet file")
        self.set_default_wallet_id(rec.wallet_id)
        return rec.wallet_id

    def list_wallets(self) -> list[WalletRecord]:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf:
            return []
        return wf.wallets

    def _default_record(self) -> WalletRecord:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found; run: mhcoin wallet create")
        wid = wf.default_wallet_id or wf.wallets[0].wallet_id
        for w in wf.wallets:
            if w.wallet_id == wid:
                return w
        return wf.wallets[0]

    def default_address(self) -> str:
        return self._default_record().address

    def address_for_label(self, label: str) -> str:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found; run: mhcoin wallet create")
        matches = [w for w in wf.wallets if w.label == label]
        if not matches:
            raise WalletError(f"no wallet with label {label!r}")
        return matches[-1].address

    def unlock_default(self, password: str | None = None) -> KeyPair:
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to unlock wallet")
        rec = self._default_record()
        return self.unlock_key(rec.wallet_id, pwd)

    def unlock_with_password(
        self, password: str, *, wallet_id: str | None = None
    ) -> str:
        """Unlock one wallet by password and make it the active default.

        If wallet_id is set, only that record is tried. Otherwise try the current
        default first, then every other wallet (so Lock → Open still works when
        the active key changed after creating another wallet).
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to unlock wallet")
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found; run: mhcoin wallet create")

        ordered: list[WalletRecord] = []
        if wallet_id:
            rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
            if not rec:
                raise WalletError("unknown wallet id")
            ordered = [rec]
        else:
            preferred = wf.default_wallet_id or wf.wallets[0].wallet_id
            ordered = sorted(
                wf.wallets,
                key=lambda w: (0 if w.wallet_id == preferred else 1, w.created_at),
            )

        last_err: Exception | None = None
        for rec in ordered:
            try:
                self.unlock_key(rec.wallet_id, pwd)
            except WalletError as e:
                last_err = e
                continue
            if wf.default_wallet_id != rec.wallet_id:
                wf.default_wallet_id = rec.wallet_id
                save_wallet_file(self.paths.wallet_file, wf)
            return rec.address
        raise WalletError("wrong password or corrupt wallet") from last_err

    def unlock_key(self, wallet_id: str, password: str) -> KeyPair:
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf:
            raise WalletError("wallet file missing")
        rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
        if not rec:
            raise WalletError("unknown wallet id")
        kdf = wf.kdf
        try:
            priv = decrypt_private_key(
                rec.encrypted_private_key,
                password,
                n=int(kdf["n"]),
                r=int(kdf["r"]),
                p=int(kdf["p"]),
            )
        except Exception as e:
            raise WalletError("wrong password or corrupt wallet") from e
        pub = bytes.fromhex(rec.public_key_hex)
        if private_key_to_public_key(priv) != pub:
            raise WalletError("wallet corruption or wrong password")
        self._unlocked_keys[wallet_id] = priv
        self._password = password
        return KeyPair(private_key=priv, public_key_compressed=pub)

    def balance(self, address: str | None = None) -> tuple[int, int]:
        addr = address or self.default_address()
        if not validate_address(addr, hrp=self.paths.hrp):
            raise WalletError("invalid address")
        pkh = address_to_pubkey_hash(addr, hrp=self.paths.hrp)
        cs = self.paths.utxo_path
        legacy = cs.with_name("utxo.sqlite")
        if not (cs.is_dir() or legacy.is_file()):
            return 0, 0
        utxo = UTXOSet(cs)
        try:
            confirmed = utxo.balance_for_pubkey_hash(pkh)
        finally:
            utxo.close()
        return confirmed, 0

    def history(self, address: str | None = None) -> list[dict]:
        return []

    def send(
        self,
        to_address: str,
        amount_text: str,
        *,
        password: str | None = None,
        fee_sats: int | None = None,
        utxo: UTXOSet | None = None,
        exclude_outpoints: set[str] | None = None,
    ) -> SendResult:
        from mhcoin.constants import DEFAULT_FEE_SATOSHIS

        if not validate_address(to_address, hrp=self.paths.hrp):
            raise WalletError("invalid destination address")
        amount = parse_amount_mhc(amount_text)
        kp = self.unlock_default(password)
        pkh = hash160(kp.public_key_compressed)
        own_utxo = utxo
        close = False
        if own_utxo is None:
            cs = self.paths.utxo_path
            legacy = cs.with_name("utxo.sqlite")
            if not (cs.is_dir() or legacy.is_file()):
                raise WalletError("no chain UTXO yet — mine some MHC first")
            own_utxo = UTXOSet(cs)
            close = True
        try:
            return build_send_tx(
                utxo=own_utxo,
                from_pubkey_hash=pkh,
                private_key=kp.private_key,
                public_key=kp.public_key_compressed,
                to_address=to_address,
                amount_sats=amount,
                fee_sats=fee_sats if fee_sats is not None else DEFAULT_FEE_SATOSHIS,
                hrp=self.paths.hrp,
                exclude_outpoints=exclude_outpoints,
            )
        except ValueError as e:
            raise WalletError(str(e)) from e
        finally:
            if close and own_utxo is not None:
                own_utxo.close()
