"""Pool share / job helpers (extranonce, merkle branch, share target).

Outside consensus fingerprint — used by ``mhcoin.pool`` and CLI pool miners.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.blockchain.merkle import merkle_root
from mhcoin.consensus.difficulty import bits_to_target
from mhcoin.crypto.hashing import hash256
from mhcoin.transaction.input import TxIn
from mhcoin.transaction.transaction import Transaction

POOL_COINBASE_TAG = b"MHCP"
EXTRANONCE1_SIZE = 4
EXTRANONCE2_SIZE = 4


def pool_coinbase_extra(extranonce1: bytes, extranonce2: bytes) -> bytes:
    if len(extranonce1) != EXTRANONCE1_SIZE:
        raise ValueError(f"extranonce1 must be {EXTRANONCE1_SIZE} bytes")
    if len(extranonce2) != EXTRANONCE2_SIZE:
        raise ValueError(f"extranonce2 must be {EXTRANONCE2_SIZE} bytes")
    return POOL_COINBASE_TAG + extranonce1 + extranonce2


def share_target(network_bits: int, share_factor: int) -> int:
    """Easier target for shares: network_target * share_factor (share_factor >= 1)."""
    factor = max(1, int(share_factor))
    return bits_to_target(network_bits) * factor


def hash_meets_share(block_hash: bytes, network_bits: int, share_factor: int) -> bool:
    value = int.from_bytes(block_hash, "little")
    return value <= share_target(network_bits, share_factor)


def merkle_branch(txids: list[bytes], index: int = 0) -> list[bytes]:
    """Sibling hashes to recompute merkle root from ``txids[index]``."""
    if not txids:
        raise ValueError("empty txid list")
    if index < 0 or index >= len(txids):
        raise ValueError("txid index out of range")
    branch: list[bytes] = []
    idx = int(index)
    level = list(txids)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        if idx % 2 == 0:
            sibling = level[idx + 1]
        else:
            sibling = level[idx - 1]
        branch.append(sibling)
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            nxt.append(hash256(level[i] + level[i + 1]))
        level = nxt
        idx //= 2
    return branch


def merkle_root_from_branch(leaf: bytes, branch: list[bytes], index: int = 0) -> bytes:
    h = leaf
    idx = int(index)
    for sibling in branch:
        if idx % 2 == 0:
            h = hash256(h + sibling)
        else:
            h = hash256(sibling + h)
        idx //= 2
    return h


def _replace_coinbase_extra(coinbase: Transaction, extra: bytes) -> Transaction:
    if not coinbase.is_coinbase() or not coinbase.inputs:
        raise ValueError("not a coinbase")
    height = int.from_bytes(coinbase.inputs[0].script_sig[:4], "little")
    new_in = TxIn.coinbase(height, extra=extra)
    return Transaction(inputs=[new_in], outputs=list(coinbase.outputs))


def apply_extranonces(block: Block, extranonce1: bytes, extranonce2: bytes) -> Block:
    """Return a copy of ``block`` with coinbase extranonces applied + merkle fixed."""
    out = copy.deepcopy(block)
    extra = pool_coinbase_extra(extranonce1, extranonce2)
    out.transactions[0] = _replace_coinbase_extra(out.transactions[0], extra)
    out.set_merkle_root()
    return out


@dataclass
class PoolJob:
    job_id: str
    height: int
    version: int
    prevhash: bytes
    coinb1: bytes  # coinbase tx bytes before extranonce2
    coinb2: bytes  # after extranonce2
    merkle_branches: list[bytes]
    nbits: int
    ntime: int
    share_factor: int
    # Server-side template (extranonce2 = zeros); not sent to miner as blob.
    template: Block
    reward_sats: int


def _split_coinbase_at_extranonce2(coinbase: Transaction, extranonce1: bytes) -> tuple[bytes, bytes]:
    """Serialize coinbase with en2=zeros; split so en2 sits between coinb1 and coinb2."""
    zeros = b"\x00" * EXTRANONCE2_SIZE
    extra = pool_coinbase_extra(extranonce1, zeros)
    height = int.from_bytes(coinbase.inputs[0].script_sig[:4], "little")
    fixed = Transaction(
        inputs=[TxIn.coinbase(height, extra=extra)],
        outputs=list(coinbase.outputs),
    )
    raw = fixed.serialize()
    needle = POOL_COINBASE_TAG + extranonce1 + zeros
    pos = raw.find(needle)
    if pos < 0:
        raise ValueError("extranonce marker missing from coinbase")
    en2_at = pos + len(POOL_COINBASE_TAG) + EXTRANONCE1_SIZE
    return raw[:en2_at], raw[en2_at + EXTRANONCE2_SIZE :]


def build_pool_job(
    template: Block,
    *,
    job_id: str,
    extranonce1: bytes,
    share_factor: int,
) -> PoolJob:
    """Build a miner job from a pool coinbase template (any placeholder extra)."""
    # Normalize template coinbase to this worker's extranonce1 + zero en2.
    zeros = b"\x00" * EXTRANONCE2_SIZE
    base = apply_extranonces(template, extranonce1, zeros)
    txids = [tx.txid() for tx in base.transactions]
    branches = merkle_branch(txids, 0) if len(txids) > 1 else []
    # When only coinbase, branch is empty and root == hash256(txid) via merkle_root.
    if len(txids) == 1:
        branches = []
    coinb1, coinb2 = _split_coinbase_at_extranonce2(base.transactions[0], extranonce1)
    reward = int(base.transactions[0].outputs[0].value) if base.transactions[0].outputs else 0
    return PoolJob(
        job_id=job_id,
        height=int.from_bytes(base.transactions[0].inputs[0].script_sig[:4], "little"),
        version=base.header.version,
        prevhash=base.header.previous_block_hash,
        coinb1=coinb1,
        coinb2=coinb2,
        merkle_branches=branches,
        nbits=base.header.bits,
        ntime=base.header.timestamp,
        share_factor=max(1, int(share_factor)),
        template=base,
        reward_sats=reward,
    )


def assemble_block_from_share(
    job: PoolJob,
    *,
    extranonce1: bytes,
    extranonce2: bytes,
    nonce: int,
    ntime: int | None = None,
) -> Block:
    """Rebuild full block for a submitted share."""
    block = apply_extranonces(job.template, extranonce1, extranonce2)
    # Recompute merkle from coinbase + branches for consistency check.
    leaf = block.transactions[0].txid()
    if job.merkle_branches:
        root = merkle_root_from_branch(leaf, job.merkle_branches, 0)
    else:
        root = merkle_root([leaf])
    block.header.merkle_root = root
    block.header.nonce = int(nonce) & 0xFFFFFFFF
    if ntime is not None:
        block.header.timestamp = int(ntime)
    # Ensure merkle matches full tree (authoritative).
    block.set_merkle_root()
    return block


def job_to_wire(job: PoolJob, extranonce1: bytes) -> dict:
    return {
        "job_id": job.job_id,
        "height": job.height,
        "version": job.version,
        "prevhash": job.prevhash.hex(),
        "coinb1": job.coinb1.hex(),
        "coinb2": job.coinb2.hex(),
        "extranonce1": extranonce1.hex(),
        "extranonce2_size": EXTRANONCE2_SIZE,
        "merkle_branches": [b.hex() for b in job.merkle_branches],
        "nbits": f"{job.nbits:08x}",
        "ntime": f"{job.ntime:08x}",
        "share_factor": job.share_factor,
    }


def header_from_parts(
    *,
    version: int,
    prevhash: bytes,
    merkle_root: bytes,
    ntime: int,
    nbits: int,
    nonce: int,
) -> BlockHeader:
    return BlockHeader(
        version=version,
        previous_block_hash=prevhash,
        merkle_root=merkle_root,
        timestamp=ntime,
        bits=nbits,
        nonce=nonce,
    )


def coinbase_txid_from_parts(coinb1: bytes, extranonce2: bytes, coinb2: bytes) -> bytes:
    raw = coinb1 + extranonce2 + coinb2
    # txid = hash256(tx) — same as Transaction.txid via serialize
    return hash256(raw)
