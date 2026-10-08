"""Auto-payout worker — sends matured PROP balances from the pool wallet."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from mhcoin.pool.config import PoolConfig
from mhcoin.pool.db import PoolDB
from mhcoin.wallet.send import format_mhc
from mhcoin.wallet.wallet import Wallet, WalletError, WalletPaths

logger = logging.getLogger("mhcoin.pool.payouts")


class PayoutWorker:
    def __init__(self, cfg: PoolConfig, db: PoolDB, node):
        self.cfg = cfg
        self.db = db
        self.node = node
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.cfg.payout_threshold_sats <= 0:
            logger.info("auto-payout disabled (threshold=0)")
            return
        if not self.cfg.wallet_password:
            logger.warning("auto-payout enabled but MHCOIN_POOL_PASSWORD unset — skipping sends")
            return
        self._thread = threading.Thread(target=self._loop, name="mhcoin-pool-payouts", daemon=True)
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

    def tick(self) -> None:
        threshold = int(self.cfg.payout_threshold_sats)
        if threshold <= 0 or not self.cfg.wallet_password:
            return
        due = self.db.balances_above(threshold)
        if not due:
            return
        paths = WalletPaths(
            data_dir=Path(self.cfg.data_dir),
            network=self.cfg.network,
            hrp=self.cfg.hrp,
        )
        w = Wallet(paths, password=self.cfg.wallet_password)
        for address, matured in due:
            # Leave a dust buffer for fees; send almost all matured.
            send_sats = int(matured) - 10_000
            if send_sats < threshold:
                continue
            amount_text = format_mhc(send_sats)
            try:
                result = w.send(
                    address,
                    amount_text,
                    password=self.cfg.wallet_password,
                    utxo=self.node.chain.utxo,
                )
                self.node.submit_tx(result.tx)
                self.db.record_payout(address, send_sats, result.txid_hex)
                logger.info(
                    "payout %s MHC → %s txid=%s",
                    amount_text,
                    address,
                    result.txid_hex,
                )
            except WalletError as e:
                logger.warning("payout to %s failed: %s", address, e)
            except Exception:
                logger.exception("payout to %s failed", address)
