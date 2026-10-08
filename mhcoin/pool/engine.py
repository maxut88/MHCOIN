"""Pool job engine: templates, share validation, block submit, PROP rounds."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

from mhcoin.consensus.difficulty import hash_meets_target
from mhcoin.mining.share import (
    EXTRANONCE1_SIZE,
    EXTRANONCE2_SIZE,
    PoolJob,
    assemble_block_from_share,
    build_pool_job,
    hash_meets_share,
    job_to_wire,
    pool_coinbase_extra,
)
from mhcoin.pool.config import PoolConfig
from mhcoin.pool.db import PoolDB
from mhcoin.wallet.addresses import validate_address

logger = logging.getLogger("mhcoin.pool")


class PoolEngine:
    def __init__(self, cfg: PoolConfig, db: PoolDB, node: Any):
        self.cfg = cfg
        self.db = db
        self.node = node
        self._lock = threading.RLock()
        self._template = None
        self._template_height = -1
        self._template_bits = 0
        self._job_seq = 0
        self._jobs: dict[str, PoolJob] = {}
        self._en1_by_session: dict[str, bytes] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def refresh_template(self) -> None:
        # Placeholder extranonces; per-worker en1 applied in build_pool_job.
        extra = pool_coinbase_extra(
            b"\x00" * EXTRANONCE1_SIZE, b"\x00" * EXTRANONCE2_SIZE
        )
        try:
            block, height, bits = self.node.prepare_block_template(
                self.cfg.pool_address,
                hrp=self.cfg.hrp,
                coinbase_extra=extra,
            )
        except Exception:
            logger.exception("prepare_block_template failed")
            return
        with self._lock:
            self._template = block
            self._template_height = int(height)
            self._template_bits = int(bits)
            self._job_seq += 1
            # Drop stale jobs (keep last few for in-flight submits).
            if len(self._jobs) > 64:
                for k in list(self._jobs.keys())[:-32]:
                    self._jobs.pop(k, None)

    def _ensure_template(self) -> None:
        tip = -1
        try:
            tip = int(self.node.chain.height) + 1
        except Exception:
            tip = -1
        if self._template is None or tip != self._template_height:
            self.refresh_template()

    def register_worker(self, address: str, worker: str) -> bytes:
        if not validate_address(address, hrp=self.cfg.hrp):
            raise ValueError(f"invalid address for hrp={self.cfg.hrp}")
        worker = (worker or "default")[:32]
        key = f"{address}:{worker}"
        with self._lock:
            en1 = self._en1_by_session.get(key)
            if en1 is None:
                en1 = self.db.next_extranonce1()
                self._en1_by_session[key] = en1
        self.db.upsert_worker(address, worker, en1)
        return en1

    def get_job(self, address: str, worker: str) -> dict:
        en1 = self.register_worker(address, worker)
        self._ensure_template()
        with self._lock:
            if self._template is None:
                raise RuntimeError("no template yet — node syncing?")
            job_id = f"{self._job_seq}-{secrets.token_hex(4)}"
            job = build_pool_job(
                self._template,
                job_id=job_id,
                extranonce1=en1,
                share_factor=self.cfg.share_factor,
            )
            self._jobs[job_id] = job
            self.db.touch_worker(address, worker or "default")
            return job_to_wire(job, en1)

    def submit(
        self,
        *,
        address: str,
        worker: str,
        job_id: str,
        extranonce2_hex: str,
        nonce_hex: str,
        ntime_hex: str | None = None,
    ) -> dict:
        worker = worker or "default"
        key = f"{address}:{worker}"
        with self._lock:
            en1 = self._en1_by_session.get(key)
            job = self._jobs.get(job_id)
        if en1 is None:
            en1 = self.register_worker(address, worker)
        if job is None:
            return {"ok": False, "error": "unknown or stale job"}
        try:
            en2 = bytes.fromhex(extranonce2_hex)
            nonce = int(nonce_hex, 16)
            ntime = int(ntime_hex, 16) if ntime_hex else None
        except ValueError:
            return {"ok": False, "error": "bad hex"}
        if len(en2) != EXTRANONCE2_SIZE:
            return {"ok": False, "error": f"extranonce2 must be {EXTRANONCE2_SIZE} bytes"}

        try:
            block = assemble_block_from_share(
                job, extranonce1=en1, extranonce2=en2, nonce=nonce, ntime=ntime
            )
        except Exception as e:
            return {"ok": False, "error": f"assemble failed: {e}"}

        bh = block.block_hash()
        if not hash_meets_share(bh, job.nbits, job.share_factor):
            return {"ok": False, "error": "low difficulty share"}

        is_block = hash_meets_target(bh, job.nbits)
        block_hash_hex = bh.hex()
        self.db.add_share(
            address=address,
            worker=worker,
            job_id=job_id,
            height=job.height,
            difficulty=float(job.share_factor),
            is_block=is_block,
            block_hash=block_hash_hex if is_block else None,
        )
        self.db.touch_worker(address, worker)

        if not is_block:
            return {"ok": True, "block": False}

        # Network block — submit to node.
        try:
            parent = block.header.previous_block_hash
            if self.node.chain.tip_hash != parent:
                return {"ok": False, "error": "stale block"}
            height = self.node.accept_block(block)
            reward = int(block.transactions[0].outputs[0].value)
            self.db.close_round_with_block(
                height=int(height),
                block_hash=block_hash_hex,
                reward_sats=reward,
                fee_percent=self.cfg.fee_percent,
            )
            self.refresh_template()
            logger.info("POOL BLOCK height=%s hash=%s", height, block_hash_hex)
            return {"ok": True, "block": True, "height": int(height), "hash": block_hash_hex}
        except Exception as e:
            logger.exception("accept_block failed")
            return {"ok": False, "error": f"accept_block: {e}"}

    def set_hashrate(self, address: str, worker: str, hashrate: float) -> None:
        self.db.touch_worker(address, worker or "default", hashrate=float(hashrate))

    def maintenance_tick(self) -> None:
        try:
            tip = int(self.node.chain.height)
        except Exception:
            return
        self.db.mature_rounds(tip, self.cfg.mature_confirms)
        # Refresh jobs periodically / on tip change.
        self._ensure_template()

    def template_loop(self) -> None:
        while not self._stop:
            try:
                self.maintenance_tick()
            except Exception:
                logger.exception("maintenance")
            time.sleep(max(2.0, float(self.cfg.job_refresh_sec)))
