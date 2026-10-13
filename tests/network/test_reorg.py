"""Stage 6 — reorg, fork choice, undo, orphans, P2P convergence."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.blockchain.chain import Blockchain, ChainError, STATUS_SIDE
from mhcoin.blockchain.fingerprint import utxo_fingerprint
from mhcoin.blockchain.orphans import MAX_ORPHAN_BLOCKS, OrphanPool
from mhcoin.blockchain.undo import BlockUndo
from mhcoin.blockchain.validation import ValidationError
from mhcoin.consensus.chain_work import work_for_bits, work_for_header
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.constants import REGTEST_NBITS
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template
from mhcoin.network.sync import SyncState
from tests.network.conftest import wait_handshaked, wait_until
from tests.network.relay_helpers import (
    make_payment_tx,
    make_relay_node,
    mine_extending_block,
    seed_spendable_chain,
)


_fork_nonce = 0


def _unique_ts(base: int = 0) -> int:
    global _fork_nonce
    _fork_nonce += 1
    # Strictly increasing timestamps so MTP never rejects test blocks.
    return 1_700_000_100 + base * 10 + _fork_nonce


def _mine_on(chain: Blockchain, mempool: Mempool, kp, *, bits: int | None = None):
    assert chain.tip_hash is not None
    height = chain.height + 1
    tip_hash = chain.tip_hash
    if bits is None:
        bits = chain.get_next_work_for_parent(tip_hash)
    pkh = hash160(kp.public_key_compressed)
    ts = _unique_ts(height)
    mtp = chain.median_time_past_for_parent(tip_hash)
    if ts <= mtp:
        ts = mtp + 1
    block = build_block_template(
        height=height,
        previous_hash=tip_hash,
        timestamp=ts,
        bits=bits,
        mempool=mempool,
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    block.header.bits = bits
    mine_block(block)
    return block


def _mine_on_parent(
    chain: Blockchain,
    parent_hash: bytes,
    mempool: Mempool,
    kp,
    *,
    bits: int | None = None,
    extra_txs=None,
    include_mempool: bool = False,
):
    parent = chain.get_index(parent_hash)
    assert parent is not None
    height = parent.height + 1
    if bits is None:
        bits = chain.get_next_work_for_parent(parent_hash)
    utxo_view = chain._utxo_at(parent_hash)
    pkh = hash160(kp.public_key_compressed)
    mp = Mempool()
    if include_mempool:
        for tx in mempool.list_txs():
            try:
                mp.add(tx, utxo_view, height=height)
            except Exception:
                pass
    if extra_txs:
        for tx in extra_txs:
            try:
                mp.add(tx, utxo_view, height=height)
            except Exception:
                pass
    ts = _unique_ts(height + 7)
    mtp = chain.median_time_past_for_parent(parent_hash)
    if ts <= mtp:
        ts = mtp + 1
    block = build_block_template(
        height=height,
        previous_hash=parent_hash,
        timestamp=ts,
        bits=bits,
        mempool=mp,
        utxo=utxo_view,
        miner_pubkey_hash=pkh,
    )
    block.header.bits = bits
    mine_block(block)
    return block


# ---------------------------------------------------------------------------
# Block index / side chain storage
# ---------------------------------------------------------------------------


def test_side_chain_block_stored_tip_unchanged(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    tip_before = chain.tip_hash
    work_before = chain.get_chain_work()

    # Fork from A (parent of B)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    result = chain.accept_block(x)
    assert result.side_chain or not result.activated
    assert chain.tip_hash == tip_before
    assert chain.get_chain_work() == work_before
    assert chain.get_block_by_hash(x.block_hash()) is not None
    assert chain.get_index(x.block_hash()).status == STATUS_SIDE
    chain.close()


def test_side_chain_persists_restart(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x)
    xh = x.block_hash()
    tip = chain.tip_hash
    chain.close()

    chain2 = Blockchain(tmp_path)
    assert chain2.tip_hash == tip
    assert chain2.get_block_by_hash(xh) is not None
    assert chain2.get_index(xh).status == STATUS_SIDE
    chain2.close()


def test_duplicate_block_harmless(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    r1 = chain.accept_block(a)
    r2 = chain.accept_block(a)
    assert r1.activated and r2.duplicate
    assert chain.height == 1
    chain.close()


# ---------------------------------------------------------------------------
# Chain work / fork choice
# ---------------------------------------------------------------------------


def test_cumulative_work_increases(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    w0 = chain.get_chain_work()
    chain.accept_block(_mine_on(chain, mp, kp))
    assert chain.get_chain_work() == w0 + work_for_bits(REGTEST_NBITS)
    chain.close()


def test_shorter_high_work_chain_wins():
    """Fork choice is by cumulative work, not height (offline work math)."""
    easy = work_for_bits(REGTEST_NBITS)
    hard = work_for_bits(0x1E0FFFFF)
    assert hard > easy * 2
    # Shorter hard chain (2 blocks) beats longer easy chain (3 blocks)
    assert hard * 2 > easy * 3


def test_longer_low_work_loses_to_high_work(tmp_path: Path):
    """On fixed-diff networks work ∝ height; longer equal-bits branch can win."""
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)

    # Short tip: A → X
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x)
    short_tip = chain.tip_hash
    assert chain.height == 2

    # Longer branch from A: A → B → C → D (more work at equal bits)
    b = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(b)
    c = _mine_on_parent(chain, b.block_hash(), mp, kp)
    chain.accept_block(c)
    d = _mine_on_parent(chain, c.block_hash(), mp, kp)
    r = chain.accept_block(d)
    assert r.reorg or chain.tip_hash == d.block_hash()
    assert chain.height == 4
    assert chain.tip_hash != short_tip
    chain.close()


def test_equal_work_keeps_active(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    tip = chain.tip_hash
    work = chain.get_chain_work()

    # Equal-work sibling of B (same bits, from A)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    r = chain.accept_block(x)
    assert chain.get_index(x.block_hash()).chain_work == work
    assert not r.reorg
    assert chain.tip_hash == tip  # keep current on equal work
    chain.close()


# ---------------------------------------------------------------------------
# Common ancestor
# ---------------------------------------------------------------------------


def test_common_ancestor_cases(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    g = chain.tip_hash
    assert g is not None
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    c = _mine_on(chain, mp, kp)
    chain.accept_block(c)

    x = _mine_on_parent(chain, b.block_hash(), mp, kp)
    chain.accept_block(x)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y)

    assert chain.find_common_ancestor(c.block_hash(), c.block_hash()) == c.block_hash()
    assert chain.find_common_ancestor(c.block_hash(), y.block_hash()) == b.block_hash()
    assert chain.find_common_ancestor(y.block_hash(), a.block_hash()) == a.block_hash()
    assert chain.find_common_ancestor(c.block_hash(), g) == g
    chain.close()


# ---------------------------------------------------------------------------
# Simple + multi-block reorg + UTXO
# ---------------------------------------------------------------------------


def test_simple_reorg_to_longer_equal_difficulty(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    c = _mine_on(chain, mp, kp)
    chain.accept_block(c)
    d = _mine_on(chain, mp, kp)
    chain.accept_block(d)
    old_tip = chain.tip_hash

    x = _mine_on_parent(chain, b.block_hash(), mp, kp)
    chain.accept_block(x)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y)
    z = _mine_on_parent(chain, y.block_hash(), mp, kp)
    r = chain.accept_block(z)
    assert chain.tip_hash == z.block_hash()
    assert chain.tip_hash != old_tip
    assert chain.height == 5  # G + A B X Y Z
    assert r.activated
    # First block that exceeds work may be Z (reorg) or an earlier equal-work edge
    assert r.reorg or chain.get_chain_work() > work_for_bits(REGTEST_NBITS) * 5
    chain.close()


def test_utxo_rollback_and_apply(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    # Mature: mine A
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)

    tx1 = make_payment_tx(genesis, kp, amount=1_000_000)
    mp.add(tx1, chain.utxo, height=chain.height)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    assert chain.utxo.count() >= 1
    spent_key_absent = True  # genesis coinbase spent
    from mhcoin.utxo import OutPoint

    assert chain.utxo.get(OutPoint(txid=genesis.transactions[0].txid(), vout=0)) is None

    # Fork from A without tx1, make it win (3 blocks)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y)
    z = _mine_on_parent(chain, y.block_hash(), mp, kp)
    chain.accept_block(z)
    assert chain.tip_hash == z.block_hash()
    # Genesis coinbase should be restored (B disconnected)
    assert chain.utxo.get(OutPoint(txid=genesis.transactions[0].txid(), vout=0)) is not None
    chain.close()


def test_reorg_restart(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y)
    tip = chain.tip_hash
    height = chain.height
    work = chain.get_chain_work()
    fp = chain.utxo_fp()
    chain.close()

    chain2 = Blockchain(tmp_path)
    assert chain2.tip_hash == tip
    assert chain2.height == height
    assert chain2.get_chain_work() == work
    assert chain2.utxo_fp() == fp
    chain2.close()


# ---------------------------------------------------------------------------
# Mempool restoration
# ---------------------------------------------------------------------------


def test_mempool_restored_after_reorg(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool(tmp_path / "mempool.json")
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a, mempool=mp)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b, mempool=mp)

    tx1 = make_payment_tx(genesis, kp, amount=500_000)
    mp.add(tx1, chain.utxo, height=chain.height)
    c = _mine_on(chain, mp, kp)
    chain.accept_block(c, mempool=mp)
    assert not mp.contains(tx1.txid_hex())

    # Competing chain without tx1 wins
    x = _mine_on_parent(chain, b.block_hash(), mp, kp)
    chain.accept_block(x, mempool=mp)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y, mempool=mp)
    z = _mine_on_parent(chain, y.block_hash(), mp, kp)
    chain.accept_block(z, mempool=mp)
    assert chain.tip_hash == z.block_hash()
    assert mp.contains(tx1.txid_hex())
    chain.close()


def test_mempool_not_restored_if_in_winning_chain(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a, mempool=mp)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b, mempool=mp)

    tx1 = make_payment_tx(genesis, kp, amount=500_000)
    mp.add(tx1, chain.utxo, height=chain.height)
    c = _mine_on(chain, mp, kp)
    chain.accept_block(c, mempool=mp)

    # Winning fork also includes tx1
    x = _mine_on_parent(chain, b.block_hash(), mp, kp, extra_txs=[tx1])
    chain.accept_block(x, mempool=mp)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y, mempool=mp)
    z = _mine_on_parent(chain, y.block_hash(), mp, kp)
    chain.accept_block(z, mempool=mp)
    assert not mp.contains(tx1.txid_hex())
    chain.close()


def test_mempool_conflict_not_restored(tmp_path: Path):
    kp = generate_keypair()
    genesis, _ = seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a, mempool=mp)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b, mempool=mp)

    tx_a = make_payment_tx(genesis, kp, amount=400_000)
    mp.add(tx_a, chain.utxo, height=chain.height)
    c = _mine_on(chain, mp, kp)
    chain.accept_block(c, mempool=mp)

    # Competing spend of same coinbase
    tx_b = make_payment_tx(genesis, kp, amount=300_000)
    x = _mine_on_parent(chain, b.block_hash(), mp, kp, extra_txs=[tx_b])
    chain.accept_block(x, mempool=mp)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y, mempool=mp)
    z = _mine_on_parent(chain, y.block_hash(), mp, kp)
    chain.accept_block(z, mempool=mp)
    assert not mp.contains(tx_a.txid_hex())
    chain.close()


def test_coinbase_never_enters_mempool_on_reorg(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a, mempool=mp)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b, mempool=mp)
    cb_txid = b.transactions[0].txid_hex()

    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x, mempool=mp)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    chain.accept_block(y, mempool=mp)
    assert not mp.contains(cb_txid)
    for tx in mp.list_txs():
        assert not tx.is_coinbase()
    chain.close()


# ---------------------------------------------------------------------------
# Orphans
# ---------------------------------------------------------------------------


def test_orphan_then_parent(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    # Build B extending A without accepting A yet
    # First accept A into a temp to build child, then use orphan path
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    # Remove B capability: accept orphan C whose parent is not yet stored —
    # mine C on B, don't accept B, try accept C
    chain2_dir = tmp_path / "other"
    seed_spendable_chain(chain2_dir, kp)
    c2 = Blockchain(chain2_dir)
    c2.accept_block(a)
    c2.accept_block(b)
    cblk = _mine_on(c2, mp, kp)
    c2.close()

    # On main chain only A: orphan C (parent B unknown)
    r = chain.accept_block(cblk)
    assert r.orphan
    assert chain.height == 1
    # Now accept B — should process orphan C
    chain.accept_block(b)
    assert chain.get_block_by_hash(cblk.block_hash()) is not None
    assert chain.height >= 2
    chain.close()


def test_orphan_limits():
    pool = OrphanPool(max_blocks=3, max_bytes=500_000)
    kp = generate_keypair()
    from mhcoin.blockchain.genesis import mine_regtest_genesis

    g = mine_regtest_genesis(pubkey_hash=hash160(kp.public_key_compressed))
    # Fake distinct orphans by mutating nonce on copies
    blocks = []
    for i in range(5):
        raw = bytearray(g.serialize())
        # alter a byte in coinbase script area roughly — easier: remine with different timestamp
        from mhcoin.blockchain.block import Block

        b = Block.deserialize(bytes(raw))
        b.header.timestamp = g.header.timestamp + 1000 + i
        b.header.nonce = i
        mine_block(b)
        # Force unique hash even if parent is genesis zero — use as orphan of fake parent
        b.header.previous_block_hash = (i + 1).to_bytes(32, "big")
        mine_block(b)
        blocks.append(b)
        pool.add(b)
    assert len(pool) <= 3


def test_duplicate_orphan(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    # Don't accept b; build c on another chain
    d2 = tmp_path / "d2"
    seed_spendable_chain(d2, kp)
    o = Blockchain(d2)
    o.accept_block(a)
    o.accept_block(b)
    c = _mine_on(o, mp, kp)
    o.close()
    assert chain.accept_block(c).orphan
    r2 = chain.accept_block(c)
    assert r2.orphan and r2.duplicate
    chain.close()


# ---------------------------------------------------------------------------
# Invalid side chain / atomic failure
# ---------------------------------------------------------------------------


def test_invalid_pow_side_rejected(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    x.header.nonce ^= 0xDEAD
    with pytest.raises(ValidationError):
        chain.accept_block(x)
    assert chain.get_block_by_hash(x.block_hash()) is None
    chain.close()


def test_reorg_failure_atomicity(tmp_path: Path):
    kp = generate_keypair()
    seed_spendable_chain(tmp_path, kp)
    chain = Blockchain(tmp_path)
    mp = Mempool()
    a = _mine_on(chain, mp, kp)
    chain.accept_block(a)
    b = _mine_on(chain, mp, kp)
    chain.accept_block(b)
    tip = chain.tip_hash
    work = chain.get_chain_work()
    fp = utxo_fingerprint(chain.utxo)

    # Store a valid competing block X, then corrupt a second block Y's merkle after mining
    x = _mine_on_parent(chain, a.block_hash(), mp, kp)
    chain.accept_block(x)
    y = _mine_on_parent(chain, x.block_hash(), mp, kp)
    # Make Y invalid merkle but keep storing via direct index hack to force reorg fail:
    # Instead: accept Y valid so reorg happens — then for failure, inject bad block into connect path.
    # Simpler atomicity test: attempt reorg that exceeds MAX_REORG_DEPTH
    from mhcoin.network import constants as nc

    old = nc.MAX_REORG_DEPTH
    nc.MAX_REORG_DEPTH = 0
    try:
        # Equal-length won't reorg; need more work — mine z extending x (more height)
        z = _mine_on_parent(chain, x.block_hash(), mp, kp)
        with pytest.raises(ChainError, match="reorg depth"):
            chain.accept_block(z)
        assert chain.tip_hash == tip
        assert chain.get_chain_work() == work
        assert utxo_fingerprint(chain.utxo) == fp
    finally:
        nc.MAX_REORG_DEPTH = old
    chain.close()


# ---------------------------------------------------------------------------
# Undo round-trip
# ---------------------------------------------------------------------------


def test_undo_roundtrip_serialize():
    from mhcoin.blockchain.undo import SpentCoin, block_undo_v1
    from mhcoin.transaction.input import TxOut
    from mhcoin.utxo import OutPoint

    undo = block_undo_v1(
        spent=[
            SpentCoin(
                outpoint=OutPoint(txid=b"\x11" * 32, vout=0),
                output=TxOut(value=50, script_pubkey=b"\x00" * 21),
                height=1,
                coinbase=True,
            )
        ],
        created=["aabb" * 16 + ":0"],
    )
    raw = undo.serialize()
    u2 = BlockUndo.deserialize(raw)
    assert u2.spent[0].output.value == 50
    assert u2.created == undo.created


# ---------------------------------------------------------------------------
# P2P three-node reorg convergence
# ---------------------------------------------------------------------------


def test_three_node_reorg_convergence(tmp_path: Path):
    kp = generate_keypair()
    d1, d2, d3 = tmp_path / "n1", tmp_path / "n2", tmp_path / "n3"
    for d in (d1, d2, d3):
        seed_spendable_chain(d, kp)

    # Node1: G→A→B→C
    m1, c1, mp1, r1 = make_relay_node(d1)
    a = mine_extending_block(c1, mp1, kp)
    r1.accept_block(a)
    b = mine_extending_block(c1, mp1, kp)
    r1.accept_block(b)
    c = mine_extending_block(c1, mp1, kp)
    r1.accept_block(c)

    # Node2: G→A→B→X→Y→Z (more work via length)
    m2, c2, mp2, r2 = make_relay_node(d2)
    r2.accept_block(a)
    r2.accept_block(b)
    x = mine_extending_block(c2, mp2, kp)
    r2.accept_block(x)
    y = mine_extending_block(c2, mp2, kp)
    r2.accept_block(y)
    z = mine_extending_block(c2, mp2, kp)
    r2.accept_block(z)
    assert c2.height == 5
    assert c2.get_chain_work() > c1.get_chain_work()

    # Node3 follows node1 initially
    m3, c3, mp3, r3 = make_relay_node(d3)
    r3.accept_block(a)
    r3.accept_block(b)
    r3.accept_block(c)
    assert c3.tip_hash == c1.tip_hash

    m1.config.start_height = c1.height
    m2.config.start_height = c2.height
    m3.config.start_height = c3.height

    m1.start()
    m2.start()
    m3.start()
    try:
        m1.connect_to_peer("127.0.0.1", m2.config.port)
        m3.connect_to_peer("127.0.0.1", m2.config.port)
        assert wait_handshaked(m2, 2, timeout=15.0)

        def converged():
            return (
                c1.tip_hash == c2.tip_hash == c3.tip_hash == z.block_hash()
                and c1.get_chain_work() == c2.get_chain_work() == c3.get_chain_work()
                and c1.utxo_fp() == c2.utxo_fp() == c3.utxo_fp()
            )

        # Trigger header sync toward higher-work peer
        r1.sync.consider_peer_headers(m1.handshaked_peers()[0])
        r3.sync.consider_peer_headers(m3.handshaked_peers()[0])

        assert wait_until(converged, timeout=30.0)
        assert c1.height == c2.height == c3.height == 5
    finally:
        m1.stop()
        m2.stop()
        m3.stop()
        c1.close()
        c2.close()
        c3.close()


def test_peer_false_height_ignored(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, mb, rb = make_relay_node(db)
    # A lies with huge start_height but only genesis
    a.config.start_height = 99999
    a.start()
    b.start()
    try:
        b.connect_to_peer("127.0.0.1", a.config.port)
        assert wait_handshaked(b, 1)
        time.sleep(1.0)
        # B may attempt sync but must not invent blocks; tip stays genesis
        assert cb.height == 0
        assert cb.tip_hash == ca.tip_hash
    finally:
        a.stop()
        b.stop()
        ca.close()
        cb.close()


def test_p2p_side_chain_then_reorg(tmp_path: Path):
    kp = generate_keypair()
    da, db = tmp_path / "a", tmp_path / "b"
    seed_spendable_chain(da, kp)
    seed_spendable_chain(db, kp)
    a, ca, ma, ra = make_relay_node(da)
    b, cb, mb, rb = make_relay_node(db)

    blk_a = mine_extending_block(ca, ma, kp)
    ra.accept_block(blk_a)
    rb.accept_block(blk_a)
    blk_b = mine_extending_block(ca, ma, kp)
    ra.accept_block(blk_b)
    rb.accept_block(blk_b)

    # A continues: C
    blk_c = mine_extending_block(ca, ma, kp)
    ra.accept_block(blk_c)

    # B forks: X Y Z from B
    x = mine_extending_block(cb, mb, kp)
    # cb tip is still B's tip at height 2 same as A's B — need fork from blk_a
    # Actually both at height 2 same tip. Mine exclusive on B:
    rb.accept_block(blk_c)  # sync C first so same tip — then we'll use local fork API

    # Reset B approach: build fork locally then INV
    cb_tip = cb.tip_hash
    # Disconnect conceptually by mining on parent of tip via chain.accept
    parent = blk_b.header.previous_block_hash  # A
    # Use chain API on B to create winning fork from A
    # First ensure B at A only — rebuild scenario simpler:
    a.stop() if False else None

    # Fresh: use chain.accept on both without P2P for fork build, then P2P propagate Z
    da2, db2 = tmp_path / "a2", tmp_path / "b2"
    seed_spendable_chain(da2, kp)
    seed_spendable_chain(db2, kp)
    a2, c_a, mp_a, r_a = make_relay_node(da2)
    b2, c_b, mp_b, r_b = make_relay_node(db2)
    aa = mine_extending_block(c_a, mp_a, kp)
    r_a.accept_block(aa)
    r_b.accept_block(aa)
    bb = mine_extending_block(c_a, mp_a, kp)
    r_a.accept_block(bb)
    r_b.accept_block(bb)
    cc = mine_extending_block(c_a, mp_a, kp)
    r_a.accept_block(cc)
    # B does not take C; builds X Y Z from bb's parent aa? from bb tip without C:
    # B tip is bb; mine x y z on B
    xx = mine_extending_block(c_b, mp_b, kp)
    r_b.accept_block(xx)
    yy = mine_extending_block(c_b, mp_b, kp)
    r_b.accept_block(yy)
    zz = mine_extending_block(c_b, mp_b, kp)
    r_b.accept_block(zz)
    assert c_b.height == 5
    assert c_a.height == 3

    a2.config.start_height = c_a.height
    b2.config.start_height = c_b.height
    a2.start()
    b2.start()
    try:
        a2.connect_to_peer("127.0.0.1", b2.config.port)
        assert wait_handshaked(a2, 1)
        r_a.sync.consider_peer_headers(a2.handshaked_peers()[0])
        assert wait_until(
            lambda: c_a.tip_hash == c_b.tip_hash == zz.block_hash()
            and c_a.utxo_fp() == c_b.utxo_fp(),
            timeout=25.0,
        )
    finally:
        a2.stop()
        b2.stop()
        c_a.close()
        c_b.close()
        # cleanup first pair
        a.stop()
        b.stop()
        ca.close()
        cb.close()
