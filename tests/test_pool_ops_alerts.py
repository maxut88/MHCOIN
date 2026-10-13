"""Pool ops: stale immature alerts + block enrichment notes."""

from __future__ import annotations

from pathlib import Path

from mhcoin.pool.db import PoolDB
from mhcoin.pool.monitoring import (
    collect_pool_metrics,
    count_stale_immature,
    evaluate_alerts,
)


ADDR = "mhc1qnkfc9zcfpun8pkud45gz9qutv6xtrmrun9p07r"


def _round(db: PoolDB, *, height: int, h: str) -> int:
    db.add_share(
        address=ADDR, worker="t", job_id="j", height=height, difficulty=1.0, is_block=True, block_hash=h
    )
    return db.close_round_with_block(
        height=height, block_hash=h, reward_sats=5_000_000_000, fee_percent=1.0
    )


def test_count_stale_immature(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    _round(db, height=100, h="aa" * 32)
    assert count_stale_immature(db, tip=119, mature_confirms=20) == 0
    assert count_stale_immature(db, tip=120, mature_confirms=20) == 1
    db.close()


def test_stale_immature_alert(tmp_path: Path):
    db = PoolDB(tmp_path / "p.sqlite")
    _round(db, height=100, h="bb" * 32)

    class _Chain:
        height = 130

    class _Eng:
        node = type("N", (), {"chain": _Chain()})()
        _template_height = 130

    m = collect_pool_metrics(db=db, engine=_Eng(), mature_confirms=20)
    assert m["stale_immature_rounds"] == 1
    alerts = evaluate_alerts(m)
    assert any(a.code == "stale_immature" for a in alerts)
    db.close()


def test_enrich_blocks_orphaned_note(tmp_path: Path):
    from mhcoin.pool.config import PoolConfig
    from mhcoin.pool.engine import PoolEngine

    db = PoolDB(tmp_path / "p.sqlite")
    h_pool = "cc" * 32
    h_canon = "dd" * 32
    rid = _round(db, height=50, h=h_pool)
    db._conn.execute("UPDATE rounds SET status='orphaned' WHERE id=?", (rid,))
    db._conn.commit()

    class _Chain:
        height = 80

        def get_block_by_height(self, h):
            class B:
                def block_hash(self_inner):
                    return bytes.fromhex(h_canon)

            return B() if int(h) == 50 else None

    class _Node:
        chain = _Chain()

    eng = PoolEngine(PoolConfig(pool_address=ADDR, network="mainnet"), db, _Node())
    rows = eng.enrich_blocks(
        [{"height": 50, "block_hash": h_pool, "reward_sats": 5_000_000_000, "status": "orphaned"}]
    )
    assert rows[0]["on_best_chain"] is False
    assert "reorg" in rows[0]["status_note"]
    assert rows[0]["canonical_hash"] == h_canon
    db.close()
