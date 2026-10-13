"""MHCOIN block header and block body."""

from __future__ import annotations

from dataclasses import dataclass, field

from mhcoin.blockchain.merkle import merkle_root
from mhcoin.constants import BLOCK_VERSION, MAX_TXS_PER_BLOCK
from mhcoin.crypto.hashing import hash256
from mhcoin.transaction.serialize import Reader, write_hash32, write_u32, write_varint
from mhcoin.transaction.transaction import Transaction


@dataclass
class BlockHeader:
    version: int = BLOCK_VERSION
    previous_block_hash: bytes = b"\x00" * 32
    merkle_root: bytes = b"\x00" * 32
    timestamp: int = 0
    bits: int = 0  # compact difficulty target (nBits)
    nonce: int = 0

    def serialize(self) -> bytes:
        return (
            write_u32(self.version)
            + write_hash32(self.previous_block_hash)
            + write_hash32(self.merkle_root)
            + write_u32(self.timestamp)
            + write_u32(self.bits)
            + write_u32(self.nonce)
        )

    @classmethod
    def deserialize(cls, data: bytes) -> BlockHeader:
        r = Reader(data)
        h = cls(
            version=r.u32(),
            previous_block_hash=r.hash32(),
            merkle_root=r.hash32(),
            timestamp=r.u32(),
            bits=r.u32(),
            nonce=r.u32(),
        )
        if r.remaining() != 0:
            raise ValueError("trailing header bytes")
        return h

    def block_hash(self) -> bytes:
        return hash256(self.serialize())

    def hash_hex(self) -> str:
        return self.block_hash().hex()


@dataclass
class Block:
    header: BlockHeader
    transactions: list[Transaction] = field(default_factory=list)

    def serialize(self) -> bytes:
        out = self.header.serialize()
        out += write_varint(len(self.transactions))
        for tx in self.transactions:
            raw = tx.serialize()
            out += write_varint(len(raw)) + raw
        return out

    @classmethod
    def deserialize(cls, data: bytes) -> Block:
        r = Reader(data)
        header = BlockHeader(
            version=r.u32(),
            previous_block_hash=r.hash32(),
            merkle_root=r.hash32(),
            timestamp=r.u32(),
            bits=r.u32(),
            nonce=r.u32(),
        )
        n = r.varint()
        if n > MAX_TXS_PER_BLOCK:
            raise ValueError("too many transactions in block")
        txs: list[Transaction] = []
        for _ in range(n):
            raw = r.read(r.varint())
            txs.append(Transaction.deserialize(raw))
        if r.remaining() != 0:
            raise ValueError("trailing block bytes")
        return cls(header=header, transactions=txs)

    def recompute_merkle_root(self) -> bytes:
        return merkle_root([tx.txid() for tx in self.transactions])

    def set_merkle_root(self) -> None:
        self.header.merkle_root = self.recompute_merkle_root()

    def block_hash(self) -> bytes:
        return self.header.block_hash()
