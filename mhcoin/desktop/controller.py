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
from mhcoin.mining.block_template import build_block_template
from mhcoin.node.local_node import LocalNode
from mhcoin.node.runtime import NodeRuntime
from mhcoin.wallet.addresses import address_to_pubkey_hash, validate_address
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
        self._resume_node_after_mine = False
        self._balance_cache_sats: int = 0
        self._balance_cache_valid: bool = False
        self._recent_cache: list[dict] = []

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
        # While solo-mining, LocalNode owns the datadir — keep cache fresh from chain.
        if self._node is None:
            return self.refresh_recent_cache(limit=limit, full_chain=False)
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
        if node_running:
            # NodeRuntime owns the datadir — do not open a second LocalNode.
            try:
                st = NodeRuntime.read_status(self.data_dir) or {}
                peers = int(st.get("peer_count") or 0)
                height = max(0, int(st.get("height") or 0))
                tip = str(st["tip"]) if st.get("tip") else None
                sync = "Network synchronized" if peers > 0 else "Node running (no peers)"
            except Exception:
                sync = "Node running"
        else:
            try:
                with self._io:
                    node = self._get_local()
                    tip = node.chain.tip_hash.hex() if node.chain.tip_hash else None
                    height = max(node.chain.height, 0)
                sync = "Ready (solo / local)" if height >= 0 else "No chain yet"
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
            key = (addr, tip, mem_n, bool(full_chain), int(limit))
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

            mining_rows: list[TxRow] = []
            transfer_rows: list[TxRow] = []

            for height, block in reversed(blocks):
                for tx in block.transactions:
                    ours = [o for o in tx.outputs if _out_is_ours(o)]
                    foreign = [o for o in tx.outputs if not _out_is_ours(o)]
                    to_us = sum(o.value for o in ours)
                    paid_out = sum(o.value for o in foreign)
                    spends = _tx_spends_ours(tx)

                    if tx.is_coinbase():
                        if to_us > 0:
                            mining_rows.append(
                                TxRow(
                                    kind="Mining reward",
                                    amount_sats=to_us,
                                    detail=f"block {height}",
                                    height=height,
                                    txid=tx.txid_hex(),
                                )
                            )
                        continue

                    # Sent ONLY if we spent our own outpoint (never treat foreign change as our send).
                    if spends:
                        transfer_rows.append(
                            TxRow(
                                kind="Sent",
                                amount_sats=paid_out,
                                detail=tx.txid_hex(),
                                height=height,
                                txid=tx.txid_hex(),
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
                if spends:
                    pending.append(
                        TxRow(
                            kind="Sent (pending)",
                            amount_sats=paid_out,
                            detail=txid,
                            height=None,
                            txid=txid,
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
        }

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

        def _loop() -> None:
            try:
                while not self._miner_stop.is_set():
                    t0 = time.time()
                    try:

                        def _prog(nonce: int, _h: bytes, hps: float) -> None:
                            if self._miner_stop.is_set():
                                raise KeyboardInterrupt()
                            self._hashrate = hps

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
                            continue
                        connected = node.chain.connect_block(block)
                        node.mempool.clear_included(block.transactions[1:])
                        reward = block.transactions[0].outputs[0].value
                        self._blocks_found += 1
                        self._rewards_sats += reward
                        self._hashrate = max(self._hashrate, 1.0 / max(time.time() - t0, 1e-9))
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
                                )
                            )
                        except Exception:
                            pass
                    if on_block:
                        on_block(connected, block.block_hash().hex(), reward)
                    time.sleep(0.05)
            except Exception as e:
                logger.exception("miner stopped: %s", e)
            finally:
                self._mining = False
                self._hashrate = 0.0
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
                    except Exception:
                        logger.exception("resume node after mining failed")

        self._miner_thread = threading.Thread(target=_loop, name="mhcoin-miner", daemon=True)
        self._miner_thread.start()

    def stop_mining(self) -> None:
        self._miner_stop.set()
        if self._miner_thread and self._miner_thread.is_alive():
            self._miner_thread.join(timeout=5.0)
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
