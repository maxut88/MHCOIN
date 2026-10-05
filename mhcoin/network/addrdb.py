"""Persistent address book for peer discovery (Bitcoin peers.dat analogue).

Stores peers in ``peers.dat`` (versioned JSON). On first open, migrates a legacy
``peers.sqlite`` in the same directory if present.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from mhcoin.network.constants import MAX_ADDR_DB, MAX_ADDR_HOST_LEN, SERVICES_NODE_NETWORK
from mhcoin.network.messages import NetAddress

PEERS_DAT_VERSION = 1


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
    Bounded file address store (``peers.dat``). Survives restart.
    Does not include DNS seeds or central directories.
    """

    def __init__(self, path: Path, *, max_entries: int = MAX_ADDR_DB):
        # Accept legacy ``peers.sqlite`` path from callers and map to ``peers.dat``.
        path = Path(path)
        if path.suffix == ".sqlite" or path.name == "peers.sqlite":
            path = path.with_name("peers.dat")
        elif path.suffix != ".dat":
            # If a directory was passed, place peers.dat inside.
            if path.is_dir() or not path.suffix:
                path = path / "peers.dat"
        self.path = path
        self.max_entries = max_entries
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._addrs: dict[tuple[str, int], AddrRecord] = {}
        self._migrate_sqlite_if_needed()
        self._load()

    def _sqlite_legacy_path(self) -> Path:
        return self.path.with_name("peers.sqlite")

    def _migrate_sqlite_if_needed(self) -> None:
        legacy = self._sqlite_legacy_path()
        if self.path.is_file() or not legacy.is_file():
            return
        try:
            con = sqlite3.connect(str(legacy))
            rows = con.execute(
                "SELECT host, port, services, last_seen, last_try, attempts, source FROM addrs"
            ).fetchall()
            con.close()
        except Exception:
            return
        for row in rows:
            rec = AddrRecord(*row)
            self._addrs[(rec.host, rec.port)] = rec
        self._save_unlocked()
        try:
            legacy.rename(legacy.with_suffix(".sqlite.bak"))
        except OSError:
            pass

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # Corrupt file → start empty (same resilience as before).
            self._addrs = {}
            return
        if not isinstance(raw, dict):
            self._addrs = {}
            return
        addrs = raw.get("addrs") or []
        out: dict[tuple[str, int], AddrRecord] = {}
        if isinstance(addrs, list):
            for item in addrs:
                if not isinstance(item, dict):
                    continue
                try:
                    rec = AddrRecord(
                        host=str(item["host"]),
                        port=int(item["port"]),
                        services=int(item.get("services", SERVICES_NODE_NETWORK)),
                        last_seen=int(item.get("last_seen", 0)),
                        last_try=int(item.get("last_try", 0)),
                        attempts=int(item.get("attempts", 0)),
                        source=str(item.get("source", "gossip")),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                out[(rec.host, rec.port)] = rec
        self._addrs = out

    def _save_unlocked(self) -> None:
        payload = {
            "version": PEERS_DAT_VERSION,
            "addrs": [asdict(r) for r in self._addrs.values()],
        }
        tmp = self.path.with_suffix(".dat.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _save(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._save_unlocked()

    def count(self) -> int:
        with self._lock:
            if self._closed:
                return 0
            return len(self._addrs)

    def get(self, host: str, port: int) -> AddrRecord | None:
        with self._lock:
            if self._closed:
                return None
            return self._addrs.get((host, port))

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
            key = (host, port)
            existing = self._addrs.get(key)
            if existing is not None:
                new_seen = max(existing.last_seen, seen)
                new_source = existing.source if existing.source == "manual" else source
                self._addrs[key] = AddrRecord(
                    host=host,
                    port=port,
                    services=services,
                    last_seen=new_seen,
                    last_try=existing.last_try,
                    attempts=existing.attempts,
                    source=new_source,
                )
                self._save_unlocked()
                return True
            if len(self._addrs) >= self.max_entries:
                self._evict_oldest(1)
            self._addrs[key] = AddrRecord(
                host=host,
                port=port,
                services=services,
                last_seen=seen,
                last_try=0,
                attempts=0,
                source=source,
            )
            self._save_unlocked()
            return True

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
            rec = self._addrs.get((host, port))
            if rec is None:
                return
            self._addrs[(host, port)] = AddrRecord(
                host=rec.host,
                port=rec.port,
                services=rec.services,
                last_seen=rec.last_seen,
                last_try=int(time.time()),
                attempts=rec.attempts + 1,
                source=rec.source,
            )
            self._save_unlocked()

    def mark_success(self, host: str, port: int) -> None:
        with self._lock:
            if self._closed:
                return
            rec = self._addrs.get((host, port))
            if rec is None:
                return
            self._addrs[(host, port)] = AddrRecord(
                host=rec.host,
                port=rec.port,
                services=rec.services,
                last_seen=int(time.time()),
                last_try=rec.last_try,
                attempts=0,
                source=rec.source,
            )
            self._save_unlocked()

    def remove(self, host: str, port: int) -> None:
        with self._lock:
            if self._closed:
                return
            if self._addrs.pop((host, port), None) is not None:
                self._save_unlocked()

    def list_recent(self, limit: int = 32) -> list[AddrRecord]:
        with self._lock:
            if self._closed:
                return []
            rows = sorted(self._addrs.values(), key=lambda r: r.last_seen, reverse=True)
            return rows[:limit]

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
            rows = sorted(
                self._addrs.values(),
                key=lambda r: (r.attempts, -r.last_seen),
            )
        for rec in rows:
            if rec.key in exclude:
                continue
            if rec.last_try and (now - rec.last_try) < min_retry_age:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out

    def _evict_oldest(self, n: int) -> None:
        rows = sorted(
            (r for r in self._addrs.values() if r.source != "manual"),
            key=lambda r: r.last_seen,
        )
        for rec in rows[:n]:
            self._addrs.pop((rec.host, rec.port), None)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._save_unlocked()
            except Exception:
                pass
