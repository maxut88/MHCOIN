"""Run the MHCOIN mining pool (node + JSON + stratum + web + payouts)."""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path

from mhcoin.consensus.params import get_network_params
from mhcoin.network.seeds import default_connect_peers
from mhcoin.node.runtime import NodeRuntime
from mhcoin.pool.config import PoolConfig
from mhcoin.pool.db import PoolDB
from mhcoin.pool.engine import PoolEngine
from mhcoin.pool.payouts import PayoutWorker
from mhcoin.pool.protocol import start_json_server
from mhcoin.pool.stratum import start_stratum_server
from mhcoin.pool.web import start_web
from mhcoin.wallet.addresses import validate_address

logger = logging.getLogger("mhcoin.pool")


def run_pool(cfg: PoolConfig) -> None:
    if not cfg.pool_address or not validate_address(cfg.pool_address, hrp=cfg.hrp):
        raise SystemExit(
            f"valid --address / MHCOIN_POOL_ADDRESS required (hrp={cfg.hrp})"
        )
    cfg.pool_dir.mkdir(parents=True, exist_ok=True)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    params = get_network_params(cfg.network)
    cfg.hrp = params.address_hrp
    peers = default_connect_peers(cfg.network)
    # Optional override: MHCOIN_CONNECT=host:port,host2:port
    import os

    extra = (os.environ.get("MHCOIN_CONNECT") or "").strip()
    if extra:
        override = [p.strip() for p in extra.split(",") if p.strip()]
        peers = override + [p for p in peers if p not in override]

    logger.info(
        "Starting pool network=%s address=%s data=%s",
        cfg.network,
        cfg.pool_address,
        cfg.data_dir,
    )
    rt = NodeRuntime(
        data_dir=Path(cfg.data_dir),
        network=cfg.network,
        host="127.0.0.1",
        port=18444,  # unused when listen disabled; avoid colliding with seed :8333
        connect=peers,
        enable_listen=False,
    )

    def _node() -> None:
        try:
            rt.start(blocking=True)
        except Exception:
            logger.exception("pool node stopped")

    threading.Thread(target=_node, name="mhcoin-pool-node", daemon=True).start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if getattr(rt, "_running", False):
            break
        time.sleep(0.05)
    if not getattr(rt, "_running", False):
        raise SystemExit("failed to start pool node")

    # Wait for tip a bit on live nets.
    print("  pool node starting — syncing tip…", flush=True)
    sync_deadline = time.monotonic() + 90
    while time.monotonic() < sync_deadline:
        if int(rt.chain.height) >= 0:
            break
        time.sleep(0.5)

    db = PoolDB(cfg.db_path)
    engine = PoolEngine(cfg, db, rt)
    engine.refresh_template()
    threading.Thread(target=engine.template_loop, name="mhcoin-pool-jobs", daemon=True).start()

    json_srv = start_json_server(engine, cfg.listen_host, cfg.listen_port)
    stratum_srv = start_stratum_server(engine, cfg.listen_host, cfg.stratum_port)
    web_srv = start_web(
        db,
        cfg.web_host,
        cfg.web_port,
        pool_address=cfg.pool_address,
        json_port=cfg.listen_port,
        stratum_port=cfg.stratum_port,
        fee_percent=cfg.fee_percent,
        share_factor=cfg.share_factor,
        explorer_url=os.environ.get("MHCOIN_EXPLORER_URL", "http://192.168.0.221:8766"),
    )
    payouts = PayoutWorker(cfg, db, rt)
    payouts.start()

    print(
        f"  JSON miners  {cfg.listen_host}:{cfg.listen_port}\n"
        f"  Stratum      {cfg.listen_host}:{cfg.stratum_port}\n"
        f"  Stats        http://{cfg.web_host}:{cfg.web_port}/\n"
        f"  Coinbase     {cfg.pool_address}\n"
        f"  Tip height   {rt.chain.height}",
        flush=True,
    )

    stop = threading.Event()

    def _stop(*_a) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        engine.stop()
        payouts.stop()
        try:
            json_srv.shutdown()
        except Exception:
            pass
        try:
            stratum_srv.shutdown()
        except Exception:
            pass
        try:
            web_srv.shutdown()
        except Exception:
            pass
        try:
            rt.stop()
        except Exception:
            pass
        db.close()
        logger.info("pool stopped")
