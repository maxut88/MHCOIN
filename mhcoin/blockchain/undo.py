"""Block undo records for UTXO rollback (Stage 6 reorg).

Versioned JSON — no pickle. Restores exact UTXO state before a block was applied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from mhcoin.transaction.input import TxOut
from mhcoin.utxo import OutPoint, UTXOEntry

UNDO_VERSION = 1


@dataclass
class SpentCoin:
    """A UTXO that was spent by the block — restored on disconnect."""

    outpoint: OutPoint
    output: TxOut
    height: int
    coinbase: bool

    def to_dict(self) -> dict:
        return {
            "txid": self.outpoint.txid.hex(),
            "vout": self.outpoint.vout,
            "value": self.output.value,
            "script_pubkey": self.output.script_pubkey.hex(),
            "height": self.height,
            "coinbase": self.coinbase,
        }

    @staticmethod
    def from_dict(d: dict) -> SpentCoin:
        return SpentCoin(
            outpoint=OutPoint(txid=bytes.fromhex(d["txid"]), vout=int(d["vout"])),
            output=TxOut(
                value=int(d["value"]),
                script_pubkey=bytes.fromhex(d["script_pubkey"]),
            ),
            height=int(d["height"]),
            coinbase=bool(d["coinbase"]),
        )

    def to_entry(self) -> UTXOEntry:
        return UTXOEntry(
            outpoint=self.outpoint,
            output=self.output,
            height=self.height,
            coinbase=self.coinbase,
        )


@dataclass
class BlockUndo:
    """
    Enough information to reverse one block's UTXO transition.

    On disconnect:
      1. Remove all created outpoints
      2. Restore all spent coins
    """

    version: int
    spent: list[SpentCoin]
    created: list[str]  # outpoint keys created by the block

    def serialize(self) -> bytes:
        payload = {
            "version": self.version,
            "spent": [s.to_dict() for s in self.spent],
            "created": list(self.created),
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @staticmethod
    def deserialize(raw: bytes) -> BlockUndo:
        data = json.loads(raw.decode("utf-8"))
        version = int(data.get("version", 0))
        if version != UNDO_VERSION:
            raise ValueError(f"unsupported undo version {version}")
        spent = [SpentCoin.from_dict(s) for s in data.get("spent", [])]
        created = [str(k) for k in data.get("created", [])]
        return BlockUndo(version=version, spent=spent, created=created)


def block_undo_v1(*, spent: list[SpentCoin], created: list[str]) -> BlockUndo:
    return BlockUndo(version=UNDO_VERSION, spent=spent, created=created)
