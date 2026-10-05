"""Desktop controller: bridges GUI to MHCOIN core (wallet / chain / miner / optional node).

Uses one shared LocalNode + lock so GUI refresh and mining do not race the same datadir
(that race produced endless "UTXO tip mismatch — rebuilding" spam).
"""

from __future__ import annotations

import json

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mhcoin.blockchain.disk_lock import chain_disk_lock
from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.blockchain.chain import ChainError
from mhcoin.config_loader import resolve_data_dir
from mhcoin.consensus.params import get_network_params
from mhcoin.desktop.prefs import (
    clamp_mine_intensity,
    load_mine_intensity,
    save_mine_intensity,
    save_preferred_network,
)
from mhcoin.desktop.seeds import default_connect_peers
from mhcoin.mempool import Mempool
from mhcoin.mining.abortable_pow import (
    MiningAborted,
    mine_block_cancellable,
)
from mhcoin.mining.miner import format_mine_plain_found
from mhcoin.mining.block_template import build_block_template
from mhcoin.node.local_node import LocalNode
from mhcoin.node.runtime import NodeRuntime
from mhcoin.wallet.addresses import address_to_pubkey_hash, pubkey_hash_to_address, validate_address
from mhcoin.wallet.send import format_mhc, parse_amount_mhc
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths

logger = logging.getLogger("mhcoin.desktop")


@dataclass
class TxRow:
    kind: str  # mining / send / receive
    amount_sats: int
    detail: str
    height: int | None = None
    txid: str | None = None
    timestamp: int | None = None  # unix seconds (block time)
    from_addrs: list[str] = field(default_factory=list)
    to_addrs: list[str] = field(default_factory=list)
    fee_sats: int | None = None
    confirmations: int | None = None


@dataclass
class AppState:
    network: str = "localnet"
    data_dir: Path = field(default_factory=lambda: resolve_data_dir("localnet"))
    password: str | None = None


