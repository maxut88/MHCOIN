"""Pool metrics snapshot + alert rule definitions (no secrets).

Production alerting must be wired explicitly — this module only computes signals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Alert:
    code: str
    severity: str  # info|warn|crit
    message: str


def collect_pool_metrics(
    *,
    db: Any,
    engine: Any | None = None,
    rate_limiter: Any | None = None,
    process_start_ts: float | None = None,
) -> dict:
    """Build a JSON-serializable metrics dict (addresses truncated)."""
    now = time.time()
    ov = db.stats_overview() if hasattr(db, "stats_overview") else {}
    tip = None
    template_h = None
    node_ok = False
    if engine is not None:
        try:
            tip = int(engine.node.chain.height)
            node_ok = tip >= 0
            template_h = int(getattr(engine, "_template_height", -1))
        except Exception:
            node_ok = False
    immature = matured = paid = 0
    try:
        row = db._conn.execute(
            "SELECT COALESCE(SUM(immature_sats),0), COALESCE(SUM(matured_sats),0), "
            "COALESCE(SUM(paid_sats),0) FROM balances"
        ).fetchone()
        immature, matured, paid = int(row[0]), int(row[1]), int(row[2])
    except Exception:
        pass
    orphans = 0
    try:
        orphans = int(
            db._conn.execute(
                "SELECT COUNT(*) FROM rounds WHERE status='orphaned'"
            ).fetchone()[0]
        )
    except Exception:
        pass
    pending_n = 0
    try:
        pending_n = len(db.list_pending_accepts())
    except Exception:
        pass
    out = {
        "ts": now,
        "uptime_sec": (now - process_start_ts) if process_start_ts else None,
        "mainnet_tip": tip,
        "pool_template_height": template_h,
        "node_connected": node_ok,
        "workers_active": ov.get("workers_active"),
        "pool_hashrate": ov.get("pool_hashrate"),
        "shares_1h": ov.get("shares_1h"),
        "blocks_total": ov.get("blocks_total"),
        "orphaned_rounds": orphans,
        "pending_accepts": pending_n,
        "immature_sats": immature,
        "matured_pending_sats": matured,
        "paid_sats": paid,
        "rate_limit": rate_limiter.snapshot() if rate_limiter else None,
    }
    return out


def evaluate_alerts(metrics: dict, *, prev: dict | None = None) -> list[Alert]:
    alerts: list[Alert] = []
    if not metrics.get("node_connected"):
        alerts.append(Alert("node_unavailable", "crit", "pool node tip unavailable"))
    tip = metrics.get("mainnet_tip")
    th = metrics.get("pool_template_height")
    if tip is not None and th is not None and th >= 0 and tip + 1 < th - 1:
        alerts.append(
            Alert("template_ahead", "warn", f"template height {th} vs tip {tip}")
        )
    if tip is not None and th is not None and th >= 0 and th < tip:
        alerts.append(Alert("pool_tip_lag", "warn", f"template {th} behind tip {tip}"))
    if int(metrics.get("pending_accepts") or 0) > 0:
        alerts.append(
            Alert(
                "pending_accept",
                "crit",
                "node-accepted block missing committed accounting",
            )
        )
    if int(metrics.get("orphaned_rounds") or 0) > 0 and prev:
        if int(metrics["orphaned_rounds"]) > int(prev.get("orphaned_rounds") or 0):
            alerts.append(Alert("new_orphan", "warn", "new orphaned round detected"))
    rl = metrics.get("rate_limit") or {}
    rejects = rl.get("rejects") or {}
    if int(rejects.get("reconnect_storm") or 0) > 0:
        alerts.append(Alert("reconnect_storm", "warn", "reconnect storm rejects seen"))
    if int(rejects.get("max_connections") or 0) > 0:
        alerts.append(Alert("conn_limit", "warn", "max connections hit"))
    return alerts


# Documented alert catalog for operators (no secrets).
ALERT_CATALOG = [
    ("node_unavailable", "Pool cannot read chain tip"),
    ("pool_tip_lag", "Template height behind node tip"),
    ("pending_accept", "Block accepted by node but credits not committed"),
    ("new_orphan", "Immature round voided after reorg"),
    ("duplicate_credit_attempt", "close_round idempotent hit / unique hash"),
    ("database_locked", "SQLite busy (from logs)"),
    ("high_reject_rate", "Share reject ratio elevated (ops rule)"),
    ("payout_failure", "Auto-payout send failed"),
    ("missing_utxo_repeat", "Repeated accept_block missing UTXO"),
    ("unexpected_restart", "Process uptime reset"),
]
