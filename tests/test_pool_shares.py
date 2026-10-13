"""Unit tests for pool extranonce / merkle / share helpers."""

from __future__ import annotations

from mhcoin.blockchain.block import Block, BlockHeader
from mhcoin.constants import BLOCK_VERSION
from mhcoin.crypto.hashing import hash256
from mhcoin.mining.block_template import create_coinbase
from mhcoin.mining.share import (
    apply_extranonces,
    assemble_block_from_share,
    build_pool_job,
    hash_meets_share,
    merkle_branch,
    merkle_root_from_branch,
    pool_coinbase_extra,
    share_target,
)


def _fake_template(height: int = 10) -> Block:
    pkh = bytes.fromhex("11" * 20)
    cb = create_coinbase(
        height=height,
        fees=0,
        pubkey_hash=pkh,
        extra=pool_coinbase_extra(b"\x00" * 4, b"\x00" * 4),
    )
    # second dummy tx-id-like entry via another coinbase-shaped? use empty non-cb not needed —
    # single-tx block is enough for branch=[] path; add a second fake tx by cloning structure
    # Simplest: one-tx block.
    block = Block(
        header=BlockHeader(
            version=BLOCK_VERSION,
            previous_block_hash=b"\x22" * 32,
            merkle_root=b"\x00" * 32,
            timestamp=1_700_000_000,
            bits=0x1F0FFFFF,  # regtest-easy; used for share math
            nonce=0,
        ),
        transactions=[cb],
    )
    block.set_merkle_root()
    return block


def test_pool_coinbase_extra_roundtrip():
    en1 = b"\x01\x02\x03\x04"
    en2 = b"\xaa\xbb\xcc\xdd"
    extra = pool_coinbase_extra(en1, en2)
    assert extra.startswith(b"MHCP")
    assert en1 in extra and en2 in extra


def test_merkle_branch_index0():
    leaves = [hash256(bytes([i])) for i in range(5)]
    branch = merkle_branch(leaves, 0)
    root = merkle_root_from_branch(leaves[0], branch, 0)
    from mhcoin.blockchain.merkle import merkle_root

    assert root == merkle_root(leaves)


def test_apply_extranonces_changes_txid():
    block = _fake_template()
    en1 = b"\x10\x11\x12\x13"
    a = apply_extranonces(block, en1, b"\x00" * 4)
    b = apply_extranonces(block, en1, b"\xff" * 4)
    assert a.transactions[0].txid() != b.transactions[0].txid()
    assert a.header.merkle_root != b.header.merkle_root


def test_build_job_and_assemble_share():
    template = _fake_template(height=42)
    en1 = b"\xde\xad\xbe\xef"
    job = build_pool_job(template, job_id="j1", extranonce1=en1, share_factor=1_000_000)
    assert job.height == 42
    assert job.coinb1 and job.coinb2
    en2 = b"\x01\x02\x03\x04"
    block = assemble_block_from_share(job, extranonce1=en1, extranonce2=en2, nonce=123)
    assert block.header.nonce == 123
    # With huge share_factor, zero nonce often meets share on easy bits — just check helpers.
    st = share_target(job.nbits, job.share_factor)
    assert st > 0
    bh = block.block_hash()
    # hash_meets_share must agree with integer compare
    ok = hash_meets_share(bh, job.nbits, job.share_factor)
    assert ok == (int.from_bytes(bh, "little") <= st)


def test_create_coinbase_accepts_extra():
    tx = create_coinbase(height=7, fees=0, pubkey_hash=b"\x00" * 20, extra=b"MHCPTEST")
    assert tx.is_coinbase()
    assert b"MHCPTEST" in tx.inputs[0].script_sig
