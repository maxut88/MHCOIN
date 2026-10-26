"""Pool job engine: templates, share validation, block submit, PROP rounds."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

from mhcoin.consensus.chain_work import work_for_bits
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


class FaultInjected(Exception):
    """Raised by test-only fault points (never enabled in production config)."""


class PoolEngine:
    def __init__(self, cfg: PoolConfig, db: PoolDB, node: Any):
        self.cfg = cfg
        self.db = db
        self.node = node
        self._lock = threading.RLock()
        self._block_lock = threading.RLock()  # serialize network-block accept+credit
        self._template = None
        self._template_height = -1
        self._template_bits = 0
        self._job_seq = 0
        self._jobs: dict[str, PoolJob] = {}
        self._en1_by_session: dict[str, bytes] = {}
        self._stop = False
        self._status_last_broadcast = 0.0
        self._hashrate_sample_last = 0.0
        self._metrics_prev: dict[str, Any] | None = None
        self._last_metrics: dict[str, Any] = {}
        self._last_alerts: list[dict[str, str]] = []
        self._process_start_ts = time.time()
        self._enrich_cache_tip: int | None = None
        self._enrich_cache: dict[str, dict] = {}
        self._enrich_cache_tip: int | None = None
        self._enrich_cache: dict[str, dict] = {}
        # Test-only: set of fault point names that raise FaultInjected once.
        self._fault_points: set[str] = set()
        self._fault_hits: set[str] = set()
        cleared = self.db.reconcile_rejected_block_shares()
        if cleared:
            logger.info("reconciled %s rejected block share(s)", cleared)
        recovered = self.reconcile_pending_accepts()
        if recovered:
            logger.info("recovered %s pending accept(s) after restart", recovered)

    def stop(self) -> None:
        self._stop = True

    def arm_fault(self, name: str) -> None:
        """Test helper: next pass through ``name`` raises FaultInjected once."""
        self._fault_points.add(name)

    def _fault(self, name: str) -> None:
        if name in self._fault_points and name not in self._fault_hits:
            self._fault_hits.add(name)
            self._fault_points.discard(name)
            raise FaultInjected(name)

    def _canonical_hash_at(self, height: int) -> str | None:
        try:
            block = self.node.chain.get_block_by_height(int(height))
            if block is None:
                return None
            return block.block_hash().hex()
        except Exception:
            logger.exception("canonical_hash_at failed height=%s", height)
            return None

    def _lookup_indexed_hash(self, block_hash_hex: str) -> dict[str, Any] | None:
        """Return indexed metadata for ``block_hash_hex``, or None if unknown.

        Keys: height (int), status (int), active (bool) — active means STATUS_ACTIVE
        and the hash matches ``get_block_by_height(height)``.
        """
        from mhcoin.blockchain.chain import STATUS_ACTIVE

        hx = (block_hash_hex or "").strip().lower()
        if not hx:
            return None
        try:
            raw = bytes.fromhex(hx)
            idx = self.node.chain.get_index(raw)
            if idx is None:
                return None
            height = int(idx.height)
            status = int(idx.status)
            active = False
            if status == STATUS_ACTIVE:
                try:
                    at = self.node.chain.get_block_by_height(height)
                    active = (
                        at is not None and at.block_hash().hex().lower() == hx
                    )
                except Exception:
                    active = False
            return {"height": height, "status": status, "active": active}
        except Exception:
            logger.exception("lookup_indexed_hash failed hash=%s", hx[:16])
            return None

    def _resolve_accepted_height(
        self,
        block_hash_hex: str,
        *,
        reported_height: int | None,
        parent_hash_hex: str | None,
    ) -> int | None:
        """Authoritative height for pool accounting: indexed height of hash B.

        Never persist a mutable tip reading when the index row is missing.
        """
        tip_h: int | None
        tip_hx: str | None
        try:
            tip_h = int(self.node.chain.height)
            th = self.node.chain.tip_hash
            tip_hx = th.hex() if th is not None else None
        except Exception:
            tip_h, tip_hx = None, None

        info = self._lookup_indexed_hash(block_hash_hex)
        if info is None:
            logger.warning(
                "accepted hash not yet indexed — defer round close "
                "hash=%s reported_height=%s parent=%s tip_height=%s tip_hash=%s",
                (block_hash_hex or "")[:16],
                reported_height,
                (parent_hash_hex or "")[:16] if parent_hash_hex else None,
                tip_h,
                (tip_hx or "")[:16] if tip_hx else None,
            )
            return None

        indexed = int(info["height"])
        if reported_height is not None and int(reported_height) != indexed:
            logger.warning(
                "accept height reconciled hash=%s reported=%s indexed=%s "
                "parent=%s tip_height=%s tip_hash=%s active=%s",
                (block_hash_hex or "")[:16],
                reported_height,
                indexed,
                (parent_hash_hex or "")[:16] if parent_hash_hex else None,
                tip_h,
                (tip_hx or "")[:16] if tip_hx else None,
                info.get("active"),
            )
        else:
            logger.info(
                "accept height ok hash=%s height=%s parent=%s tip_height=%s",
                (block_hash_hex or "")[:16],
                indexed,
                (parent_hash_hex or "")[:16] if parent_hash_hex else None,
                tip_h,
            )
        return indexed

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
        parent = block.header.previous_block_hash
        with self._lock:
            self._template = block
            self._template_height = int(height)
            self._template_bits = int(bits)
            self._job_seq += 1
            # Invalidate jobs built on a different tip (stale mempool/UTXO risk).
            for k, job in list(self._jobs.items()):
                if job.prevhash != parent:
                    self._jobs.pop(k, None)
            # Cap in-flight job map size.
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

        # Duplicate share detection (same job/en2/nonce/ntime).
        if not self.db.record_share_submission(
            job_id=job_id,
            extranonce2=extranonce2_hex,
            nonce=nonce_hex,
            ntime=ntime_hex or "",
            share_id=None,
        ):
            return {"ok": False, "error": "duplicate share"}

        share_id = self.db.add_share(
            address=address,
            worker=worker,
            job_id=job_id,
            height=job.height,
            difficulty=float(job.share_factor),
            is_block=is_block,
            block_hash=block_hash_hex if is_block else None,
        )
        # Live workers H/s: prefer share-window estimate so miners that never
        # call hashrate RPC (e.g. older Patoshi C++) still show real rate.
        try:
            sf = max(1, int(job.share_factor))
            share_work = float(work_for_bits(int(job.nbits))) / float(sf)
            est = self.db.estimate_worker_hashrate(
                address, worker, share_work=share_work
            )
        except Exception:
            est = 0.0
        if est > 0:
            self.db.touch_worker(address, worker, hashrate=est)
        else:
            self.db.touch_worker(address, worker)

        if not is_block:
            return {"ok": True, "block": False}

        # Idempotent: already credited this hash.
        existing = self.db.round_id_for_block_hash(block_hash_hex)
        if existing is not None:
            return {
                "ok": True,
                "block": True,
                "height": None,
                "hash": block_hash_hex,
                "duplicate": True,
            }

        # Network block — serialize accept + accounting (multi-miner race safety).
        with self._block_lock:
            existing = self.db.round_id_for_block_hash(block_hash_hex)
            if existing is not None:
                return {
                    "ok": True,
                    "block": True,
                    "height": None,
                    "hash": block_hash_hex,
                    "duplicate": True,
                }
            try:
                parent = block.header.previous_block_hash
                if self.node.chain.tip_hash != parent:
                    self.db.clear_share_block_flag(share_id, reason="stale block")
                    self.db.mark_pending_failed(block_hash_hex, reason="stale")
                    return {"ok": False, "error": "stale block"}

                self.db.begin_pending_accept(
                    share_id=share_id, block_hash=block_hash_hex
                )
                self._fault("before_accept_block")

                reported_height = self.node.accept_block(block)
                reward = int(block.transactions[0].outputs[0].value)
                parent_hx = parent.hex() if isinstance(parent, (bytes, bytearray)) else None
                height = self._resolve_accepted_height(
                    block_hash_hex,
                    reported_height=int(reported_height),
                    parent_hash_hex=parent_hx,
                )
                if height is None:
                    # Node accepted but index not visible yet — keep pending for reconcile.
                    self.db.mark_pending_node_accepted(
                        block_hash_hex,
                        height=int(reported_height),
                        reward_sats=reward,
                    )
                    logger.warning(
                        "POOL BLOCK deferred close hash=%s reported_height=%s "
                        "(await index reconcile)",
                        block_hash_hex[:16],
                        reported_height,
                    )
                    return {
                        "ok": True,
                        "block": True,
                        "height": None,
                        "hash": block_hash_hex,
                        "deferred": True,
                        "reward_sats": int(reward),
                    }

                self.db.mark_pending_node_accepted(
                    block_hash_hex, height=int(height), reward_sats=reward
                )
                self._fault("after_accept_before_db")

                self.db.close_round_with_block(
                    height=int(height),
                    block_hash=block_hash_hex,
                    reward_sats=reward,
                    fee_percent=self.cfg.fee_percent,
                    found_ts=float(block.header.timestamp),
                )
                self._fault("after_db_before_commit_mark")
                self.db.mark_pending_committed(block_hash_hex)
                self._fault("after_commit_before_reply")

                self.refresh_template()
                logger.info("POOL BLOCK height=%s hash=%s", height, block_hash_hex)
                try:
                    size_b = len(block.serialize())
                except Exception:
                    size_b = 0
                return {
                    "ok": True,
                    "block": True,
                    "height": int(height),
                    "hash": block_hash_hex,
                    "reward_sats": int(reward),
                    "nbits": f"{int(block.header.bits):08x}",
                    "size": int(size_b),
                    "time": int(block.header.timestamp),
                }
            except FaultInjected:
                raise
            except Exception as e:
                # If node already accepted but DB failed, leave pending as
                # accepted_node for startup reconcile — do not clear forever.
                pending = [
                    p
                    for p in self.db.list_pending_accepts(
                        statuses=("intent", "accepted_node")
                    )
                    if p.get("block_hash") == block_hash_hex
                ]
                if pending and pending[0].get("status") == "accepted_node":
                    logger.exception(
                        "accept_block accounting failed; pending reconcile will retry"
                    )
                    return {"ok": False, "error": f"accounting: {e}"}
                self.db.clear_share_block_flag(
                    share_id, reason=f"accept_block: {e}"[:240]
                )
                self.db.mark_pending_failed(block_hash_hex, reason="reject")
                logger.exception("accept_block failed")
                return {"ok": False, "error": f"accept_block: {e}"}

    def reconcile_pending_accepts(self) -> int:
        """Complete credits for node-accepted blocks that are still canonical.

        NEVER credits a non-canonical / missing / side-chain hash.
        If the node lookup fails (UNKNOWN) → DEFER (leave pending).
        """
        from mhcoin.blockchain.chain import STATUS_ACTIVE

        recovered = 0
        for p in self.db.list_pending_accepts(statuses=("intent", "accepted_node")):
            hx = str(p["block_hash"])
            if self.db.round_id_for_block_hash(hx) is not None:
                self.db.mark_pending_committed(hx)
                recovered += 1
                continue

            # Canonical gate — required before any credit write.
            try:
                raw = bytes.fromhex(hx)
                idx = self.node.chain.get_index(raw)
                block = self.node.chain.get_block_by_hash(raw)
            except Exception:
                logger.exception(
                    "pending reconcile: node unavailable hash=%s — defer", hx[:16]
                )
                continue  # UNKNOWN → DEFER

            if block is None or idx is None:
                # Not on disk — expire old intents; else defer briefly.
                age = time.time() - float(p.get("created_ts") or 0)
                if age > 3600:
                    self.db.mark_pending_failed(hx, reason="not_on_chain")
                    if p.get("share_id"):
                        self.db.clear_share_block_flag(
                            int(p["share_id"]), reason="not_on_chain"
                        )
                continue

            status = int(getattr(idx, "status", -1))
            height = int(idx.height)
            # Must be the active block at that height.
            try:
                active = self.node.chain.get_block_by_height(height)
                active_hx = active.block_hash().hex() if active is not None else ""
            except Exception:
                logger.exception(
                    "pending reconcile: tip lookup failed hash=%s — defer", hx[:16]
                )
                continue  # UNKNOWN → DEFER

            if status != STATUS_ACTIVE or active_hx.lower() != hx.lower():
                logger.warning(
                    "pending reconcile: non-canonical hash=%s height=%s "
                    "status=%s active=%s — discard without credits",
                    hx[:16],
                    height,
                    status,
                    (active_hx[:16] if active_hx else None),
                )
                self.db.mark_pending_failed(hx, reason="non_canonical")
                if p.get("share_id"):
                    self.db.clear_share_block_flag(
                        int(p["share_id"]), reason="non_canonical"
                    )
                continue

            reward = int(block.transactions[0].outputs[0].value)
            self.db.mark_pending_node_accepted(
                hx, height=height, reward_sats=reward
            )
            try:
                self.db.close_round_with_block(
                    height=height,
                    block_hash=hx,
                    reward_sats=reward,
                    fee_percent=self.cfg.fee_percent,
                    found_ts=float(block.header.timestamp),
                )
                self.db.mark_pending_committed(hx)
                recovered += 1
                logger.info(
                    "reconciled pending accept hash=%s height=%s", hx[:16], height
                )
            except Exception:
                logger.exception("pending reconcile credit failed hash=%s", hx[:16])
        return recovered

    def set_hashrate(self, address: str, worker: str, hashrate: float) -> None:
        self.db.touch_worker(address, worker or "default", hashrate=float(hashrate))

    def enrich_blocks(self, blocks: list[dict]) -> list[dict]:
        """Attach canonical / reorg notes for Pool UI (best-effort, never raises).

        Fast path: ``matured``/``paid`` rounds already passed the canonical check at
        maturity — only ``immature``/``orphaned`` hit the chain index. Results are
        cached per tip so / and /api/stats stay snappy on refresh.
        """
        try:
            tip = int(self.node.chain.height)
        except Exception:
            tip = -1
        if tip != self._enrich_cache_tip:
            self._enrich_cache_tip = tip
            self._enrich_cache = {}
        mature = int(self.cfg.mature_confirms)
        out: list[dict] = []
        for raw in blocks or []:
            b = dict(raw)
            h = b.get("height")
            bh = str(b.get("block_hash") or "").lower()
            st = str(b.get("status") or "")
            cache_key = f"{h}:{bh}:{st}"
            cached = self._enrich_cache.get(cache_key)
            if cached is not None:
                merged = dict(b)
                merged.update(cached)
                out.append(merged)
                continue
            canon: str | None = None
            on = False
            # Matured/paid already verified canonical at maturity — skip chain IO.
            if st in ("matured", "paid") and h is not None:
                on = True
                canon = bh or None
            elif h is not None:
                try:
                    canon = self._canonical_hash_at(int(h))
                except Exception:
                    canon = None
                on = bool(canon and bh and canon.lower() == bh)
            try:
                hi = int(h)  # type: ignore[arg-type]
                conf = (tip - hi + 1) if (on and tip >= hi) else 0
            except (TypeError, ValueError):
                conf = 0
            note = ""
            if st == "orphaned":
                if canon and not on:
                    note = f"reorg / side-chain — canonical {canon[:16]}…"
                else:
                    note = "orphaned (not on best chain)"
            elif st == "immature":
                past_maturity = on and tip >= 0 and h is not None and tip >= int(h) + mature
                if past_maturity:
                    note = (
                        f"stale immature — {conf} confirmations "
                        f"(maturity {mature})"
                    )
                    b["stale_immature"] = True
                elif on:
                    note = f"{conf}/{mature} confirmations on best chain"
                elif canon:
                    note = f"not canonical — will orphan at maturity (canon {canon[:16]}…)"
                else:
                    note = "immature"
            elif st == "matured":
                note = f"canonical · {conf} confirmations" if on else "matured"
            elif st == "paid" and on:
                note = f"canonical · paid · {conf} confirmations"
            # Attach block header timestamp (= explorer Time); backfill DB if empty.
            found_ts = b.get("found_ts")
            if (found_ts is None or found_ts == 0) and h is not None and bh:
                try:
                    blk = None
                    if on:
                        blk = self.node.chain.get_block_by_height(int(h))
                    if blk is None:
                        try:
                            blk = self.node.chain.get_block_by_hash(bytes.fromhex(bh))
                        except Exception:
                            blk = None
                    if blk is not None and blk.block_hash().hex().lower() == bh:
                        found_ts = float(blk.header.timestamp)
                        try:
                            self.db.set_round_found_ts(bh, found_ts)
                        except Exception:
                            logger.debug(
                                "found_ts backfill persist failed", exc_info=True
                            )
                except Exception:
                    found_ts = None
            extra = {
                "canonical_hash": canon,
                "on_best_chain": on,
                "confirmations": conf,
                "status_note": note,
            }
            if found_ts is not None:
                extra["found_ts"] = float(found_ts)
            if b.get("stale_immature"):
                extra["stale_immature"] = True
            self._enrich_cache[cache_key] = extra
            b.update(extra)
            out.append(b)
        return out

    def refresh_metrics(self) -> dict[str, Any]:
        from mhcoin.pool.monitoring import collect_pool_metrics, evaluate_alerts

        metrics = collect_pool_metrics(
            db=self.db,
            engine=self,
            process_start_ts=self._process_start_ts,
            mature_confirms=self.cfg.mature_confirms,
        )
        # Consume one-shot mature event codes into this metrics snapshot.
        codes = list(getattr(self, "_mature_event_codes", []) or [])
        metrics["mature_event_codes"] = codes
        self._mature_event_codes = []
        alerts = evaluate_alerts(metrics, prev=self._metrics_prev)
        self._metrics_prev = metrics
        self._last_metrics = metrics
        self._last_alerts = [
            {"code": a.code, "severity": a.severity, "message": a.message} for a in alerts
        ]
        for a in alerts:
            if a.severity in ("warn", "crit"):
                logger.warning("pool alert %s: %s", a.code, a.message)
            elif a.code == "stale_immature":
                logger.warning("pool alert %s: %s", a.code, a.message)
        return metrics

    def maintenance_tick(self) -> None:
        try:
            tip = int(self.node.chain.height)
        except Exception:
            return
        mature_events: list[dict[str, Any]] = []
        self.db.mature_rounds(
            tip,
            self.cfg.mature_confirms,
            canonical_hash_at=self._canonical_hash_at,
            tip_height_now=lambda: int(self.node.chain.height),
            lookup_indexed_hash=self._lookup_indexed_hash,
            events=mature_events,
        )
        for ev in mature_events:
            code = str(ev.get("code") or "mature_event")
            msg = str(ev.get("message") or code)
            logger.warning("pool mature event %s: %s", code, msg)
            if not hasattr(self, "_mature_event_codes"):
                self._mature_event_codes: list[str] = []
            self._mature_event_codes.append(code)
        # Refresh jobs periodically / on tip change.
        self._ensure_template()
        self._sample_pool_hashrate()
        self._broadcast_pool_status(tip)
        try:
            self.refresh_metrics()
        except Exception:
            logger.debug("metrics refresh failed", exc_info=True)

    def _sample_pool_hashrate(self) -> None:
        now = time.time()
        if (now - float(self._hashrate_sample_last or 0.0)) < 15.0:
            return
        try:
            ov = self.db.stats_overview()
            self.db.record_hashrate(
                "pool", "", float(ov.get("pool_hashrate") or 0), min_interval=14.0
            )
            self._hashrate_sample_last = now
        except Exception:
            logger.debug("hashrate sample failed", exc_info=True)

    def _broadcast_pool_status(self, tip: int) -> None:
        """Advertise tip + aggregate pool H/s to P2P peers (explorer STATUS table).

        Pool JSON miners are not P2P peers; without this the seed shows a stale
        VERSION height and ``0 miners`` forever.
        """
        now = time.time()
        if (now - float(self._status_last_broadcast or 0.0)) < 10.0:
            return
        p2p = getattr(self.node, "p2p", None)
        if p2p is None:
            return
        try:
            from mhcoin.network.messages import StatusPayload, encode_status

            ov = self.db.stats_overview()
            workers = int(ov.get("workers_active") or 0)
            hps = int(max(0.0, float(ov.get("pool_hashrate") or 0.0)))
            mining = workers > 0 and hps > 0
            n = p2p.broadcast(
                "STATUS",
                encode_status(
                    StatusPayload(
                        mining=mining,
                        hps=hps if mining else 0,
                        height=int(tip),
                    )
                ),
            )
            self._status_last_broadcast = now
            if n:
                logger.debug(
                    "pool STATUS mining=%s hps=%s height=%s peers=%s",
                    mining,
                    hps if mining else 0,
                    tip,
                    n,
                )
        except Exception:
            logger.debug("pool STATUS broadcast failed", exc_info=True)

    def template_loop(self) -> None:
        while not self._stop:
            try:
                self.maintenance_tick()
            except Exception:
                logger.exception("maintenance")
            time.sleep(max(2.0, float(self.cfg.job_refresh_sec)))
