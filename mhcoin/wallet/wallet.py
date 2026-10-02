"""MHCOIN wallet: create keys, addresses, encrypted persistence, send."""

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
from mhcoin.wallet.key_generation import create_new_keypair
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


class WalletError(Exception):
    pass


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
        return self.data_dir / "utxo.sqlite"


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
    ) -> str:
        """Create a new keypair record in wallet.json.

        make_default=True (GUI "Create New Wallet"): switch active wallet to the
        new record so balance/history/send bind to the new keys — not the previous
        wallet's UTXOs.
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("wallet password required for encrypted storage")
        kp, address = create_new_keypair(hrp=self.paths.hrp)
        wallet_id = uuid.uuid4().hex[:16]
        n, r, p = DEFAULT_SCRYPT_N, DEFAULT_SCRYPT_R, DEFAULT_SCRYPT_P
        enc, _ = encrypt_private_key(kp.private_key, pwd, n=n, r=r, p=p)
        record = WalletRecord(
            wallet_id=wallet_id,
            label=label,
            address=address,
            public_key_hex=kp.public_key_compressed.hex(),
            encrypted_private_key=enc,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        wf = load_wallet_file(self.paths.wallet_file)
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
        self._password = pwd
        if make_default:
            # Drop other unlocked keys so send cannot silently use a prior key.
            self._unlocked_keys = {wallet_id: kp.private_key}
        else:
            self._unlocked_keys[wallet_id] = kp.private_key
        return address

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
        if not self.paths.utxo_path.is_file():
            return 0, 0
        utxo = UTXOSet(self.paths.utxo_path)
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
            if not self.paths.utxo_path.is_file():
                raise WalletError("no chain UTXO yet — mine some MHC first")
            own_utxo = UTXOSet(self.paths.utxo_path)
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
