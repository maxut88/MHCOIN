"""Stratum adapter with configurable admission / rate limits."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time
from typing import Any

from mhcoin.pool.engine import PoolEngine
from mhcoin.pool.rate_limit import RateLimitConfig, RateLimiter

logger = logging.getLogger("mhcoin.pool.stratum")


class _StratumHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        cfg = getattr(self.server, "rate_cfg", None)
        timeout = float(getattr(cfg, "read_timeout_sec", 120.0) if cfg else 120.0)
        self.connection.settimeout(timeout)
        self._ip = self.client_address[0]
        self._limiter: RateLimiter = self.server.limiter  # type: ignore[attr-defined]
        self._authed_key = ""
        self._last_activity = time.time()
        deny = self._limiter.allow_connection(self._ip)
        self._accepted = deny is None
        if deny:
            try:
                self.wfile.write(
                    (
                        json.dumps(
                            {
                                "id": None,
                                "result": None,
                                "error": [20, deny, None],
                            }
                        )
                        + "\n"
                    ).encode()
                )
                self.wfile.flush()
            except Exception:
                pass
            self.connection.close()

    def finish(self) -> None:
        try:
            if getattr(self, "_accepted", False):
                self._limiter.release_connection(self._ip)
        finally:
            super().finish()

    def handle(self) -> None:
        if not getattr(self, "_accepted", False):
            return
        eng: PoolEngine = self.server.engine  # type: ignore[attr-defined]
        rate_cfg: RateLimitConfig = self.server.rate_cfg  # type: ignore[attr-defined]
        session: dict[str, Any] = {
            "address": "",
            "worker": "default",
            "extranonce1": "",
            "authorized": False,
            "sub_id": None,
        }
        buf = b""
        max_msg = int(rate_cfg.max_message_bytes)
        idle = float(rate_cfg.idle_timeout_sec)
        try:
            while True:
                if time.time() - self._last_activity > idle:
                    break
                try:
                    chunk = self.connection.recv(4096)
                except socket.timeout:
                    if time.time() - self._last_activity > idle:
                        break
                    continue
                if not chunk:
                    break
                # Slowloris / oversized buffer guard
                if len(buf) + len(chunk) > max_msg * 2:
                    self._limiter.rejects["slowloris_or_oversized"] += 1
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    err = self._limiter.check_message_size(len(line))
                    if err:
                        self._reply(None, None, error=[20, err, None])
                        continue
                    err = self._limiter.allow_request(self._ip)
                    if err:
                        self._reply(None, None, error=[20, err, None])
                        continue
                    self._last_activity = time.time()
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    self._dispatch(eng, session, msg)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _dispatch(self, eng: PoolEngine, session: dict, msg: dict) -> None:
        mid = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") or []
        if method == "mining.subscribe":
            en1 = eng.db.next_extranonce1()
            session["extranonce1"] = en1.hex()
            session["sub_id"] = f"mhc-{en1.hex()}"
            self._reply(
                mid,
                [
                    [
                        ["mining.notify", session["sub_id"]],
                        ["mining.set_difficulty", session["sub_id"]],
                    ],
                    session["extranonce1"],
                    4,
                ],
            )
            return
        if method == "mining.authorize":
            user = str(params[0]) if params else ""
            if "." in user:
                addr, worker = user.split(".", 1)
            else:
                addr, worker = user, "default"
            worker = (worker or "default")[:64]
            try:
                en1 = eng.register_worker(addr, worker)
            except Exception as e:
                self._reply(mid, False, error=[20, str(e), None])
                return
            session["address"] = addr
            session["worker"] = worker
            session["extranonce1"] = en1.hex()
            session["authorized"] = True
            self._authed_key = f"{addr}:{worker}"
            self._reply(mid, True)
            self._notify_difficulty(eng.cfg.share_factor)
            try:
                job = eng.get_job(addr, worker)
                self._notify_job(job, clean=True)
            except Exception as e:
                logger.warning("stratum job push: %s", e)
            return
        if method == "mining.submit":
            if not session["authorized"]:
                self._reply(mid, False, error=[24, "unauthorized", None])
                return
            err = self._limiter.allow_submit(self._authed_key or self._ip)
            if err:
                self._reply(mid, False, error=[20, err, None])
                return
            try:
                job_id = str(params[1])
                en2 = str(params[2])
                ntime = str(params[3])
                nonce = str(params[4])
            except (IndexError, TypeError):
                self._reply(mid, False, error=[20, "bad params", None])
                return
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
                self._reply(
                    mid, False, error=[21, res.get("error") or "reject", None]
                )
            return
        if method in {"mining.extranonce.subscribe", "mining.suggest_difficulty"}:
            self._reply(mid, True)
            return
        if mid is not None:
            self._reply(mid, None, error=[20, f"unknown method {method}", None])

    def _reply(self, mid, result, error=None) -> None:
        obj = {"id": mid, "result": result, "error": error}
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _notify_difficulty(self, share_factor: int) -> None:
        diff = max(1.0, float(share_factor))
        msg = {"id": None, "method": "mining.set_difficulty", "params": [diff]}
        self.wfile.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _notify_job(self, job: dict, *, clean: bool) -> None:
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
    request_queue_size = 32


def start_stratum_server(
    engine: PoolEngine,
    host: str,
    port: int,
    *,
    limiter: RateLimiter | None = None,
) -> _StratumServer:
    cfg = engine.cfg
    rate_cfg = RateLimitConfig(
        max_connections=int(cfg.stratum_max_connections),
        max_connections_per_ip=int(cfg.stratum_max_connections_per_ip),
        max_requests_per_ip_per_sec=float(cfg.stratum_max_requests_per_ip_per_sec),
        max_submits_per_worker_per_sec=float(
            cfg.stratum_max_submits_per_worker_per_sec
        ),
        max_message_bytes=int(cfg.stratum_max_message_bytes),
        read_timeout_sec=float(cfg.stratum_read_timeout_sec),
        idle_timeout_sec=float(cfg.stratum_idle_timeout_sec),
        max_reconnects_per_ip_per_window=int(
            cfg.stratum_max_reconnects_per_ip_per_window
        ),
        reconnect_window_sec=float(cfg.stratum_reconnect_window_sec),
    )
    srv = _StratumServer((host, port), _StratumHandler)
    srv.engine = engine  # type: ignore[attr-defined]
    srv.limiter = limiter or RateLimiter(rate_cfg)  # type: ignore[attr-defined]
    srv.rate_cfg = rate_cfg  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, name="mhcoin-pool-stratum", daemon=True)
    t.start()
    logger.info("Stratum listen %s:%s (max_conn=%s)", host, port, rate_cfg.max_connections)
    return srv
