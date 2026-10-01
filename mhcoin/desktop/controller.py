"""Desktop controller: bridges GUI to MHCOIN core (wallet / chain / miner / optional node).

Uses one shared LocalNode + lock so GUI refresh and mining do not race the same datadir
(that race produced endless "UTXO tip mismatch — rebuilding" spam).
"""

from __future__ import annotations

import hashlib
import logging
import os
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
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.desktop.prefs import save_preferred_network
from mhcoin.desktop.seeds import default_connect_peers
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
        self._mining = False
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
        self._hist_build_lock = threading.Lock()
        self._resume_node_after_mine = False
        self._balance_cache_sats: int = 0
        self._balance_cache_valid: bool = False
        self._recent_cache: list[dict] = []
        self._mine_log: deque[str] = deque(maxlen=500)
        self._mine_log_lock = threading.Lock()
        self._mine_log_last_prog = 0.0

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
        if ts:
            try:
                time_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(ts)))
            except Exception:
                time_utc = str(ts)
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
        if self._recent_cache or self._hist_dicts:
            src = self._recent_cache or self._hist_dicts
            return list(src)[:limit]
        if self._node is None:
            self.request_history_build(full_chain=False)
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
        if not password:
            raise WalletError("password required")
        # New wallet must not keep mining to the previous address / session totals.
        if self._mining:
            self.stop_mining()
        existing = len(Wallet(self.paths).list_wallets())
        label = f"Wallet {existing + 1}"
        w = Wallet(self.paths, password=password)
        addr = w.create(label=label, password=password, make_default=True)
        self._password = password
        self._reset_session_wallet_stats()
        self.ensure_chain()
        return addr

    def _reset_session_wallet_stats(self) -> None:
        """Clear process-local mining counters when the active wallet changes."""
        self._blocks_found = 0
        self._rewards_sats = 0
        self._hashrate = 0.0
        self._invalidate_history_cache()

    def list_wallets(self) -> list[dict]:
        rows = []
        for rec in Wallet(self.paths).list_wallets():
            rows.append(
                {
                    "wallet_id": rec.wallet_id,
                    "label": rec.label,
                    "address": rec.address,
                    "public_key_fp": hashlib.sha256(bytes.fromhex(rec.public_key_hex)).hexdigest()[
                        :16
                    ],
                }
            )
        return rows

    def select_wallet(self, wallet_id: str) -> str:
        if self._mining:
            self.stop_mining()
        w = Wallet(self.paths)
        w.set_default_wallet_id(wallet_id)
        self._password = None  # require unlock for the newly selected wallet
        self._reset_session_wallet_stats()
        return w.default_address()

    def select_wallet_by_address(self, address: str) -> str:
        if self._mining:
            self.stop_mining()
        w = Wallet(self.paths)
        w.set_default_by_address(address)
        self._password = None
        self._reset_session_wallet_stats()
        return w.default_address()

    def default_address(self) -> str:
        return Wallet(self.paths).default_address()

    def unlock(self, password: str, wallet_id: str | None = None) -> str:
        w = Wallet(self.paths, password=password)
        addr = w.unlock_with_password(password, wallet_id=wallet_id or None)
        self._password = password
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
            addr = self.default_address()
        except WalletError:
            return 0
        pkh = address_to_pubkey_hash(addr, hrp=self.hrp)
        with self._io:
            node = self._get_local()
            sats = int(node.chain.utxo.balance_for_pubkey_hash(pkh))
            self._balance_cache_sats = sats
            self._balance_cache_valid = True
            return sats

    def balance_text(self) -> str:
        return f"{format_mhc(self.balance_sats())} MHC"

    def cached_balance_text(self) -> str:
        """Best-effort balance when the P2P node owns the datadir."""
        if self._node is None:
            try:
                return self.balance_text()
            except Exception:
                pass
        return f"{format_mhc(self._balance_cache_sats)} MHC"

    def send(self, to_address: str, amount_mhc: str, password: str, fee_mhc: str | None = None) -> str:
        self.ensure_chain()
        with self._io:
            node = self._get_local()
            if node.chain.height < 0:
                raise WalletError("no blockchain yet — mine or sync first")
            fee = parse_amount_mhc(fee_mhc) if fee_mhc else None
            w = Wallet(self.paths, password=password)
            result = w.send(to_address, amount_mhc, password=password, fee_sats=fee, utxo=node.chain.utxo)
            node.submit_tx(result.tx)
            if self._node is not None:
                try:
                    self._node.submit_tx(result.tx)
                except Exception:
                    pass
            self._password = password
            self._last_txid = result.txid_hex
            self._invalidate_history_cache()
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
                # Bitcoin Core–style IBD: behind peer tip hint or actively downloading.
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
        else:
            try:
                with self._io:
                    node = self._get_local()
                    tip = node.chain.tip_hash.hex() if node.chain.tip_hash else None
                    height = max(node.chain.height, 0)
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
        # Keep _hist_dicts until rebuild finishes so UI stays responsive.

    def request_history_build(self, *, full_chain: bool = True) -> None:
        """Kick a background full-history scan (non-blocking for UI)."""
        with self._hist_build_lock:
            if self._hist_building:
                return
            if self._node is not None:
                # Node owns datadir — cannot scan now; serve cache only.
                return
            self._hist_building = True
            self._hist_build_error = None

        def _job() -> None:
            try:
                rows = self.wallet_history(2000, full_chain=full_chain)
                dicts = [self._txrow_to_dict(r) for r in rows]
                self._hist_dicts = dicts
                self._recent_cache = list(dicts)[:50]
            except Exception as e:  # noqa: BLE001
                logger.exception("history build failed")
                self._hist_build_error = str(e)
            finally:
                with self._hist_build_lock:
                    self._hist_building = False

        threading.Thread(target=_job, name="mhcoin-hist-build", daemon=True).start()

    def history_for_api(self, *, limit: int = 2000) -> dict:
        """Non-blocking history payload for Desktop UI."""
        building = False
        with self._hist_build_lock:
            building = bool(self._hist_building)
        # Prefer last full snapshot; else recent cache.
        txs = list(self._hist_dicts) if self._hist_dicts else list(self._recent_cache)
        partial = not bool(self._hist_dicts)
        if self._node is None and not building and (
            not self._hist_dicts or self._hist_key is None
        ):
            self.request_history_build(full_chain=True)
            with self._hist_build_lock:
                building = bool(self._hist_building)
        return {
            "ok": True,
            "txs": txs[:limit],
            "count": len(txs[:limit]),
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

    def wallet_history(self, limit: int = 2000, *, full_chain: bool = True) -> list[TxRow]:
        """Wallet activity with cache keyed by (address, tip, mempool size)."""
        try:
            addr = self.default_address()
        except WalletError:
            return []
        pkh = address_to_pubkey_hash(addr, hrp=self.hrp)

        with self._io:
            node = self._get_local()
            h = node.chain.height
            if h < 0:
                return []
            tip = node.chain.tip_hash.hex() if node.chain.tip_hash else ""
            mem_n = len(node.mempool)
            key = ("hist-v2", addr, tip, mem_n, bool(full_chain), int(limit))
            if self._hist_key == key and self._hist_rows:
                return list(self._hist_rows)[:limit]

            start = 0 if full_chain else max(0, h - 120)
            tx_index: dict[str, object] = {}
            blocks: list[tuple[int, object]] = []
            for height in range(start, h + 1):
                block = node.chain.get_block_by_height(height)
                if block is None:
                    continue
                blocks.append((height, block))
                for tx in block.transactions:
                    tx_index[tx.txid_hex()] = tx

            def _out_is_ours(out) -> bool:
                try:
                    return out.pubkey_hash() == pkh
                except Exception:
                    return False

            def _addr_of_out(out) -> str | None:
                try:
                    return pubkey_hash_to_address(out.pubkey_hash(), hrp=self.hrp)
                except Exception:
                    return None

            def _tx_spends_ours(tx) -> bool:
                if tx.is_coinbase():
                    return False
                for tin in tx.inputs:
                    prev = tx_index.get(tin.prev_txid.hex())
                    if prev is None:
                        continue
                    vout = int(tin.prev_vout)
                    if 0 <= vout < len(prev.outputs) and _out_is_ours(prev.outputs[vout]):
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

            for height, block in reversed(blocks):
                try:
                    block_ts = int(block.header.timestamp)
                except Exception:
                    block_ts = None
                for tx in block.transactions:
                    ours = [o for o in tx.outputs if _out_is_ours(o)]
                    foreign = [o for o in tx.outputs if not _out_is_ours(o)]
                    to_us = sum(o.value for o in ours)
                    paid_out = sum(o.value for o in foreign)
                    spends = _tx_spends_ours(tx)
                    from_a = _from_addrs(tx)
                    to_ours = [a for a in (_addr_of_out(o) for o in ours) if a]
                    to_foreign = [a for a in (_addr_of_out(o) for o in foreign) if a]
                    fee = _fee_sats(tx)

                    if tx.is_coinbase():
                        if to_us > 0:
                            mining_rows.append(
                                TxRow(
                                    kind="Mining reward",
                                    amount_sats=to_us,
                                    detail=f"block {height}",
                                    height=height,
                                    txid=tx.txid_hex(),
                                    timestamp=block_ts,
                                    from_addrs=["coinbase"],
                                    to_addrs=to_ours,
                                    fee_sats=0,
                                    confirmations=_confs(height),
                                )
                            )
                        continue

                    # Sent ONLY if we spent our own outpoint (never treat foreign change as our send).
                    # Skip pure self-churn (no external payout) — shows as 0.00000000 MHC otherwise.
                    if spends and paid_out > 0:
                        transfer_rows.append(
                            TxRow(
                                kind="Sent",
                                amount_sats=paid_out,
                                detail=tx.txid_hex(),
                                height=height,
                                txid=tx.txid_hex(),
                                timestamp=block_ts,
                                from_addrs=from_a or [addr],
                                to_addrs=to_foreign,
                                fee_sats=fee,
                                confirmations=_confs(height),
                            )
                        )
                    elif to_us > 0:
                        transfer_rows.append(
                            TxRow(
                                kind="Received",
                                amount_sats=to_us,
                                detail=tx.txid_hex(),
                                height=height,
                                txid=tx.txid_hex(),
                                timestamp=block_ts,
                                from_addrs=from_a,
                                to_addrs=to_ours,
                                fee_sats=fee,
                                confirmations=_confs(height),
                            )
                        )

            pending: list[TxRow] = []
            for tx in reversed(node.mempool.list_txs()):
                if tx.is_coinbase():
                    continue
                ours = [o for o in tx.outputs if _out_is_ours(o)]
                foreign = [o for o in tx.outputs if not _out_is_ours(o)]
                to_us = sum(o.value for o in ours)
                paid_out = sum(o.value for o in foreign)
                spends = _tx_spends_ours(tx)
                txid = tx.txid_hex()
                if any(r.txid == txid for r in transfer_rows):
                    continue
                from_a = _from_addrs(tx)
                to_ours = [a for a in (_addr_of_out(o) for o in ours) if a]
                to_foreign = [a for a in (_addr_of_out(o) for o in foreign) if a]
                fee = _fee_sats(tx)
                if spends and paid_out > 0:
                    pending.append(
                        TxRow(
                            kind="Sent (pending)",
                            amount_sats=paid_out,
                            detail=txid,
                            height=None,
                            txid=txid,
                            timestamp=None,
                            from_addrs=from_a or [addr],
                            to_addrs=to_foreign,
                            fee_sats=fee,
                            confirmations=0,
                        )
                    )
                elif to_us > 0:
                    pending.append(
                        TxRow(
                            kind="Received (pending)",
                            amount_sats=to_us,
                            detail=txid,
                            height=None,
                            txid=txid,
                            timestamp=None,
                            from_addrs=from_a,
                            to_addrs=to_ours,
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
            self._hist_key = key
            self._hist_rows = list(rows)
            try:
                self._hist_dicts = [self._txrow_to_dict(r) for r in self._hist_rows]
            except Exception:
                pass
            return rows[:limit]

    # --- mining -------------------------------------------------------------

    @property
    def is_mining(self) -> bool:
        return self._mining

    @property
    def mining_stats(self) -> dict:
        return {
            "mining": self._mining,
            "hashrate": self._hashrate,
            "blocks_found": self._blocks_found,
            "rewards_sats": self._rewards_sats,
            "rewards_text": f"{format_mhc(self._rewards_sats)} MHC",
            "log": self.mine_log_lines(),
        }

    def mine_log_lines(self, limit: int = 200) -> list[str]:
        with self._mine_log_lock:
            lines = list(self._mine_log)
        return lines[-limit:]

    def _mine_log_line(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        with self._mine_log_lock:
            self._mine_log.append(line)

    def start_mining(self, address: str | None = None, on_block: Callable | None = None) -> None:
        if self._mining:
            return
        addr = address or self.default_address()
        if not validate_address(addr, hrp=self.hrp):
            raise ValueError("invalid reward address")
        # Same datadir: pause P2P node while SoloMiner writes blocks.
        self._resume_node_after_mine = self._node is not None
        if self._node is not None:
            self.stop_node()
        self.ensure_chain()
        self._miner_stop.clear()
        self._mining = True
        reward_addr = addr
        try:
            tip_h = -1
            with self._io:
                node = self._get_local()
                tip_h = int(node.chain.height)
        except Exception:
            tip_h = -1
        self._mine_log_line(f"MHCOIN Solo Miner · {self.network} · tip #{tip_h}")
        self._mine_log_line(f"reward  {reward_addr}")
        self._mine_log_line(f"data    {self.data_dir}")
        self._mine_log_line("────────────────────────────────")
        self._mine_log_line("searching nonce… (Stop to quit)")

        def _loop() -> None:
            try:
                while not self._miner_stop.is_set():
                    t0 = time.time()
                    height = -1
                    try:

                        def _prog(nonce: int, _h: bytes, hps: float) -> None:
                            if self._miner_stop.is_set():
                                raise KeyboardInterrupt()
                            self._hashrate = hps
                            now = time.time()
                            # Throttle terminal-style progress (~2 lines/sec).
                            if now - self._mine_log_last_prog >= 0.5:
                                self._mine_log_last_prog = now
                                # Strip ANSI for GUI log panel.
                                plain = (
                                    f"· height {height}  nonce {nonce:,}  "
                                    f"{hps/1000:.1f} kH/s" if hps >= 1000 else
                                    f"· height {height}  nonce {nonce:,}  {hps:,.0f} H/s"
                                )
                                self._mine_log_line(plain)

                        with self._io:
                            node = self._get_local()
                            if node.chain.height < 0 or node.chain.tip_hash is None:
                                break
                            height = node.chain.height + 1
                            params = get_network_params(self.network)
                            tip = node.chain.get_block_by_hash(node.chain.tip_hash)
                            bits = tip.header.bits if tip else params.genesis_bits
                            if self.network in ("regtest", "localnet"):
                                bits = REGTEST_NBITS
                            block = build_block_template(
                                height=height,
                                previous_hash=node.chain.tip_hash,
                                timestamp=int(time.time()),
                                bits=bits,
                                mempool=node.mempool,
                                utxo=node.chain.utxo,
                                miner_pubkey_hash=address_to_pubkey_hash(reward_addr, hrp=self.hrp),
                            )
                        self._mine_log_line(f"template #{height}  bits=0x{bits:08x}")
                        # Mine outside lock so GUI can refresh
                        mine_block(block, progress=_prog)
                    except KeyboardInterrupt:
                        break
                    if self._miner_stop.is_set():
                        break
                    with self._io:
                        node = self._get_local()
                        # Tip may have moved; only connect if still extends
                        if node.chain.tip_hash != block.header.previous_block_hash:
                            self._mine_log_line(f"stale template #{height} — retry")
                            continue
                        connected = node.chain.connect_block(block)
                        node.mempool.clear_included(block.transactions[1:])
                        reward = block.transactions[0].outputs[0].value
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
                            pkh = address_to_pubkey_hash(reward_addr, hrp=self.hrp)
                            self._balance_cache_sats = int(
                                node.chain.utxo.balance_for_pubkey_hash(pkh)
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
                    time.sleep(0.05)
            except Exception as e:
                logger.exception("miner stopped: %s", e)
                self._mine_log_line(f"ERROR miner stopped: {e}")
            finally:
                self._mining = False
                self._hashrate = 0.0
                self._mine_log_line("■ Mining stopped.")
                # Snapshot UTXO balance + recent before P2P node reclaims the datadir.
                try:
                    with self._io:
                        if self._local is not None:
                            try:
                                pkh = address_to_pubkey_hash(reward_addr, hrp=self.hrp)
                                self._balance_cache_sats = int(
                                    self._local.chain.utxo.balance_for_pubkey_hash(pkh)
                                )
                                self._balance_cache_valid = True
                            except Exception:
                                pass
                    try:
                        self.refresh_recent_cache(limit=50, full_chain=True)
                    except Exception:
                        pass
                except Exception:
                    pass
                if self._resume_node_after_mine:
                    self._resume_node_after_mine = False
                    try:
                        self.start_node()
                        self._mine_log_line("Node resumed after mining.")
                    except Exception:
                        logger.exception("resume node after mining failed")
                        self._mine_log_line("WARN: failed to resume node after mining")

        self._miner_thread = threading.Thread(target=_loop, name="mhcoin-miner", daemon=True)
        self._miner_thread.start()

    def request_stop_mining(self) -> None:
        """Signal miner to stop without waiting (safe on UI / close path)."""
        self._miner_stop.set()
        self._mining = False

    def stop_mining(self) -> None:
        self.request_stop_mining()
        if self._miner_thread and self._miner_thread.is_alive():
            self._miner_thread.join(timeout=2.0)
        self._mining = False
        # Node resume (if paused) happens in miner thread finally.

    # --- optional P2P node --------------------------------------------------

    def start_node(
        self,
        host: str = "0.0.0.0",
        port: int | None = None,
        connect: list[str] | None = None,
    ) -> None:
        if self._node is not None:
            return
        if self._mining:
            raise RuntimeError("Stop mining before starting the P2P node")
        params = get_network_params(self.network)
        listen = port or params.default_port
        peers = list(connect) if connect is not None else default_connect_peers(self.network)
        # Full-node style (Bitcoin Core): listen for inbound + dial seeds/gossip.
        # NAT users still work outbound-only if inbound is filtered by the router.
        with self._io:
            node = self._get_local()
            if node.chain.height < 0:
                node.chain.init_with_genesis(get_network_genesis(self.network))
            self._close_local()
            rt = NodeRuntime(
                data_dir=self.data_dir,
                network=self.network,
                host=host,
                port=listen,
                connect=peers,
                enable_listen=True,
            )
            self._node = rt

        def _run() -> None:
            try:
                rt.start(blocking=True)
            except Exception:
                logger.exception("node stopped")
            finally:
                # Clear handle if the run-loop exits on its own.
                if self._node is rt:
                    self._node = None
                    self._node_thread = None

        self._node_thread = threading.Thread(target=_run, name="mhcoin-node", daemon=True)
        self._node_thread.start()

    def stop_node(self) -> None:
        if self._node is None:
            return
        rt = self._node
        try:
            rt.stop()
        except Exception:
            logger.exception("node stop failed")
        th = self._node_thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(timeout=15.0)
        self._node = None
        self._node_thread = None
        # Brief pause so SQLite release settles before LocalNode reopens the files.
        time.sleep(0.15)

    def last_txid(self) -> str | None:
        return self._last_txid

    def reload_chain(self) -> None:
        """Drop in-memory LocalNode so the next read reloads from disk.

        Needed when another process (e.g. SoloMiner) wrote blocks into this datadir.
        """
        with self._io:
            self._close_local()

    def shutdown(self) -> None:
        self.stop_mining()
        self.stop_node()
        with self._io:
            self._close_local()
        self.lock()
