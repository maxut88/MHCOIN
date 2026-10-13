"""peers.dat store + migration from legacy peers.sqlite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mhcoin.network.addrdb import AddrDB


def test_peers_dat_roundtrip(tmp_path: Path):
    path = tmp_path / "peers.dat"
    db = AddrDB(path)
    assert db.add("10.9.8.7", 8333, source="manual")
    db.mark_attempt("10.9.8.7", 8333)
    db.close()
    assert path.is_file()
    db2 = AddrDB(path)
    rec = db2.get("10.9.8.7", 8333)
    assert rec is not None
    assert rec.source == "manual"
    assert rec.attempts == 1
    db2.close()


def test_migrate_from_peers_sqlite(tmp_path: Path):
    legacy = tmp_path / "peers.sqlite"
    con = sqlite3.connect(str(legacy))
    con.execute(
        """
        CREATE TABLE addrs (
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
    con.execute(
        "INSERT INTO addrs VALUES (?,?,?,?,?,?,?)",
        ("1.2.3.4", 8333, 1, 100, 0, 0, "gossip"),
    )
    con.commit()
    con.close()

    db = AddrDB(tmp_path / "peers.dat")
    rec = db.get("1.2.3.4", 8333)
    assert rec is not None
    assert rec.source == "gossip"
    assert (tmp_path / "peers.dat").is_file()
    assert (tmp_path / "peers.sqlite.bak").is_file() or not legacy.is_file()
    db.close()
