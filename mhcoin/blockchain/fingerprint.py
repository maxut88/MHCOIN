"""Deterministic chain / UTXO fingerprints for multi-node convergence checks."""

from __future__ import annotations

from mhcoin.crypto.hashing import hash256
from mhcoin.utxo import UTXOSet


def utxo_fingerprint(utxo: UTXOSet) -> bytes:
    """
    HASH256 of sorted deterministic outpoint/output encodings.
    Do not compare SQLite file bytes across nodes.
    """
    parts: list[bytes] = []
    for e in sorted(utxo.all_entries(), key=lambda x: x.outpoint.key()):
        parts.append(
            e.outpoint.txid
            + e.outpoint.vout.to_bytes(4, "little")
            + int(e.output.value).to_bytes(8, "little")
            + e.output.script_pubkey
            + int(e.height).to_bytes(4, "little")
            + (b"\x01" if e.coinbase else b"\x00")
        )
    return hash256(b"".join(parts))


def chain_state_fingerprint(
    *,
    tip_hash: bytes | None,
    height: int,
    chain_work: int,
    utxo: UTXOSet,
) -> dict:
    tip = tip_hash.hex() if tip_hash else None
    return {
        "tip": tip,
        "height": height,
        "chain_work": chain_work,
        "utxo_fingerprint": utxo_fingerprint(utxo).hex(),
    }
