"""MHCOIN UTXO-model transaction: version, inputs, outputs, locktime."""

from __future__ import annotations

from dataclasses import dataclass, field

from mhcoin.constants import SIGHASH_ALL, TX_VERSION
from mhcoin.crypto.hashing import hash256
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.serialization import Reader, SerializationError, write_u32, write_varint


@dataclass
class Transaction:
    version: int = TX_VERSION
    inputs: list[TxIn] = field(default_factory=list)
    outputs: list[TxOut] = field(default_factory=list)
    locktime: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise ValueError("version must be int")
        if not 0 <= self.version <= 0xFFFFFFFF:
            raise ValueError("version out of uint32 range")
        if not isinstance(self.locktime, int) or isinstance(self.locktime, bool):
            raise ValueError("locktime must be int")
        if not 0 <= self.locktime <= 0xFFFFFFFF:
            raise ValueError("locktime out of uint32 range")
        if not isinstance(self.inputs, list) or not isinstance(self.outputs, list):
            raise ValueError("inputs/outputs must be lists")

    def is_coinbase(self) -> bool:
        return len(self.inputs) == 1 and self.inputs[0].is_coinbase()

    def serialize(self) -> bytes:
        """Canonical full serialization (used for txid)."""
        out = write_u32(self.version)
        out += write_varint(len(self.inputs))
        for tin in self.inputs:
            out += tin.serialize()
        out += write_varint(len(self.outputs))
        for tout in self.outputs:
            out += tout.serialize()
        out += write_u32(self.locktime)
        return out

    @classmethod
    def deserialize(cls, data: bytes) -> Transaction:
        if not isinstance(data, (bytes, bytearray)):
            raise SerializationError("transaction data must be bytes")
        r = Reader(bytes(data))
        version = r.u32()
        n_in = r.varint()
        if n_in > 100_000:
            raise SerializationError("input count too large")
        inputs = [TxIn.deserialize(r) for _ in range(n_in)]
        n_out = r.varint()
        if n_out > 100_000:
            raise SerializationError("output count too large")
        outputs = [TxOut.deserialize(r) for _ in range(n_out)]
        locktime = r.u32()
        if r.remaining() != 0:
            raise SerializationError("trailing bytes after transaction")
        return cls(version=version, inputs=inputs, outputs=outputs, locktime=locktime)

    def txid(self) -> bytes:
        """txid = double-SHA256(canonical_serialized_transaction)."""
        return hash256(self.serialize())

    def txid_hex(self) -> str:
        return self.txid().hex()

    def output_value(self) -> int:
        total = 0
        for o in self.outputs:
            total += o.value
            if total > (1 << 64) - 1:
                raise ValueError("output sum overflow")
        return total

    def copy_blank_sigs(self) -> Transaction:
        return Transaction(
            version=self.version,
            inputs=[
                TxIn(
                    prev_txid=i.prev_txid,
                    prev_vout=i.prev_vout,
                    script_sig=b"",
                    sequence=i.sequence,
                )
                for i in self.inputs
            ],
            outputs=[TxOut(value=o.value, script_pubkey=o.script_pubkey) for o in self.outputs],
            locktime=self.locktime,
        )

    def sighash(self, input_index: int, script_pubkey: bytes, *, value: int) -> bytes:
        """MHCOIN SIGHASH_ALL digest — full docs in later STEP 2 / TRANSACTIONS.md."""
        if not 0 <= input_index < len(self.inputs):
            raise IndexError("input_index out of range")
        tmp = self.copy_blank_sigs()
        tmp.inputs[input_index].script_sig = script_pubkey
        payload = tmp.serialize() + write_u32(SIGHASH_ALL) + int(value).to_bytes(8, "little")
        return hash256(payload)


def serialize_transaction(tx: Transaction) -> bytes:
    """Public API: deterministic binary serialization."""
    return tx.serialize()


def deserialize_transaction(data: bytes) -> Transaction:
    """Public API: parse canonical bytes into Transaction."""
    return Transaction.deserialize(data)


def fee_of(tx: Transaction, input_values: list[int]) -> int:
    """fee = sum(inputs) - sum(outputs). Raises if outputs exceed inputs."""
    if tx.is_coinbase():
        return 0
    if len(input_values) != len(tx.inputs):
        raise ValueError("input_values length mismatch")
    for v in input_values:
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError("input values must be non-negative int")
    total_in = sum(input_values)
    total_out = tx.output_value()
    if total_out > total_in:
        raise ValueError("outputs exceed inputs")
    return total_in - total_out
