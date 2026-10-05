"""UTXO set: create/spend, double-spend protection, LevelDB persistence, undo/rollback.

On-disk layout (Bitcoin-style ``chainstate/`` LevelDB):
  key ``C`` + txid(32) + vout_u32be  → coin value
  key ``M`` + utf-8 meta key         → utf-8 meta value

Legacy ``utxo.sqlite`` in the same datadir is migrated once on open.

Multiple ``UTXOSet`` opens of the same ``chainstate/`` path in one process share
one LevelDB handle + memory view (refcount). LevelDB itself is single-process.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mhcoin.transaction.input import TxOut
from mhcoin.transaction.transaction import Transaction

if TYPE_CHECKING:
    from mhcoin.blockchain.undo import BlockUndo

logger = logging.getLogger(__name__)

_PREFIX_COIN = b"C"
_PREFIX_META = b"M"

_LIVE_LOCK = threading.Lock()
_LIVE: dict[str, "_LiveChainstate"] = {}


@dataclass
class _LiveChainstate:
    path: Path
    db: Any
    mem: dict[str, "UTXOEntry"]
    lock: threading.RLock
    batch: Any
    refs: int


@dataclass(frozen=True)
class OutPoint:
    txid: bytes
    vout: int

    def key(self) -> str:
        return f"{self.txid.hex()}:{self.vout}"

    def db_key(self) -> bytes:
        if len(self.txid) != 32:
            raise ValueError("txid must be 32 bytes")
        return _PREFIX_COIN + self.txid + struct.pack(">I", self.vout)

    @staticmethod
    def from_key(key: str) -> OutPoint:
        hx, v = key.split(":")
        return OutPoint(txid=bytes.fromhex(hx), vout=int(v))

    @staticmethod
    def from_db_key(raw: bytes) -> OutPoint:
        if len(raw) != 1 + 32 + 4 or raw[0:1] != _PREFIX_COIN:
            raise ValueError("bad coin db key")
        return OutPoint(txid=raw[1:33], vout=struct.unpack(">I", raw[33:37])[0])


@dataclass
class UTXOEntry:
    outpoint: OutPoint
    output: TxOut
    height: int
    coinbase: bool


class UTXOError(Exception):
    pass


def _encode_coin(entry: UTXOEntry) -> bytes:
    return (
        struct.pack(">Q", int(entry.output.value))
        + struct.pack(">I", int(entry.height))
        + bytes([1 if entry.coinbase else 0])
        + bytes(entry.output.script_pubkey)
    )


def _decode_coin(outpoint: OutPoint, raw: bytes) -> UTXOEntry:
    if len(raw) < 8 + 4 + 1:
        raise ValueError("short coin value")
    value = struct.unpack(">Q", raw[0:8])[0]
    height = struct.unpack(">I", raw[8:12])[0]
    coinbase = bool(raw[12])
    script = raw[13:]
    return UTXOEntry(
        outpoint=outpoint,
        output=TxOut(value=value, script_pubkey=script),
        height=height,
        coinbase=coinbase,
    )


def resolve_chainstate_dir(path: Path) -> Path:
    """Map legacy ``utxo.sqlite`` path or bare file to ``chainstate/`` directory."""
    path = Path(path)
    if path.suffix == ".sqlite" or path.name == "utxo.sqlite":
        return path.with_name("chainstate")
    if path.suffix:
        return path.with_name("chainstate")
    return path


class UTXOSet:
    def __init__(self, path: Path | None = None):
        self.path: Path | None = None
        self._live: _LiveChainstate | None = None
        self._mem_local: dict[str, UTXOEntry] = {}
        self._lock_local = threading.RLock()
        self._share_key: str | None = None
        if path is not None:
            self.path = resolve_chainstate_dir(Path(path))
            self.path.mkdir(parents=True, exist_ok=True)
            key = str(self.path.resolve())
            with _LIVE_LOCK:
                existing = _LIVE.get(key)
                if existing is not None and existing.db is not None:
                    existing.refs += 1
                    self._live = existing
                    self._share_key = key
                    return
                import plyvel

                db = plyvel.DB(str(self.path), create_if_missing=True)
                live = _LiveChainstate(
                    path=self.path,
                    db=db,
                    mem={},
                    lock=threading.RLock(),
                    batch=None,
                    refs=1,
                )
                self._live = live
                self._share_key = key
                _LIVE[key] = live
            # Migrate + load outside registry lock (may take time).
            self._migrate_sqlite_if_needed(Path(path))
            self._load()

    @property
    def _mem(self) -> dict[str, UTXOEntry]:
        if self._live is not None:
            return self._live.mem
        return self._mem_local

    @_mem.setter
    def _mem(self, value: dict[str, UTXOEntry]) -> None:
        if self._live is not None:
            self._live.mem = value
        else:
            self._mem_local = value

    @property
    def _lock(self) -> threading.RLock:
        if self._live is not None:
            return self._live.lock
        return self._lock_local

    @property
    def _db(self):
        return self._live.db if self._live is not None else None

    @property
    def _batch(self):
        return self._live.batch if self._live is not None else None

    @_batch.setter
    def _batch(self, value) -> None:
        if self._live is not None:
            self._live.batch = value

    def _legacy_sqlite_path(self, requested: Path) -> Path:
        requested = Path(requested)
        if requested.suffix == ".sqlite" or requested.name == "utxo.sqlite":
            return requested
        assert self.path is not None
        return self.path.with_name("utxo.sqlite")

    def _migrate_sqlite_if_needed(self, requested: Path) -> None:
        assert self._db is not None
        legacy = self._legacy_sqlite_path(requested)
        if not legacy.is_file():
            return
        try:
            it = self._db.iterator(prefix=_PREFIX_COIN, include_value=False)
            has_coin = next(it, None) is not None
            it.close()
        except Exception:
            has_coin = False
        if has_coin:
            logger.info(
                "chainstate already populated; leaving %s in place", legacy.name
            )
            return
        try:
            con = sqlite3.connect(str(legacy))
            rows = list(
                con.execute(
                    "SELECT outpoint, txid, vout, value, script_pubkey, height, coinbase "
                    "FROM utxo"
                )
            )
            meta_rows = list(con.execute("SELECT key, value FROM meta"))
            con.close()
        except sqlite3.Error as e:
            logger.warning("skip utxo.sqlite migrate (%s): %s", legacy, e)
            return
        wb = self._db.write_batch(transaction=True)
        try:
            for row in rows:
                op = OutPoint(txid=bytes(row[1]), vout=int(row[2]))
                entry = UTXOEntry(
                    outpoint=op,
                    output=TxOut(value=int(row[3]), script_pubkey=bytes(row[4])),
                    height=int(row[5]),
                    coinbase=bool(row[6]),
                )
                wb.put(op.db_key(), _encode_coin(entry))
            for k, v in meta_rows:
                wb.put(_PREFIX_META + str(k).encode("utf-8"), str(v).encode("utf-8"))
            wb.write()
        finally:
            try:
                wb.close()
            except Exception:
                pass
        bak = legacy.with_suffix(legacy.suffix + ".bak")
        try:
            if bak.exists():
                bak.unlink()
            legacy.rename(bak)
        except OSError:
            logger.warning("migrated UTXO but could not rename %s", legacy)
        logger.info(
            "Migrated %s UTXOs from %s → %s", len(rows), legacy.name, self.path
        )

    def _load(self) -> None:
        assert self._db is not None
        self._mem.clear()
        for raw_key, raw_val in self._db.iterator(prefix=_PREFIX_COIN):
            try:
                op = OutPoint.from_db_key(raw_key)
                self._mem[op.key()] = _decode_coin(op, raw_val)
            except Exception:
                logger.exception("skip bad chainstate coin key=%r", raw_key)

    def get(self, outpoint: OutPoint) -> UTXOEntry | None:
        return self._mem.get(outpoint.key())

    def has(self, outpoint: OutPoint) -> bool:
        return outpoint.key() in self._mem

    def all_entries(self) -> list[UTXOEntry]:
        return list(self._mem.values())

    def all_for_pubkey_hash(self, pubkey_hash: bytes) -> list[UTXOEntry]:
        out = []
        for e in self._mem.values():
            try:
                if e.output.pubkey_hash() == pubkey_hash:
                    out.append(e)
            except ValueError:
                continue
        return out

    def clone_memory(self) -> UTXOSet:
        """In-memory snapshot for side-chain validation (no shared DB)."""
        clone = UTXOSet(path=None)
        clone._mem_local = {
            k: UTXOEntry(
                outpoint=e.outpoint,
                output=TxOut(value=e.output.value, script_pubkey=e.output.script_pubkey),
                height=e.height,
                coinbase=e.coinbase,
            )
            for k, e in self._mem.items()
        }
        return clone

    def clear_all(self) -> None:
        with self._lock:
            self._mem.clear()
            if self._db is not None:
                keys = list(
                    self._db.iterator(prefix=_PREFIX_COIN, include_value=False)
                )
                wb = self._db.write_batch(transaction=True)
                try:
                    for raw_key in keys:
                        wb.delete(raw_key)
                    wb.write()
                finally:
                    try:
                        wb.close()
                    except Exception:
                        pass

    def apply_transaction(self, tx: Transaction, height: int) -> None:
        with self._lock:
            self._begin()
            try:
                self._apply_transaction_no_commit(tx, height)
                self._commit()
            except Exception:
                self._abort()
                raise

    def apply_block(self, transactions: list[Transaction], height: int) -> None:
        with self._lock:
            self.apply_block_with_undo(transactions, height)

    def apply_block_with_undo(
        self, transactions: list[Transaction], height: int
    ) -> BlockUndo:
        from mhcoin.blockchain.undo import SpentCoin, block_undo_v1

        with self._lock:
            snapshot = dict(self._mem)
            spent: list[SpentCoin] = []
            created: list[str] = []
            self._begin()
            try:
                for tx in transactions:
                    spent_here, created_here = self._apply_transaction_collect(
                        tx, height
                    )
                    spent.extend(spent_here)
                    created.extend(created_here)
                self._commit()
            except Exception:
                self._mem = snapshot
                self._abort()
                raise
            return block_undo_v1(spent=spent, created=created)

    def disconnect_block(
        self, transactions: list[Transaction], undo: BlockUndo
    ) -> None:
        with self._lock:
            snapshot = dict(self._mem)
            self._begin()
            try:
                for key in undo.created:
                    op = OutPoint.from_key(key)
                    self._remove(op)
                for coin in undo.spent:
                    self._add(coin.to_entry())
                expected_created = 0
                for tx in transactions:
                    expected_created += len(tx.outputs)
                if expected_created != len(undo.created):
                    raise UTXOError(
                        f"undo created mismatch: {len(undo.created)} != {expected_created}"
                    )
                self._commit()
            except Exception:
                self._mem = snapshot
                self._abort()
                raise

    def _begin(self) -> None:
        if self._db is None:
            self._batch = None
            return
        if self._batch is not None:
            raise UTXOError("nested UTXO write batch")
        self._batch = self._db.write_batch(transaction=True)

    def _commit(self) -> None:
        if self._batch is not None:
            self._batch.write()
            try:
                self._batch.close()
            except Exception:
                pass
            self._batch = None

    def _abort(self) -> None:
        if self._batch is not None:
            try:
                self._batch.close()
            except Exception:
                pass
            self._batch = None
        if self._db is not None:
            self._mem.clear()
            self._load()

    def _apply_transaction_collect(
        self, tx: Transaction, height: int
    ) -> tuple[list, list[str]]:
        from mhcoin.blockchain.undo import SpentCoin

        spends: list[OutPoint] = []
        spent_coins: list[SpentCoin] = []
        if not tx.is_coinbase():
            for tin in tx.inputs:
                op = OutPoint(txid=tin.prev_txid, vout=tin.prev_vout)
                if not self.has(op):
                    raise UTXOError(f"missing UTXO {op.key()}")
                if op in spends:
                    raise UTXOError(f"double-spend within tx {op.key()}")
                entry = self._mem[op.key()]
                spent_coins.append(
                    SpentCoin(
                        outpoint=op,
                        output=TxOut(
                            value=entry.output.value,
                            script_pubkey=entry.output.script_pubkey,
                        ),
                        height=entry.height,
                        coinbase=entry.coinbase,
                    )
                )
                spends.append(op)

        for op in spends:
            self._remove(op)

        created: list[str] = []
        txid = tx.txid()
        for i, tout in enumerate(tx.outputs):
            entry = UTXOEntry(
                outpoint=OutPoint(txid=txid, vout=i),
                output=TxOut(value=tout.value, script_pubkey=tout.script_pubkey),
                height=height,
                coinbase=tx.is_coinbase(),
            )
            self._add(entry)
            created.append(entry.outpoint.key())
        return spent_coins, created

    def _apply_transaction_no_commit(self, tx: Transaction, height: int) -> None:
        self._apply_transaction_collect(tx, height)

    def _writer(self):
        return self._batch if self._batch is not None else self._db

    def _add(self, entry: UTXOEntry) -> None:
        k = entry.outpoint.key()
        if k in self._mem:
            raise UTXOError(f"UTXO already exists {k}")
        self._mem[k] = entry
        w = self._writer()
        if w is not None:
            w.put(entry.outpoint.db_key(), _encode_coin(entry))

    def _remove(self, outpoint: OutPoint) -> None:
        k = outpoint.key()
        if k not in self._mem:
            raise UTXOError(f"missing UTXO {k}")
        del self._mem[k]
        w = self._writer()
        if w is not None:
            w.delete(outpoint.db_key())

    def balance_for_pubkey_hash(self, pubkey_hash: bytes) -> int:
        return sum(e.output.value for e in self.all_for_pubkey_hash(pubkey_hash))

    def count(self) -> int:
        return len(self._mem)

    def set_meta(self, key: str, value: str) -> None:
        if self._db is None:
            return
        with self._lock:
            self._db.put(
                _PREFIX_META + key.encode("utf-8"), value.encode("utf-8")
            )

    def get_meta(self, key: str) -> str | None:
        if self._db is None:
            return None
        raw = self._db.get(_PREFIX_META + key.encode("utf-8"))
        if raw is None:
            return None
        return raw.decode("utf-8")

    def close(self) -> None:
        if self._share_key is None:
            return
        with _LIVE_LOCK:
            live = _LIVE.get(self._share_key)
            if live is None:
                self._live = None
                self._share_key = None
                return
            live.refs -= 1
            if live.refs <= 0:
                if live.batch is not None:
                    try:
                        live.batch.close()
                    except Exception:
                        pass
                    live.batch = None
                try:
                    live.db.close()
                except Exception:
                    pass
                live.db = None
                del _LIVE[self._share_key]
            self._live = None
            self._share_key = None
