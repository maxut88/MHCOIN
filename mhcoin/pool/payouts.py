"""Auto-payout worker — idempotent PROP matured balance sends.

Flow (crash-safe):
  1. Create ``payout_intents`` row (stable id) — locks amount against double-queue.
  2. Build+sign tx with mempool-spent outpoints excluded; cap to liquid UTXO.
  3. Mark intent ``broadcast`` with txid + raw_tx_hex + input_keys, then submit.
  4. On accept / confirmed → ``complete_payout_intent`` (debit once).

Broadcast recovery (never rebuild a conflicting spend):
  * OUR txid in local mempool or canonical chain → complete
  * reserved inputs spent by OUR txid (spender scan) → complete
  * reserved inputs spent by a FOREIGN txid → hold locked (no complete, no rebuild)
  * inputs gone but spender unknown → hold locked until clear
  * raw tx available + inputs still free → resubmit SAME raw only
  * otherwise hold for reconcile (backoff) — do NOT call wallet.send
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from mhcoin.constants import DEFAULT_FEE_SATOSHIS
from mhcoin.mempool import MempoolError
from mhcoin.pool.config import PoolConfig
from mhcoin.pool.db import PoolDB
from mhcoin.transaction.transaction import Transaction
from mhcoin.utxo import OutPoint
from mhcoin.wallet.send import format_mhc, spendable_total
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths

logger = logging.getLogger("mhcoin.pool.payouts")

# Leave headroom for fees + dust when measuring liquid wallet capacity.
_RESERVE_SATS = 50_000
_FEE_BUFFER = 10_000
_MAX_BACKOFF_SEC = 3600.0
# How many tip blocks to scan for a missing payout txid.
_CHAIN_TXID_SCAN_DEPTH = 256
# Longer pause while waiting for peer/chain confirmation of a broadcast tx.
_RECONCILE_BACKOFF_SEC = 120.0


class PayoutWorker:
    def __init__(self, cfg: PoolConfig, db: PoolDB, node):
        self.cfg = cfg
        self.db = db
        self.node = node
        self._stop = False
        self._thread: threading.Thread | None = None
        self._wallet: Wallet | None = None

    def start(self) -> None:
        if self.cfg.payout_threshold_sats <= 0:
            logger.info("auto-payout disabled (threshold=0)")
            return
        if not self.cfg.wallet_password:
            logger.warning(
                "auto-payout enabled but MHCOIN_POOL_PASSWORD unset — skipping sends"
            )
            return
        self._thread = threading.Thread(
            target=self._loop, name="mhcoin-pool-payouts", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def _loop(self) -> None:
        while not self._stop:
            try:
                self.tick()
            except Exception:
                logger.exception("payout tick failed")
            for _ in range(30):
                if self._stop:
                    return
                time.sleep(1.0)

    def _get_wallet(self) -> Wallet:
        if self._wallet is None:
            paths = WalletPaths(
                data_dir=Path(self.cfg.data_dir),
                network=self.cfg.network,
                hrp=self.cfg.hrp,
            )
            self._wallet = Wallet(paths, password=self.cfg.wallet_password)
        return self._wallet

    def _refresh_mempool(self) -> set[str]:
        mp = getattr(self.node, "mempool", None)
        if mp is None:
            return set()
        try:
            mp.reload()
            utxo = self.node.chain.utxo
            mp.evict_spent_on_chain(utxo)
        except Exception:
            logger.debug("mempool refresh before payout failed", exc_info=True)
        try:
            return set(mp.spent_keys())
        except Exception:
            return set()

    def _txid_in_mempool(self, txid: str) -> bool:
        mp = getattr(self.node, "mempool", None)
        return bool(mp is not None and mp.get(txid))

    def _txid_in_chain(self, txid: str, *, max_depth: int = _CHAIN_TXID_SCAN_DEPTH) -> bool:
        """Scan recent canonical blocks for ``txid`` (no global tx index required)."""
        if not txid:
            return False
        chain = getattr(self.node, "chain", None)
        if chain is None:
            return False
        try:
            get_tx = getattr(chain, "get_tx", None) or getattr(
                chain, "find_transaction", None
            )
            if callable(get_tx) and get_tx(txid) is not None:
                return True
        except Exception:
            logger.debug("chain get_tx probe failed", exc_info=True)
        try:
            tip = int(chain.height)
        except Exception:
            return False
        if tip < 0:
            return False
        want = txid.lower()
        start = max(0, tip - max(0, int(max_depth)) + 1)
        for h in range(tip, start - 1, -1):
            try:
                block = chain.get_block_by_height(h)
            except Exception:
                continue
            if block is None:
                continue
            for tx in block.transactions:
                if tx.txid_hex().lower() == want:
                    return True
        return False

    def _parse_input_keys(self, intent: dict) -> list[str]:
        raw = intent.get("input_keys")
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(x) for x in raw]
        try:
            data = json.loads(str(raw))
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
        return []

    def _inputs_spent_on_chain(self, input_keys: list[str]) -> bool | None:
        """Return True if any reserved input is gone from UTXO; None if unknown."""
        if not input_keys:
            return None
        utxo = getattr(getattr(self.node, "chain", None), "utxo", None)
        if utxo is None:
            return None
        for key in input_keys:
            try:
                txid_hex, vout_s = str(key).split(":", 1)
                op = OutPoint(txid=bytes.fromhex(txid_hex), vout=int(vout_s))
            except Exception:
                continue
            if not utxo.has(op):
                return True
        return False

    def _find_spender_txid(
        self, input_keys: list[str], *, max_depth: int = _CHAIN_TXID_SCAN_DEPTH
    ) -> str | None:
        """Return txid of the canonical tx that spends any reserved outpoint."""
        if not input_keys:
            return None
        want = set(str(k).lower() for k in input_keys)
        chain = getattr(self.node, "chain", None)
        if chain is None:
            return None
        try:
            tip = int(chain.height)
        except Exception:
            return None
        if tip < 0:
            return None
        start = max(0, tip - max(0, int(max_depth)) + 1)
        for h in range(tip, start - 1, -1):
            try:
                block = chain.get_block_by_height(h)
            except Exception:
                continue
            if block is None:
                continue
            for tx in block.transactions:
                if tx.is_coinbase():
                    continue
                for tin in tx.inputs:
                    key = f"{tin.prev_txid.hex()}:{tin.prev_vout}".lower()
                    if key in want:
                        return tx.txid_hex()
        return None

    def _txid_known(self, txid: str) -> bool:
        return self._txid_in_mempool(txid) or self._txid_in_chain(txid)

    def _backoff(self, attempts: int) -> float:
        return min(_MAX_BACKOFF_SEC, 30.0 * (2 ** max(0, int(attempts))))

    def _resubmit_raw(self, raw_tx_hex: str) -> str:
        tx = Transaction.deserialize(bytes.fromhex(raw_tx_hex))
        return self.node.submit_tx(tx)

    def _hold_reconcile(self, intent_id: str, attempts: int, error: str) -> None:
        self.db.mark_intent_failed(
            intent_id,
            error,
            backoff_sec=max(_RECONCILE_BACKOFF_SEC, self._backoff(attempts)),
        )
        logger.warning("payout intent %s held for reconcile: %s", intent_id, error)

    def _recover_broadcast_intent(
        self, intent: dict, exclude: set[str]
    ) -> bool:
        """Handle status=broadcast. Returns True if fully handled (no new build).

        Success requires confirmation of **our** reserved txid (mempool or chain).
        A foreign spend of the same inputs must NOT complete the intent.
        """
        intent_id = str(intent["id"])
        address = str(intent["address"])
        txid = str(intent.get("txid") or "")
        attempts = int(intent.get("attempts") or 0)
        input_keys = self._parse_input_keys(intent)
        raw_hex = intent.get("raw_tx_hex") or ""
        our = txid.lower()

        # Always reserve inputs against new payouts in this tick.
        exclude.update(input_keys)

        # 1) Explicit sighting of OUR txid — only then complete.
        if txid and self._txid_known(txid):
            self.db.complete_payout_intent(intent_id, txid)
            logger.info(
                "payout intent %s completed (our txid in mempool/chain) → %s",
                intent_id,
                address,
            )
            return True

        spent = self._inputs_spent_on_chain(input_keys)
        if spent is True:
            # Inputs gone — identify WHO spent them. Complete only if spender == our txid.
            spender = self._find_spender_txid(input_keys)
            if spender and spender.lower() == our:
                self.db.complete_payout_intent(intent_id, txid)
                logger.info(
                    "payout intent %s completed (our txid spends reserved inputs) → %s",
                    intent_id,
                    address,
                )
                return True
            if spender and spender.lower() != our:
                self._hold_reconcile(
                    intent_id,
                    attempts,
                    f"conflict_spend: reserved inputs spent by foreign txid="
                    f"{spender[:16]}… (ours={txid[:16]}…); locked until manual resolve",
                )
                return True
            # Spent but spender not found in scan window — ambiguous; keep locked.
            self._hold_reconcile(
                intent_id,
                attempts,
                "ambiguous_spend: inputs gone but our txid not found in chain scan; "
                "holding lock (no rebuild)",
            )
            return True

        if raw_hex and spent is False:
            # Same signed body only — never wallet.send a replacement.
            try:
                self._resubmit_raw(str(raw_hex))
                self.db.complete_payout_intent(intent_id, txid)
                logger.info(
                    "payout intent %s resubmitted same raw txid=%s → %s",
                    intent_id,
                    txid,
                    address,
                )
                return True
            except MempoolError as e:
                msg = str(e).lower()
                if "duplicate" in msg:
                    self.db.complete_payout_intent(intent_id, txid)
                    return True
                # Missing inputs / conflict → hold for reconcile
                self.db.mark_intent_failed(
                    intent_id,
                    f"resubmit rejected: {e}",
                    backoff_sec=max(
                        _RECONCILE_BACKOFF_SEC, self._backoff(attempts)
                    ),
                )
                logger.warning(
                    "payout intent %s holding for reconcile (resubmit): %s",
                    intent_id,
                    e,
                )
                return True
            except Exception as e:
                self.db.mark_intent_failed(
                    intent_id,
                    f"resubmit error: {e}",
                    backoff_sec=max(
                        _RECONCILE_BACKOFF_SEC, self._backoff(attempts)
                    ),
                )
                logger.warning(
                    "payout intent %s holding for reconcile: %s", intent_id, e
                )
                return True

        # No local sighting, inputs still free (or unknown), and cannot safely rebuild.
        self.db.mark_intent_failed(
            intent_id,
            "awaiting_chain_or_peers: broadcast tx not in local mempool; "
            "will not rebuild to avoid double-spend",
            backoff_sec=max(_RECONCILE_BACKOFF_SEC, self._backoff(attempts)),
        )
        logger.warning(
            "payout intent %s deferred reconcile (txid=%s missing locally; "
            "no conflicting rebuild)",
            intent_id,
            txid[:16] if txid else "",
        )
        return True

    def _process_open_intents(self, w: Wallet, exclude: set[str]) -> None:
        for intent in self.db.list_open_payout_intents(due_only=True):
            if self._stop:
                return
            self._execute_intent(w, intent, exclude)

    def _execute_intent(self, w: Wallet, intent: dict, exclude: set[str]) -> None:
        intent_id = str(intent["id"])
        address = str(intent["address"])
        amount = int(intent["amount_sats"])
        status = str(intent["status"])
        attempts = int(intent.get("attempts") or 0)

        if status == "broadcast" and intent.get("txid"):
            self._recover_broadcast_intent(intent, exclude)
            return

        if amount + DEFAULT_FEE_SATOSHIS > self._liquid_sats(w, exclude):
            err = (
                f"insufficient liquid funds for {amount} sats "
                f"(need fee+reserve; will retry)"
            )
            self.db.mark_intent_failed(
                intent_id, err, backoff_sec=self._backoff(attempts)
            )
            logger.warning("payout intent %s deferred: %s", intent_id, err)
            return

        amount_text = format_mhc(amount)
        try:
            result = w.send(
                address,
                amount_text,
                password=self.cfg.wallet_password,
                utxo=self.node.chain.utxo,
                exclude_outpoints=exclude,
            )
        except WalletError as e:
            self.db.mark_intent_failed(
                intent_id, str(e), backoff_sec=self._backoff(attempts)
            )
            logger.warning("payout intent %s build failed: %s", intent_id, e)
            return
        except Exception as e:
            self.db.mark_intent_failed(
                intent_id, str(e), backoff_sec=self._backoff(attempts)
            )
            logger.exception("payout intent %s build failed", intent_id)
            return

        input_keys = [
            f"{tin.prev_txid.hex()}:{tin.prev_vout}" for tin in result.tx.inputs
        ]
        raw_hex = result.tx.serialize().hex()
        # Persist signed body BEFORE submit so crash mid-flight keeps the same tx.
        self.db.mark_intent_broadcast(
            intent_id,
            result.txid_hex,
            raw_tx_hex=raw_hex,
            input_keys=input_keys,
        )
        exclude.update(input_keys)

        try:
            self.node.submit_tx(result.tx)
        except MempoolError as e:
            msg = str(e).lower()
            if "duplicate" in msg:
                self.db.complete_payout_intent(intent_id, result.txid_hex)
                logger.info(
                    "payout intent %s already in mempool txid=%s",
                    intent_id,
                    result.txid_hex,
                )
                return
            self.db.mark_intent_failed(
                intent_id, str(e), backoff_sec=self._backoff(attempts)
            )
            logger.warning("payout intent %s submit rejected: %s", intent_id, e)
            return
        except Exception as e:
            # Keep status=broadcast with txid+raw — may have propagated to peers.
            self.db.mark_intent_failed(
                intent_id,
                str(e),
                backoff_sec=max(_RECONCILE_BACKOFF_SEC, self._backoff(attempts)),
            )
            logger.exception("payout intent %s submit failed — holding raw for reconcile", intent_id)
            return

        self.db.complete_payout_intent(intent_id, result.txid_hex)
        logger.info(
            "payout %s MHC → %s txid=%s intent=%s",
            amount_text,
            address,
            result.txid_hex,
            intent_id,
        )

    def _liquid_sats(self, w: Wallet, exclude: set[str]) -> int:
        """Spendable account total minus fee reserve (best-effort without re-unlock)."""
        try:
            from mhcoin.crypto.hashing import hash160
            from mhcoin.wallet.storage import load_wallet_file

            pwd = self.cfg.wallet_password
            paths = w.paths
            wf = load_wallet_file(paths.wallet_file)
            if not wf or not wf.wallets:
                return 0
            active = w._default_record()
            acc = w._account_id_for(active)
            keys_by_pkh: dict[bytes, tuple[bytes, bytes]] = {}
            for rec in wf.wallets:
                if w._account_id_for(rec) != acc:
                    continue
                if rec.watch_only or not rec.encrypted_private_key:
                    continue
                try:
                    kp = w.unlock_key(rec.wallet_id, pwd)
                except WalletError:
                    continue
                keys_by_pkh[hash160(kp.public_key_compressed)] = (
                    kp.private_key,
                    kp.public_key_compressed,
                )
            from mhcoin.consensus.params import get_coinbase_maturity_activation

            tip_h = max(int(self.node.chain.height), 0)
            total = spendable_total(
                self.node.chain.utxo,
                keys_by_pkh,
                exclude_outpoints=exclude,
                tip_height=tip_h,
                coinbase_maturity_activation=get_coinbase_maturity_activation(
                    getattr(self.node.chain, "network", None)
                    or getattr(self.cfg, "network", "mainnet")
                ),
            )
            return max(0, total - _RESERVE_SATS)
        except Exception:
            logger.debug("liquid sats probe failed", exc_info=True)
            return 0

    def tick(self) -> None:
        threshold = int(self.cfg.payout_threshold_sats)
        if threshold <= 0 or not self.cfg.wallet_password:
            return

        exclude = self._refresh_mempool()
        # Also exclude inputs reserved by open broadcast intents (even if not due yet).
        for intent in self.db.list_open_payout_intents(due_only=False):
            if str(intent.get("status")) == "broadcast":
                exclude.update(self._parse_input_keys(intent))

        w = self._get_wallet()

        # 1) Resume / retry open intents first (idempotent).
        self._process_open_intents(w, exclude)

        # 2) Queue new intents for matured balances (no send yet).
        due = self.db.balances_above(threshold)
        if not due:
            return

        liquid = self._liquid_sats(w, exclude)
        for address, matured_avail in due:
            if self._stop:
                return
            send_sats = int(matured_avail) - _FEE_BUFFER
            if send_sats < threshold:
                continue
            if liquid <= threshold:
                logger.warning(
                    "payout queue paused: liquid=%s below threshold (liability exists)",
                    liquid,
                )
                break
            send_sats = min(send_sats, liquid)
            if send_sats < threshold:
                continue
            try:
                intent_id = self.db.create_payout_intent(address, send_sats)
            except ValueError as e:
                logger.debug("skip intent for %s: %s", address, e)
                continue
            intent = self.db.get_payout_intent(intent_id)
            if intent is None:
                continue
            self._execute_intent(w, intent, exclude)
            liquid = max(0, liquid - int(intent["amount_sats"]) - DEFAULT_FEE_SATOSHIS)
