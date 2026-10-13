"""JSON-line TCP protocol for MHCOIN pool miners."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
from typing import Any

from mhcoin.pool.engine import PoolEngine

logger = logging.getLogger("mhcoin.pool.proto")


class _Handler(socketserver.StreamRequestHandler):
    engine: PoolEngine  # set on server

    def handle(self) -> None:
        address = ""
        worker = "default"
        peer = self.client_address[0]
        try:
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    self._reply({"ok": False, "error": "invalid json"})
                    continue
                method = str(msg.get("method") or "")
                req_id = msg.get("id")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                try:
                    result = self._dispatch(method, params, address, worker)
                    if method == "login" and result.get("ok"):
                        address = str(params.get("address") or address)
                        worker = str(params.get("worker") or worker or "default")
                    self._reply({"id": req_id, **result})
                except Exception as e:
                    logger.exception("peer=%s method=%s", peer, method)
                    self._reply({"id": req_id, "ok": False, "error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(
        self, method: str, params: dict[str, Any], address: str, worker: str
    ) -> dict:
        eng: PoolEngine = self.server.engine  # type: ignore[attr-defined]
        if method == "login":
            addr = str(params.get("address") or "")
            w = str(params.get("worker") or "default")
            en1 = eng.register_worker(addr, w)
            return {
                "ok": True,
                "extranonce1": en1.hex(),
                "extranonce2_size": 4,
                "share_factor": eng.cfg.share_factor,
            }
        if method == "getjob":
            addr = str(params.get("address") or address)
            w = str(params.get("worker") or worker)
            job = eng.get_job(addr, w)
            return {"ok": True, "job": job}
        if method == "submit":
            addr = str(params.get("address") or address)
            w = str(params.get("worker") or worker)
            return eng.submit(
                address=addr,
                worker=w,
                job_id=str(params.get("job_id") or ""),
                extranonce2_hex=str(params.get("extranonce2") or ""),
                nonce_hex=str(params.get("nonce") or ""),
                ntime_hex=str(params.get("ntime") or "") or None,
            )
        if method == "hashrate":
            addr = str(params.get("address") or address)
            w = str(params.get("worker") or worker)
            eng.set_hashrate(addr, w, float(params.get("hashrate") or 0))
            return {"ok": True}
        if method == "ping":
            return {"ok": True, "pong": True}
        return {"ok": False, "error": f"unknown method {method}"}

    def _reply(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        self.wfile.write(data)
        self.wfile.flush()


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_json_server(engine: PoolEngine, host: str, port: int) -> ThreadedTCPServer:
    _Handler.engine = engine  # type: ignore[attr-defined]

    class Srv(ThreadedTCPServer):
        pass

    srv = Srv((host, port), _Handler)
    srv.engine = engine  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, name="mhcoin-pool-json", daemon=True)
    t.start()
    logger.info("JSON pool listen %s:%s", host, port)
    return srv
