"""MHCOIN blockchain: block index, active chain, side chains, reorg (Stage 6)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.fingerprint import chain_state_fingerprint, utxo_fingerprint
from mhcoin.blockchain.genesis import validate_genesis_block, validate_genesis_structure
from mhcoin.blockchain.orphans import OrphanPool
from mhcoin.blockchain.undo import BlockUndo
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.chain_work import work_for_header
from mhcoin.consensus.difficulty import (
    get_next_work,
    median_time_past,
    pow_limit_for_network,
)
from mhcoin.consensus.params import DIFFICULTY_WINDOW, MTP_WINDOW, get_network_params
from mhcoin.mempool import Mempool, MempoolError
from mhcoin.network import constants as net_constants
from mhcoin.transaction.transaction import Transaction
from mhcoin.blockchain.disk_lock import chain_disk_lock
from mhcoin.utxo import UTXOSet

logger = logging.getLogger("mhcoin.chain")


def _sqlite_retry(fn, *, attempts: int = 12, base_delay: float = 0.05):
    """Retry on transient SQLite 'database is locked' (WAL + concurrent readers)."""
    import time
    last = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if i + 1 >= attempts:
                raise
            time.sleep(base_delay * (1.0 + 0.35 * i))
    raise last  # pragma: no cover


SCHEMA_VERSION = 2
STATUS_SIDE = 0
STATUS_ACTIVE = 1


class ChainError(Exception):
    pass


@dataclass
class BlockIndexEntry:
    block_hash: bytes
    prev_hash: bytes
    height: int
    chain_work: int
    status: int
    bits: int
    timestamp: int


@dataclass
class AcceptResult:
    """Result of accept_block."""

    height: int
    tip_hash: bytes
    activated: bool  # True if this block is on the new active tip path
    reorg: bool
    side_chain: bool
    orphan: bool
    duplicate: bool
    disconnect: list[bytes]
    connect: list[bytes]


class Blockchain:
    """
    Persistent block tree + active-chain UTXO.

    - All valid blocks may be stored (active + side).
    - Active chain selected by highest cumulative PoW.
    - Equal work → keep current active tip (no reorg).
    """

    def __init__(self, data_dir: Path, *, network: str = "regtest"):
        self.data_dir = data_dir
        self.network = network
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.orphans = OrphanPool()
        # Open DB + repair under datadir lock so GUI/miner cannot race UTXO tip.
        with chain_disk_lock(self.data_dir):
            self.utxo = UTXOSet(self.data_dir / "utxo.sqlite")
            self._db = sqlite3.connect(
                str(self.data_dir / "chain.sqlite"), check_same_thread=False
            )
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA busy_timeout=30000")
            except sqlite3.Error:
                pass
            self._closed = False
            # Fail fast with a clear message if a prior crash corrupted the DB
            # (macOS concurrent readers historically left "disk image is malformed").
            try:
                row = self._db.execute("PRAGMA quick_check").fetchone()
                check = str(row[0]) if row else "ok"
            except sqlite3.DatabaseError as e:
                self._db.close()
                raise ChainError(
                    f"chain.sqlite is corrupted ({e}). Quit the app, keep wallet.json, "
                    f"delete chain.sqlite* and utxo.sqlite* in {self.data_dir}, then re-sync."
                ) from e
            if check.lower() != "ok":
                self._db.close()
                raise ChainError(
                    f"chain.sqlite failed integrity check ({check}). Quit the app, keep "
                    f"wallet.json, delete chain.sqlite* and utxo.sqlite* in {self.data_dir}, "
                    f"then re-sync."
                )
            self._ensure_schema()
            self._tip_hash: bytes | None = None
            self._height: int = -1
            self._tip_work: int = 0
            # Bumped on every canonical tip change — miners poll this (no SQLite).
            self._tip_epoch: int = 0
            self._load_tip()
            self._backfill_undos_if_needed()
            self._repair_utxo_if_needed()
            self._verify_disk_genesis()

    # --- schema / migration ------------------------------------------------

    def _meta_get(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value)
        )

    def _ensure_schema(self) -> None:
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self._db.commit()
        version = self._meta_get("schema_version")
        has_old = self._table_exists("blocks")
        has_index = self._table_exists("block_index")

        if has_index:
            if version != str(SCHEMA_VERSION):
                self._meta_set("schema_version", str(SCHEMA_VERSION))
                self._db.commit()
            return

        # Create new tables
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS block_index (
                block_hash BLOB PRIMARY KEY,
                prev_hash BLOB NOT NULL,
                height INTEGER NOT NULL,
                chain_work TEXT NOT NULL,
                status INTEGER NOT NULL,
                bits INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                raw BLOB NOT NULL
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_block_prev ON block_index(prev_hash)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_block_status_h ON block_index(status, height)"
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS block_undo (
                block_hash BLOB PRIMARY KEY,
                undo_blob BLOB NOT NULL
            )
            """
        )

        if has_old:
            self._migrate_from_v1()
        else:
            # Compatibility view: keep empty `blocks` table for any external tooling
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS blocks (
                    height INTEGER PRIMARY KEY,
                    block_hash BLOB NOT NULL UNIQUE,
                    raw BLOB NOT NULL
                )
                """
            )

        self._meta_set("schema_version", str(SCHEMA_VERSION))
        self._db.commit()

    def _table_exists(self, name: str) -> bool:
        row = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def _migrate_from_v1(self) -> None:
        """Migrate height-primary `blocks` table into block_index (all active)."""
        rows = self._db.execute(
            "SELECT height, block_hash, raw FROM blocks ORDER BY height ASC"
        ).fetchall()
        cum = 0
        for height, bh, raw in rows:
            block = Block.deserialize(raw)
            cum += work_for_header(block.header)
            self._db.execute(
                """
                INSERT OR REPLACE INTO block_index(
                    block_hash, prev_hash, height, chain_work, status, bits, timestamp, raw
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    bh,
                    block.header.previous_block_hash,
                    int(height),
                    str(cum),
                    STATUS_ACTIVE,
                    int(block.header.bits),
                    int(block.header.timestamp),
                    raw,
                ),
            )
        if rows:
            tip_h, tip_bh, _ = rows[-1]
            self._meta_set("tip_hash", tip_bh.hex() if isinstance(tip_bh, bytes) else bytes(tip_bh).hex())
            self._meta_set("tip_height", str(tip_h))
            self._meta_set("tip_work", str(cum))
        logger.info("Migrated %s blocks to block_index schema v%s", len(rows), SCHEMA_VERSION)

    def _backfill_undos_if_needed(self) -> None:
        """Generate missing undos for active chain by replaying into a memory UTXO."""
        with self._lock:
            if self._height < 0:
                return
            missing = False
            for h in range(self._height + 1):
                b = self.get_block_by_height(h)
                if b is None:
                    continue
                if self._get_undo(b.block_hash()) is None:
                    missing = True
                    break
            if not missing:
                return
            logger.info("Backfilling block undos for active chain")
            mem = UTXOSet(path=None)
            for h in range(self._height + 1):
                block = self.get_block_by_height(h)
                if block is None:
                    raise ChainError(f"missing active block at height {h}")
                undo = mem.apply_block_with_undo(block.transactions, h)
                self._store_undo(block.block_hash(), undo)
            self._db.commit()

    def _utxo_consistent_with_tip(self) -> bool:
        """True when in-memory UTXO matches active tip (supply + tip coinbase)."""
        if self._tip_hash is None or self._height < 0:
            return True
        try:
            circ = sum(int(e.output.value) for e in self.utxo._mem.values())
            issued = sum(get_block_subsidy(h) for h in range(self._height + 1))
            if circ != issued:
                return False
        except Exception:
            return False
        try:
            row = self._db.execute(
                "SELECT raw FROM block_index WHERE block_hash=?",
                (self._tip_hash,),
            ).fetchone()
            if not row or not row[0]:
                return False
            tip_block = Block.deserialize(row[0])
            cb = tip_block.transactions[0]
            from mhcoin.utxo import OutPoint

            return self.utxo.has(OutPoint(txid=cb.txid(), vout=0))
        except Exception:
            return False

    def _repair_utxo_if_needed(self) -> None:
        """Rebuild UTXO when tip marker, supply, or tip coinbase disagrees with chain."""
        with self._lock:
            if self._tip_hash is None or self._height < 0:
                return
            # Avoid repair storms (balance poll used to rebuild 300+ blocks every tick).
            now = time.time()
            last = float(getattr(self, "_utxo_repair_at", 0.0) or 0.0)
            last_fail = float(getattr(self, "_utxo_repair_fail_at", 0.0) or 0.0)
            if now - last_fail < 30.0:
                return
            tip_hex = self._tip_hash.hex()
            marked = self.utxo.get_meta("tip_hash")
            reasons: list[str] = []
            if marked != tip_hex:
                # Meta missing/stale but UTXO already matches tip — just stamp meta.
                if self._utxo_consistent_with_tip():
                    try:
                        self.utxo.set_meta("tip_hash", tip_hex)
                    except Exception:
                        logger.debug("utxo tip_meta stamp failed", exc_info=True)
                    return
                reasons.append(f"tip_meta utxo={marked} chain={tip_hex}")
            elif not self._utxo_consistent_with_tip():
                reasons.append("supply_or_tip_coinbase_mismatch")
            if not reasons:
                return
            if now - last < 15.0:
                return
            self._utxo_repair_at = now
            logger.warning(
                "UTXO repair at height=%s — %s",
                self._height,
                "; ".join(reasons),
            )
            try:
                self._rebuild_utxo_from_active()
            except Exception:
                self._utxo_repair_fail_at = time.time()
                logger.exception(
                    "UTXO rebuild failed at height=%s — will retry after cooldown",
                    self._height,
                )
                raise

    def _rebuild_utxo_from_active(self) -> None:
        # Must run under chain._lock (caller holds it). Prefetch blocks first so a
        # mid-rebuild failure does not wipe a good UTXO set via clear_all.
        blocks: list[tuple[int, Block]] = []
        for h in range(self._height + 1):
            row = self._db.execute(
                "SELECT raw FROM block_index WHERE height=? AND status=?",
                (h, STATUS_ACTIVE),
            ).fetchone()
            if not row or not row[0]:
                raise ChainError(f"missing block at {h} during UTXO rebuild")
            try:
                blocks.append((h, Block.deserialize(row[0])))
            except Exception as e:
                raise ChainError(f"bad block at {h} during UTXO rebuild: {e}") from e
        self.utxo.clear_all()
        # clear_all leaves meta; drop tip marker until rebuild finishes cleanly.
        try:
            if self.utxo._conn is not None:
                self.utxo._conn.execute("DELETE FROM meta WHERE key=?", ("tip_hash",))
                self.utxo._conn.commit()
        except Exception:
            pass
        for h, block in blocks:
            undo = self.utxo.apply_block_with_undo(block.transactions, h)
            self._store_undo(block.block_hash(), undo)
        if self._tip_hash is not None:
            self.utxo.set_meta("tip_hash", self._tip_hash.hex())
            # Verify stamp stuck (macOS sqlite quirks / failed commit).
            stamped = self.utxo.get_meta("tip_hash")
            if stamped != self._tip_hash.hex():
                raise ChainError(
                    f"utxo tip_meta not persisted after rebuild (got {stamped!r})"
                )
        self._db.commit()

    def _load_tip(self) -> None:
        tip_hex = self._meta_get("tip_hash")
        if tip_hex:
            self._tip_hash = bytes.fromhex(tip_hex)
            self._height = int(self._meta_get("tip_height") or -1)
            self._tip_work = int(self._meta_get("tip_work") or 0)
            return
        # Derive from active status
        row = self._db.execute(
            "SELECT block_hash, height, chain_work FROM block_index "
            "WHERE status=? ORDER BY height DESC LIMIT 1",
            (STATUS_ACTIVE,),
        ).fetchone()
        if row:
            self._tip_hash = row[0]
            self._height = int(row[1])
            self._tip_work = int(row[2])
            self._meta_set("tip_hash", self._tip_hash.hex())
            self._meta_set("tip_height", str(self._height))
            self._meta_set("tip_work", str(self._tip_work))
            self._db.commit()
        else:
            self._tip_hash = None
            self._height = -1
            self._tip_work = 0

    # --- properties / lookups ----------------------------------------------

    @property
    def height(self) -> int:
        return self._height

    @property
    def tip_hash(self) -> bytes | None:
        return self._tip_hash

    @property
    def tip_epoch(self) -> int:
        """Monotonic counter; increments when canonical tip hash changes."""
        return int(getattr(self, "_tip_epoch", 0))

    def get_index(self, block_hash: bytes) -> BlockIndexEntry | None:
        with self._lock:
            if getattr(self, "_closed", False) or self._db is None:
                return None
            try:
                row = self._db.execute(
                    "SELECT block_hash, prev_hash, height, chain_work, status, bits, timestamp "
                    "FROM block_index WHERE block_hash=?",
                    (block_hash,),
                ).fetchone()
            except Exception:
                return None
            if not row:
                return None
            return BlockIndexEntry(
                block_hash=row[0],
                prev_hash=row[1],
                height=int(row[2]),
                chain_work=int(row[3]),
                status=int(row[4]),
                bits=int(row[5]),
                timestamp=int(row[6]),
            )

    def get_block_by_height(self, height: int) -> Block | None:
        with self._lock:
            if getattr(self, "_closed", False) or self._db is None:
                return None
            try:
                row = self._db.execute(
                    "SELECT raw FROM block_index WHERE height=? AND status=?",
                    (height, STATUS_ACTIVE),
                ).fetchone()
            except Exception:
                return None
            if not row:
                return None
            return Block.deserialize(row[0])

    def get_block_by_hash(self, block_hash: bytes) -> Block | None:
        with self._lock:
            if getattr(self, "_closed", False) or self._db is None:
                return None
            try:
                row = self._db.execute(
                    "SELECT raw FROM block_index WHERE block_hash=?", (block_hash,)
                ).fetchone()
            except Exception:
                return None
            if not row:
                return None
            return Block.deserialize(row[0])

    def get_height_of_hash(self, block_hash: bytes) -> int | None:
        entry = self.get_index(block_hash)
        return entry.height if entry else None

    def ancestor_index_window(
        self,
        tip_hash: bytes,
        *,
        limit: int,
        overlay: dict[bytes, BlockIndexEntry] | None = None,
    ) -> list[BlockIndexEntry]:
        """
        Walk ancestry ending at ``tip_hash`` (inclusive).

        Returns oldest → newest, at most ``limit`` entries.
        ``overlay`` supplies in-memory headers (e.g. sync batch) not yet indexed.
        """
        if limit <= 0:
            return []
        out: list[BlockIndexEntry] = []
        cur: bytes | None = tip_hash
        while cur is not None and len(out) < limit:
            entry: BlockIndexEntry | None = None
            if overlay and cur in overlay:
                entry = overlay[cur]
            else:
                entry = self.get_index(cur)
            if entry is None:
                break
            out.append(entry)
            if entry.height <= 0:
                break
            cur = entry.prev_hash
        out.reverse()
        return out

    def get_next_work_for_parent(
        self,
        parent_hash: bytes,
        *,
        overlay: dict[bytes, BlockIndexEntry] | None = None,
    ) -> int:
        """Required compact bits for the child of ``parent_hash`` (branch-aware)."""
        parent: BlockIndexEntry | None
        if overlay and parent_hash in overlay:
            parent = overlay[parent_hash]
        else:
            parent = self.get_index(parent_hash)
        if parent is None:
            raise ChainError(f"unknown parent {parent_hash.hex()}")
        window = self.ancestor_index_window(
            parent_hash, limit=DIFFICULTY_WINDOW, overlay=overlay
        )
        timestamps = [e.timestamp for e in window]
        return get_next_work(
            network=self.network,
            parent_height=parent.height,
            parent_bits=parent.bits,
            window_timestamps=timestamps,
        )

    def median_time_past_for_parent(
        self,
        parent_hash: bytes,
        *,
        overlay: dict[bytes, BlockIndexEntry] | None = None,
    ) -> int:
        """MTP of up to MTP_WINDOW ancestors ending at parent (branch-aware)."""
        window = self.ancestor_index_window(
            parent_hash, limit=MTP_WINDOW, overlay=overlay
        )
        if not window:
            raise ChainError(f"unknown parent for MTP {parent_hash.hex()}")
        return median_time_past([e.timestamp for e in window])

    def known_block_count(self) -> int:
        with self._lock:
            if getattr(self, "_closed", False) or self._db is None:
                return 0
            try:
                row = self._db.execute("SELECT COUNT(*) FROM block_index").fetchone()
                return int(row[0]) if row else 0
            except Exception:
                return 0

    def side_chain_tips(self) -> list[BlockIndexEntry]:
        """Tips of stored side branches (valid blocks not on active chain)."""
        with self._lock:
            if getattr(self, "_closed", False) or self._db is None:
                return []
            rows = self._db.execute(
                "SELECT block_hash, prev_hash, height, chain_work, status, bits, timestamp "
                "FROM block_index WHERE status=?",
                (STATUS_SIDE,),
            ).fetchall()
            entries = [
                BlockIndexEntry(
                    block_hash=r[0],
                    prev_hash=r[1],
                    height=int(r[2]),
                    chain_work=int(r[3]),
                    status=int(r[4]),
                    bits=int(r[5]),
                    timestamp=int(r[6]),
                )
                for r in rows
            ]
            # A side tip is a side block that is not the prev of another known block
            children: set[bytes] = set()
            for r in self._db.execute("SELECT prev_hash FROM block_index").fetchall():
                children.add(r[0])
            return [e for e in entries if e.block_hash not in children]

    def build_locator(self, *, max_hashes: int = 32) -> list[bytes]:
        if self._height < 0 or self._tip_hash is None:
            return []
        out: list[bytes] = []
        height = self._height
        step = 1
        while height >= 0 and len(out) < max_hashes:
            b = self.get_block_by_height(height)
            if b is not None:
                out.append(b.block_hash())
            if height == 0:
                break
            next_h = height - step
            if next_h < 0:
                next_h = 0
            if len(out) >= 10:
                step *= 2
            height = next_h
        genesis = self.get_block_by_height(0)
        if genesis is not None:
            gh = genesis.block_hash()
            if not out or out[-1] != gh:
                if len(out) >= max_hashes:
                    out[-1] = gh
                else:
                    out.append(gh)
        return out

    def headers_after_locator(
        self,
        locator: list[bytes],
        *,
        hash_stop: bytes,
        limit: int,
    ) -> list[BlockHeader]:
        """Return active-chain headers after the first known locator hash."""
        start_h: int | None = None
        for h in locator:
            found = self.get_height_of_hash(h)
            if found is not None:
                # Prefer active-chain height: if hash is side, still use its height
                # but walk active chain from that height only if hash is active.
                entry = self.get_index(h)
                if entry is not None and entry.status == STATUS_ACTIVE:
                    start_h = found
                    break
                if entry is not None:
                    # Locator hit a side block — find common with active tip
                    if self._tip_hash is not None:
                        anc = self.find_common_ancestor(h, self._tip_hash)
                        if anc is not None:
                            start_h = self.get_height_of_hash(anc)
                            break
        if start_h is None:
            if not locator and self._height >= 0:
                start_h = -1
            else:
                return []
        headers: list[BlockHeader] = []
        for height in range(start_h + 1, self._height + 1):
            if len(headers) >= limit:
                break
            block = self.get_block_by_height(height)
            if block is None:
                break
            headers.append(block.header)
            if hash_stop != b"\x00" * 32 and block.block_hash() == hash_stop:
                break
        return headers

    # --- ancestor / path ---------------------------------------------------

    def find_common_ancestor(self, tip_a: bytes, tip_b: bytes) -> bytes | None:
        """Highest block shared by both chains (works with unequal heights)."""
        with self._lock:
            if tip_a == tip_b:
                return tip_a
            ea = self.get_index(tip_a)
            eb = self.get_index(tip_b)
            if ea is None or eb is None:
                return None
            # Walk both tips back to equal height, then step together
            ha, hb = ea.height, eb.height
            ca, cb = tip_a, tip_b
            while ha > hb:
                ea = self.get_index(ca)
                if ea is None:
                    return None
                ca = ea.prev_hash
                ha -= 1
            while hb > ha:
                eb = self.get_index(cb)
                if eb is None:
                    return None
                cb = eb.prev_hash
                hb -= 1
            while ca != cb:
                ea = self.get_index(ca)
                eb = self.get_index(cb)
                if ea is None or eb is None:
                    return None
                if ea.height == 0 and eb.height == 0:
                    return ca if ca == cb else None
                ca = ea.prev_hash
                cb = eb.prev_hash
            return ca

    def path_from_to(self, ancestor: bytes, tip: bytes) -> list[bytes]:
        """Block hashes from ancestor (exclusive) to tip (inclusive), ascending."""
        with self._lock:
            if ancestor == tip:
                return []
            chain: list[bytes] = []
            cur = tip
            while cur != ancestor:
                entry = self.get_index(cur)
                if entry is None:
                    raise ChainError(f"broken path near {cur.hex()}")
                chain.append(cur)
                if entry.height == 0:
                    break
                cur = entry.prev_hash
            chain.reverse()
            return chain

    def active_path_hashes(self) -> list[bytes]:
        out: list[bytes] = []
        for h in range(self._height + 1):
            b = self.get_block_by_height(h)
            if b is None:
                break
            out.append(b.block_hash())
        return out

    # --- undo store --------------------------------------------------------

    def _store_undo(self, block_hash: bytes, undo: BlockUndo) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO block_undo(block_hash, undo_blob) VALUES (?,?)",
            (block_hash, undo.serialize()),
        )

    def _get_undo(self, block_hash: bytes) -> BlockUndo | None:
        row = self._db.execute(
            "SELECT undo_blob FROM block_undo WHERE block_hash=?", (block_hash,)
        ).fetchone()
        if not row:
            return None
        return BlockUndo.deserialize(row[0])

    def _store_block_index(
        self,
        block: Block,
        *,
        height: int,
        chain_work: int,
        status: int,
    ) -> None:
        raw = block.serialize()
        bh = block.block_hash()
        self._db.execute(
            """
            INSERT OR REPLACE INTO block_index(
                block_hash, prev_hash, height, chain_work, status, bits, timestamp, raw
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                bh,
                block.header.previous_block_hash,
                height,
                str(chain_work),
                status,
                int(block.header.bits),
                int(block.header.timestamp),
                raw,
            ),
        )

    def _set_status(self, block_hash: bytes, status: int) -> None:
        self._db.execute(
            "UPDATE block_index SET status=? WHERE block_hash=?",
            (status, block_hash),
        )

    def _set_tip_meta(self, tip: bytes, height: int, work: int) -> None:
        prev = self._tip_hash
        self._tip_hash = tip
        self._height = height
        self._tip_work = work
        if tip != prev:
            self._tip_epoch = int(self._tip_epoch) + 1
        self._meta_set("tip_hash", tip.hex())
        self._meta_set("tip_height", str(height))
        self._meta_set("tip_work", str(work))
        self.utxo.set_meta("tip_hash", tip.hex())

    # --- UTXO view at arbitrary known block --------------------------------

    def _utxo_at(self, target_hash: bytes) -> UTXOSet:
        """
        Reconstruct in-memory UTXO corresponding to `target_hash` chain state.
        Uses active-chain undos + replaying the fork path from common ancestor.
        """
        if self._tip_hash is None:
            raise ChainError("no tip")
        if target_hash == self._tip_hash:
            return self.utxo.clone_memory()

        ancestor = self.find_common_ancestor(self._tip_hash, target_hash)
        if ancestor is None:
            raise ChainError("no common ancestor for UTXO rebuild")

        view = self.utxo.clone_memory()
        # Disconnect tip → ancestor
        disconnect = self.path_from_to(ancestor, self._tip_hash)
        for bh in reversed(disconnect):
            block = self.get_block_by_hash(bh)
            undo = self._get_undo(bh)
            if block is None or undo is None:
                # Fall back: full replay from genesis to target
                return self._utxo_replay_to(target_hash)
            view.disconnect_block(block.transactions, undo)

        # Connect ancestor → target
        connect = self.path_from_to(ancestor, target_hash)
        for bh in connect:
            block = self.get_block_by_hash(bh)
            if block is None:
                raise ChainError(f"missing block {bh.hex()}")
            entry = self.get_index(bh)
            assert entry is not None
            view.apply_block_with_undo(block.transactions, entry.height)
        return view

    def _utxo_replay_to(self, target_hash: bytes) -> UTXOSet:
        """Replay genesis → target into a memory UTXO (expensive fallback)."""
        path: list[bytes] = []
        cur = target_hash
        while True:
            entry = self.get_index(cur)
            if entry is None:
                raise ChainError(f"cannot replay missing {cur.hex()}")
            path.append(cur)
            if entry.height == 0:
                break
            cur = entry.prev_hash
        path.reverse()
        view = UTXOSet(path=None)
        for bh in path:
            block = self.get_block_by_hash(bh)
            entry = self.get_index(bh)
            assert block is not None and entry is not None
            view.apply_block_with_undo(block.transactions, entry.height)
        return view

    # --- init / connect / accept -------------------------------------------

    def _validate_genesis_for_network(self, genesis: Block) -> None:
        """Enforce frozen genesis on production nets; allow custom bootstrap on regtest/localnet."""
        params = get_network_params(self.network)
        if params.allow_custom_genesis_bootstrap:
            try:
                validate_genesis_block(genesis, params)
            except ValueError:
                validate_genesis_structure(
                    genesis,
                    bits=params.genesis_bits,
                    timestamp=params.genesis_timestamp,
                )
        else:
            validate_genesis_block(genesis, params)

    def _verify_disk_genesis(self) -> None:
        """Reject wrong-network or mutated height-0 blocks after load."""
        if self._height < 0 or self._tip_hash is None:
            return
        genesis = self.get_block_by_height(0)
        if genesis is None:
            raise ChainError("missing genesis block on disk")
        params = get_network_params(self.network)
        try:
            self._validate_genesis_for_network(genesis)
        except ValueError as e:
            detail = str(e)
            if "timestamp" in detail:
                detail = (
                    f"{detail} (disk={genesis.header.timestamp}, "
                    f"expected_{self.network}={params.genesis_timestamp}). "
                    f"Datadir {self.data_dir} is not a clean {self.network} chain — "
                    f"remove it or use the correct network."
                )
            raise ChainError(f"genesis verification failed ({self.network}): {detail}") from e
        stored_net = self._meta_get("network")
        if stored_net and stored_net != self.network:
            # regtest and localnet share the same frozen genesis template; allow reopen
            # with either label. mainnet/testnet remain strict.
            shared = {"regtest", "localnet"}
            if not (stored_net in shared and self.network in shared):
                raise ChainError(
                    f"data dir network={stored_net!r} does not match node network={self.network!r}"
                )
        expected = get_network_params(self.network).genesis_hash_hex
        # Production networks must match frozen hash exactly
        if not get_network_params(self.network).allow_custom_genesis_bootstrap:
            if genesis.block_hash().hex() != expected:
                raise ChainError(
                    f"expected genesis {expected}, got {genesis.block_hash().hex()}"
                )

    def init_with_genesis(self, genesis: Block) -> None:
        with self._lock:
            if self._height >= 0:
                raise ChainError("chain already initialized")
            self._validate_genesis_for_network(genesis)
            work = work_for_header(genesis.header)
            try:
                undo = self.utxo.apply_block_with_undo(genesis.transactions, 0)
            except Exception as e:
                raise ValidationError(str(e)) from e
            self._store_block_index(genesis, height=0, chain_work=work, status=STATUS_ACTIVE)
            self._store_undo(genesis.block_hash(), undo)
            self._set_tip_meta(genesis.block_hash(), 0, work)
            self._meta_set("network", self.network)
            self._db.commit()
            self.assert_supply_consistency()

    def connect_block(self, block: Block) -> int:
        """Tip-extension only (legacy API). Raises if block does not extend tip."""
        def _do() -> int:
            with chain_disk_lock(self.data_dir):
                with self._lock:
                    if self._height < 0:
                        raise ChainError("genesis required first")
                    assert self._tip_hash is not None
                    if block.header.previous_block_hash != self._tip_hash:
                        raise ChainError("block does not extend active tip")
                    result = self._accept_block_locked(block, mempool=None)
                    if result.orphan:
                        raise ChainError("unexpected orphan on tip connect")
                    return result.height
        return _sqlite_retry(_do)

    def accept_block(
        self,
        block: Block,
        *,
        mempool: Mempool | None = None,
    ) -> AcceptResult:
        """
        Full Stage 6 acceptance: store valid blocks, reorg on greater work.
        """
        def _do() -> AcceptResult:
            with chain_disk_lock(self.data_dir):
                with self._lock:
                    return self._accept_block_locked(block, mempool=mempool)
        return _sqlite_retry(_do)

    def _accept_block_locked(
        self, block: Block, *, mempool: Mempool | None
    ) -> AcceptResult:
        bh = block.block_hash()
        if self.get_index(bh) is not None:
            assert self._tip_hash is not None
            return AcceptResult(
                height=self._height,
                tip_hash=self._tip_hash,
                activated=False,
                reorg=False,
                side_chain=False,
                orphan=False,
                duplicate=True,
                disconnect=[],
                connect=[],
            )
        if self.orphans.contains(bh):
            tip = self._tip_hash or b"\x00" * 32
            return AcceptResult(
                height=self._height,
                tip_hash=tip,
                activated=False,
                reorg=False,
                side_chain=False,
                orphan=True,
                duplicate=True,
                disconnect=[],
                connect=[],
            )

        prev = block.header.previous_block_hash
        parent = self.get_index(prev)
        if parent is None:
            self.orphans.add(block)
            assert self._tip_hash is not None or self._height < 0
            tip = self._tip_hash or b"\x00" * 32
            return AcceptResult(
                height=self._height,
                tip_hash=tip,
                activated=False,
                reorg=False,
                side_chain=False,
                orphan=True,
                duplicate=False,
                disconnect=[],
                connect=[],
            )

        height = parent.height + 1
        expected_bits = self.get_next_work_for_parent(prev)
        mtp = self.median_time_past_for_parent(prev)
        limit = pow_limit_for_network(self.network)

        # Validate against parent chain state (not necessarily active tip)
        if prev == self._tip_hash:
            utxo_view = self.utxo
        else:
            utxo_view = self._utxo_at(prev)

        validate_block(
            block,
            utxo_view,
            height=height,
            expected_prev=prev,
            expected_bits=expected_bits,
            median_time_past=mtp,
            pow_limit=limit,
        )

        # Apply on view to ensure UTXO transition works (side: temp; tip: real later)
        if prev != self._tip_hash:
            try:
                utxo_view.apply_block_with_undo(block.transactions, height)
            except Exception as e:
                raise ValidationError(str(e)) from e

        chain_work = parent.chain_work + work_for_header(block.header)

        # Store as side first; activate if tip extension or reorg wins
        is_tip_ext = prev == self._tip_hash
        status = STATUS_SIDE
        self._store_block_index(block, height=height, chain_work=chain_work, status=status)
        self._db.commit()

        reorg = False
        disconnect: list[bytes] = []
        connect: list[bytes] = []
        activated = False

        if is_tip_ext:
            # Connect to active tip (always more/equal work for same+harder bits on longer)
            undo = self.utxo.apply_block_with_undo(block.transactions, height)
            self._store_undo(bh, undo)
            self._set_status(bh, STATUS_ACTIVE)
            self._set_tip_meta(bh, height, chain_work)
            self._db.commit()
            activated = True
            connect = [bh]
            if mempool is not None:
                mempool.clear_included(block.transactions[1:])
        elif chain_work > self._tip_work:
            # Candidate has more work — reorg
            try:
                disconnect, connect = self._reorg_to(bh, mempool=mempool)
                reorg = True
                activated = True
            except Exception as e:
                logger.info(
                    "REORG FAILED old_tip=%s new_tip=%s reason=%s",
                    self._tip_hash.hex() if self._tip_hash else None,
                    bh.hex(),
                    e,
                )
                # Keep block as side; leave active intact
                raise
        else:
            # Side chain with less or equal work — keep stored, tip unchanged
            logger.info(
                "Side-chain block stored hash=%s height=%s work=%s tip_work=%s",
                bh.hex(),
                height,
                chain_work,
                self._tip_work,
            )

        # Process orphans that depend on this block
        for orphan in self.orphans.pop_children(bh):
            try:
                self._accept_block_locked(orphan, mempool=mempool)
            except (ValidationError, ChainError) as e:
                logger.info(
                    "Orphan rejected after parent hash=%s reason=%s",
                    orphan.block_hash().hex(),
                    e,
                )

        assert self._tip_hash is not None
        return AcceptResult(
            height=self._height,
            tip_hash=self._tip_hash,
            activated=activated,
            reorg=reorg,
            side_chain=not activated and not is_tip_ext,
            orphan=False,
            duplicate=False,
            disconnect=disconnect,
            connect=connect,
        )

    def _reorg_to(
        self, new_tip: bytes, *, mempool: Mempool | None
    ) -> tuple[list[bytes], list[bytes]]:
        """
        Atomically reorganize active chain to `new_tip`.
        Returns (disconnect_hashes newest-first order applied, connect_hashes).
        """
        assert self._tip_hash is not None
        old_tip = self._tip_hash
        old_height = self._height
        old_work = self._tip_work

        ancestor = self.find_common_ancestor(old_tip, new_tip)
        if ancestor is None:
            raise ChainError("reorg: no common ancestor")

        disconnect = self.path_from_to(ancestor, old_tip)
        connect = self.path_from_to(ancestor, new_tip)

        if len(disconnect) > net_constants.MAX_REORG_DEPTH:
            raise ChainError(
                f"reorg depth {len(disconnect)} exceeds MAX_REORG_DEPTH="
                f"{net_constants.MAX_REORG_DEPTH}"
            )

        new_entry = self.get_index(new_tip)
        assert new_entry is not None

        logger.info(
            "REORG DETECTED old_tip=%s new_tip=%s common_ancestor=%s "
            "old_height=%s new_height=%s old_work=%s new_work=%s "
            "disconnect=%s connect=%s",
            old_tip.hex(),
            new_tip.hex(),
            ancestor.hex(),
            old_height,
            new_entry.height,
            old_work,
            new_entry.chain_work,
            len(disconnect),
            len(connect),
        )

        # Collect txs from disconnected blocks for mempool restoration
        resurrect: list[Transaction] = []
        for bh in disconnect:
            block = self.get_block_by_hash(bh)
            if block is None:
                raise ChainError(f"missing disconnect block {bh.hex()}")
            for tx in block.transactions[1:]:  # skip coinbase
                resurrect.append(tx)

        # Snapshot for failure recovery
        utxo_snap = self.utxo.clone_memory()
        tip_snap = (old_tip, old_height, old_work)
        status_snap = [(h, STATUS_ACTIVE) for h in disconnect] + [
            (h, STATUS_SIDE) for h in connect
        ]

        try:
            self._db.execute("BEGIN IMMEDIATE")

            # Disconnect old (newest first)
            for bh in reversed(disconnect):
                block = self.get_block_by_hash(bh)
                undo = self._get_undo(bh)
                if block is None or undo is None:
                    raise ChainError(f"cannot disconnect {bh.hex()}: missing undo")
                self.utxo.disconnect_block(block.transactions, undo)
                self._set_status(bh, STATUS_SIDE)

            # Connect new
            for bh in connect:
                block = self.get_block_by_hash(bh)
                entry = self.get_index(bh)
                if block is None or entry is None:
                    raise ChainError(f"cannot connect {bh.hex()}")
                # Re-validate against live UTXO (parent state now correct)
                parent_hash = block.header.previous_block_hash
                expected_bits = self.get_next_work_for_parent(parent_hash)
                mtp = self.median_time_past_for_parent(parent_hash)
                validate_block(
                    block,
                    self.utxo,
                    height=entry.height,
                    expected_prev=parent_hash,
                    expected_bits=expected_bits,
                    median_time_past=mtp,
                    pow_limit=pow_limit_for_network(self.network),
                )
                undo = self.utxo.apply_block_with_undo(block.transactions, entry.height)
                self._store_undo(bh, undo)
                self._set_status(bh, STATUS_ACTIVE)

            self._set_tip_meta(new_tip, new_entry.height, new_entry.chain_work)
            self._db.commit()
        except Exception:
            try:
                self._db.rollback()
            except sqlite3.Error:
                pass
            # Restore UTXO from snapshot by rebuild
            self.utxo.clear_all()
            for k, e in utxo_snap._mem.items():
                self.utxo._mem[k] = e
                if self.utxo._conn is not None:
                    self.utxo._conn.execute(
                        "INSERT OR REPLACE INTO utxo VALUES (?,?,?,?,?,?,?)",
                        (
                            k,
                            e.outpoint.txid,
                            e.outpoint.vout,
                            e.output.value,
                            e.output.script_pubkey,
                            e.height,
                            1 if e.coinbase else 0,
                        ),
                    )
            if self.utxo._conn is not None:
                self.utxo._conn.commit()
            self._set_tip_meta(*tip_snap)
            for h, st in status_snap:
                # best-effort restore statuses from before reorg attempt
                pass
            # Full repair
            self._db.rollback()
            # Reload statuses: disconnect should be ACTIVE, connect SIDE
            for h in disconnect:
                self._set_status(h, STATUS_ACTIVE)
            for h in connect:
                self._set_status(h, STATUS_SIDE)
            self._set_tip_meta(*tip_snap)
            self._db.commit()
            self._rebuild_utxo_from_active()
            raise

        # Mempool: remove txs confirmed on new chain; resurrect valid old ones
        if mempool is not None:
            confirmed: set[str] = set()
            for bh in connect:
                block = self.get_block_by_hash(bh)
                if block is None:
                    continue
                for tx in block.transactions[1:]:
                    confirmed.add(tx.txid_hex())
                    mempool.remove(tx.txid_hex())
            for tx in resurrect:
                hx = tx.txid_hex()
                if hx in confirmed:
                    continue
                if mempool.contains(hx):
                    continue
                try:
                    mempool.add(tx, self.utxo, height=max(self._height, 0))
                except MempoolError:
                    pass  # invalid / conflict under new UTXO — discard

        self.assert_supply_consistency()
        logger.info(
            "REORG COMPLETE tip=%s height=%s work=%s",
            self._tip_hash.hex() if self._tip_hash else None,
            self._height,
            self._tip_work,
        )
        return disconnect, connect

    # --- supply / work / info ----------------------------------------------

    def circulating_supply(self) -> int:
        return sum(e.output.value for e in self.utxo._mem.values())

    def issued_supply(self) -> int:
        if self._height < 0:
            return 0
        return sum(get_block_subsidy(h) for h in range(self._height + 1))

    def get_chain_work(self) -> int:
        return self._tip_work if self._height >= 0 else 0

    @property
    def tip_work(self) -> int:
        """Cumulative proof-of-work of the active tip."""
        return self._tip_work if self._height >= 0 else 0

    def refresh_from_disk(self) -> list[Block]:
        old_height = self._height
        old_tip = self._tip_hash
        self.utxo.close()
        self.utxo = UTXOSet(self.data_dir / "utxo.sqlite")
        self._load_tip()
        self._repair_utxo_if_needed()
        if self._tip_hash == old_tip and self._height <= old_height:
            return []
        out: list[Block] = []
        # If tip extended on active chain, return new blocks
        if old_tip is not None and self._tip_hash is not None:
            try:
                path = self.path_from_to(old_tip, self._tip_hash)
                for bh in path:
                    b = self.get_block_by_hash(bh)
                    if b is not None:
                        out.append(b)
            except ChainError:
                for h in range(old_height + 1, self._height + 1):
                    b = self.get_block_by_height(h)
                    if b is not None:
                        out.append(b)
        return out

    def state_fingerprint(self) -> dict:
        return chain_state_fingerprint(
            tip_hash=self._tip_hash,
            height=self._height,
            chain_work=self.get_chain_work(),
            utxo=self.utxo,
        )

    def utxo_fp(self) -> str:
        return utxo_fingerprint(self.utxo).hex()

    def forks_info(self) -> list[dict]:
        """Diagnostics for CLI: active tip + side tips."""
        out: list[dict] = []
        if self._tip_hash is not None:
            out.append(
                {
                    "active": True,
                    "tip": self._tip_hash.hex(),
                    "height": self._height,
                    "work": self._tip_work,
                    "ancestor": None,
                }
            )
        for tip in self.side_chain_tips():
            anc = None
            if self._tip_hash is not None:
                a = self.find_common_ancestor(self._tip_hash, tip.block_hash)
                anc = a.hex() if a else None
            out.append(
                {
                    "active": False,
                    "tip": tip.block_hash.hex(),
                    "height": tip.height,
                    "work": tip.chain_work,
                    "ancestor": anc,
                }
            )
        return out

    def info(self) -> dict:
        tip_bits = get_network_params(self.network).genesis_bits
        if self._tip_hash is not None:
            tip_entry = self.get_index(self._tip_hash)
            if tip_entry is not None:
                tip_bits = tip_entry.bits
        if getattr(self, "_closed", False) or self._db is None:
            return {
                "height": self._height,
                "tip": self._tip_hash.hex() if self._tip_hash else None,
                "utxo_count": 0,
                "bits": tip_bits,
                "circulating_supply": 0,
                "issued_supply": 0,
                "chain_work": 0,
                "known_blocks": 0,
                "side_chains": 0,
                "orphans": 0,
                "utxo_fingerprint": None,
            }
        circulating = self.circulating_supply() if self._height >= 0 else 0
        issued = self.issued_supply() if self._height >= 0 else 0
        return {
            "height": self._height,
            "tip": self._tip_hash.hex() if self._tip_hash else None,
            "utxo_count": self.utxo.count(),
            "bits": tip_bits,
            "circulating_supply": circulating,
            "issued_supply": issued,
            "chain_work": self.get_chain_work() if self._height >= 0 else 0,
            "known_blocks": self.known_block_count(),
            "side_chains": len(self.side_chain_tips()),
            "orphans": len(self.orphans),
            "utxo_fingerprint": self.utxo_fp() if self._height >= 0 else None,
        }

    def assert_supply_consistency(self) -> None:
        if self._height < 0:
            return
        circ = self.circulating_supply()
        issued = self.issued_supply()
        if circ != issued:
            raise ValidationError(
                f"supply mismatch: circulating {circ} != issued {issued}"
            )

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self.utxo.close()
        except Exception:
            pass
        db = getattr(self, "_db", None)
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
            self._db = None