class CoreController:
    def __init__(self, network: str = "localnet", data_dir: Path | None = None):
        params = get_network_params(network)
        self.network = params.name
        self.hrp = params.address_hrp
        self.data_dir = (data_dir or resolve_data_dir(params.name)).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = WalletPaths(data_dir=self.data_dir, network=params.name, hrp=params.address_hrp)
        self._password: str | None = None
        self._io = threading.RLock()
        self._local: LocalNode | None = None
        self._miner_thread: threading.Thread | None = None
        self._miner_stop = threading.Event()
        self._miner_reconfigure = threading.Event()
        self._mining = False
        self._mine_stopping = False
        self._blocks_found = 0
        self._rewards_sats = 0
        self._hashrate = 0.0
        self._node: NodeRuntime | None = None
        self._node_thread: threading.Thread | None = None
        self._last_txid: str | None = None
        self._hist_key: tuple | None = None
        self._hist_rows: list[TxRow] = []
        self._hist_dicts: list[dict] = []
        self._hist_building = False
        self._hist_build_error: str | None = None
        self._hist_need_full = False
        self._hist_build_lock = threading.Lock()
        self._resume_node_after_mine = False
        self._node_ctrl = threading.RLock()
        self._tip_height_hint = 0
        self._balance_cache_sats: int = 0
        self._balance_cache_valid: bool = False
        self._balance_cache_height: int = -1
        self._recent_cache: list[dict] = []
        self._mine_log: deque[str] = deque(maxlen=500)
        self._mine_log_lock = threading.Lock()
        self._mine_log_last_prog = 0.0
        self._mine_intensity = load_mine_intensity(100)
        self._mine_workers = self._workers_for_intensity(self._mine_intensity)
        self._caffeine_proc = None

    @staticmethod
    def _txrow_to_dict(r: TxRow) -> dict:
        kind = (r.kind or "").strip()
        kl = kind.lower()
        if "mining" in kl or "reward" in kl:
            title = "Block found"
            type_key = "mined"
            sign = "+"
        elif kind == "Sent" or kind.startswith("Sent"):
            title = "Sent" if kind == "Sent" else kind
            type_key = "sent"
            sign = "-"
        elif kind in ("Received", "Receive") or kind.startswith("Received"):
            title = "Received" if kind in ("Received", "Receive") else kind
            type_key = "received"
            sign = "+"
        else:
            title = kind or "Activity"
            type_key = "other"
            sign = "+"
        amount = abs(int(r.amount_sats)) / 1e8
        txid = r.txid or ""
        short = (txid[:12] + "…" + txid[-10:]) if len(txid) > 28 else txid
        height = r.height
        ts = r.timestamp
        time_utc = ""
        # Block header timestamps are unix seconds. Reject tiny values (e.g. mistaken
        # confirmations=1) so the UI never shows epoch "1970-01-01 00:00:01".
        try:
            ts_i = int(ts) if ts is not None else 0
        except (TypeError, ValueError):
            ts_i = 0
        if ts_i >= 1_000_000_000:
            try:
                # Local wall clock — same timezone basis as mining log [HH:MM:SS].
                time_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_i))
            except Exception:
                time_utc = str(ts_i)
        ts = ts_i if ts_i >= 1_000_000_000 else None
        fee = None
        if r.fee_sats is not None:
            fee = f"{abs(int(r.fee_sats)) / 1e8:.8f}"
        return {
            "kind": kind,
            "type": type_key,
            "title": title,
            "amount": f"{sign}{amount:.8f}",
            "amount_mhc": f"{amount:.8f}",
            "sign": sign,
            "height": height,
            "txid": txid,
            "txid_short": short,
            "timestamp": ts,
            "time_utc": time_utc,
            "from": list(r.from_addrs or []),
            "to": list(r.to_addrs or []),
            "fee_mhc": fee,
            "confirmations": r.confirmations,
            "text": (
                f"{title}  {sign}{amount:.8f} MHC"
                + (f"  ·  block #{height}" if height is not None else "")
            ),
        }

    def _push_recent(self, row: TxRow) -> None:
        item = self._txrow_to_dict(row)
        # Newest first; dedupe by txid+kind when possible.
        self._recent_cache = [
            x
            for x in self._recent_cache
            if not (item["txid"] and x.get("txid") == item["txid"] and x.get("kind") == item["kind"])
        ]
        self._recent_cache.insert(0, item)
        self._recent_cache = self._recent_cache[:50]

    def refresh_recent_cache(self, *, limit: int = 25, full_chain: bool = False) -> list[dict]:
        """Scan chain into recent cache when LocalNode can own the datadir."""
        if self._node is not None:
            return list(self._recent_cache)[:limit]
        try:
            rows = self.recent_transactions(limit, full_chain=full_chain)
            self._recent_cache = [self._txrow_to_dict(r) for r in rows]
        except Exception:
            logger.debug("refresh_recent_cache failed", exc_info=True)
        return list(self._recent_cache)[:limit]

    def recent_for_ui(self, limit: int = 25) -> list[dict]:
        # Never block the UI thread on a chain scan — serve cache and refresh async.
        if self._hist_tip_stale() or self._hist_key is None or self._hist_key_is_partial():
            self.request_history_build(full_chain=True)
        # Prefer full history snapshot (all mining rewards) over the short recent trim.
        if self._hist_dicts:
            return list(self._hist_dicts)[:limit]
        if self._recent_cache:
            return list(self._recent_cache)[:limit]
        self.request_history_build(full_chain=True)
        return list(self._recent_cache)[:limit]

    def switch_network(self, network: str) -> dict:
        """Switch Desktop to another network (separate datadir). Requires re-unlock."""
        params = get_network_params(network)
        if params.name == self.network:
            return {
                "network": self.network,
                "data_dir": str(self.data_dir),
                "genesis_hash": params.genesis_hash_hex,
                "seeds": default_connect_peers(params.name),
                "wallet_exists": self.wallet_exists(),
                "changed": False,
            }
        self.stop_mining()
        self.stop_node()
        with self._io:
            self._close_local()
        self._password = None
        self._last_txid = None
        self._resume_node_after_mine = False
        self._reset_session_wallet_stats()
        self._invalidate_history_cache()
        self.network = params.name
        self.hrp = params.address_hrp
        # Clear sticky MHCOIN_DATA so each network uses ~/.mhcoin/<net>
        os.environ.pop("MHCOIN_DATA", None)
        self.data_dir = resolve_data_dir(params.name).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.paths = WalletPaths(
            data_dir=self.data_dir, network=params.name, hrp=params.address_hrp
        )
        save_preferred_network(params.name)
        return {
            "network": self.network,
            "data_dir": str(self.data_dir),
            "genesis_hash": params.genesis_hash_hex,
            "seeds": default_connect_peers(params.name),
            "wallet_exists": self.wallet_exists(),
            "changed": True,
        }

    def _get_local(self) -> LocalNode:
        """Single LocalNode for this controller (caller must hold self._io)."""
        if self._local is None:
            self._local = LocalNode(self.data_dir, hrp=self.hrp, network=self.network)
        return self._local

    def _close_local(self) -> None:
        if self._local is not None:
            try:
                self._local.close()
            except Exception:
                pass
            self._local = None

    # --- wallet -------------------------------------------------------------

    def wallet_exists(self) -> bool:
        return self.paths.wallet_file.is_file()

    def wallet_file_path(self) -> Path:
        return self.paths.wallet_file

    def backup_wallet(self) -> dict:
        """Copy encrypted wallet.json to data_dir/backups and return download payload.

        The backup file is still password-encrypted. The wallet password is NEVER
        written into the backup — the user must store the password separately.
        """
        src = self.paths.wallet_file
        if not src.is_file():
            raise WalletError("no wallet file to back up — create a wallet first")
        content = src.read_text(encoding="utf-8")
        # Basic sanity: must look like our wallet store
        try:
            raw = __import__("json").loads(content)
        except Exception as e:  # noqa: BLE001
            raise WalletError(f"wallet file unreadable: {e}") from e
        if not isinstance(raw, dict) or "wallets" not in raw:
            raise WalletError("wallet file is not a valid MHCOIN wallet.json")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        fname = f"MHCOIN-wallet-{self.network}-backup-{stamp}.json"
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / fname
        dest.write_text(content, encoding="utf-8")
        try:
            dest.chmod(0o600)
        except OSError:
            pass

        addr = ""
        try:
            addr = self.default_address()
        except WalletError:
            pass

        return {
            "ok": True,
            "filename": fname,
            "content": content,
            "saved_path": str(dest),
            "wallet_path": str(src),
            "network": self.network,
            "address": addr,
            "reminder": (
                "Backup = this encrypted wallet file + your password. "
                "The password is NOT inside the file. Store both safely."
            ),
        }

    def create_wallet(self, password: str) -> str:
        """Create HD wallet; returns address (mnemonic via create_hd_wallet)."""
        return self.create_hd_wallet(password)["address"]

    def create_hd_wallet(self, password: str) -> dict[str, str]:
        """Create BIP39 HD wallet. Returns address + mnemonic (show once)."""
        if not password:
            raise WalletError("password required")
        if self._mining:
            self.stop_mining()
        existing = len(Wallet(self.paths).list_wallets())
        label = f"Wallet {existing + 1}"
        w = Wallet(self.paths, password=password)
        created = w.create(label=label, password=password, make_default=True)
        self._password = password
        self._reset_session_wallet_stats()
        self.ensure_chain()
        return {
            "address": created.address,
            "mnemonic": created.mnemonic or "",
            "wallet_id": created.wallet_id,
        }

    def restore_wallet(self, password: str, mnemonic: str) -> str:
        """Restore HD wallet from BIP39 words; returns address."""
        if not password:
            raise WalletError("password required")
        if not (mnemonic or "").strip():
            raise WalletError("mnemonic required")
        if self._mining:
            self.stop_mining()
        existing = len(Wallet(self.paths).list_wallets())
        label = f"Restored {existing + 1}"
        w = Wallet(self.paths, password=password)
        created = w.restore_from_mnemonic(
            mnemonic,
            password=password,
            label=label,
            make_default=True,
        )
        self._password = password
        self._reset_session_wallet_stats()
        self.ensure_chain()
        return created.address

    def export_seed(self, password: str | None = None) -> str:
        """Reveal stored BIP39 mnemonic for the active wallet."""
        pwd = password or self._password
        if not pwd:
            raise WalletError("password required")
        return Wallet(self.paths, password=pwd).export_mnemonic(password=pwd)

    def export_xpub(self, password: str | None = None) -> str:
        """Export BIP84 account xpub (m/84'/0'/0') for watch-only use."""
        pwd = password or self._password
        return Wallet(self.paths, password=pwd).export_account_xpub(password=pwd)

    def import_xpub(self, xpub: str, *, label: str = "watch") -> str:
        """Import account xpub as watch-only; returns first receive address."""
        if self._mining:
            self.stop_mining()
        created = Wallet(self.paths).import_account_xpub(xpub, label=label)
        self._password = None
        self._reset_session_wallet_stats()
        self._refresh_balance_cache(created.address)
        return created.address

    # --- address book (local labels, never broadcast) -----------------------

    def address_book(self) -> dict[str, str]:
        from mhcoin.desktop.address_book import load_address_book

        return load_address_book(self.data_dir)

    def set_address_label(self, address: str, label: str) -> dict[str, str]:
        from mhcoin.desktop.address_book import set_label

        addr = (address or "").strip()
        if not validate_address(addr, hrp=self.hrp):
            raise WalletError("invalid address")
        return set_label(self.data_dir, addr, label)

    def remove_address_label(self, address: str) -> dict[str, str]:
        from mhcoin.desktop.address_book import remove_label

        return remove_label(self.data_dir, address)

    # --- receive QR (mhcoin: URI, offline SVG) -------------------------------

    def receive_qr(self, address: str | None = None, amount_mhc: str | None = None) -> dict:
        """SVG QR for ``mhcoin:<address>[?amount=]`` (BIP21-style, offline).

        Reuses the explorer's vendored qrcodegen — no extra dependency. The
        explorer page itself still encodes a plain address, so Desktop (and
        the explorer) both stay scannable by any plain MHC address reader.
        """
        from mhcoin.desktop.uri import build_payment_uri
        from mhcoin.explorer.decode import qr_svg

        addr = (address or "").strip() or self.default_address()
        if not validate_address(addr, hrp=self.hrp):
            raise WalletError("invalid address")
        uri = build_payment_uri(addr, amount_mhc)
        return {"address": addr, "uri": uri, "svg": qr_svg(uri)}

    def _invalidate_balance_cache(self) -> None:
        self._balance_cache_sats = 0
        self._balance_cache_valid = False
        self._balance_cache_height = -1

    def _refresh_balance_cache(self, address: str | None = None) -> None:
        """Recompute balance cache for the active HD account (all receive+change).

        Prefer live NodeRuntime UTXO while the P2P node owns the datadir.
        Must run on every UI poll — otherwise Overview freezes on a stale
        total while peers keep finding blocks (e.g. 9000 vs chain 9100).
        """
        try:
            addr = address or self.default_address()
        except WalletError:
            self._invalidate_balance_cache()
            return
        try:
            w = Wallet(self.paths)
            pkhs = w.account_pubkey_hashes()
            if not pkhs:
                pkh = address_to_pubkey_hash(addr, hrp=self.hrp)
                pkhs = {pkh}
        except Exception:
            self._invalidate_balance_cache()
            return
        rt = self._node
        live = (
            rt is not None
            and not getattr(rt, "_stopped", False)
            and getattr(rt, "_running", False)
        )
        if live:
            assert rt is not None
            try:
                try:
                    tip_now = rt.chain.tip_hash.hex() if rt.chain.tip_hash else ""
                    last_tip = str(getattr(self, "_balance_repair_tip", "") or "")
                    if tip_now and tip_now != last_tip:
                        rt.chain._repair_utxo_if_needed()
                        self._balance_repair_tip = tip_now
                except Exception:
                    logger.debug("live UTXO repair skipped", exc_info=True)
                with rt.chain._lock:
                    height = int(rt.chain.height)
                    if hasattr(rt.chain.utxo, "balance_for_pubkey_hashes"):
                        self._balance_cache_sats = int(
                            rt.chain.utxo.balance_for_pubkey_hashes(pkhs)
                        )
                    else:
                        self._balance_cache_sats = sum(
                            int(rt.chain.utxo.balance_for_pubkey_hash(p)) for p in pkhs
                        )
                self._tip_height_hint = max(0, height)
                self._balance_cache_height = height
                self._balance_cache_valid = True
                return
            except Exception:
                logger.debug("live balance refresh failed", exc_info=True)
                return
        if self._mining or self._node is not None:
            return
        try:
            with self._io:
                node = self._get_local()
                height = int(node.chain.height)
                if hasattr(node.chain.utxo, "balance_for_pubkey_hashes"):
                    self._balance_cache_sats = int(
                        node.chain.utxo.balance_for_pubkey_hashes(pkhs)
                    )
                else:
                    self._balance_cache_sats = sum(
                        int(node.chain.utxo.balance_for_pubkey_hash(p)) for p in pkhs
                    )
                self._tip_height_hint = max(0, height)
                self._balance_cache_height = height
                self._balance_cache_valid = True
        except Exception:
            self._invalidate_balance_cache()

    def _reset_session_wallet_stats(self) -> None:
        """Clear process-local mining counters when the active wallet changes."""
        self._blocks_found = 0
        self._rewards_sats = 0
        self._hashrate = 0.0
        self._invalidate_balance_cache()
        self._invalidate_history_cache()

    def list_wallets(self) -> list[dict]:
        rows = []
        for rec in Wallet(self.paths).list_wallets():
            rows.append(
                {
                    "wallet_id": rec.wallet_id,
                    "label": rec.label,
                    "address": rec.address,
                    "path": rec.derivation_path,
                    "account_id": rec.account_id,
                    "public_key_fp": hashlib.sha256(bytes.fromhex(rec.public_key_hex)).hexdigest()[
                        :16
                    ],
                    "watch_only": bool(getattr(rec, "watch_only", False)),
                }
            )
        return rows

    def active_watch_only(self) -> bool:
        """True when the active wallet has no private key (xpub-imported)."""
        try:
            addr = self.default_address()
        except WalletError:
            return False
        for w in self.list_wallets():
            if w.get("address") == addr:
                return bool(w.get("watch_only"))
        return False

    def list_receive_addresses(self) -> list[dict]:
        return Wallet(self.paths).list_receive_addresses()

    def new_receive_address(self) -> dict[str, str]:
        """Derive next HD receive address; requires unlocked BIP39 wallet."""
        if not self._password:
            raise WalletError("unlock wallet first")
        created = Wallet(self.paths, password=self._password).new_receive_address(
            password=self._password,
            make_default=True,
        )
        self._reset_session_wallet_stats()
        self._refresh_balance_cache(created.address)
        self._invalidate_history_cache()
        return {
            "address": created.address,
            "wallet_id": created.wallet_id,
            "path": created.derivation_path or "",
        }

    def select_wallet(self, wallet_id: str) -> str:
        if self._mining:
            self.stop_mining()
        pwd = self._password
        w = Wallet(self.paths, password=pwd)
        # Watch-only rows have no private key — switch default without unlock.
        rec = next((r for r in w.list_wallets() if r.wallet_id == wallet_id), None)
        if rec is not None and (getattr(rec, "watch_only", False) or not rec.encrypted_private_key):
            w.set_default_wallet_id(wallet_id)
            self._reset_session_wallet_stats()
            self._refresh_balance_cache(rec.address)
            return rec.address
        w.set_default_wallet_id(wallet_id)
        self._reset_session_wallet_stats()
        if pwd:
            try:
                addr = w.unlock_with_password(pwd, wallet_id=wallet_id)
                self._password = pwd
                self._refresh_balance_cache(addr)
                return addr
            except WalletError:
                self._password = None
                raise
        self._password = None  # require unlock for Settings-style wallet switch
        return w.default_address()

    def select_wallet_by_address(self, address: str) -> str:
        if self._mining:
            self.stop_mining()
        pwd = self._password
        w = Wallet(self.paths, password=pwd)
        wid = w.set_default_by_address(address)
        self._reset_session_wallet_stats()
        if pwd:
            try:
                addr = w.unlock_with_password(pwd, wallet_id=wid)
                self._password = pwd
                self._refresh_balance_cache(addr)
                return addr
            except WalletError:
                self._password = None
                raise
        self._password = None
        return w.default_address()

    def default_address(self) -> str:
        return Wallet(self.paths).default_address()

    def unlock(self, password: str, wallet_id: str | None = None) -> str:
        prev: str | None = None
        try:
            prev = self.default_address()
        except WalletError:
            prev = None
        w = Wallet(self.paths, password=password)
        addr = w.unlock_with_password(password, wallet_id=wallet_id or None)
        self._password = password
        # Overview/Settings switch only calls unlock(wallet_id=…) — must not keep
        # mining rewards / balance cache from the previous active wallet.
        if prev is not None and prev != addr:
            if self._mining:
                self.stop_mining()
            self._reset_session_wallet_stats()
        else:
            self._invalidate_balance_cache()
        self._refresh_balance_cache(addr)
        return addr

    def lock(self) -> None:
        self._password = None

    def lock_session(self) -> None:
        """Leave the wallet UI: clear unlock immediately; stop mining/node in background."""
        self.lock()
        self._reset_session_wallet_stats()
        def _cleanup() -> None:
            try:
                if self._mining:
                    self.stop_mining()
            except Exception:
                logger.exception("stop mining on lock failed")
            try:
                self.stop_node()
            except Exception:
                logger.exception("stop node on lock failed")

        threading.Thread(target=_cleanup, name="mhcoin-lock-cleanup", daemon=True).start()

    def balance_sats(self) -> int:
        try:
            w = Wallet(self.paths)
            pkhs = w.account_pubkey_hashes()
        except WalletError:
            return 0
        if not pkhs:
            return 0
        with self._io:
            node = self._get_local()
            if hasattr(node.chain.utxo, "balance_for_pubkey_hashes"):
                sats = int(node.chain.utxo.balance_for_pubkey_hashes(pkhs))
            else:
                sats = sum(int(node.chain.utxo.balance_for_pubkey_hash(p)) for p in pkhs)
            self._balance_cache_sats = sats
            self._balance_cache_valid = True
            return sats

    def balance_text(self) -> str:
        return f"{format_mhc(self.balance_sats())} MHC"

    def _balance_hint_from_history(self) -> int | None:
        """Rough confirmed balance from last history snapshot (UTXO fallback)."""
        rows = self._hist_rows or []
        if rows:
            tip_h = -1
            try:
                tip_h = max((int(r.height) for r in rows if r.height is not None), default=-1)
            except Exception:
                tip_h = -1
            net = 0
            for r in rows:
                if r.height is None:
                    continue  # skip mempool
                kind = (r.kind or "")
                amt = abs(int(r.amount_sats))
                if kind == "Mining reward" or kind.startswith("Mining"):
                    net += amt
                elif kind == "Sent" or kind.startswith("Sent"):
                    net -= amt
                    if r.fee_sats:
                        net -= abs(int(r.fee_sats))
                elif kind.startswith("Received") or kind in ("Received", "Receive"):
                    net += amt
            return max(0, net) if tip_h >= 0 else None
        dicts = self._hist_dicts or []
        if not dicts:
            return None
        net = 0
        saw = False
        for d in dicts:
            if d.get("height") is None or d.get("height") == "":
                continue
            saw = True
            amt = abs(int(round(float(d.get("amount_mhc") or 0) * 1e8)))
            sign = d.get("sign") or "+"
            typ = (d.get("type") or "").lower()
            kind = d.get("kind") or ""
            if typ == "sent" or str(kind).startswith("Sent") or sign == "-":
                net -= amt
                fee = d.get("fee_mhc")
                if fee not in (None, "", "—"):
                    try:
                        net -= abs(int(round(float(fee) * 1e8)))
                    except Exception:
                        pass
            else:
                net += amt
        return max(0, net) if saw else None

    def cached_balance_text(self) -> str:
        """Best-effort balance when the P2P node owns the datadir.

        Always re-read live UTXO when the node is up so Overview matches the
        explorer (stale cache previously lagged by mined/received blocks).
        """
        if self._node is None and not self._mining:
            try:
                return self.balance_text()
            except Exception:
                pass
        # Refresh every poll while live — tip can advance without a local find.
        try:
            self._refresh_balance_cache()
        except Exception:
            logger.debug("cached_balance refresh failed", exc_info=True)
        if self._balance_cache_valid:
            return f"{format_mhc(self._balance_cache_sats)} MHC"
        # Never invent a balance from history while node/mining is up — that
        # path under-counted (e.g. 9000 vs real UTXO 9100).
        if self._node is not None or self._mining:
            return f"{format_mhc(self._balance_cache_sats)} MHC"
        hint = self._balance_hint_from_history()
        if hint is not None:
            self._balance_cache_sats = int(hint)
            self._balance_cache_valid = True
            return f"{format_mhc(self._balance_cache_sats)} MHC"
        return f"{format_mhc(self._balance_cache_sats)} MHC"

    def send(self, to_address: str, amount_mhc: str, password: str, fee_mhc: str | None = None) -> str:
        # Accept a plain address OR a BIP21-style "mhcoin:<addr>?amount=" URI
        # (Desktop's own Receive QR emits the URI form; explorer QR is plain —
        # pasted/scanned values from either must work here).
        from mhcoin.desktop.uri import parse_payment_uri

        parsed = parse_payment_uri(to_address)
        if parsed.get("address"):
            to_address = parsed["address"]
        if not (amount_mhc or "").strip() and parsed.get("amount"):
            amount_mhc = str(parsed["amount"])
        self.ensure_chain()
        with self._io:
            fee = parse_amount_mhc(fee_mhc) if fee_mhc else None
            w = Wallet(self.paths, password=password)
            rt = self._node
            live = (
                rt is not None
                and not getattr(rt, "_stopped", False)
                and getattr(rt, "_running", False)
            )
            if live:
                # Prefer live P2P node — LocalNode mempool/UTXO go stale while mining.
                assert rt is not None
                try:
                    rt.mempool.reload()
                    rt.mempool.evict_spent_on_chain(rt.chain.utxo)
                except Exception:
                    logger.debug("mempool refresh before send failed", exc_info=True)
                if rt.chain.height < 0:
                    raise WalletError("no blockchain yet — mine or sync first")
                result = w.send(
                    to_address,
                    amount_mhc,
                    password=password,
                    fee_sats=fee,
                    utxo=rt.chain.utxo,
                    exclude_outpoints=rt.mempool.spent_keys(),
                )
                rt.submit_tx(result.tx)
            else:
                node = self._get_local()
                if node.chain.height < 0:
                    raise WalletError("no blockchain yet — mine or sync first")
                try:
                    node.mempool.reload()
                    node.mempool.evict_spent_on_chain(node.chain.utxo)
                except Exception:
                    logger.debug("local mempool refresh before send failed", exc_info=True)
                result = w.send(
                    to_address,
                    amount_mhc,
                    password=password,
                    fee_sats=fee,
                    utxo=node.chain.utxo,
                    exclude_outpoints=node.mempool.spent_keys(),
                )
                node.submit_tx(result.tx)
            self._password = password
            self._last_txid = result.txid_hex
            try:
                from_addr = w.default_address()
            except Exception:
                try:
                    from_addr = self.default_address()
                except Exception:
                    from_addr = ""
            try:
                self._append_transfer_log(
                    {
                        "kind": "Sent",
                        "txid": result.txid_hex,
                        "amount_sats": int(result.amount),
                        "fee_sats": int(result.fee),
                        "from": from_addr,
                        "to": to_address,
                        "timestamp": int(time.time()),
                        "confirmed": False,
                    }
                )
                self._push_recent(
                    TxRow(
                        kind="Sent (pending)",
                        amount_sats=int(result.amount),
                        detail=result.txid_hex,
                        height=None,
                        txid=result.txid_hex,
                        timestamp=int(time.time()),
                        from_addrs=[from_addr] if from_addr else [],
                        to_addrs=[to_address],
                        fee_sats=int(result.fee),
                        confirmations=0,
                    )
                )
            except Exception:
                logger.exception("send transfer log failed")
            self._invalidate_history_cache()
            try:
                self.request_history_build(full_chain=True)
            except Exception:
                pass
            return result.txid_hex

    # --- chain / history ----------------------------------------------------

    def ensure_chain(self) -> None:
        with self._io:
            node = self._get_local()
            if node.chain.height < 0:
                node.chain.init_with_genesis(get_network_genesis(self.network))

    def chain_info(self) -> dict:
        params = get_network_params(self.network)
        tip = None
        height = 0
        peers = 0
        node_running = self._node is not None
        sync = "Ready (solo / local)"
        chain_error: str | None = None
        sync_state = "IDLE"
        progress_height = 0
        target_hint = 0
        pending_blocks = 0
        syncing = False
        sync_pct: float | None = None
        if node_running:
            # NodeRuntime owns the datadir — do not open a second LocalNode.
            try:
                st = NodeRuntime.read_status(self.data_dir) or {}
                peers = int(st.get("peer_count") or 0)
                height = max(0, int(st.get("height") or 0))
                tip = str(st["tip"]) if st.get("tip") else None
                sm = st.get("sync") or {}
                if isinstance(sm, dict):
                    sync_state = str(sm.get("state") or "IDLE")
                    progress_height = int(sm.get("progress_height") or height)
                    target_hint = int(sm.get("target_hint") or 0)
                    pending_blocks = int(sm.get("pending_blocks") or 0)
                # Initial block download: behind peer tip hint or actively downloading.
                syncing = sync_state in (
                    "REQUESTING_HEADERS",
                    "DOWNLOADING_BLOCKS",
                ) or (target_hint > max(progress_height, height) + 1)
                if syncing and target_hint > 0:
                    cur = max(progress_height, height)
                    sync_pct = max(0.0, min(99.9, 100.0 * cur / max(target_hint, 1)))
                    left = max(0, target_hint - cur)
                    sync = (
                        f"Synchronizing with network… "
                        f"Block {cur} of {target_hint} ({sync_pct:.1f}%)"
                        + (f" · {left} left" if left else "")
                    )
                elif peers > 0:
                    sync = "Network synchronized"
                    sync_pct = 100.0
                else:
                    sync = "Connecting to peers…"
            except Exception:
                sync = "Node running"
        elif self._node_thread is not None and self._node_thread.is_alive() and self._node is None:
            # Node thread shutting down — don't open LocalNode yet.
            height = max(0, int(getattr(self, "_tip_height_hint", 0) or 0))
            progress_height = height
            tip = None
            sync = "Node stopping…"
            sync_pct = None
        else:
            try:
                with self._io:
                    node = self._get_local()
                    tip = node.chain.tip_hash.hex() if node.chain.tip_hash else None
                    height = max(node.chain.height, 0)
                    self._tip_height_hint = height
                progress_height = height
                sync = "Ready (solo / local)" if height >= 0 else "No chain yet"
                sync_pct = 100.0 if height >= 0 else None
            except ChainError as e:
                chain_error = str(e)
                sync = "Datadir error"
                with self._io:
                    self._close_local()
            except Exception as e:  # noqa: BLE001
                chain_error = str(e)
                sync = "Datadir error"
                with self._io:
                    self._close_local()
        return {
            "height": height,
            "tip": tip,
            "peers": peers,
            "sync": sync,
            "syncing": syncing,
            "sync_state": sync_state,
            "sync_progress": progress_height,
            "sync_target": target_hint,
            "sync_pending": pending_blocks,
            "sync_percent": sync_pct,
            "network": self.network,
            "genesis_hash": params.genesis_hash_hex,
            "seeds": default_connect_peers(self.network),
            "node_running": node_running,
            "listen_port": params.default_port,
            "chain_error": chain_error,
        }

    def _invalidate_history_cache(self) -> None:
        self._hist_key = None
        self._hist_rows = []
        self._hist_dicts = []
        self._recent_cache = []
        self._hist_need_full = True


    def _transfer_log_path(self) -> Path:
        return self.data_dir / "wallet_transfers.json"

    def _load_transfer_log(self) -> list[dict]:
        path = self._transfer_log_path()
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("txs") if isinstance(raw, dict) else raw
            return [r for r in (rows or []) if isinstance(r, dict)]
        except Exception:
            logger.debug("transfer log load failed", exc_info=True)
            return []

    def _append_transfer_log(self, entry: dict) -> None:
        """Durable Sent journal — survives mempool wipe when mining pauses P2P."""
        path = self._transfer_log_path()
        rows = self._load_transfer_log()
        txid = str(entry.get("txid") or "")
        if txid and any(
            str(r.get("txid") or "") == txid and r.get("kind") == entry.get("kind") for r in rows
        ):
            return
        rows.append(entry)
        rows = rows[-2000:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"txs": rows}, indent=2), encoding="utf-8")
        except Exception:
            logger.exception("transfer log write failed")

    def _transfer_log_fee_map(self) -> dict[str, int]:
        """txid → fee_sats from durable send journal (when known)."""
        out: dict[str, int] = {}
        for e in self._load_transfer_log():
            txid = str(e.get("txid") or "")
            if not txid or e.get("fee_sats") is None:
                continue
            try:
                out[txid] = abs(int(e["fee_sats"]))
            except (TypeError, ValueError):
                continue
        return out

    def _enrich_fees_from_log(self, rows: list[TxRow]) -> list[TxRow]:
        """Fill missing Sent fees from wallet_transfers.json (chain scan can miss vin)."""
        fees = self._transfer_log_fee_map()
        if not fees:
            return rows
        for r in rows:
            if r.fee_sats is not None or not r.txid:
                continue
            if not (r.kind or "").startswith("Sent"):
                continue
            fee = fees.get(r.txid)
            if fee is not None:
                r.fee_sats = fee
        return rows

    def _merge_transfer_log(self, rows: list[TxRow], addr: str) -> list[TxRow]:
        """Ensure logged sends appear even if mempool was dropped before confirm."""
        rows = self._enrich_fees_from_log(rows)
        have = {r.txid for r in rows if r.txid}
        extra: list[TxRow] = []
        for e in self._load_transfer_log():
            from_a = str(e.get("from") or "")
            to_a = str(e.get("to") or "")
            kind = str(e.get("kind") or "Sent")
            txid = str(e.get("txid") or "")
            if not txid or txid in have:
                continue
            log_fee = e.get("fee_sats")
            try:
                log_fee_i = abs(int(log_fee)) if log_fee is not None else None
            except (TypeError, ValueError):
                log_fee_i = None
            if kind.startswith("Sent"):
                if from_a and from_a != addr:
                    continue
                extra.append(
                    TxRow(
                        kind="Sent" if e.get("confirmed") else "Sent (pending)",
                        amount_sats=abs(int(e.get("amount_sats") or 0)),
                        detail=txid,
                        height=e.get("height"),
                        txid=txid,
                        timestamp=e.get("timestamp"),
                        from_addrs=[from_a or addr],
                        to_addrs=[to_a] if to_a else [],
                        fee_sats=log_fee_i,
                        confirmations=(
                            e.get("confirmations") if e.get("height") is not None else 0
                        ),
                    )
                )
            elif kind.startswith("Received"):
                if to_a and to_a != addr:
                    continue
                extra.append(
                    TxRow(
                        kind="Received" if e.get("confirmed") else "Received (pending)",
                        amount_sats=abs(int(e.get("amount_sats") or 0)),
                        detail=txid,
                        height=e.get("height"),
                        txid=txid,
                        timestamp=e.get("timestamp"),
                        from_addrs=[from_a] if from_a else [],
                        to_addrs=[to_a or addr],
                        fee_sats=log_fee_i,
                        confirmations=(
                            e.get("confirmations") if e.get("height") is not None else 0
                        ),
                    )
                )
        return extra + rows if extra else rows

    def _hist_key_is_partial(self) -> bool:
        """True when last wallet_history cache was a short-window (non-full) scan."""
        key = self._hist_key
        # key = ("hist-v6", addr, tip, mem_n, bool(full_chain), int(limit))
        return isinstance(key, tuple) and len(key) >= 5 and key[4] is False

    def _current_tip_hex(self) -> str:
        """Best-effort tip hash for history cache invalidation.

        Never open LocalNode while the P2P node (or solo miner) owns the
        datadir — a second sqlite handle SIGSEGV'd Apple libsqlite / closed
        the Desktop window on macOS.
        """
        try:
            if self._node is not None and not getattr(self._node, "_stopped", False):
                tip = self._node.chain.tip_hash
                return tip.hex() if tip else ""
        except Exception:
            logger.debug("live tip hex read failed", exc_info=True)
            return ""
        # Node owns datadir, or miner has it — do not open a second chain.
        if self._node is not None or self._mining:
            return ""
        try:
            with self._io:
                tip = self._get_local().chain.tip_hash
                return tip.hex() if tip else ""
        except Exception:
            return ""

    def _hist_tip_stale(self) -> bool:
        """True when cached history was built for a different tip than now."""
        key = self._hist_key
        if not isinstance(key, tuple) or len(key) < 3:
            return self._hist_key is None
        cached = str(key[2] or "")
        cur = self._current_tip_hex()
        if not cur:
            return False
        return cached != cur

    def _ensure_history_before_node(self, *, timeout: float = 120.0) -> None:
        """Wait for async history or run a sync full scan before P2P owns the datadir."""
        if self._node is not None:
            return
        deadline = time.monotonic() + max(1.0, float(timeout))

        def _needs_full() -> bool:
            return (
                self._hist_key is None
                or self._hist_key_is_partial()
                or bool(getattr(self, "_hist_need_full", False))
            )

        while time.monotonic() < deadline:
            with self._hist_build_lock:
                building = bool(self._hist_building)
            if building:
                time.sleep(0.05)
                continue
            if not _needs_full():
                return
            if self._node is not None:
                return
            try:
                rows = self.wallet_history(2000, full_chain=True)
                dicts = [self._txrow_to_dict(r) for r in rows]
                self._hist_dicts = dicts
                self._recent_cache = list(dicts)[:50]
                with self._hist_build_lock:
                    self._hist_need_full = False
                try:
                    self.balance_sats()
                except Exception:
                    logger.exception("balance snapshot after history failed")
                return
            except Exception:
                logger.exception("sync history before node failed")
                return
        logger.warning("history snapshot before node timed out after %.0fs", timeout)

    def request_history_build(self, *, full_chain: bool = True) -> None:
        """Kick a background full-history scan (non-blocking for UI).

        While the P2P node is up, scans use the node's Blockchain connection under
        chain._lock (never a second sqlite handle — that SIGSEGV'd Apple libsqlite
        on macOS via ReadOnlyChain). Solo-mining without a node still defers until
        the miner releases the LocalNode datadir.
        """
        # Solo-mine path (no P2P): LocalNode + miner both touch sqlite — defer.
        if self._mining and self._node is None:
            with self._hist_build_lock:
                if full_chain:
                    self._hist_need_full = True
            return
        with self._hist_build_lock:
            if full_chain:
                self._hist_need_full = True
            if self._hist_building:
                return
            self._hist_building = True
            self._hist_build_error = None
            want_full = bool(full_chain or getattr(self, "_hist_need_full", False))

        def _job() -> None:
            try:
                if self._mining and self._node is None:
                    with self._hist_build_lock:
                        self._hist_need_full = True
                    return
                do_full = want_full
                rows = self.wallet_history(5000, full_chain=do_full)
                dicts = [self._txrow_to_dict(r) for r in rows]
                self._hist_dicts = dicts
                # Keep a generous recent window so Overview still shows mining.
                self._recent_cache = list(dicts)[:500]
                if self._node is None:
                    try:
                        self.balance_sats()
                    except Exception:
                        logger.debug("balance snapshot after hist job failed", exc_info=True)
                if do_full:
                    with self._hist_build_lock:
                        self._hist_need_full = False
            except Exception as e:  # noqa: BLE001
                logger.exception("history build failed")
                self._hist_build_error = str(e)
            finally:
                with self._hist_build_lock:
                    self._hist_building = False
                    need_again = bool(
                        getattr(self, "_hist_need_full", False) or self._hist_tip_stale()
                    )
                if need_again and not (self._mining and self._node is None):
                    self.request_history_build(full_chain=True)

        threading.Thread(target=_job, name="mhcoin-hist-build", daemon=True).start()

    def history_for_api(self, *, limit: int = 2000) -> dict:
        """Non-blocking history payload for Desktop UI."""
        building = False
        with self._hist_build_lock:
            building = bool(self._hist_building)
        # Merge full snapshot + recent + durable send log (dedupe by txid+kind).
        txs: list[dict] = []
        seen: set[tuple] = set()

        def _add(rows: list) -> None:
            for d in rows:
                if not isinstance(d, dict):
                    continue
                k = (str(d.get("txid") or ""), str(d.get("kind") or ""), str(d.get("type") or ""))
                if k in seen:
                    # Prefer a known fee when a later source (recent / transfer log) has it.
                    if d.get("fee_mhc") not in (None, "", "—"):
                        for prev in txs:
                            pk = (
                                str(prev.get("txid") or ""),
                                str(prev.get("kind") or ""),
                                str(prev.get("type") or ""),
                            )
                            if pk == k and prev.get("fee_mhc") in (None, "", "—"):
                                prev["fee_mhc"] = d.get("fee_mhc")
                                break
                    continue
                seen.add(k)
                txs.append(d)

        _add(list(self._hist_dicts or []))
        _add(list(self._recent_cache or []))
        # Ensure transfer-log sends are visible even before/without a full scan.
        try:
            addr_now = self.default_address()
        except Exception:
            addr_now = ""
        try:
            log_rows = self._merge_transfer_log([], addr_now or "")
            _add([self._txrow_to_dict(r) for r in log_rows])
        except Exception:
            logger.debug("history log merge failed", exc_info=True)
        key_partial = self._hist_key_is_partial()
        tip_stale = self._hist_tip_stale()
        # Empty history is valid (fresh wipe / no txs yet). Rebuild when the tip
        # moved — otherwise mining rewards after a resync never appear.
        partial = (self._hist_key is None) or key_partial or tip_stale
        if not building and (
            self._hist_key is None or key_partial or tip_stale
        ):
            self.request_history_build(full_chain=True)
            with self._hist_build_lock:
                building = bool(self._hist_building)
        # Newest first for display (mining + sends interleaved by height).
        try:
            txs.sort(
                key=lambda d: (
                    0 if d.get("height") is not None else 1,
                    -(int(d["height"]) if d.get("height") is not None else -1),
                    0 if (d.get("type") == "mined") else 1,
                )
            )
        except Exception:
            pass
        sliced = txs[:limit]
        counts = {"mined": 0, "sent": 0, "received": 0, "other": 0}
        for d in sliced:
            typ = str(d.get("type") or "other")
            if typ not in counts:
                typ = "other"
            counts[typ] += 1
        return {
            "ok": True,
            "txs": sliced,
            "count": len(sliced),
            "counts": counts,
            "building": building,
            "partial": partial and not building,
            "cached": True,
            "error": self._hist_build_error,
        }

    def recent_transactions(
        self,
        limit: int = 30,
        *,
        full_chain: bool = False,
    ) -> list[TxRow]:
        """Fast path for Overview: recent window; transfers ranked above mining."""
        return self.wallet_history(limit=limit, full_chain=full_chain)

    def _history_scan_sources(self):
        """Chain + mempool for wallet_history.

        When the P2P node owns the datadir, reuse NodeRuntime.chain (same sqlite
        connection) under chain._lock — never open a second ReadOnlyChain handle
        (that path SIGSEGV'd on macOS Apple libsqlite).
        Returns (chain, mempool_txs, cleanup_fn).
        """
        if self._node is not None:
            rt = self._node
            mem_txs: list = []
            try:
                mem_txs = list(rt.mempool.list_txs())
            except Exception:
                logger.debug("node mempool list for history failed", exc_info=True)
            return rt.chain, mem_txs, None

        node = self._get_local()
        return node.chain, list(node.mempool.list_txs()), None

    def wallet_history(self, limit: int = 2000, *, full_chain: bool = True) -> list[TxRow]:
        """Activity for the active HD account (all receive + change addresses)."""
        try:
            addr = self.default_address()
            pkhs = Wallet(self.paths).account_pubkey_hashes()
            if not pkhs:
                pkhs = {address_to_pubkey_hash(addr, hrp=self.hrp)}
        except WalletError:
            return []

        # Prefer node's chain (no second sqlite). Do NOT hold chain._lock for the
        # whole scan — that starved P2P on macOS and left History with only the
        # durable send log (no mining). get_block_by_height locks per read.
        hold_io = False
        if self._node is None:
            hold_io = True
            self._io.acquire()
        cleanup = None
        try:
            chain, mempool_txs, cleanup = self._history_scan_sources()
            with chain._lock:
                h = int(chain.height)
                tip = chain.tip_hash.hex() if chain.tip_hash else ""
            if h < 0:
                # Fresh datadir (no genesis yet) — mark scan complete so unlock
                # boot is not stuck on partial forever.
                key = ("hist-v7-empty", addr, "", 0, True, int(limit))
                self._hist_key = key
                self._hist_rows = []
                try:
                    self._hist_dicts = []
                except Exception:
                    pass
                return []
            mem_n = len(mempool_txs)
            # Account-scoped cache — switching accounts must not reuse history.
            acc_fp = ",".join(sorted(p.hex() for p in pkhs))
            key = ("hist-v7", acc_fp, tip, mem_n, bool(full_chain), int(limit))
            # Empty rows are a valid cache hit (wallet has no sends/receives yet).
            if self._hist_key == key:
                return list(self._hist_rows or [])[:limit]

            # Classification window (UI limit). Prev-out index always covers the
            # full active chain — otherwise Sent is missed when the spent UTXO
            # was created outside a short window.
            window_start = 0 if full_chain else max(0, h - 120)
            tx_index: dict[str, object] = {}
            window_blocks: list[tuple[int, object]] = []
            for height in range(0, h + 1):
                block = chain.get_block_by_height(height)
                if block is None:
                    continue
                for tx in block.transactions:
                    tx_index[tx.txid_hex()] = tx
                if height >= window_start:
                    window_blocks.append((height, block))

            def _out_is_active(out) -> bool:
                try:
                    return out.pubkey_hash() in pkhs
                except Exception:
                    return False

            def _addr_of_out(out) -> str | None:
                try:
                    return pubkey_hash_to_address(out.pubkey_hash(), hrp=self.hrp)
                except Exception:
                    return None

            def _tx_spends_active(tx) -> bool:
                """True iff any input spends an outpoint of the active wallet."""
                if tx.is_coinbase():
                    return False
                for tin in tx.inputs:
                    prev = tx_index.get(tin.prev_txid.hex())
                    if prev is None:
                        continue
                    vout = int(tin.prev_vout)
                    if 0 <= vout < len(prev.outputs) and _out_is_active(prev.outputs[vout]):
                        return True
                return False

            def _from_addrs(tx) -> list[str]:
                if tx.is_coinbase():
                    return ["coinbase"]
                seen: list[str] = []
                for tin in tx.inputs:
                    prev = tx_index.get(tin.prev_txid.hex())
                    if prev is None:
                        continue
                    vout = int(tin.prev_vout)
                    if 0 <= vout < len(prev.outputs):
                        a = _addr_of_out(prev.outputs[vout])
                        if a and a not in seen:
                            seen.append(a)
                return seen

            def _fee_sats(tx) -> int | None:
                if tx.is_coinbase():
                    return 0
                vin = 0
                known = True
                for tin in tx.inputs:
                    prev = tx_index.get(tin.prev_txid.hex())
                    if prev is None:
                        known = False
                        break
                    vout = int(tin.prev_vout)
                    if not (0 <= vout < len(prev.outputs)):
                        known = False
                        break
                    vin += int(prev.outputs[vout].value)
                if not known:
                    return None
                vout = sum(int(o.value) for o in tx.outputs)
                return max(0, vin - vout)

            def _confs(height: int | None) -> int | None:
                if height is None:
                    return 0
                return max(0, int(h) - int(height) + 1)

            mining_rows: list[TxRow] = []
            transfer_rows: list[TxRow] = []

            for height, block in reversed(window_blocks):
                try:
                    block_ts = int(block.header.timestamp)
                except Exception:
                    block_ts = None
                for tx in block.transactions:
                    active_outs = [o for o in tx.outputs if _out_is_active(o)]
                    other_outs = [o for o in tx.outputs if not _out_is_active(o)]
                    to_active = sum(int(o.value) for o in active_outs)
                    paid_away = sum(int(o.value) for o in other_outs)
                    spends_active = _tx_spends_active(tx)
                    from_a = _from_addrs(tx)
                    to_active_addrs = [a for a in (_addr_of_out(o) for o in active_outs) if a]
                    to_other_addrs = [a for a in (_addr_of_out(o) for o in other_outs) if a]
                    fee = _fee_sats(tx)

                    if tx.is_coinbase():
                        if to_active > 0:
                            mining_rows.append(
                                TxRow(
                                    kind="Mining reward",
                                    amount_sats=to_active,
                                    detail=f"block {height}",
                                    height=height,
                                    txid=tx.txid_hex(),
                                    timestamp=block_ts,
                                    from_addrs=["coinbase"],
                                    to_addrs=to_active_addrs or [addr],
                                    fee_sats=0,
                                    confirmations=_confs(height),
                                )
                            )
                        continue

                    # Active wallet sent: spent our UTXO, value left to others
                    # (external or another wallet in the same file). Change to
                    # self is not listed separately.
                    if spends_active and paid_away > 0:
                        transfer_rows.append(
                            TxRow(
                                kind="Sent",
                                amount_sats=paid_away,
                                detail=tx.txid_hex(),
                                height=height,
                                txid=tx.txid_hex(),
                                timestamp=block_ts,
                                from_addrs=[addr],
                                to_addrs=to_other_addrs,
                                fee_sats=fee,
                                confirmations=_confs(height),
                            )
                        )
                    elif to_active > 0 and not spends_active:
                        # Incoming (or payment into this wallet from another).
                        transfer_rows.append(
                            TxRow(
                                kind="Received",
                                amount_sats=to_active,
                                detail=tx.txid_hex(),
                                height=height,
                                txid=tx.txid_hex(),
                                timestamp=block_ts,
                                from_addrs=from_a,
                                to_addrs=to_active_addrs or [addr],
                                fee_sats=fee,
                                confirmations=_confs(height),
                            )
                        )

            pending: list[TxRow] = []
            for tx in reversed(mempool_txs):
                if tx.is_coinbase():
                    continue
                active_outs = [o for o in tx.outputs if _out_is_active(o)]
                other_outs = [o for o in tx.outputs if not _out_is_active(o)]
                to_active = sum(int(o.value) for o in active_outs)
                paid_away = sum(int(o.value) for o in other_outs)
                spends_active = _tx_spends_active(tx)
                txid = tx.txid_hex()
                if any(r.txid == txid for r in transfer_rows):
                    continue
                from_a = _from_addrs(tx)
                to_active_addrs = [a for a in (_addr_of_out(o) for o in active_outs) if a]
                to_other_addrs = [a for a in (_addr_of_out(o) for o in other_outs) if a]
                fee = _fee_sats(tx)
                if spends_active and paid_away > 0:
                    pending.append(
                        TxRow(
                            kind="Sent (pending)",
                            amount_sats=paid_away,
                            detail=txid,
                            height=None,
                            txid=txid,
                            timestamp=None,
                            from_addrs=[addr],
                            to_addrs=to_other_addrs,
                            fee_sats=fee,
                            confirmations=0,
                        )
                    )
                elif to_active > 0 and not spends_active:
                    pending.append(
                        TxRow(
                            kind="Received (pending)",
                            amount_sats=to_active,
                            detail=txid,
                            height=None,
                            txid=txid,
                            timestamp=None,
                            from_addrs=from_a,
                            to_addrs=to_active_addrs or [addr],
                            fee_sats=fee,
                            confirmations=0,
                        )
                    )

            # Newest height first (do NOT pin old transfers above fresh mining).
            confirmed = transfer_rows + mining_rows
            confirmed.sort(
                key=lambda r: (
                    0 if r.height is not None else 1,
                    -(r.height if r.height is not None else -1),
                    0 if "Mining" in (r.kind or "") else 1,
                )
            )
            rows = pending + confirmed
            # Durable send journal (mempool can be wiped when mining pauses P2P).
            rows = self._merge_transfer_log(rows, addr)
            # Mark log entries confirmed when we see them on-chain; backfill fee.
            try:
                onchain_fees = {
                    r.txid: r.fee_sats
                    for r in transfer_rows
                    if r.txid and (r.kind or "").startswith("Sent")
                }
                log_rows = self._load_transfer_log()
                changed = False
                for e in log_rows:
                    txid = e.get("txid")
                    if txid in onchain_fees and not e.get("confirmed"):
                        e["confirmed"] = True
                        changed = True
                    if (
                        txid in onchain_fees
                        and e.get("fee_sats") is None
                        and onchain_fees.get(txid) is not None
                    ):
                        e["fee_sats"] = int(onchain_fees[txid])
                        changed = True
                if changed:
                    self._transfer_log_path().write_text(
                        json.dumps({"txs": log_rows}, indent=2), encoding="utf-8"
                    )
            except Exception:
                logger.debug("transfer log confirm update failed", exc_info=True)
            self._hist_key = key
            self._hist_rows = list(rows)
            try:
                self._hist_dicts = [self._txrow_to_dict(r) for r in self._hist_rows]
            except Exception:
                pass
            return rows[:limit]
        finally:
            if cleanup is not None:
                try:
                    cleanup()
                except Exception:
                    pass
            if hold_io:
                self._io.release()

    # --- mining -------------------------------------------------------------

    @property
    def is_mining(self) -> bool:
        return self._mining

    @property
    def mining_stats(self) -> dict:
        cpus = max(1, int(os.cpu_count() or 1))
        alive = bool(self._miner_thread and self._miner_thread.is_alive())
        mining = bool(self._mining and alive and not self._miner_stop.is_set())
        return {
            "mining": mining,
            "stopping": bool(self._mine_stopping or (self._miner_stop.is_set() and alive)),
            "hashrate": self._hashrate,
            "blocks_found": self._blocks_found,
            "rewards_sats": self._rewards_sats,
            "rewards_text": f"{format_mhc(self._rewards_sats)} MHC",
            "log": self.mine_log_lines(),
            "intensity": int(self._mine_intensity),
            "workers": int(self._mine_workers),
            "cpus": cpus,
        }

    def set_mine_intensity(self, value: object) -> int:
        """Set Desktop mining CPU load (10-100%). Applies immediately while mining."""
        n = save_mine_intensity(value)
        self._mine_intensity = n
        self._mine_workers = self._workers_for_intensity(n)
        if self._mining and not self._miner_stop.is_set():
            self._mine_log_line(
                f"CPU load -> {n}% · {self._mine_workers}/{max(1, int(os.cpu_count() or 1))} workers"
            )
            # Abort current PoW slice so duty-cycle / worker count apply now.
            self._miner_reconfigure.set()
        return n

    @staticmethod
    def _workers_for_intensity(intensity: int) -> int:
        """Worker count for intensity (stats / legacy slider).

        Desktop PoW uses the **0.3.7.3** single-lane loop (max stable H/s).
        Multi-worker inflated CPU load while often lowering hashrate (GIL).
        """
        cpus = max(1, int(os.cpu_count() or 1))
        pct = clamp_mine_intensity(intensity)
        usable = cpus - 1 if cpus >= 4 else cpus
        return max(1, int(round(usable * pct / 100.0)))

    def _start_caffeine(self) -> None:
        """Keep Mac awake while mining (App Nap / idle sleep). Same app process."""
        self._stop_caffeine()
        if sys.platform != "darwin":
            return
        try:
            self._caffeine_proc = subprocess.Popen(
                ["caffeinate", "-dims", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            self._caffeine_proc = None

    def _stop_caffeine(self) -> None:
        proc = getattr(self, "_caffeine_proc", None)
        self._caffeine_proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass

    def _broadcast_miner_status(self, *, force: bool = False) -> None:
        """Advise peers of local mining hashrate (STATUS). Throttled ~10s."""
        now = time.time()
        last = float(getattr(self, "_status_last_broadcast", 0.0) or 0.0)
        if not force and (now - last) < 10.0:
            return
        rt = self._node
        if rt is None or getattr(rt, "_stopped", False):
            return
        p2p = getattr(rt, "p2p", None)
        if p2p is None:
            return
        try:
            from mhcoin.network.messages import StatusPayload, encode_status

            mining = bool(self._mining)
            hps = int(max(0.0, float(self._hashrate or 0.0))) if mining else 0
            try:
                height = int(rt.chain.height)
            except Exception:
                height = -1
            n = p2p.broadcast(
                "STATUS",
                encode_status(
                    StatusPayload(mining=mining, hps=hps, height=height)
                ),
            )
            self._status_last_broadcast = now
            if n:
                logger.debug(
                    "STATUS broadcast mining=%s hps=%s height=%s peers=%s",
                    mining,
                    hps,
                    height,
                    n,
                )
        except Exception:
            logger.debug("STATUS broadcast failed", exc_info=True)

    def mine_log_lines(self, limit: int = 200) -> list[str]:
        with self._mine_log_lock:
            lines = list(self._mine_log)
        return lines[-limit:]

    def _mine_log_line(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._mine_log_lock:
            self._mine_log.append(line)

    def _wait_datadir_free(self, *, timeout: float = 8.0) -> None:
        """Wait until chain disk lock is free (zombie node thread after stop)."""
        deadline = time.monotonic() + max(0.5, float(timeout))
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with chain_disk_lock(self.data_dir, timeout=0.35):
                    return
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(0.15)
        raise RuntimeError(
            f"chain still busy after stopping node ({last_err}) — wait a moment and retry"
        )

    def start_mining(self, address: str | None = None, on_block: Callable | None = None) -> None:
        # Refuse a second miner; if a stop is in flight, wait briefly then retry once.
        th = self._miner_thread
        if th is not None and th.is_alive():
            if not self._miner_stop.is_set():
                return
            # Windows: never block the HTTP/UI thread for long — Stop already
            # signals workers; a 3s join made Start appear stuck.
            join_t = 0.35 if sys.platform == "win32" else 3.0
            th.join(timeout=join_t)
            if th.is_alive():
                raise RuntimeError("miner still stopping — try Start again in a moment")
        addr = address or self.default_address()
        if not validate_address(addr, hrp=self.hrp):
            raise ValueError("invalid reward address")
        # Mine through the live P2P node (same process owns datadir).
        # Do NOT stop the node — template/accept go via NodeRuntime + relay INV.
        self._resume_node_after_mine = False
        reward_addr = addr
        self._miner_stop.clear()
        self._miner_reconfigure.clear()
        self._mine_stopping = False
        self._mining = True
        # 0.3.7.3-style: one tight PoW lane (max stable hashrate).
        self._mine_intensity = 100
        self._mine_workers = 1
        self._hashrate = 0.0
        self._start_caffeine()
        self._mine_log_line(f"MHCOIN Live Miner · {self.network}")
        self._mine_log_line(f"reward  {reward_addr}")
        self._mine_log_line(f"data    {self.data_dir}")
        self._mine_log_line("mode    live node (P2P stays online)")
        self._mine_log_line("CPU     single-lane PoW (0.3.7.3-style)")
        self._mine_log_line("────────────────────────────────")
        self._status_last_broadcast = 0.0

        def _loop() -> None:
            try:
                # Start node inside miner thread so /api/mine/start returns instantly.
                if self._node is None:
                    self._mine_log_line("Starting P2P node for live mining…")
                    try:
                        self.start_node(skip_history_wait=True, allow_while_mining=True)
                    except Exception as e:
                        self._mine_log_line(f"ERROR starting node: {e}")
                        return
                    deadline = time.monotonic() + 15.0
                    while time.monotonic() < deadline and not self._miner_stop.is_set():
                        rt = self._node
                        if rt is not None and not getattr(rt, "_stopped", False):
                            try:
                                if rt.chain.height >= 0 and getattr(rt, "_running", False):
                                    break
                            except Exception:
                                pass
                        time.sleep(0.05)
                    if self._miner_stop.is_set():
                        return
                    if self._node is None or getattr(self._node, "_stopped", False):
                        self._mine_log_line("ERROR P2P node failed to start — cannot mine")
                        return
                    if not getattr(self._node, "_running", False):
                        self._mine_log_line("ERROR P2P node not running — cannot mine")
                        return
                with self._io:
                    self._close_local()
                tip_h = -1
                try:
                    tip_h = int(self._node.chain.height)
                    self._tip_height_hint = max(0, tip_h)
                except Exception:
                    tip_h = int(getattr(self, "_tip_height_hint", 0) or 0)
                self._mine_log_line(f"tip #{tip_h}")
                self._mine_log_line("searching nonce… (Stop to quit)")
                self._broadcast_miner_status(force=True)

                while not self._miner_stop.is_set():
                    t0 = time.time()
                    height = -1
                    bits = 0
                    block = None
                    rt = self._node
                    if rt is None or getattr(rt, "_stopped", False) or not getattr(rt, "_running", True):
                        self._mine_log_line("P2P node not running — mining stopped")
                        break
                    try:

                        def _prog(nonce: int, _h: bytes, hps: float) -> None:
                            if self._miner_stop.is_set():
                                return
                            # 0.4.1.10-style: live measured H/s (no 50k cap / EMA lag).
                            self._hashrate = float(hps or 0.0)
                            self._broadcast_miner_status()
                            now = time.time()
                            if now - self._mine_log_last_prog >= 0.5:
                                self._mine_log_last_prog = now
                                rate = (
                                    f"{hps/1000:.1f} kH/s"
                                    if hps >= 1000
                                    else f"{hps:,.0f} H/s"
                                )
                                self._mine_log_line(
                                    f"· height {height}  nonce {nonce:,}  {rate}"
                                )

                        block, height, bits = rt.prepare_block_template(
                            reward_addr, hrp=self.hrp
                        )
                        parent = block.header.previous_block_hash
                        epoch0 = int(rt.chain.tip_epoch)
                        self._mine_log_line(f"template #{height}  bits=0x{bits:08x}")

                        def _abort() -> bool:
                            if self._miner_stop.is_set():
                                return True
                            # In-memory tip/epoch only — never touch SQLite here.
                            if int(rt.chain.tip_epoch) != epoch0:
                                return True
                            tip = rt.chain.tip_hash
                            return tip is not None and tip != parent

                        try:
                            # Same tight single-lane PoW as MHCOIN Core 0.3.7.3.
                            mine_block_cancellable(
                                block,
                                progress=_prog,
                                abort_check=_abort,
                            )
                        except MiningAborted:
                            if self._miner_stop.is_set():
                                break
                            self._mine_log_line(
                                f"stale template #{height} — tip moved, rebuilding"
                            )
                            continue
                    except KeyboardInterrupt:
                        break
                    except Exception as e:
                        logger.exception("mine template/PoW failed")
                        self._mine_log_line(f"WARN mine step: {e}")
                        time.sleep(0.5)
                        continue
                    if self._miner_stop.is_set():
                        break
                    rt = self._node
                    if rt is None or block is None:
                        break
                    try:
                        # Tip may have moved in the last PoW iteration — retry.
                        if rt.chain.tip_hash != block.header.previous_block_hash:
                            self._mine_log_line(f"stale template #{height} — retry")
                            continue
                        connected = rt.accept_block(block)
                        reward = block.transactions[0].outputs[0].value
                        self._tip_height_hint = int(connected)
                        self._blocks_found += 1
                        self._rewards_sats += reward
                        elapsed = max(time.time() - t0, 1e-9)
                        self._hashrate = max(self._hashrate, 1.0 / elapsed)
                        bhash = block.block_hash().hex()
                        for line in format_mine_plain_found(
                            height=int(connected),
                            block_hash=bhash,
                            reward_sats=int(reward),
                            elapsed=elapsed,
                        ).splitlines():
                            self._mine_log_line(line)
                        self._mine_log_line(
                            f"session {self._blocks_found} blocks · "
                            f"{format_mhc(self._rewards_sats)} MHC"
                        )
                        self._mine_log_line("────────────────────────────────")
                        self._invalidate_history_cache()
                        try:
                            # Only refresh Overview balance if still on this wallet.
                            if self.default_address() == reward_addr:
                                pkh = address_to_pubkey_hash(reward_addr, hrp=self.hrp)
                                with rt.chain._lock:
                                    self._balance_cache_sats = int(
                                        rt.chain.utxo.balance_for_pubkey_hash(pkh)
                                    )
                                self._balance_cache_valid = True
                        except Exception:
                            pass
                        try:
                            cb = block.transactions[0]
                            self._push_recent(
                                TxRow(
                                    kind="Mining reward",
                                    amount_sats=reward,
                                    detail=f"block {connected}",
                                    height=int(connected),
                                    txid=cb.txid_hex(),
                                    timestamp=int(block.header.timestamp),
                                    from_addrs=["coinbase"],
                                    to_addrs=[reward_addr],
                                    fee_sats=0,
                                    confirmations=1,
                                )
                            )
                        except Exception:
                            pass
                        if on_block:
                            on_block(connected, block.block_hash().hex(), reward)
                    except Exception as e:
                        err = str(e).lower()
                        if "malformed" in err or "disk image" in err:
                            logger.exception("accept mined block failed — corrupt chain DB")
                            self._mine_log_line(
                                "ERROR chain.sqlite is corrupted — stop mining, "
                                "delete chain/utxo sqlite files (keep wallet.json), re-sync"
                            )
                            self._miner_stop.set()
                            break
                        logger.exception("accept mined block failed")
                        self._mine_log_line(f"stale/reject #{height}: {e}")
                        continue
                    time.sleep(0.05)
            except Exception as e:
                logger.exception("miner stopped: %s", e)
                self._mine_log_line(f"ERROR miner stopped: {e}")
            finally:
                self._mining = False
                self._mine_stopping = False
                self._hashrate = 0.0
                self._miner_reconfigure.clear()
                self._stop_caffeine()
                self._broadcast_miner_status(force=True)
                self._mine_log_line("■ Mining stopped.")
                # Node stays up — only refresh balance cache + history.
                try:
                    rt = self._node
                    if rt is not None:
                        self._tip_height_hint = max(0, int(rt.chain.height))
                        self._refresh_balance_cache()
                except Exception:
                    pass
                try:
                    self.request_history_build(full_chain=True)
                except Exception:
                    pass

        self._miner_thread = threading.Thread(target=_loop, name="mhcoin-miner", daemon=True)
        self._miner_thread.start()

    def request_stop_mining(self) -> None:
        """Signal miner to stop without waiting (safe on UI / close path)."""
        if self._miner_thread and self._miner_thread.is_alive():
            self._mine_stopping = True
            self._mine_log_line("Stopping miner…")
        self._miner_stop.set()
        self._miner_reconfigure.set()

    def stop_mining(self, *, wait: bool = False, join_timeout: float = 3.0) -> None:
        """Stop miner. Default is non-blocking so Desktop UI stays responsive."""
        self.request_stop_mining()
        if wait and self._miner_thread and self._miner_thread.is_alive():
            self._miner_thread.join(timeout=max(0.2, float(join_timeout)))
        # UI path must not join: GIL-heavy PoW made a blocking join freeze the app.

    # --- optional P2P node --------------------------------------------------

    def start_node(
        self,
        host: str = "0.0.0.0",
        port: int | None = None,
        connect: list[str] | None = None,
        skip_history_wait: bool = False,
        allow_while_mining: bool = False,
    ) -> None:
        with self._node_ctrl:
            if self._node is not None:
                return
            if self._mining and not allow_while_mining:
                raise RuntimeError("Stop mining before starting the P2P node")
            params = get_network_params(self.network)
            listen = port or params.default_port
            peers = list(connect) if connect is not None else default_connect_peers(self.network)
            if skip_history_wait:
                try:
                    self.request_history_build(full_chain=True)
                except Exception:
                    pass
            else:
                self._ensure_history_before_node(timeout=20.0)
            try:
                self.balance_sats()
            except Exception:
                logger.exception("balance snapshot before node failed")
            if not self._balance_cache_valid:
                hint = self._balance_hint_from_history()
                if hint is not None:
                    self._balance_cache_sats = int(hint)
                    self._balance_cache_valid = True
            with self._io:
                node = self._get_local()
                if node.chain.height < 0:
                    node.chain.init_with_genesis(get_network_genesis(self.network))
                try:
                    self._tip_height_hint = max(0, int(node.chain.height))
                except Exception:
                    pass
                self._close_local()
                rt = NodeRuntime(
                    data_dir=self.data_dir,
                    network=self.network,
                    host=host,
                    port=listen,
                    connect=peers,
                    # Desktop: outbound-only — listening invites LAN reconnect floods
                    # that trip misbehavior bans (and fights the seed for :8333).
                    enable_listen=False,
                )
                self._node = rt

            def _run() -> None:
                try:
                    rt.start(blocking=True)
                except Exception:
                    logger.exception("node stopped")
                finally:
                    if self._node is rt:
                        self._node = None
                        self._node_thread = None

            self._node_thread = threading.Thread(target=_run, name="mhcoin-node", daemon=True)
            self._node_thread.start()

    def stop_node(self, *, rebuild_history: bool = True, join_timeout: float = 3.0) -> None:
        """Stop P2P node.

        keep join_timeout short — Desktop API (and UI) used to freeze for 15s+ when
        Start mining called stop_node under the global state lock.
        """
        with self._node_ctrl:
            rt = self._node
            th = self._node_thread
            if rt is None and not (th and th.is_alive()):
                return
            if rt is not None:
                try:
                    rt.stop()
                except Exception:
                    logger.exception("node stop failed")
            if th is not None and th.is_alive() and th is not threading.current_thread():
                th.join(timeout=max(0.5, float(join_timeout)))
                if th.is_alive():
                    logger.warning("node thread still alive after %.1fs join", join_timeout)
                    try:
                        if rt is not None:
                            rt.stop()
                    except Exception:
                        pass
                    th.join(timeout=2.0)
            self._node = None
            if th is None or not th.is_alive():
                self._node_thread = None
            with self._io:
                self._close_local()
            time.sleep(0.1)
        if rebuild_history:
            try:
                self.request_history_build(full_chain=True)
            except Exception:
                logger.debug("post-stop history rebuild failed", exc_info=True)

    def last_txid(self) -> str | None:
        return self._last_txid

    def reload_chain(self) -> None:
        """Drop in-memory LocalNode so the next read reloads from disk.

        Needed when another process (e.g. SoloMiner) wrote blocks into this datadir.
        """
        with self._io:
            self._close_local()

    def shutdown(self) -> None:
        self.stop_mining(wait=True, join_timeout=3.0)
        self.stop_node()
        with self._io:
            self._close_local()
        self.lock()
