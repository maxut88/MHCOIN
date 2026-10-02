"""Local mempool — valid txs only; persisted for Milestone 2 (no P2P)."""

from __future__ import annotations

import json
from pathlib import Path

from mhcoin.blockchain.validation import ValidationError, validate_transaction
from mhcoin.transaction.transaction import Transaction
from mhcoin.utxo import OutPoint, UTXOSet


class MempoolError(Exception):
    pass


class Mempool:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._txs: dict[str, Transaction] = {}
        self._spent: set[str] = set()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        if not self.path.is_file():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for hx, hexblob in raw.get("txs", {}).items():
            tx = Transaction.deserialize(bytes.fromhex(hexblob))
            self._txs[hx] = tx
            for tin in tx.inputs:
                self._spent.add(OutPoint(txid=tin.prev_txid, vout=tin.prev_vout).key())

    def _save(self) -> None:
        if self.path is None:
            return
        data = {"txs": {hx: tx.serialize().hex() for hx, tx in self._txs.items()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, tx: Transaction, utxo: UTXOSet, *, height: int) -> str:
        if tx.is_coinbase():
            raise MempoolError("coinbase not allowed in mempool")
        txid = tx.txid()
        hx = txid.hex()
        if hx in self._txs:
            raise MempoolError("duplicate transaction")
        for tin in tx.inputs:
            op = OutPoint(txid=tin.prev_txid, vout=tin.prev_vout)
            if op.key() in self._spent:
                raise MempoolError(f"mempool double-spend {op.key()}")
        try:
            validate_transaction(tx, utxo, height=height)
        except ValidationError as e:
            raise MempoolError(str(e)) from e
        for tin in tx.inputs:
            self._spent.add(OutPoint(txid=tin.prev_txid, vout=tin.prev_vout).key())
        self._txs[hx] = tx
        self._save()
        return hx

    def get(self, txid_hex: str) -> Transaction | None:
        return self._txs.get(txid_hex)

    def contains(self, txid_hex: str) -> bool:
        return txid_hex in self._txs

    def reload(self) -> None:
        """Re-read mempool.json from disk (wallet submit → running node)."""
        if self.path is None or not self.path.is_file():
            return
        self._txs.clear()
        self._spent.clear()
        self._load()

    def list_txs(self) -> list[Transaction]:
        return list(self._txs.values())

    def remove(self, txid_hex: str) -> None:
        tx = self._txs.pop(txid_hex, None)
        if not tx:
            return
        for tin in tx.inputs:
            self._spent.discard(OutPoint(txid=tin.prev_txid, vout=tin.prev_vout).key())
        self._save()

    def clear_included(self, txs: list[Transaction]) -> None:
        for tx in txs:
            self.remove(tx.txid_hex())

    def spent_keys(self) -> set[str]:
        return set(self._spent)

    def evict_spent_on_chain(self, utxo: UTXOSet) -> int:
        """Drop mempool txs whose inputs are already gone from the UTXO set
        (confirmed in a block or otherwise spent on disk)."""
        drop: list[str] = []
        for hx, tx in list(self._txs.items()):
            for tin in tx.inputs:
                op = OutPoint(txid=tin.prev_txid, vout=tin.prev_vout)
                if not utxo.has(op):
                    drop.append(hx)
                    break
        for hx in drop:
            self.remove(hx)
        return len(drop)

    def __len__(self) -> int:
        return len(self._txs)
