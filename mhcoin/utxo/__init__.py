"""UTXO set: create/spend, double-spend protection, SQLite persistence, undo/rollback."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mhcoin.transaction.input import TxOut
from mhcoin.transaction.transaction import Transaction

if TYPE_CHECKING:
    from mhcoin.blockchain.undo import BlockUndo


@dataclass(frozen=True)
class OutPoint:
    txid: bytes
    vout: int

    def key(self) -> str:
        return f"{self.txid.hex()}:{self.vout}"

    @staticmethod
    def from_key(key: str) -> OutPoint:
        hx, v = key.split(":")
        return OutPoint(txid=bytes.fromhex(hx), vout=int(v))


@dataclass
class UTXOEntry:
    outpoint: OutPoint
    output: TxOut
    height: int
    coinbase: bool


class UTXOError(Exception):
    pass


class UTXOSet:
    def __init__(self, path: Path | None = None):
        self.path = path
        self._mem: dict[str, UTXOEntry] = {}
        self._lock = threading.RLock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=30000")
            except sqlite3.Error:
                pass
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS utxo (
                    outpoint TEXT PRIMARY KEY,
                    txid BLOB NOT NULL,
                    vout INTEGER NOT NULL,
                    value INTEGER NOT NULL,
                    script_pubkey BLOB NOT NULL,
                    height INTEGER NOT NULL,
                    coinbase INTEGER NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()
            self._load()
        else:
            self._conn = None

    def _load(self) -> None:
        assert self._conn is not None
        self._mem.clear()
        for row in self._conn.execute(
            "SELECT outpoint, txid, vout, value, script_pubkey, height, coinbase FROM utxo"
        ):
            op = OutPoint(txid=row[1], vout=row[2])
            self._mem[row[0]] = UTXOEntry(
                outpoint=op,
                output=TxOut(value=row[3], script_pubkey=row[4]),
                height=row[5],
                coinbase=bool(row[6]),
            )

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
        clone._mem = {
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
            if self._conn is not None:
                self._conn.execute("DELETE FROM utxo")
                self._conn.commit()

    def apply_transaction(self, tx: Transaction, height: int) -> None:
        """Spend inputs and create outputs. Commits immediately if DB-backed."""
        with self._lock:
            self._apply_transaction_no_commit(tx, height)
            if self._conn is not None:
                self._conn.commit()

    def apply_block(self, transactions: list[Transaction], height: int) -> None:
        """
        Apply all block transactions atomically.
        On failure: memory and DB are restored; no partial permanent update.
        """
        with self._lock:
            self.apply_block_with_undo(transactions, height)

    def apply_block_with_undo(
        self, transactions: list[Transaction], height: int
    ) -> BlockUndo:
        """
        Apply block atomically and return undo data sufficient for disconnect.
        """
        from mhcoin.blockchain.undo import SpentCoin, block_undo_v1

        with self._lock:
            snapshot = dict(self._mem)
            spent: list[SpentCoin] = []
            created: list[str] = []
            try:
                if self._conn is not None:
                    self._conn.execute("BEGIN IMMEDIATE")
                for tx in transactions:
                    spent_here, created_here = self._apply_transaction_collect(
                        tx, height
                    )
                    spent.extend(spent_here)
                    created.extend(created_here)
                if self._conn is not None:
                    self._conn.commit()
            except Exception:
                self._mem = snapshot
                if self._conn is not None:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                    self._mem.clear()
                    self._load()
                raise
            return block_undo_v1(spent=spent, created=created)

    def disconnect_block(
        self, transactions: list[Transaction], undo: BlockUndo
    ) -> None:
        """
        Reverse apply_block_with_undo using persisted undo (not guesswork from txs).
        `transactions` is kept for sanity checks (created count).
        """
        with self._lock:
            snapshot = dict(self._mem)
            try:
                if self._conn is not None:
                    self._conn.execute("BEGIN IMMEDIATE")
                # Remove outputs created by the block
                for key in undo.created:
                    op = OutPoint.from_key(key)
                    self._remove(op)
                # Restore spent coins (order does not matter for set semantics)
                for coin in undo.spent:
                    self._add(coin.to_entry())
                # Sanity: coinbase + normal outs should match created keys
                expected_created = 0
                for tx in transactions:
                    expected_created += len(tx.outputs)
                if expected_created != len(undo.created):
                    raise UTXOError(
                        f"undo created mismatch: {len(undo.created)} != {expected_created}"
                    )
                if self._conn is not None:
                    self._conn.commit()
            except Exception:
                self._mem = snapshot
                if self._conn is not None:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                    self._mem.clear()
                    self._load()
                raise

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

    def _add(self, entry: UTXOEntry) -> None:
        k = entry.outpoint.key()
        if k in self._mem:
            raise UTXOError(f"UTXO already exists {k}")
        self._mem[k] = entry
        if self._conn is not None:
            self._conn.execute(
                "INSERT INTO utxo VALUES (?,?,?,?,?,?,?)",
                (
                    k,
                    entry.outpoint.txid,
                    entry.outpoint.vout,
                    entry.output.value,
                    entry.output.script_pubkey,
                    entry.height,
                    1 if entry.coinbase else 0,
                ),
            )

    def _remove(self, outpoint: OutPoint) -> None:
        k = outpoint.key()
        if k not in self._mem:
            raise UTXOError(f"missing UTXO {k}")
        del self._mem[k]
        if self._conn is not None:
            self._conn.execute("DELETE FROM utxo WHERE outpoint=?", (k,))

    def balance_for_pubkey_hash(self, pubkey_hash: bytes) -> int:
        return sum(e.output.value for e in self.all_for_pubkey_hash(pubkey_hash))

    def count(self) -> int:
        return len(self._mem)

    def set_meta(self, key: str, value: str) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value)
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
