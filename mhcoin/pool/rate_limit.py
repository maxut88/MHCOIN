"""Configurable in-memory rate limits for Stratum (isolated / pool-local).

Does not affect PoW consensus validation — only connection/request admission.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    max_connections: int = 64
    max_connections_per_ip: int = 8
    max_requests_per_ip_per_sec: float = 40.0
    max_submits_per_worker_per_sec: float = 30.0
    max_message_bytes: int = 65_536
    read_timeout_sec: float = 120.0
    idle_timeout_sec: float = 300.0
    reconnect_window_sec: float = 10.0
    max_reconnects_per_ip_per_window: int = 30


class RateLimiter:
    def __init__(self, cfg: RateLimitConfig | None = None):
        self.cfg = cfg or RateLimitConfig()
        self._lock = threading.Lock()
        self._conn_total = 0
        self._conn_per_ip: dict[str, int] = defaultdict(int)
        self._req_times: dict[str, deque[float]] = defaultdict(deque)
        self._submit_times: dict[str, deque[float]] = defaultdict(deque)
        self._reconnect_times: dict[str, deque[float]] = defaultdict(deque)
        self.rejects: dict[str, int] = defaultdict(int)

    def _prune(self, q: deque[float], window: float, now: float) -> None:
        while q and now - q[0] > window:
            q.popleft()

    def note_reconnect(self, ip: str) -> str | None:
        now = time.time()
        with self._lock:
            q = self._reconnect_times[ip]
            self._prune(q, self.cfg.reconnect_window_sec, now)
            q.append(now)
            if len(q) > self.cfg.max_reconnects_per_ip_per_window:
                self.rejects["reconnect_storm"] += 1
                return "reconnect storm — try later"
        return None

    def allow_connection(self, ip: str) -> str | None:
        storm = self.note_reconnect(ip)
        if storm:
            return storm
        with self._lock:
            if self._conn_total >= self.cfg.max_connections:
                self.rejects["max_connections"] += 1
                return "server full"
            if self._conn_per_ip[ip] >= self.cfg.max_connections_per_ip:
                self.rejects["per_ip_connections"] += 1
                return "too many connections from IP"
            self._conn_total += 1
            self._conn_per_ip[ip] += 1
        return None

    def release_connection(self, ip: str) -> None:
        with self._lock:
            self._conn_total = max(0, self._conn_total - 1)
            if self._conn_per_ip[ip] > 0:
                self._conn_per_ip[ip] -= 1
            if self._conn_per_ip[ip] == 0:
                self._conn_per_ip.pop(ip, None)

    def allow_request(self, ip: str) -> str | None:
        now = time.time()
        with self._lock:
            q = self._req_times[ip]
            self._prune(q, 1.0, now)
            if len(q) >= self.cfg.max_requests_per_ip_per_sec:
                self.rejects["req_rate"] += 1
                return "request rate limit"
            q.append(now)
        return None

    def allow_submit(self, worker_key: str) -> str | None:
        now = time.time()
        with self._lock:
            q = self._submit_times[worker_key]
            self._prune(q, 1.0, now)
            if len(q) >= self.cfg.max_submits_per_worker_per_sec:
                self.rejects["submit_rate"] += 1
                return "share submit rate limit"
            q.append(now)
        return None

    def check_message_size(self, n: int) -> str | None:
        if n > self.cfg.max_message_bytes:
            self.rejects["oversized"] += 1
            return "message too large"
        return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connections": self._conn_total,
                "per_ip": dict(self._conn_per_ip),
                "rejects": dict(self.rejects),
                "cfg": {
                    "max_connections": self.cfg.max_connections,
                    "max_connections_per_ip": self.cfg.max_connections_per_ip,
                    "max_requests_per_ip_per_sec": self.cfg.max_requests_per_ip_per_sec,
                    "max_submits_per_worker_per_sec": self.cfg.max_submits_per_worker_per_sec,
                    "max_message_bytes": self.cfg.max_message_bytes,
                },
            }
