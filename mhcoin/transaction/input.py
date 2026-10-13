"""Transaction input / output models (integer amounts only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from mhcoin.constants import (
    COINBASE_VOUT,
    MAX_SEQUENCE,
    MAX_SUPPLY_SATOSHIS,
    NULL_TXID,
    SCRIPT_P2PKH_VERSION,
)
from mhcoin.transaction.serialization import Reader, write_bytes, write_hash32, write_u32, write_u64


def _check_amount(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("amount must be int (base units), not float/bool")
    if value < 0:
        raise ValueError("negative amount")
    if value > MAX_SUPPLY_SATOSHIS:
        raise ValueError("amount exceeds max supply")
    return value


def _check_u32(n: int, name: str) -> int:
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError(f"{name} must be int")
    if not 0 <= n <= 0xFFFFFFFF:
        raise ValueError(f"{name} out of uint32 range")
    return n


@dataclass
class TxOut:
    """TxOutput: amount in smallest MHC units + locking script."""

    value: int
    script_pubkey: bytes

    def __post_init__(self) -> None:
        self.value = _check_amount(self.value)
        if not isinstance(self.script_pubkey, (bytes, bytearray)):
            raise ValueError("script_pubkey must be bytes")
        self.script_pubkey = bytes(self.script_pubkey)

    def serialize(self) -> bytes:
        return write_u64(self.value) + write_bytes(self.script_pubkey)

    @classmethod
    def deserialize(cls, r: Reader) -> TxOut:
        return cls(value=r.u64(), script_pubkey=r.bytes_blob())

    @staticmethod
    def p2pkh(value: int, pubkey_hash: bytes) -> TxOut:
        if len(pubkey_hash) != 20:
            raise ValueError("pubkey hash must be 20 bytes")
        return TxOut(value=value, script_pubkey=bytes([SCRIPT_P2PKH_VERSION]) + pubkey_hash)

    def pubkey_hash(self) -> bytes:
        if len(self.script_pubkey) != 21 or self.script_pubkey[0] != SCRIPT_P2PKH_VERSION:
            raise ValueError("unsupported scriptPubKey")
        return self.script_pubkey[1:]


@dataclass
class TxIn:
    """TxInput: previous outpoint + unlocking data + sequence."""

    prev_txid: bytes
    prev_vout: int
    script_sig: bytes = field(default_factory=bytes)
    sequence: int = MAX_SEQUENCE

    def __post_init__(self) -> None:
        if not isinstance(self.prev_txid, (bytes, bytearray)) or len(self.prev_txid) != 32:
            raise ValueError("prev_txid must be 32 bytes")
        self.prev_txid = bytes(self.prev_txid)
        self.prev_vout = _check_u32(self.prev_vout, "prev_vout")
        if not isinstance(self.script_sig, (bytes, bytearray)):
            raise ValueError("script_sig must be bytes")
        self.script_sig = bytes(self.script_sig)
        self.sequence = _check_u32(self.sequence, "sequence")

    def is_coinbase(self) -> bool:
        return self.prev_txid == NULL_TXID and self.prev_vout == COINBASE_VOUT

    def serialize(self) -> bytes:
        return (
            write_hash32(self.prev_txid)
            + write_u32(self.prev_vout)
            + write_bytes(self.script_sig)
            + write_u32(self.sequence)
        )

    @classmethod
    def deserialize(cls, r: Reader) -> TxIn:
        return cls(
            prev_txid=r.hash32(),
            prev_vout=r.u32(),
            script_sig=r.bytes_blob(),
            sequence=r.u32(),
        )

    @staticmethod
    def coinbase(height: int, extra: bytes = b"MHCOIN") -> TxIn:
        height_bytes = height.to_bytes(4, "little")
        return TxIn(
            prev_txid=NULL_TXID,
            prev_vout=COINBASE_VOUT,
            script_sig=height_bytes + extra,
            sequence=MAX_SEQUENCE,
        )


TxInput = TxIn
TxOutput = TxOut
