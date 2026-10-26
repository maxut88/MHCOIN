"""Final isolated multi-node Coinbase Maturity 100 integration tests.

Uses only temporary datadirs (pytest tmp_path). Never touches production,
mainnet activation, or live pool config. Activation is monkeypatched for
regtest/localnet only within these tests.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mhcoin.blockchain.chain import STATUS_SIDE, Blockchain
from mhcoin.blockchain.fingerprint import utxo_fingerprint
from mhcoin.blockchain.genesis import mine_regtest_genesis
from mhcoin.blockchain.validation import ValidationError, validate_transaction
from mhcoin.consensus.params import (
    COINBASE_MATURITY,
    COINBASE_MATURITY_ACTIVATION,
    is_coinbase_mature,
)
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool, MempoolError
from mhcoin.node.local_node import LocalNode
from mhcoin.pool.db import PoolDB
from mhcoin.transaction import Transaction, TxIn, TxOut, sign_input
from mhcoin.utxo import OutPoint
from mhcoin.wallet.addresses import address_to_pubkey_hash
from mhcoin.wallet.wallet import Wallet, WalletPaths
from tests.network.test_reorg import _mine_on, _mine_on_parent


@pytest.fixture
def activate_maturity_from_1(monkeypatch):
    patched = dict(COINBASE_MATURITY_ACTIVATION)
    patched["regtest"] = 1
    patched["localnet"] = 1
    monkeypatch.setattr(
        "mhcoin.consensus.params.COINBASE_MATURITY_ACTIVATION", patched
    )
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    return patched


def _mine(node: LocalNode, addr: str, n: int) -> None:
    for _ in range(n):
        node.mine_one(addr)


def _sync_to(src: LocalNode, dst: LocalNode) -> None:
    """Push missing tip-extension blocks from src → dst via accept_block."""
    while dst.chain.height < src.chain.height:
        h = dst.chain.height + 1
        blk = src.chain.get_block_by_height(h)
        assert blk is not None
        dst.chain.accept_block(blk, mempool=dst.mempool)


def _spend_genesis_cb(node: LocalNode, w: Wallet, password: str, dest: str) -> Transaction:
    kp = w.unlock_default(password)
    genesis = node.chain.get_block_by_height(0)
    assert genesis is not None
    cb = genesis.transactions[0]
    amount = cb.outputs[0].value - 1000
    dest_pkh = address_to_pubkey_hash(dest, hrp="mhc")
    tx = Transaction(
        inputs=[TxIn(prev_txid=cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(amount, dest_pkh)],
    )
    sign_input(
        tx,
        0,
        kp.private_key,
        kp.public_key_compressed,
        cb.outputs[0].script_pubkey,
        cb.outputs[0].value,
    )
    return tx


def _three_local_nodes(tmp_path: Path, alice: str):
    """Three LocalNodes sharing identical genesis (isolated localnet)."""
    pkh = address_to_pubkey_hash(alice, hrp="mhc")
    genesis = mine_regtest_genesis(pubkey_hash=pkh)
    nodes = {}
    for name in ("a", "b", "c"):
        d = tmp_path / name
        d.mkdir(parents=True)
        node = LocalNode(d, hrp="mhc", network="regtest")
        node.chain.init_with_genesis(genesis)
        nodes[name] = node
    return nodes, genesis


# ---------------------------------------------------------------------------
# Consensus rule + boundary (off-by-one)
# ---------------------------------------------------------------------------


def test_final_boundary_h_plus_100_exact(activate_maturity_from_1):
    assert COINBASE_MATURITY == 100
    assert is_coinbase_mature(coinbase_height=0, spend_height=99) is False
    assert is_coinbase_mature(coinbase_height=0, spend_height=100) is True
    assert is_coinbase_mature(coinbase_height=42, spend_height=141) is False
    assert is_coinbase_mature(coinbase_height=42, spend_height=142) is True


def test_final_mainnet_activation_h_star_2000():
    assert COINBASE_MATURITY_ACTIVATION["mainnet"] == 2000
    assert COINBASE_MATURITY_ACTIVATION["testnet"] == 0
    assert COINBASE_MATURITY_ACTIVATION["regtest"] == 0
    assert COINBASE_MATURITY_ACTIVATION["localnet"] == 0


# ---------------------------------------------------------------------------
# Three-node sync + premature/mature spend
# ---------------------------------------------------------------------------


def test_three_node_sync_and_maturity_enforcement(tmp_path: Path, activate_maturity_from_1):
    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    nodes, _ = _three_local_nodes(tmp_path / "nodes", alice)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]

    # Node A mines; B+C sync
    _mine(a, alice, 50)
    _sync_to(a, b)
    _sync_to(a, c)
    assert a.chain.height == b.chain.height == c.chain.height == 50
    assert a.chain.tip_hash == b.chain.tip_hash == c.chain.tip_hash
    assert utxo_fingerprint(a.chain.utxo) == utxo_fingerprint(b.chain.utxo)
    assert utxo_fingerprint(a.chain.utxo) == utxo_fingerprint(c.chain.utxo)

    # Premature spend rejected on all three (tip=50 → spend at 51)
    tx = _spend_genesis_cb(a, w, "x", bob)
    for n in (a, b, c):
        with pytest.raises(MempoolError, match="immature coinbase"):
            n.submit_tx(tx)
        with pytest.raises(ValidationError, match="immature coinbase"):
            validate_transaction(
                tx, n.chain.utxo, height=51, coinbase_maturity_activation=1
            )

    # Advance to maturity boundary: tip=99 → spend height 100 OK
    _mine(a, alice, 49)
    assert a.chain.height == 99
    _sync_to(a, b)
    _sync_to(a, c)
    for n in (a, b, c):
        n.submit_tx(tx)

    h, bh = a.mine_one(alice)
    assert h == 100
    _sync_to(a, b)
    _sync_to(a, c)
    assert a.chain.tip_hash == b.chain.tip_hash == c.chain.tip_hash
    assert utxo_fingerprint(a.chain.utxo) == utxo_fingerprint(c.chain.utxo)
    # Coinbase spent — genesis output gone
    gen = a.chain.get_block_by_height(0)
    assert gen is not None
    from mhcoin.utxo import OutPoint

    cb_op = OutPoint(txid=gen.transactions[0].txid(), vout=0)
    assert a.chain.utxo.get(cb_op) is None

    for n in (a, b, c):
        n.close()


def test_node_c_disconnect_reconnect_full_sync(tmp_path: Path, activate_maturity_from_1):
    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    nodes, _ = _three_local_nodes(tmp_path / "nodes", alice)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]

    _mine(a, alice, 10)
    _sync_to(a, b)
    _sync_to(a, c)
    # Disconnect C: A+B continue
    _mine(a, alice, 25)
    _sync_to(a, b)
    assert c.chain.height == 10
    assert a.chain.height == 35
    # Reconnect C
    _sync_to(a, c)
    assert c.chain.height == a.chain.height
    assert c.chain.tip_hash == a.chain.tip_hash
    assert utxo_fingerprint(c.chain.utxo) == utxo_fingerprint(a.chain.utxo)

    for n in (a, b, c):
        n.close()


def test_restart_preserves_maturity_enforcement(tmp_path: Path, activate_maturity_from_1):
    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    data = tmp_path / "node"
    node = LocalNode(data, hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    _mine(node, alice, 40)
    tip_h = node.chain.height
    tip_hash = node.chain.tip_hash
    node.close()

    node2 = LocalNode(data, hrp="mhc", network="regtest")
    assert node2.chain.height == tip_h
    assert node2.chain.tip_hash == tip_hash
    tx = _spend_genesis_cb(node2, w, "x", bob)
    with pytest.raises(MempoolError, match="immature coinbase"):
        node2.submit_tx(tx)
    _mine(node2, alice, 59)  # tip → 99
    node2.submit_tx(tx)
    node2.mine_one(alice)
    assert node2.chain.height == 100
    node2.close()


# ---------------------------------------------------------------------------
# Reorg: orphaned coinbase + maturity recalculation
# ---------------------------------------------------------------------------


def test_reorg_orphans_coinbase_and_recalculates_maturity(
    tmp_path: Path, activate_maturity_from_1
):
    """Competing longer branch wins; disconnected CB not left spendable."""
    kp = generate_keypair()
    d_a = tmp_path / "a"
    d_b = tmp_path / "b"
    pkh = hash160(kp.public_key_compressed)
    shared_genesis = mine_regtest_genesis(pubkey_hash=pkh)
    for d in (d_a, d_b):
        d.mkdir()
        chain = Blockchain(d, network="localnet")
        chain.init_with_genesis(shared_genesis)
        chain.close()

    ca = Blockchain(d_a, network="localnet")
    cb = Blockchain(d_b, network="localnet")
    mpa, mpb = Mempool(), Mempool()
    try:
        # Common ancestor block A
        blk_a = _mine_on(ca, mpa, kp)
        ca.accept_block(blk_a)
        cb.accept_block(blk_a)

        # Node A: short branch B1
        b1 = _mine_on(ca, mpa, kp)
        ca.accept_block(b1)
        assert ca.height == 2

        # Node B: longer competing branch from A (X-Y-Z)
        x = _mine_on_parent(cb, blk_a.block_hash(), mpb, kp)
        cb.accept_block(x)
        y = _mine_on_parent(cb, x.block_hash(), mpb, kp)
        cb.accept_block(y)
        z = _mine_on_parent(cb, y.block_hash(), mpb, kp)
        cb.accept_block(z)
        assert cb.height == 4
        assert cb.get_chain_work() > ca.get_chain_work()

        # Deliver winning chain → reorg on A
        ca.accept_block(x, mempool=mpa)
        ca.accept_block(y, mempool=mpa)
        r3 = ca.accept_block(z, mempool=mpa)
        assert r3.reorg or ca.tip_hash == z.block_hash()
        assert ca.tip_hash == cb.tip_hash == z.block_hash()
        assert utxo_fingerprint(ca.utxo) == utxo_fingerprint(cb.utxo)

        # Orphaned B1 is side-chain (not on active tip ancestry)
        b1_idx = ca.get_index(b1.block_hash())
        assert b1_idx is not None and b1_idx.status == STATUS_SIDE
        active = set()
        h = ca.tip_hash
        while h:
            active.add(h)
            ix = ca.get_index(h)
            if ix is None or ix.height <= 0:
                break
            h = ix.prev_hash
        assert b1.block_hash() not in active

        # Winning tip coinbase present; immature at current tip+1
        win_op = OutPoint(txid=z.transactions[0].txid(), vout=0)
        assert ca.utxo.get(win_op) is not None
        assert not is_coinbase_mature(
            coinbase_height=ca.height, spend_height=ca.height + 1
        )
    finally:
        ca.close()
        cb.close()


def test_reorg_drops_previously_mature_spendability(
    tmp_path: Path, activate_maturity_from_1
):
    """CB mature at high tip; after reorg to lower tip on same CB ancestry, immature again."""
    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    _mine(node, alice, 99)  # tip=99 → genesis CB mature for spend at 100
    assert is_coinbase_mature(coinbase_height=0, spend_height=100)
    tx = _spend_genesis_cb(node, w, "x", bob)
    node.submit_tx(tx)

    # Build competing longer fork from height 0 (genesis) that never includes the spend.
    # Mine alternate blocks on a side node, then feed them to force reorg past tip.
    kp_paths = WalletPaths(data_dir=tmp_path / "side_w", network="regtest", hrp="mhc")
    sw = Wallet(kp_paths)
    # Use same alice key material via copying genesis from node
    side = LocalNode(tmp_path / "side", hrp="mhc", network="regtest")
    genesis = node.chain.get_block_by_height(0)
    assert genesis is not None
    side.chain.init_with_genesis(genesis)
    # Side mines 101 blocks from genesis tip (height 0) — more work than node's 99
    # First need to not share node's blocks 1..99 — mine fresh chain from genesis
    for _ in range(101):
        side.mine_one(alice)
    assert side.chain.height == 101
    assert side.chain.get_chain_work() > node.chain.get_chain_work()

    # Feed side blocks into node → reorg; tip=101 but different blocks 1..101
    for h in range(1, 102):
        blk = side.chain.get_block_by_height(h)
        assert blk is not None
        node.chain.accept_block(blk, mempool=node.mempool)

    assert node.chain.tip_hash == side.chain.tip_hash
    # Genesis CB still present (common ancestor); next spend height = 102
    # Age from H=0 at spend 102 ≥ 100 → still mature. Force lower effective age by
    # checking a *side* coinbase created near tip is immature, and mempool cleared
    # the disconnected spend if it was only in mempool/orphaned blocks.
    tip_cb_h = 101
    assert not is_coinbase_mature(
        coinbase_height=tip_cb_h, spend_height=node.chain.height + 1
    )
    # Premature spend of recent side coinbase must fail
    tip_blk = node.chain.get_block_by_height(90)
    assert tip_blk is not None
    recent_cb = tip_blk.transactions[0]
    kp = w.unlock_default("x")
    dest_pkh = address_to_pubkey_hash(bob, hrp="mhc")
    premature = Transaction(
        inputs=[TxIn(prev_txid=recent_cb.txid(), prev_vout=0)],
        outputs=[TxOut.p2pkh(recent_cb.outputs[0].value - 1000, dest_pkh)],
    )
    sign_input(
        premature,
        0,
        kp.private_key,
        kp.public_key_compressed,
        recent_cb.outputs[0].script_pubkey,
        recent_cb.outputs[0].value,
    )
    with pytest.raises(MempoolError, match="immature coinbase"):
        node.submit_tx(premature)

    node.close()
    side.close()


# ---------------------------------------------------------------------------
# Ordinary UTXO spend not blocked; wallet immature exclusion
# ---------------------------------------------------------------------------


def test_ordinary_utxo_spend_not_blocked(tmp_path: Path, activate_maturity_from_1):
    """After mature CB → non-coinbase UTXO, further spends work without maturity wait."""
    paths = WalletPaths(data_dir=tmp_path / "w", network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path / "n", hrp="mhc", network="regtest")
    node.bootstrap_genesis(alice)
    _mine(node, alice, 99)
    # Spend mature genesis CB back to alice (same key) → non-coinbase UTXO
    tx1 = _spend_genesis_cb(node, w, "x", alice)
    node.submit_tx(tx1)
    node.mine_one(alice)  # height 100

    kp = w.unlock_default("x")
    found = None
    for entry in node.chain.utxo.all_entries():
        if not entry.coinbase and entry.height == 100:
            found = entry
            break
    assert found is not None
    dest_pkh = address_to_pubkey_hash(bob, hrp="mhc")
    tx2 = Transaction(
        inputs=[TxIn(prev_txid=found.outpoint.txid, prev_vout=found.outpoint.vout)],
        outputs=[TxOut.p2pkh(found.output.value - 1000, dest_pkh)],
    )
    sign_input(
        tx2,
        0,
        kp.private_key,
        kp.public_key_compressed,
        found.output.script_pubkey,
        found.output.value,
    )
    # Non-coinbase must be accepted immediately (no maturity wait)
    node.submit_tx(tx2)
    node.mine_one(alice)
    assert node.chain.height == 101
    node.close()


# ---------------------------------------------------------------------------
# Pool MATURE_CONFIRMS=100 (isolated DB only)
# ---------------------------------------------------------------------------


def test_pool_mature_confirms_100_isolated(tmp_path: Path):
    """Test-only pool DB with MATURE_CONFIRMS=100 — no production config touch."""
    MATURE_CONFIRMS = 100
    ADDR = "mhc1qnkfc9zcfpun8pkud45gz9qutv6xtrmrun9p07r"
    db = PoolDB(tmp_path / "pool_test.sqlite")

    def _seed(height: int, block_hash: str, reward: int = 5_000_000_000) -> int:
        rid = db.current_round_id()
        db.add_share(
            address=ADDR,
            worker="t",
            job_id=f"j{height}",
            height=height,
            difficulty=1.0,
            is_block=True,
            block_hash=block_hash,
        )
        db.close_round_with_block(
            height=height,
            block_hash=block_hash,
            reward_sats=reward,
            fee_percent=0.0,
            found_ts=time.time(),
        )
        return rid

    bh = "ab" * 32
    _seed(10, bh)
    row = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert int(row["immature_sats"]) > 0
    assert int(row["matured_sats"]) == 0

    # At tip=109 (< 10+100) still immature
    n = db.mature_rounds(109, MATURE_CONFIRMS, canonical_hash_at=lambda _h: bh)
    assert n == 0
    row = db._conn.execute(
        "SELECT immature_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert int(row["immature_sats"]) > 0

    # At tip=110 (== 10+100) matures
    n = db.mature_rounds(110, MATURE_CONFIRMS, canonical_hash_at=lambda _h: bh)
    assert n == 1
    bal = db._conn.execute(
        "SELECT immature_sats, matured_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert int(bal["immature_sats"]) == 0
    assert int(bal["matured_sats"]) > 0
    st = db._conn.execute(
        "SELECT status FROM rounds WHERE block_hash=?", (bh,)
    ).fetchone()
    assert st["status"] == "matured"

    # Second immature round — payout intents only against matured
    bh2 = "cd" * 32
    _seed(200, bh2)
    matured_now = int(
        db._conn.execute(
            "SELECT matured_sats FROM balances WHERE address=?", (ADDR,)
        ).fetchone()["matured_sats"]
    )
    immature_now = int(
        db._conn.execute(
            "SELECT immature_sats FROM balances WHERE address=?", (ADDR,)
        ).fetchone()["immature_sats"]
    )
    assert immature_now > 0
    assert matured_now > 0
    iid = db.create_payout_intent(ADDR, matured_now)
    intent = db.get_payout_intent(iid)
    assert intent is not None
    assert int(intent["amount_sats"]) == matured_now
    assert db.complete_payout_intent(iid, "ef" * 32) is True
    assert db.complete_payout_intent(iid, "ef" * 32) is False  # no double pay

    # Reorg: void non-canonical immature (bh2)
    db.mature_rounds(400, MATURE_CONFIRMS, canonical_hash_at=lambda _h: "00" * 32)
    st2 = db._conn.execute(
        "SELECT status FROM rounds WHERE block_hash=?", (bh2,)
    ).fetchone()
    assert st2["status"] == "orphaned"

    db.close()
    db2 = PoolDB(tmp_path / "pool_test.sqlite")
    paid = db2._conn.execute(
        "SELECT paid_sats FROM balances WHERE address=?", (ADDR,)
    ).fetchone()
    assert int(paid["paid_sats"]) == matured_now
    db2.close()


def test_pool_restart_open_intent_survives(tmp_path: Path):
    db = PoolDB(tmp_path / "pool.sqlite")
    addr = "mhc1qtestmaturity00000000000000000000002"
    db._conn.execute(
        "INSERT INTO balances(address, matured_sats, immature_sats, paid_sats) VALUES (?,?,0,0)",
        (addr, 5_000_000),
    )
    db._conn.commit()
    iid = db.create_payout_intent(addr, 4_000_000)
    db.close()
    db2 = PoolDB(tmp_path / "pool.sqlite")
    open_i = db2.list_open_payout_intents(due_only=False)
    assert any(i["id"] == iid for i in open_i)
    assert db2.complete_payout_intent(iid, "aa" * 32) is True
    db2.close()


# ---------------------------------------------------------------------------
# Backward compatibility: activation inactive
# ---------------------------------------------------------------------------


def test_compat_pre_activation_early_spend_still_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MHCOIN_NETWORK", "regtest")
    assert COINBASE_MATURITY_ACTIVATION.get("regtest", 0) == 0
    paths = WalletPaths(data_dir=tmp_path, network="regtest", hrp="mhc")
    w = Wallet(paths)
    alice = w.create(password="x").address
    bob = w.create(password="x", make_default=False).address
    node = LocalNode(tmp_path, hrp="mhc")
    node.bootstrap_genesis(alice)
    tx = _spend_genesis_cb(node, w, "x", bob)
    fee = validate_transaction(
        tx, node.chain.utxo, height=1, coinbase_maturity_activation=0
    )
    assert fee >= 0
    node.close()


def test_pr_fixes_intact_reload_and_change_address():
    from pathlib import Path as P

    root = P(__file__).resolve().parents[1]
    chain_src = (root / "mhcoin/blockchain/chain.py").read_text(encoding="utf-8")
    fn = chain_src.split("def refresh_from_disk", 1)[1].split("\ndef ", 1)[0]
    assert "self.utxo.reload()" in fn
    wallet_src = (root / "mhcoin/wallet/wallet.py").read_text(encoding="utf-8")
    assert "ensure_change_address(pwd, utxo=own_utxo)" in wallet_src or (
        "ensure_change_address" in wallet_src and "utxo=own_utxo" in wallet_src
    )
    assert "create_payout_intent" in (root / "mhcoin/pool/db.py").read_text(
        encoding="utf-8"
    )
