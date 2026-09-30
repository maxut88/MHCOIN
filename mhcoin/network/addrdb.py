"""Persistent address book for peer discovery (Bitcoin peers.dat analogue)."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mhcoin.network.constants import MAX_ADDR_DB, MAX_ADDR_HOST_LEN, SERVICES_NODE_NETWORK
from mhcoin.network.messages import NetAddress


@dataclass
class AddrRecord:
    host: str
    port: int
    services: int
    last_seen: int
    last_try: int
    attempts: int
    source: str  # "manual" | "gossip" | "inbound"

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    def to_net_address(self) -> NetAddress:
        return NetAddress(
            timestamp=self.last_seen,
            services=self.services,
            host=self.host,
            port=self.port,
        )


class AddrDB:
    """
    Bounded SQLite address store. Survives restart.
    Does not include DNS seeds or central directories.
    """

    def __init__(self, path: Path, *, max_entries: int = MAX_ADDR_DB):
        self.path = path
        self.max_entries = max_entries
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS addrs (
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                services INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                last_try INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                PRIMARY KEY (host, port)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_addrs_seen ON addrs(last_seen DESC)"
        )
        self._db.commit()

    def count(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            try:
                row = self._db.execute("SELECT COUNT(*) FROM addrs").fetchone()
                return int(row[0]) if row else 0
            except sqlite3.ProgrammingError:
                return 0

    def get(self, host: str, port: int) -> AddrRecord | None:
        with self._lock:
            if self._closed:
                return None
            try:
                row = self._db.execute(
                    "SELECT host, port, services, last_seen, last_try, attempts, source "
                    "FROM addrs WHERE host=? AND port=?",
                    (host, port),
                ).fetchone()
            except sqlite3.ProgrammingError:
                return None
            if not row:
                return None
            return AddrRecord(*row)

    def add(
        self,
        host: str,
        port: int,
        *,
        services: int = SERVICES_NODE_NETWORK,
        last_seen: int | None = None,
        source: str = "gossip",
    ) -> bool:
        """Insert or refresh an address. Returns False if rejected."""
        host = host.strip()
        if not host or len(host.encode("utf-8")) > MAX_ADDR_HOST_LEN:
            return False
        if not 1 <= port <= 65535:
            return False
        if "\x00" in host or "/" in host or "\\" in host:
            return False
        seen = int(time.time()) if last_seen is None else int(last_seen)
        with self._lock:
            if self._closed:
                return False
            try:
                existing = self.get(host, port)
                if existing is not None:
                    new_seen = max(existing.last_seen, seen)
                    new_source = existing.source if existing.source == "manual" else source
                    self._db.execute(
                        "UPDATE addrs SET services=?, last_seen=?, source=? WHERE host=? AND port=?",
                        (services, new_seen, new_source, host, port),
                    )
                    self._db.commit()
                    return True
                if self.count() >= self.max_entries:
                    self._evict_oldest(1)
                self._db.execute(
                    "INSERT INTO addrs(host, port, services, last_seen, last_try, attempts, source) "
                    "VALUES (?,?,?,?,0,0,?)",
                    (host, port, services, seen, source),
                )
                self._db.commit()
                return True
            except sqlite3.ProgrammingError:
                return False

    def add_net_address(self, addr: NetAddress, *, source: str = "gossip") -> bool:
        return self.add(
            addr.host,
            addr.port,
            services=addr.services,
            last_seen=addr.timestamp,
            source=source,
        )

    def mark_attempt(self, host: str, port: int) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._db.execute(
                    "UPDATE addrs SET last_try=?, attempts=attempts+1 WHERE host=? AND port=?",
                    (int(time.time()), host, port),
                )
                self._db.commit()
            except sqlite3.ProgrammingError:
                return

    def mark_success(self, host: str, port: int) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._db.execute(
                    "UPDATE addrs SET last_seen=?, attempts=0 WHERE host=? AND port=?",
                    (int(time.time()), host, port),
                )
                self._db.commit()
            except sqlite3.ProgrammingError:
                return

    def remove(self, host: str, port: int) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._db.execute("DELETE FROM addrs WHERE host=? AND port=?", (host, port))
                self._db.commit()
            except sqlite3.ProgrammingError:
                return

    def list_recent(self, limit: int = 32) -> list[AddrRecord]:
        with self._lock:
            if self._closed:
                return []
            try:
                rows = self._db.execute(
                    "SELECT host, port, services, last_seen, last_try, attempts, source "
                    "FROM addrs ORDER BY last_seen DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [AddrRecord(*r) for r in rows]
            except sqlite3.ProgrammingError:
                return []

    def candidates_for_outbound(
        self,
        *,
        exclude: set[str],
        limit: int = 16,
        min_retry_age: float = 30.0,
    ) -> list[AddrRecord]:
        """Prefer freshest addresses not currently connected / recently tried."""
        now = time.time()
        out: list[AddrRecord] = []
        with self._lock:
            if self._closed:
                return []
            try:
                rows = self._db.execute(
                    "SELECT host, port, services, last_seen, last_try, attempts, source "
                    "FROM addrs ORDER BY attempts ASC, last_seen DESC"
                ).fetchall()
            except sqlite3.ProgrammingError:
                return []
        for r in rows:
            rec = AddrRecord(*r)
            if rec.key in exclude:
                continue
            if rec.last_try and (now - rec.last_try) < min_retry_age:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def _evict_oldest(self, n: int) -> None:
        rows = self._db.execute(
            "SELECT host, port FROM addrs WHERE source!='manual' "
            "ORDER BY last_seen ASC LIMIT ?",
            (n,),
        ).fetchall()
        for host, port in rows:
            self._db.execute("DELETE FROM addrs WHERE host=? AND port=?", (host, port))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._db.close()
            except sqlite3.Error:
                pass
