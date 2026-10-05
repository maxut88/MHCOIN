"""MHCOIN wallet: create keys, addresses, encrypted persistence, send.

Bitcoin-style recovery:
- BIP39 mnemonic (12/24 words) + BIP32 path ``m/84'/0'/0'/0/0``
- Import raw hex or WIF private key

BIP84 HD features on top of the base single/multi-address wallet:
- Account-level xpub export/import (watch-only wallets, ``m/84'/0'/0'``)
- Internal change chain (``m/84'/0'/0'/1/n``) with automatic change-address
  selection on send
- Gap-limit account discovery (receive + change) on restore / xpub import
"""

from __future__ import annotations

import logging
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
from mhcoin.wallet.hd import (
    DEFAULT_DERIVATION_PATH,
    DEFAULT_GAP_LIMIT,
    account_xpub_from_seed,
    change_path,
    decode_xpub,
    derive_private_key,
    derive_pubkey_from_account_xpub,
    is_change_path,
    is_receive_path,
    path_index,
    receive_path,
)
from mhcoin.wallet.send import SendResult, build_account_send_tx, parse_amount_mhc
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

logger = logging.getLogger(__name__)


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
            xpub = account_xpub_from_seed(seed)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=label,
            make_default=make_default,
            mnemonic=mnemonic,
            derivation_path=DEFAULT_DERIVATION_PATH,
            account_id=None,  # set to wallet_id after id is known
            use_wallet_id_as_account=True,
            account_xpub=xpub,
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
        gap_limit: int = DEFAULT_GAP_LIMIT,
    ) -> WalletCreateResult:
        """Restore wallet from BIP39 words (optional BIP39 passphrase).

        Persists the account root at ``path`` (default ``…/0/0``), then runs a
        gap-limit scan of the receive + change chains against on-chain history
        so previously-used addresses are rediscovered automatically.
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("wallet password required for encrypted storage")
        words = normalize_mnemonic(mnemonic)
        if not validate_mnemonic(words):
            raise WalletError("invalid BIP39 mnemonic (bad words or checksum)")
        try:
            seed = mnemonic_to_seed(words, passphrase=passphrase)
            priv = derive_private_key(seed, path)
            xpub = account_xpub_from_seed(seed) if path == DEFAULT_DERIVATION_PATH else None
        except ValueError as e:
            raise WalletError(str(e)) from e
        result = self._persist_key(
            private_key=priv,
            password=pwd,
            label=label,
            make_default=make_default,
            mnemonic=words,
            derivation_path=path,
            account_id=None,
            use_wallet_id_as_account=True,
            account_xpub=xpub,
        )
        if path == DEFAULT_DERIVATION_PATH:
            try:
                self._gap_scan_account(
                    seed=seed,
                    password=pwd,
                    account_id=result.wallet_id,
                    gap_limit=gap_limit,
                )
            except WalletError:
                raise
            except Exception:
                # Discovery is best-effort — a scan failure must never block restore.
                logger.debug("gap scan after restore failed", exc_info=True)
        return result

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
            account_id=None,
            use_wallet_id_as_account=False,
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
        # Derived receive addresses keep account_id of the HD root (where mnemonic lives).
        root = rec
        if not root.encrypted_mnemonic and root.account_id:
            root = next(
                (w for w in wf.wallets if w.wallet_id == root.account_id and w.encrypted_mnemonic),
                None,
            ) or next(
                (w for w in wf.wallets if w.account_id == rec.account_id and w.encrypted_mnemonic),
                None,
            )
        if root is None or not root.encrypted_mnemonic:
            raise WalletError("this wallet has no stored mnemonic (imported key only)")
        kdf = wf.kdf
        try:
            raw = decrypt_private_key(
                root.encrypted_mnemonic,
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

    def export_account_xpub(
        self, *, password: str | None = None, wallet_id: str | None = None
    ) -> str:
        """Return the BIP84 account xpub (``m/84'/0'/0'``) for watch-only sharing.

        Uses the ``account_xpub`` stored on the account root if present,
        otherwise derives it from the stored mnemonic (requires password).
        """
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found")
        if wallet_id:
            rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
            if rec is None:
                raise WalletError("unknown wallet id")
        else:
            rec = self._default_record()
        acc = self._account_id_for(rec)
        root = next((w for w in wf.wallets if w.wallet_id == acc), None) or rec
        if root.account_xpub:
            return root.account_xpub
        if not root.encrypted_mnemonic:
            raise WalletError("this wallet has no BIP39 seed (imported key only)")
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to derive account xpub")
        words = self.export_mnemonic(password=pwd, wallet_id=root.wallet_id)
        try:
            xpub = account_xpub_from_seed(mnemonic_to_seed(words))
        except ValueError as e:
            raise WalletError(str(e)) from e
        # Backfill so future exports (and watch-only sharing) don't need the password.
        root.account_xpub = xpub
        save_wallet_file(self.paths.wallet_file, wf)
        return xpub

    def import_account_xpub(self, xpub: str, *, label: str = "watch") -> WalletCreateResult:
        """Import a BIP84 account xpub as a watch-only wallet.

        Persists the external address at index 0 (``…/0/0``) as the account
        root, then gap-scans both chains using public derivation only.
        """
        try:
            decode_xpub(xpub)
            pub0, path0 = derive_pubkey_from_account_xpub(xpub, change=False, index=0)
        except ValueError as e:
            raise WalletError(f"invalid account xpub: {e}") from e
        result = self._persist_key(
            password=None,
            label=label,
            make_default=False,
            mnemonic=None,
            derivation_path=path0,
            account_id=None,
            use_wallet_id_as_account=True,
            watch_only=True,
            account_xpub=xpub,
            public_key=pub0,
        )
        try:
            self._gap_scan_xpub(xpub=xpub, account_id=result.wallet_id)
        except Exception:
            logger.debug("gap scan after xpub import failed", exc_info=True)
        return result

    def _persist_key(
        self,
        *,
        private_key: bytes | None = None,
        password: str | None = None,
        label: str,
        make_default: bool,
        mnemonic: str | None,
        derivation_path: str | None,
        account_id: str | None = None,
        use_wallet_id_as_account: bool = False,
        watch_only: bool = False,
        account_xpub: str | None = None,
        public_key: bytes | None = None,
    ) -> WalletCreateResult:
        if watch_only:
            if public_key is None:
                raise WalletError("watch-only import requires a public key")
            pub = public_key
        else:
            if private_key is None:
                raise WalletError("private key required")
            if not password:
                raise WalletError("wallet password required for encrypted storage")
            pub = private_key_to_public_key(private_key, compressed=True)
        address = pubkey_to_address(pub, hrp=self.paths.hrp)
        # Reject duplicate addresses in the same wallet file.
        existing = load_wallet_file(self.paths.wallet_file)
        if existing:
            for w in existing.wallets:
                if w.address == address or w.public_key_hex == pub.hex():
                    raise WalletError(f"key already in wallet ({address})")
        wallet_id = uuid.uuid4().hex[:16]
        acc = wallet_id if use_wallet_id_as_account else account_id
        n, r, p = DEFAULT_SCRYPT_N, DEFAULT_SCRYPT_R, DEFAULT_SCRYPT_P
        enc = ""
        enc_mnemonic = None
        if not watch_only:
            enc, _ = encrypt_private_key(private_key, password, n=n, r=r, p=p)
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
            account_id=acc,
            watch_only=watch_only,
            account_xpub=account_xpub,
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
        if not watch_only:
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
            wif=None if watch_only else encode_wif(private_key, compressed=True),
        )

    def _account_id_for(self, rec: WalletRecord) -> str:
        return rec.account_id or rec.wallet_id

    def _parse_receive_index(self, path: str | None) -> int | None:
        if not path or not is_receive_path(path):
            return None
        return path_index(path)

    def _parse_change_index(self, path: str | None) -> int | None:
        if not path or not is_change_path(path):
            return None
        return path_index(path)

    def _used_pubkey_hashes(self) -> set[bytes]:
        """Best-effort set of every pubkey-hash ever seen on-chain or in the UTXO set.

        Used for gap-limit account discovery. UTXO coins alone catch addresses
        with a current balance; the optional one-pass block scan (via the
        read-only chain reader, safe to open alongside a live node) also
        catches addresses that received and later fully spent their funds.
        """
        used: set[bytes] = set()
        cs = self.paths.utxo_path
        legacy = cs.with_name("utxo.sqlite")
        if cs.is_dir() or legacy.is_file():
            try:
                utxo = UTXOSet(cs)
                try:
                    used |= utxo.pubkey_hashes_with_coins()
                finally:
                    utxo.close()
            except Exception:
                logger.debug("UTXO scan for gap-limit discovery failed", exc_info=True)
        try:
            from mhcoin.blockchain.readonly_chain import ReadOnlyChain

            rc = ReadOnlyChain(self.paths.data_dir)
            try:
                height = rc.height
                for h in range(height + 1):
                    block = rc.get_block_by_height(h)
                    if block is None:
                        continue
                    for tx in block.transactions:
                        for out in tx.outputs:
                            try:
                                used.add(out.pubkey_hash())
                            except Exception:
                                continue
            finally:
                rc.close()
        except Exception:
            logger.debug("chain scan for gap-limit discovery failed", exc_info=True)
        return used

    def _gap_scan_account(
        self,
        *,
        seed: bytes,
        password: str,
        account_id: str,
        gap_limit: int = DEFAULT_GAP_LIMIT,
    ) -> list[WalletCreateResult]:
        """Discover previously-used receive/change addresses from a seed.

        Standard BIP44-style gap-limit scan: stop a chain once ``gap_limit``
        consecutive unused indices are seen. The account root (…/0/0) is
        expected to already be persisted and is skipped as a duplicate.
        """
        used = self._used_pubkey_hashes()
        discovered: list[WalletCreateResult] = []
        if not used:
            return discovered
        wf = load_wallet_file(self.paths.wallet_file)
        existing_addrs = {w.address for w in (wf.wallets if wf else [])}
        for change in (False, True):
            consecutive_unused = 0
            i = 0
            while consecutive_unused < gap_limit:
                path = change_path(i) if change else receive_path(i)
                try:
                    priv = derive_private_key(seed, path)
                except ValueError:
                    break
                pub = private_key_to_public_key(priv, compressed=True)
                pkh = hash160(pub)
                address = pubkey_to_address(pub, hrp=self.paths.hrp)
                if pkh in used:
                    consecutive_unused = 0
                    if address not in existing_addrs:
                        label = f"{'change' if change else 'receive'} #{i}"
                        result = self._persist_key(
                            private_key=priv,
                            password=password,
                            label=label,
                            make_default=False,
                            mnemonic=None,
                            derivation_path=path,
                            account_id=account_id,
                            use_wallet_id_as_account=False,
                        )
                        discovered.append(result)
                        existing_addrs.add(address)
                else:
                    consecutive_unused += 1
                i += 1
                if i > 100_000:
                    break
        return discovered

    def _gap_scan_xpub(
        self,
        *,
        xpub: str,
        account_id: str,
        gap_limit: int = DEFAULT_GAP_LIMIT,
    ) -> list[WalletCreateResult]:
        """Like ``_gap_scan_account`` but public-key-only (for watch-only imports)."""
        used = self._used_pubkey_hashes()
        discovered: list[WalletCreateResult] = []
        if not used:
            return discovered
        wf = load_wallet_file(self.paths.wallet_file)
        existing_addrs = {w.address for w in (wf.wallets if wf else [])}
        for change in (False, True):
            consecutive_unused = 0
            i = 0
            while consecutive_unused < gap_limit:
                try:
                    pub, path = derive_pubkey_from_account_xpub(xpub, change=change, index=i)
                except ValueError:
                    break
                pkh = hash160(pub)
                address = pubkey_to_address(pub, hrp=self.paths.hrp)
                if pkh in used:
                    consecutive_unused = 0
                    if address not in existing_addrs:
                        label = f"{'change' if change else 'receive'} #{i}"
                        result = self._persist_key(
                            password=None,
                            label=label,
                            make_default=False,
                            mnemonic=None,
                            derivation_path=path,
                            account_id=account_id,
                            use_wallet_id_as_account=False,
                            watch_only=True,
                            public_key=pub,
                        )
                        discovered.append(result)
                        existing_addrs.add(address)
                else:
                    consecutive_unused += 1
                i += 1
                if i > 100_000:
                    break
        return discovered

    def list_receive_addresses(self, *, wallet_id: str | None = None) -> list[dict]:
        """List HD *external* receive addresses for the active (or given) account.

        Change-chain (…/1/n) addresses are excluded; imported no-path entries
        are kept (they still own a single receivable address).
        """
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            return []
        if wallet_id:
            rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
            if rec is None:
                raise WalletError("unknown wallet id")
        else:
            rec = self._default_record()
        acc = self._account_id_for(rec)
        rows: list[dict] = []
        for w in wf.wallets:
            if self._account_id_for(w) != acc:
                continue
            if is_change_path(w.derivation_path):
                continue
            # Imported keys (no path) still show as a single entry.
            idx = self._parse_receive_index(w.derivation_path)
            rows.append(
                {
                    "wallet_id": w.wallet_id,
                    "address": w.address,
                    "label": w.label,
                    "path": w.derivation_path,
                    "index": idx,
                    "is_default": w.wallet_id == (wf.default_wallet_id or ""),
                    "watch_only": w.watch_only,
                    "has_seed": bool(w.encrypted_mnemonic)
                    or any(
                        x.wallet_id == acc and x.encrypted_mnemonic for x in wf.wallets
                    ),
                }
            )
        rows.sort(key=lambda r: (r["index"] is None, r["index"] if r["index"] is not None else 0))
        return rows

    def account_records(self, *, wallet_id: str | None = None) -> list[WalletRecord]:
        """All records (receive + change + imported) sharing one HD account."""
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            return []
        if wallet_id:
            rec = next((w for w in wf.wallets if w.wallet_id == wallet_id), None)
            if rec is None:
                raise WalletError("unknown wallet id")
        else:
            rec = self._default_record()
        acc = self._account_id_for(rec)
        return [w for w in wf.wallets if self._account_id_for(w) == acc]

    def account_pubkey_hashes(self, *, wallet_id: str | None = None) -> set[bytes]:
        """Every pubkey-hash owned by the account (receive + change + imported)."""
        out: set[bytes] = set()
        for w in self.account_records(wallet_id=wallet_id):
            try:
                out.add(address_to_pubkey_hash(w.address, hrp=self.paths.hrp))
            except Exception:
                continue
        return out

    def account_addresses(self, *, wallet_id: str | None = None) -> list[str]:
        """Every address owned by the account (receive + change + imported)."""
        return [w.address for w in self.account_records(wallet_id=wallet_id)]

    def new_receive_address(
        self,
        *,
        password: str | None = None,
        make_default: bool = True,
    ) -> WalletCreateResult:
        """Derive the next unused receive address for the active HD account.

        Requires a BIP39 mnemonic on the account root. Imported single-key
        wallets cannot create more addresses.
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required")
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found")
        active = self._default_record()
        acc = self._account_id_for(active)
        root = next(
            (w for w in wf.wallets if w.wallet_id == acc and w.encrypted_mnemonic),
            None,
        )
        if root is None:
            root = next(
                (w for w in wf.wallets if self._account_id_for(w) == acc and w.encrypted_mnemonic),
                None,
            )
        if root is None or not root.encrypted_mnemonic:
            raise WalletError(
                "cannot derive addresses — this wallet has no BIP39 seed "
                "(imported key only). Create/restore from seed to use multi-address."
            )
        # Backfill account_id on pre-multi-address HD roots.
        if not root.account_id:
            root.account_id = root.wallet_id
            save_wallet_file(self.paths.wallet_file, wf)
            acc = root.wallet_id
        words = self.export_mnemonic(password=pwd, wallet_id=root.wallet_id)
        used: set[int] = set()
        for w in wf.wallets:
            if self._account_id_for(w) != acc:
                continue
            idx = self._parse_receive_index(w.derivation_path)
            if idx is not None:
                used.add(idx)
        next_i = 0
        while next_i in used:
            next_i += 1
            if next_i > 100_000:
                raise WalletError("receive index exhausted")
        path = receive_path(next_i)
        try:
            seed = mnemonic_to_seed(words)
            priv = derive_private_key(seed, path)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=f"receive #{next_i}",
            make_default=make_default,
            mnemonic=None,
            derivation_path=path,
            account_id=acc,
            use_wallet_id_as_account=False,
        )

    def ensure_change_address(self, password: str | None = None) -> WalletCreateResult:
        """Return an unused internal change address (``…/1/n``) for the active account.

        Prefers an already-stored change address with zero current balance;
        otherwise derives the next unused change index from the seed.
        """
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required")
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found")
        active = self._default_record()
        acc = self._account_id_for(active)
        account_recs = [w for w in wf.wallets if self._account_id_for(w) == acc]
        change_recs = sorted(
            (w for w in account_recs if is_change_path(w.derivation_path)),
            key=lambda r: path_index(r.derivation_path) or 0,
        )

        cs = self.paths.utxo_path
        legacy = cs.with_name("utxo.sqlite")
        has_chain = cs.is_dir() or legacy.is_file()
        picked: WalletRecord | None = None
        if change_recs:
            if has_chain:
                utxo = UTXOSet(cs)
                try:
                    for w in change_recs:
                        pkh = address_to_pubkey_hash(w.address, hrp=self.paths.hrp)
                        if utxo.balance_for_pubkey_hash(pkh) == 0:
                            picked = w
                            break
                finally:
                    utxo.close()
            else:
                picked = change_recs[0]
        if picked is not None:
            return WalletCreateResult(
                address=picked.address,
                wallet_id=picked.wallet_id,
                derivation_path=picked.derivation_path,
            )

        # No unused change address available — derive the next one from seed.
        root = next((w for w in account_recs if w.encrypted_mnemonic), None)
        if root is None or not root.encrypted_mnemonic:
            raise WalletError(
                "cannot derive a change address — this wallet has no BIP39 seed "
                "(imported key only)."
            )
        if not root.account_id:
            root.account_id = root.wallet_id
            save_wallet_file(self.paths.wallet_file, wf)
            acc = root.wallet_id
        words = self.export_mnemonic(password=pwd, wallet_id=root.wallet_id)
        used_idx = {
            path_index(w.derivation_path) for w in change_recs if path_index(w.derivation_path) is not None
        }
        next_i = 0
        while next_i in used_idx:
            next_i += 1
            if next_i > 100_000:
                raise WalletError("change index exhausted")
        path = change_path(next_i)
        try:
            seed = mnemonic_to_seed(words)
            priv = derive_private_key(seed, path)
        except ValueError as e:
            raise WalletError(str(e)) from e
        return self._persist_key(
            private_key=priv,
            password=pwd,
            label=f"change #{next_i}",
            make_default=False,
            mnemonic=None,
            derivation_path=path,
            account_id=acc,
            use_wallet_id_as_account=False,
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
        if rec.watch_only or not rec.encrypted_private_key:
            raise WalletError("watch-only wallet has no private key to unlock")
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

    def balance(
        self, address: str | None = None, *, account: bool = True
    ) -> tuple[int, int]:
        """Confirmed/unconfirmed balance in satoshis.

        - ``account=True`` (default): sum UTXOs across every pubkey-hash in the
          HD account that owns ``address`` (or the active wallet's account when
          ``address`` is omitted). For single-key wallets this is identical to
          the single-address balance, so plain ``balance()`` stays backward
          compatible.
        - ``account=False``: single-address balance only.
        """
        cs = self.paths.utxo_path
        legacy = cs.with_name("utxo.sqlite")
        if not (cs.is_dir() or legacy.is_file()):
            return 0, 0

        if not account:
            addr = address or self.default_address()
            if not validate_address(addr, hrp=self.paths.hrp):
                raise WalletError("invalid address")
            pkh = address_to_pubkey_hash(addr, hrp=self.paths.hrp)
            utxo = UTXOSet(cs)
            try:
                return utxo.balance_for_pubkey_hash(pkh), 0
            finally:
                utxo.close()

        wallet_id: str | None = None
        if address is not None:
            if not validate_address(address, hrp=self.paths.hrp):
                raise WalletError("invalid address")
            wf = load_wallet_file(self.paths.wallet_file)
            rec = next((w for w in (wf.wallets if wf else []) if w.address == address), None)
            if rec is None:
                # Not a tracked wallet address (e.g. foreign address) — fall back
                # to a plain single-address lookup so callers still get an answer.
                pkh = address_to_pubkey_hash(address, hrp=self.paths.hrp)
                utxo = UTXOSet(cs)
                try:
                    return utxo.balance_for_pubkey_hash(pkh), 0
                finally:
                    utxo.close()
            wallet_id = rec.wallet_id

        pkhs = self.account_pubkey_hashes(wallet_id=wallet_id)
        if not pkhs:
            return 0, 0
        utxo = UTXOSet(cs)
        try:
            return utxo.balance_for_pubkey_hashes(pkhs), 0
        finally:
            utxo.close()

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
        """Spend from every spendable key in the active HD account.

        Refuses watch-only wallets. Unlocks all non-watch-only keys that share
        the active account, selects coins across all of them, and sends change
        to a fresh (or reused, zero-balance) internal change address.
        """
        from mhcoin.constants import DEFAULT_FEE_SATOSHIS

        if not validate_address(to_address, hrp=self.paths.hrp):
            raise WalletError("invalid destination address")
        amount = parse_amount_mhc(amount_text)
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required to unlock wallet")

        active = self._default_record()
        if active.watch_only:
            raise WalletError("watch-only wallet cannot send — import the private key or seed")
        acc = self._account_id_for(active)
        wf = load_wallet_file(self.paths.wallet_file)
        if not wf or not wf.wallets:
            raise WalletError("no wallet found")

        keys_by_pkh: dict[bytes, tuple[bytes, bytes]] = {}
        last_err: Exception | None = None
        for rec in wf.wallets:
            if self._account_id_for(rec) != acc:
                continue
            if rec.watch_only or not rec.encrypted_private_key:
                continue
            try:
                kp = self.unlock_key(rec.wallet_id, pwd)
            except WalletError as e:
                last_err = e
                continue
            keys_by_pkh[hash160(kp.public_key_compressed)] = (
                kp.private_key,
                kp.public_key_compressed,
            )
        if not keys_by_pkh:
            raise WalletError("wrong password or corrupt wallet") from last_err

        change_result = self.ensure_change_address(pwd)
        change_pkh = address_to_pubkey_hash(change_result.address, hrp=self.paths.hrp)

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
            return build_account_send_tx(
                utxo=own_utxo,
                keys_by_pkh=keys_by_pkh,
                to_address=to_address,
                amount_sats=amount,
                fee_sats=fee_sats if fee_sats is not None else DEFAULT_FEE_SATOSHIS,
                change_pubkey_hash=change_pkh,
                hrp=self.paths.hrp,
                exclude_outpoints=exclude_outpoints,
            )
        except ValueError as e:
            raise WalletError(str(e)) from e
        finally:
            if close and own_utxo is not None:
                own_utxo.close()
