"""Bitcoin-style Stratum adapter (phase 2) over the MHC pool engine.

Compatible subset: mining.subscribe / authorize / notify / submit.
Jobs use the same extranonce + merkle fields as the JSON protocol.
"""

from __future__ import annotations

import json
import logging
import socketserver
import threading
from typing import Any

from mhcoin.pool.engine import PoolEngine

logger = logging.getLogger("mhcoin.pool.stratum")


class _StratumHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        eng: PoolEngine = self.server.engine  # type: ignore[attr-defined]
        session: dict[str, Any] = {
            "address": "",
            "worker": "default",
            "extranonce1": "",
            "authorized": False,
            "sub_id": None,
        }
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                method = str(msg.get("method") or "")
                params = msg.get("params") or []
                if method == "mining.subscribe":
                    # Assign a temporary en1 until authorize (re-bound then).
                    en1 = eng.db.next_extranonce1()
                    session["extranonce1"] = en1.hex()
                    session["sub_id"] = f"mhc-{en1.hex()}"
                    self._reply(
                        mid,
                        [
                            [["mining.notify", session["sub_id"]], ["mining.set_difficulty", session["sub_id"]]],
                            session["extranonce1"],
                            4,
                        ],
                    )
                    continue
                if method == "mining.authorize":
                    user = str(params[0]) if params else ""
                    # user = address or address.worker
                    if "." in user:
                        addr, worker = user.split(".", 1)
                    else:
                        addr, worker = user, "default"
                    try:
                        en1 = eng.register_worker(addr, worker)
                    except Exception as e:
                        self._reply(mid, False, error=[20, str(e), None])
                        continue
                    session["address"] = addr
                    session["worker"] = worker
                    session["extranonce1"] = en1.hex()
                    session["authorized"] = True
                    self._reply(mid, True)
                    # Push difficulty + job
                    self._notify_difficulty(eng.cfg.share_factor)
                    try:
                        job = eng.get_job(addr, worker)
                        self._notify_job(job, clean=True)
                    except Exception as e:
                        logger.warning("stratum job push: %s", e)
                    continue
                if method == "mining.submit":
                    if not session["authorized"]:
                        self._reply(mid, False, error=[24, "unauthorized", None])
                        continue
                    # params: [worker, job_id, extranonce2, ntime, nonce]
                    try:
                        job_id = str(params[1])
                        en2 = str(params[2])
                        ntime = str(params[3])
                        nonce = str(params[4])
                    except (IndexError, TypeError):
                        self._reply(mid, False, error=[20, "bad params", None])
                        continue
                    res = eng.submit(
                        address=session["address"],
                        worker=session["worker"],
                        job_id=job_id,
                        extranonce2_hex=en2,
                        nonce_hex=nonce,
                        ntime_hex=ntime,
                    )
                    if res.get("ok"):
                        self._reply(mid, True)
                    else:
                        self._reply(mid, False, error=[21, res.get("error") or "reject", None])
                    continue
                if method in {"mining.extranonce.subscribe", "mining.suggest_difficulty"}:
                    self._reply(mid, True)
                    continue
                # unknown
                if mid is not None:
                    self._reply(mid, None, error=[20, f"unknown method {method}", None])
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _reply(self, mid, result, error=None) -> None:
        obj = {"id": mid, "result": result, "error": error}
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _notify_difficulty(self, share_factor: int) -> None:
        # Map share_factor to a stratum difficulty number (informational).
        diff = max(1.0, float(share_factor))
        msg = {"id": None, "method": "mining.set_difficulty", "params": [diff]}
        self.wfile.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _notify_job(self, job: dict, *, clean: bool) -> None:
        # Bitcoin stratum notify params:
        # job_id, prevhash, coinb1, coinb2, merkle_branches, version, nbits, ntime, clean_jobs
        params = [
            job["job_id"],
            job["prevhash"],
            job["coinb1"],
            job["coinb2"],
            job.get("merkle_branches") or [],
            f"{int(job['version']):08x}",
            job["nbits"],
            job["ntime"],
            bool(clean),
        ]
        msg = {"id": None, "method": "mining.notify", "params": params}
        self.wfile.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.wfile.flush()


class _StratumServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_stratum_server(engine: PoolEngine, host: str, port: int) -> _StratumServer:
    srv = _StratumServer((host, port), _StratumHandler)
    srv.engine = engine  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, name="mhcoin-pool-stratum", daemon=True)
    t.start()
    logger.info("Stratum listen %s:%s", host, port)
    return srv
