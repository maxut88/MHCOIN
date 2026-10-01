"""Block and transaction consensus validation (local chain)."""

from __future__ import annotations

import time

from mhcoin.blockchain.block import Block
from mhcoin.blockchain.merkle import verify_merkle_root
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.consensus.proof_of_work import verify_proof_of_work
from mhcoin.constants import (
    MAX_BLOCK_SIZE,
    MAX_FUTURE_BLOCK_TIME,
    MAX_TXS_PER_BLOCK,
    SUPPORTED_BLOCK_VERSIONS,
)
from mhcoin.transaction.signing import verify_input
from mhcoin.transaction.transaction import Transaction, fee_of
from mhcoin.utxo import OutPoint, UTXOSet


class ValidationError(Exception):
    pass


def validate_transaction(tx: Transaction, utxo: UTXOSet, *, height: int) -> int:
    """
    Validate tx against UTXO set (do not apply).
    Returns fee in satoshis (0 for coinbase).
    """
    if not tx.inputs or not tx.outputs:
        raise ValidationError("empty inputs/outputs")
    if any(o.value < 0 for o in tx.outputs):
        raise ValidationError("negative output")
    if tx.output_value() < 0:
        raise ValidationError("negative total output")

    if tx.is_coinbase():
        if len(tx.inputs) != 1:
            raise ValidationError("coinbase must have one input")
        # Exact coinbase ceiling (subsidy + fees) is enforced in validate_block.
        return 0

    # Normal txs cannot mint: every input must spend an existing UTXO.
    input_values: list[int] = []
    seen: set[str] = set()
    for i, tin in enumerate(tx.inputs):
        if tin.is_coinbase():
            raise ValidationError("coinbase input only allowed in coinbase tx")
        op = OutPoint(txid=tin.prev_txid, vout=tin.prev_vout)
        if op.key() in seen:
            raise ValidationError(f"double-spend {op.key()}")
        seen.add(op.key())
        entry = utxo.get(op)
        if entry is None:
            raise ValidationError(f"missing UTXO {op.key()}")
        if not verify_input(tx, i, entry.output.script_pubkey, entry.output.value):
            raise ValidationError(f"bad signature on input {i}")
        input_values.append(entry.output.value)

    try:
        fee = fee_of(tx, input_values)
    except ValueError as e:
        raise ValidationError(str(e)) from e
    if fee < 0:
        raise ValidationError("negative fee")
    # Explicit minting ban: outputs cannot exceed inputs (fee_of already enforces).
    if sum(input_values) < tx.output_value():
        raise ValidationError("transaction creates coins")
    return fee


def validate_block(
    block: Block,
    utxo: UTXOSet,
    *,
    height: int,
    expected_prev: bytes,
    expected_bits: int,
    median_time_past: int | None = None,
    pow_limit: int | None = None,
    now: int | None = None,
) -> None:
    """
    Full consensus checks. Does not mutate UTXO.

    ``expected_bits`` must equal ``block.header.bits`` exactly (from get_next_work).
    ``median_time_past`` when set requires timestamp > MTP.
    """
    raw_size = len(block.serialize())
    if raw_size > MAX_BLOCK_SIZE:
        raise ValidationError(f"block exceeds MAX_BLOCK_SIZE ({MAX_BLOCK_SIZE})")

    if block.header.version not in SUPPORTED_BLOCK_VERSIONS:
        raise ValidationError(f"unsupported block version {block.header.version}")

    if block.header.previous_block_hash != expected_prev:
        raise ValidationError("bad previous_block_hash")

    # Exact difficulty schedule — reject easier *or* harder mismatches.
    try:
        bits_to_target(block.header.bits)  # reject malformed compact
        bits_to_target(expected_bits)
    except ValueError as e:
        raise ValidationError(f"invalid difficulty bits: {e}") from e
    if block.header.bits != expected_bits:
        raise ValidationError(
            f"unexpected difficulty bits: got 0x{block.header.bits:08x}, "
            f"expected 0x{expected_bits:08x}"
        )
    if pow_limit is not None:
        target = bits_to_target(block.header.bits)
        if target <= 0:
            raise ValidationError("non-positive PoW target")
        if target > pow_limit:
            raise ValidationError("target exceeds POW_LIMIT")

    if median_time_past is not None:
        if block.header.timestamp <= median_time_past:
            raise ValidationError(
                f"block timestamp {block.header.timestamp} <= median time past {median_time_past}"
            )

    clock = int(time.time()) if now is None else now
    if block.header.timestamp > clock + MAX_FUTURE_BLOCK_TIME:
        raise ValidationError("block timestamp too far in the future")

    if not verify_proof_of_work(block.header):
        raise ValidationError("invalid Proof-of-Work")
    if not block.transactions:
        raise ValidationError("block has no transactions")
    if len(block.transactions) > MAX_TXS_PER_BLOCK:
        raise ValidationError("too many transactions in block")
    if not block.transactions[0].is_coinbase():
        raise ValidationError("first tx must be coinbase")
    for tx in block.transactions[1:]:
        if tx.is_coinbase():
            raise ValidationError("only first tx may be coinbase")

    # Duplicate txids in block
    txids = [tx.txid() for tx in block.transactions]
    if len(set(txids)) != len(txids):
        raise ValidationError("duplicate transaction in block")
    if not verify_merkle_root(txids, block.header.merkle_root):
        raise ValidationError("invalid merkle root")

    # Validate non-coinbase against current UTXO (without applying yet)
    fees = 0
    spent_in_block: set[str] = set()
    for tx in block.transactions[1:]:
        for tin in tx.inputs:
            op = OutPoint(txid=tin.prev_txid, vout=tin.prev_vout)
            if op.key() in spent_in_block:
                raise ValidationError(f"double-spend in block {op.key()}")
            spent_in_block.add(op.key())
        fees += validate_transaction(tx, utxo, height=height)

    subsidy = get_block_subsidy(height)
    coinbase = block.transactions[0]
    if not coinbase.is_coinbase():
        raise ValidationError("invalid coinbase")
    if len(coinbase.outputs) < 1:
        raise ValidationError("coinbase has no outputs")
    max_coinbase = subsidy + fees
    if coinbase.output_value() > max_coinbase:
        raise ValidationError(
            f"excessive block reward: {coinbase.output_value()} > subsidy {subsidy} + fees {fees}"
        )
