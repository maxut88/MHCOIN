"""Flat blocks/blk*.dat storage + migration from sqlite-embedded raw."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from mhcoin.blockchain.chain import SCHEMA_VERSION, Blockchain
from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template
from mhcoin.consensus.proof_of_work import mine_block


def test_new_chain_writes_flat_blk_files(tmp_path: Path):
    chain = Blockchain(tmp_path / "c", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    assert (tmp_path / "c" / "blocks" / "blk00000.dat").is_file()
    g = chain.get_block_by_height(0)
    assert g is not None
    assert chain._meta_get("schema_version") == str(SCHEMA_VERSION)

    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tip = chain.tip_hash
    assert tip is not None
    bits = chain.get_next_work_for_parent(tip)
    block = build_block_template(
        height=1,
        previous_hash=tip,
        timestamp=max(int(time.time()), chain.median_time_past_for_parent(tip) + 1),
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    chain.accept_block(block)
    b1 = chain.get_block_by_hash(block.block_hash())
    assert b1 is not None
    assert b1.block_hash() == block.block_hash()
    # Index row should point at flat file, not embed raw.
    row = chain._db.execute(
        "SELECT length(raw), file_id, data_len FROM block_index WHERE height=1"
    ).fetchone()
    assert row is not None
    assert int(row[0] or 0) == 0
    assert row[1] is not None
    assert int(row[2]) > 0
    chain.close()


def test_migrate_sqlite_raw_into_flat_files(tmp_path: Path):
    """Old v2 datadir (raw in sqlite) upgrades to flat blk files on open."""
    data = tmp_path / "old"
    data.mkdir()
    # Build a small chain with current code first, then stuff raw back into sqlite
    # to simulate pre-v3 layout and force remigration.
    chain = Blockchain(data, network="regtest")
    genesis = get_network_genesis("regtest")
    chain.init_with_genesis(genesis)
    raw0 = genesis.serialize()
    chain.close()

    # Simulate legacy row: clear flat coords, put raw back.
    con = sqlite3.connect(str(data / "chain.sqlite"))
    con.execute(
        "UPDATE block_index SET raw=?, file_id=NULL, data_pos=NULL, data_len=NULL",
        (raw0,),
    )
    con.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES ('schema_version','2')"
    )
    con.commit()
    con.close()
    # Remove flat files so reopen must recreate them from raw.
    blk = data / "blocks" / "blk00000.dat"
    if blk.is_file():
        blk.unlink()

    chain2 = Blockchain(data, network="regtest")
    assert chain2.height == 0
    g = chain2.get_block_by_height(0)
    assert g is not None
    assert g.block_hash() == genesis.block_hash()
    assert (data / "blocks" / "blk00000.dat").is_file()
    row = chain2._db.execute(
        "SELECT length(raw), file_id, data_len FROM block_index WHERE height=0"
    ).fetchone()
    assert int(row[0] or 0) == 0
    assert row[1] is not None
    chain2.close()


def test_readonly_chain_reads_flat_blocks(tmp_path: Path):
    data = tmp_path / "ro"
    chain = Blockchain(data, network="regtest")
    genesis = get_network_genesis("regtest")
    chain.init_with_genesis(genesis)
    tip_hash = chain.tip_hash
    assert tip_hash is not None
    # Ensure raw is empty in index (flat-only).
    row = chain._db.execute(
        "SELECT length(raw), file_id FROM block_index WHERE height=0"
    ).fetchone()
    assert int(row[0] or 0) == 0
    assert row[1] is not None
    chain.close()

    ro = ReadOnlyChain(data)
    assert ro.height == 0
    g = ro.get_block_by_height(0)
    assert g is not None
    assert g.block_hash() == genesis.block_hash()
    g2 = ro.get_block_by_hash(tip_hash)
    assert g2 is not None
    assert g2.block_hash() == tip_hash
    ro.close()
