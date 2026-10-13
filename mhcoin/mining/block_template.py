"""Build a block from mempool + coinbase."""

from __future__ import annotations

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.constants import BLOCK_VERSION
from mhcoin.mempool import Mempool
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction
from mhcoin.utxo import UTXOSet


def create_coinbase(
    *,
    height: int,
    fees: int,
    pubkey_hash: bytes,
    extra: bytes = b"MHCOIN",
) -> Transaction:
    reward = get_block_subsidy(height) + fees
    return Transaction(
        inputs=[TxIn.coinbase(height, extra=extra)],
        outputs=[TxOut.p2pkh(reward, pubkey_hash)],
    )


def build_block_template(
    *,
    height: int,
    previous_hash: bytes,
    timestamp: int,
    bits: int,
    mempool: Mempool,
    utxo: UTXOSet,
    miner_pubkey_hash: bytes,
    max_txs: int = 1000,
    coinbase_extra: bytes = b"MHCOIN",
) -> Block:
    """Select mempool txs against a working UTXO that applies each accepted spend.

    Validates with existing ``validate_transaction``, then applies the tx to a
    memory clone so dependent txs see prior outputs and double-spends fail.
    """
    from mhcoin.blockchain.validation import validate_transaction

    working = utxo.clone_memory()
    selected: list[Transaction] = []
    fees = 0
    for tx in mempool.list_txs()[:max_txs]:
        try:
            fee = validate_transaction(tx, working, height=height)
            working.apply_transaction(tx, height)
        except Exception:
            continue
        selected.append(tx)
        fees += fee

    coinbase = create_coinbase(
        height=height,
        fees=fees,
        pubkey_hash=miner_pubkey_hash,
        extra=coinbase_extra,
    )
    block = Block(
        header=BlockHeader(
            version=BLOCK_VERSION,
            previous_block_hash=previous_hash,
            merkle_root=b"\x00" * 32,
            timestamp=timestamp,
            bits=bits,
            nonce=0,
        ),
        transactions=[coinbase, *selected],
    )
    block.set_merkle_root()
    return block
