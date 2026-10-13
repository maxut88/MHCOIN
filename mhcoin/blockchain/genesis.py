"""Network genesis construction, freeze verification, and validation."""

from __future__ import annotations

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.consensus.block_reward import get_block_subsidy
from mhcoin.consensus.params import (
    BLOCK_VERSION,
    NetworkParams,
    get_network_params,
)
from mhcoin.consensus.proof_of_work import mine_block, verify_proof_of_work
from mhcoin.transaction.input import TxIn, TxOut
from mhcoin.transaction.transaction import Transaction


def build_genesis_coinbase(
    *,
    pubkey_hash: bytes,
    extra: bytes,
    height: int = 0,
) -> Transaction:
    subsidy = get_block_subsidy(height)
    return Transaction(
        inputs=[TxIn.coinbase(height, extra=extra)],
        outputs=[TxOut.p2pkh(subsidy, pubkey_hash)],
        locktime=0,
    )


def build_genesis_template(params: NetworkParams, *, pubkey_hash: bytes | None = None) -> Block:
    pkh = pubkey_hash if pubkey_hash is not None else params.genesis_pubkey_hash
    coinbase = build_genesis_coinbase(pubkey_hash=pkh, extra=params.genesis_coinbase_extra)
    block = Block(
        header=BlockHeader(
            version=BLOCK_VERSION,
            previous_block_hash=b"\x00" * 32,
            merkle_root=b"\x00" * 32,
            timestamp=params.genesis_timestamp,
            bits=params.genesis_bits,
            nonce=0,
        ),
        transactions=[coinbase],
    )
    block.set_merkle_root()
    return block


def mine_genesis(params: NetworkParams, *, pubkey_hash: bytes | None = None) -> Block:
    """Mine genesis for params (tooling). Does not mutate frozen constants."""
    block = build_genesis_template(params, pubkey_hash=pubkey_hash)
    return mine_block(block)


def get_network_genesis(network: str | NetworkParams) -> Block:
    """
    Return the frozen canonical genesis for a network.
    Never mines. Raises if frozen constants do not match the template.
    """
    params = get_network_params(network) if isinstance(network, str) else network
    block = build_genesis_template(params)
    block.header.nonce = params.genesis_nonce
    if block.header.merkle_root.hex() != params.genesis_merkle_hex:
        raise RuntimeError(
            f"{params.name} genesis merkle mismatch — frozen constants outdated"
        )
    if block.block_hash().hex() != params.genesis_hash_hex:
        raise RuntimeError(
            f"{params.name} genesis hash mismatch — remine via genesis tool and freeze"
        )
    if not verify_proof_of_work(block.header):
        raise RuntimeError(f"{params.name} genesis PoW invalid")
    return block


def get_shared_regtest_genesis() -> Block:
    """Backward-compatible alias for localnet/regtest shared genesis."""
    return get_network_genesis("regtest")


def mine_regtest_genesis(*, pubkey_hash: bytes) -> Block:
    """Mine a regtest genesis paying an arbitrary pubkey (local tooling only)."""
    params = get_network_params("regtest")
    return mine_genesis(params, pubkey_hash=pubkey_hash)


def validate_genesis_structure(
    block: Block,
    *,
    bits: int,
    timestamp: int | None = None,
) -> None:
    """Structural genesis checks (PoW, merkle, coinbase). Used by regtest custom bootstrap."""
    if block.header.previous_block_hash != b"\x00" * 32:
        raise ValueError("genesis previous hash must be zero")
    if timestamp is not None and block.header.timestamp != timestamp:
        raise ValueError("unexpected genesis timestamp")
    if block.header.bits != bits:
        raise ValueError("unexpected genesis bits")
    if block.header.merkle_root != block.recompute_merkle_root():
        raise ValueError("invalid genesis merkle root")
    if not verify_proof_of_work(block.header):
        raise ValueError("invalid genesis PoW")
    if not block.transactions or not block.transactions[0].is_coinbase():
        raise ValueError("genesis must have coinbase")
    if block.transactions[0].outputs[0].value != get_block_subsidy(0):
        raise ValueError("genesis subsidy mismatch")


def validate_genesis_block(block: Block, network: str | NetworkParams = "regtest") -> None:
    """
    Validate a block is the **frozen** expected genesis for `network`.
    Mainnet/testnet always use this path.
    """
    params = get_network_params(network) if isinstance(network, str) else network
    validate_genesis_structure(
        block, bits=params.genesis_bits, timestamp=params.genesis_timestamp
    )
    if block.header.nonce != params.genesis_nonce:
        raise ValueError("unexpected genesis nonce")
    if block.header.merkle_root.hex() != params.genesis_merkle_hex:
        raise ValueError("genesis merkle does not match frozen network params")
    if block.block_hash().hex() != params.genesis_hash_hex:
        raise ValueError("genesis hash does not match frozen network params")
    cb = block.transactions[0]
    if params.genesis_coinbase_extra not in cb.inputs[0].script_sig:
        raise ValueError("genesis coinbase extra mismatch")
    if cb.outputs[0].pubkey_hash() != params.genesis_pubkey_hash:
        raise ValueError("genesis coinbase pubkey hash mismatch")


def verify_frozen_genesis(network: str) -> dict:
    """Rebuild + verify frozen genesis; return summary dict (for CLI/tests)."""
    params = get_network_params(network)
    block = get_network_genesis(params)
    validate_genesis_block(block, params)
    return {
        "network": params.name,
        "genesis_hash": block.block_hash().hex(),
        "merkle_root": block.header.merkle_root.hex(),
        "nonce": block.header.nonce,
        "timestamp": block.header.timestamp,
        "bits": params.genesis_bits,
        "bits_hex": f"0x{params.genesis_bits:08x}",
        "magic": params.magic.hex(),
        "default_port": params.default_port,
        "pubkey_hash": params.genesis_pubkey_hash.hex(),
        "verified": True,
    }
