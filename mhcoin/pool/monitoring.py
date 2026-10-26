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


def count_stale_immature(
    db: Any, *, tip: int | None, mature_confirms: int
) -> int:
    """Immature rounds that already have ≥ mature_confirms on the tip."""
    if tip is None or tip < 0:
        return 0
    confirms = max(1, int(mature_confirms))
    try:
        row = db._conn.execute(
            """
            SELECT COUNT(*) FROM rounds
            WHERE status='immature'
              AND height IS NOT NULL
              AND ? >= height + ?
            """,
            (int(tip), confirms),
        ).fetchone()
        return int(row[0] if not hasattr(row, "keys") else row[0])
    except Exception:
        return 0


def collect_pool_metrics(
    *,
    db: Any,
    engine: Any | None = None,
    rate_limiter: Any | None = None,
    process_start_ts: float | None = None,
    mature_confirms: int = 20,
) -> dict:
    """Build a JSON-serializable metrics dict (addresses truncated)."""
    now = time.time()
    ov = db.stats_overview() if hasattr(db, "stats_overview") else {}
    tip = None
    template_h = None
    node_ok = False
    peer_count = None
    known_addrs = None
    outbound_target = None
    peers: list[dict] = []
    if engine is not None:
        try:
            tip = int(engine.node.chain.height)
            node_ok = tip >= 0
            template_h = int(getattr(engine, "_template_height", -1))
        except Exception:
            node_ok = False
        try:
            p2p = getattr(engine.node, "p2p", None)
            if p2p is not None:
                peer_count = int(p2p.peer_count())
                cfg = getattr(p2p, "config", None)
                if cfg is not None:
                    outbound_target = int(getattr(cfg, "outbound_target", 0) or 0)
                addrdb = getattr(p2p, "addrdb", None)
                if addrdb is not None and hasattr(addrdb, "count"):
                    known_addrs = int(addrdb.count())
                # Best-effort peer summary (no secrets).
                hp = getattr(p2p, "handshaked_peers", None)
                peer_iter = hp() if callable(hp) else []
                for peer in list(peer_iter or [])[:16]:
                    try:
                        st = getattr(peer, "state", "") or ""
                        st_s = getattr(st, "name", None) or str(st)
                        if st_s.startswith("PeerState."):
                            st_s = st_s.split(".", 1)[-1]
                        peers.append(
                            {
                                "addr": getattr(peer, "addr", None)
                                or f"{getattr(peer, 'host', '?')}:{getattr(peer, 'port', '?')}",
                                "inbound": bool(getattr(peer, "inbound", False)),
                                "state": st_s,
                                "height": getattr(peer, "height", None),
                            }
                        )
                    except Exception:
                        continue
        except Exception:
            pass
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
    stale_n = count_stale_immature(
        db, tip=tip, mature_confirms=mature_confirms
    )
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
        "stale_immature_rounds": stale_n,
        "mature_confirms": int(mature_confirms),
        "pending_accepts": pending_n,
        "mature_event_codes": list(
            getattr(engine, "_mature_event_codes", []) or []
        )[-16:],
        "immature_sats": immature,
        "matured_pending_sats": matured,
        "paid_sats": paid,
        "peer_count": peer_count,
        "known_addrs": known_addrs,
        "outbound_target": outbound_target,
        "peers": peers[:16],
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
            alerts.append(
                Alert(
                    "new_orphan",
                    "warn",
                    "new voided pool round (ledger) — not necessarily a chain orphan",
                )
            )
    for code in metrics.get("mature_event_codes") or []:
        if code == "height_mismatch_repair":
            alerts.append(
                Alert(
                    "height_mismatch_repair",
                    "warn",
                    "immature round height repaired from block index (false-orphan avoided)",
                )
            )
        elif code in ("height_mismatch_ambiguous", "hash_unknown", "canonical_unavailable"):
            alerts.append(
                Alert(
                    "maturity_deferred",
                    "info",
                    f"maturity deferred ({code})",
                )
            )
    stale = int(metrics.get("stale_immature_rounds") or 0)
    if stale > 0:
        mat = int(metrics.get("mature_confirms") or 20)
        alerts.append(
            Alert(
                "stale_immature",
                "warn",
                f"{stale} immature round(s) past {mat} confirmations (maturity lag)",
            )
        )
    pc = metrics.get("peer_count")
    ot = metrics.get("outbound_target")
    if pc is not None and ot is not None and int(ot) > 0 and int(pc) < min(2, int(ot)):
        alerts.append(
            Alert(
                "low_peer_count",
                "info",
                f"only {pc} P2P peer(s) connected (target {ot})",
            )
        )
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
    ("new_orphan", "Pool ledger round voided (status=orphaned); not chain OrphanPool"),
    ("height_mismatch_repair", "Round height corrected from block index before void"),
    ("maturity_deferred", "Maturity deferred (unknown hash / ambiguous height)"),
    ("stale_immature", "Immature round past maturity depth still not matured"),
    ("low_peer_count", "Fewer P2P peers than outbound target"),
    ("duplicate_credit_attempt", "close_round idempotent hit / unique hash"),
    ("database_locked", "SQLite busy (from logs)"),
    ("high_reject_rate", "Share reject ratio elevated (ops rule)"),
    ("payout_failure", "Auto-payout send failed"),
    ("missing_utxo_repeat", "Repeated accept_block missing UTXO"),
    ("unexpected_restart", "Process uptime reset"),
]
