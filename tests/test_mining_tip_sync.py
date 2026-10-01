"""Regression: abandon stale mining work when canonical tip moves (live P2P race).

Does NOT change consensus difficulty/genesis. Uses regtest fixtures + abort_check.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import get_network_genesis
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.desktop.controller import CoreController
from mhcoin.mempool import Mempool
from mhcoin.mining.abortable_pow import MiningAborted, mine_block_cancellable
from mhcoin.mining.block_template import build_block_template
from mhcoin.wallet.addresses import pubkey_hash_to_address


def _mine_extending(chain: Blockchain, pkh: bytes, mempool: Mempool | None = None):
    assert chain.tip_hash is not None
    tip = chain.tip_hash
    height = chain.height + 1
    bits = chain.get_next_work_for_parent(tip)
    mtp = chain.median_time_past_for_parent(tip)
    ts = max(int(time.time()), mtp + 1)
    block = build_block_template(
        height=height,
        previous_hash=tip,
        timestamp=ts,
        bits=bits,
        mempool=mempool or Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    chain.accept_block(block)
    return block


def test_mine_block_aborts_mid_search(tmp_path: Path):
    chain = Blockchain(tmp_path / "c", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tip = chain.tip_hash
    assert tip is not None
    bits = chain.get_next_work_for_parent(tip)
    block = build_block_template(
        height=1,
        previous_hash=tip,
        timestamp=chain.median_time_past_for_parent(tip) + 1,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    # Force a hard target so we loop many nonces; abort after ~3 polls.
    block.header.bits = 0x1D00FFFF  # much harder than regtest
    polls = {"n": 0}

    def abort() -> bool:
        polls["n"] += 1
        return polls["n"] >= 3

    with pytest.raises(MiningAborted):
        mine_block_cancellable(block, abort_check=abort, abort_every=1)
    assert polls["n"] >= 3
    chain.close()


def test_abortable_pow_emits_progress_across_chunks(tmp_path: Path):
    """25k abort chunks must still drive Desktop nonce/H/s log via progress()."""
    chain = Blockchain(tmp_path / "prog", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tip = chain.tip_hash
    assert tip is not None
    bits = chain.get_next_work_for_parent(tip)
    block = build_block_template(
        height=1,
        previous_hash=tip,
        timestamp=chain.median_time_past_for_parent(tip) + 1,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    block.header.bits = 0x1D00FFFF
    seen: list[tuple[int, float]] = []

    def progress(nonce: int, _h: bytes, hps: float) -> None:
        seen.append((nonce, hps))

    polls = {"n": 0}

    def abort() -> bool:
        polls["n"] += 1
        return polls["n"] >= 5

    with pytest.raises(MiningAborted):
        mine_block_cancellable(
            block,
            abort_check=abort,
            abort_every=25_000,
            progress=progress,
        )
    assert len(seen) >= 3, f"expected chunk-boundary progress, got {seen}"
    assert all(hps > 0 for _, hps in seen)
    chain.close()


def test_remote_block_cancels_stale_pow_and_advances_tip(tmp_path: Path):
    """While PoW runs on template N, accept remote block N → miner aborts, tip advances."""
    data = tmp_path / "node"
    chain = Blockchain(data, network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)

    tip0 = chain.tip_hash
    assert tip0 is not None
    epoch0 = chain.tip_epoch
    bits = chain.get_next_work_for_parent(tip0)
    stale = build_block_template(
        height=1,
        previous_hash=tip0,
        timestamp=chain.median_time_past_for_parent(tip0) + 1,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    # Hard bits so PoW does not finish before remote block is accepted.
    stale.header.bits = 0x1D00FFFF

    remote_ready = threading.Event()
    aborted = threading.Event()
    errors: list[BaseException] = []

    def _abort() -> bool:
        if int(chain.tip_epoch) != epoch0:
            return True
        tip = chain.tip_hash
        return tip is not None and tip != tip0

    def miner() -> None:
        try:
            remote_ready.wait(timeout=5.0)
            # Give accept_block a moment to bump tip_epoch
            time.sleep(0.05)
            mine_block_cancellable(stale, abort_check=_abort, abort_every=100)
        except MiningAborted:
            aborted.set()
        except BaseException as e:
            errors.append(e)

    th = threading.Thread(target=miner, name="stale-miner", daemon=True)
    th.start()
    # Competing valid block at same height (easy regtest PoW)
    remote = build_block_template(
        height=1,
        previous_hash=tip0,
        timestamp=chain.median_time_past_for_parent(tip0) + 2,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(remote)
    chain.accept_block(remote)
    assert chain.height == 1
    assert chain.tip_epoch > epoch0
    remote_ready.set()
    th.join(timeout=10.0)
    assert not th.is_alive()
    assert not errors
    assert aborted.is_set()
    assert chain.tip_hash == remote.block_hash()
    chain.close()


def test_desktop_miner_auto_rebuilds_after_remote_tip(tmp_path: Path):
    """CoreController mining ON: remote tip change → abandon → new template (no Stop)."""
    data = tmp_path / "desk"
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    addr = pubkey_hash_to_address(pkh, hrp="mhc")

    ctrl = CoreController(network="regtest", data_dir=data)
    ctrl.start_node(host="127.0.0.1", port=28555, connect=[], skip_history_wait=True)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        rt0 = ctrl._node
        if rt0 is not None and getattr(rt0, "_running", False) and rt0.chain.height >= 0:
            break
        time.sleep(0.05)
    assert ctrl._node is not None and getattr(ctrl._node, "_running", False)
    rt = ctrl._node
    assert rt.chain.height == 0

    real_prepare = rt.prepare_block_template
    state = {"hard_once": True, "templates": []}

    def prepare_wrap(miner_address: str, *, hrp: str | None = None):
        block, height, bits = real_prepare(miner_address, hrp=hrp)
        state["templates"].append(int(height))
        if state["hard_once"]:
            state["hard_once"] = False
            block.header.bits = 0x1D00FFFF  # force long PoW → abort on tip
        return block, height, bits

    rt.prepare_block_template = prepare_wrap  # type: ignore[method-assign]

    ctrl.start_mining(address=addr)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ctrl._mining and state["templates"]:
            break
        time.sleep(0.05)
    assert ctrl._mining, ctrl.mine_log_lines(40)
    remote = build_block_template(
        height=1,
        previous_hash=rt.chain.tip_hash,
        timestamp=rt.chain.median_time_past_for_parent(rt.chain.tip_hash) + 1,
        bits=rt.chain.get_next_work_for_parent(rt.chain.tip_hash),
        mempool=Mempool(),
        utxo=rt.chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(remote)
    rt.accept_block(remote)
    assert rt.chain.height == 1

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if any(h >= 2 for h in state["templates"]):
            break
        time.sleep(0.05)
    assert any(h >= 2 for h in state["templates"]), (
        state["templates"],
        ctrl.mine_log_lines(40),
        ctrl._mining,
    )
    assert ctrl._mining is True
    assert ctrl._miner_stop.is_set() is False

    ctrl.stop_mining()
    ctrl.stop_node()


def test_p2p_accept_while_cpu_mining(tmp_path: Path):
    """Chain accept_block works while another thread is in mine_block (no global PoW lock)."""
    chain = Blockchain(tmp_path / "c2", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    tip = chain.tip_hash
    bits = chain.get_next_work_for_parent(tip)
    hard = build_block_template(
        height=1,
        previous_hash=tip,
        timestamp=chain.median_time_past_for_parent(tip) + 1,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    hard.header.bits = 0x1D00FFFF
    stop = threading.Event()

    def busy() -> None:
        try:
            mine_block_cancellable(
                hard,
                abort_check=lambda: stop.is_set(),
                abort_every=500,
            )
        except MiningAborted:
            pass

    th = threading.Thread(target=busy, daemon=True)
    th.start()
    time.sleep(0.05)
    easy = build_block_template(
        height=1,
        previous_hash=tip,
        timestamp=chain.median_time_past_for_parent(tip) + 2,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(easy)
    chain.accept_block(easy)
    assert chain.height == 1
    stop.set()
    th.join(timeout=5.0)
    assert not th.is_alive()
    chain.close()


def test_local_vs_remote_race_no_corruption(tmp_path: Path):
    chain = Blockchain(tmp_path / "race", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp_a = generate_keypair()
    kp_b = generate_keypair()
    pkh_a = hash160(kp_a.public_key_compressed)
    pkh_b = hash160(kp_b.public_key_compressed)
    tip = chain.tip_hash
    bits = chain.get_next_work_for_parent(tip)
    mtp = chain.median_time_past_for_parent(tip)

    def build_at(pkh, ts_off):
        return build_block_template(
            height=1,
            previous_hash=tip,
            timestamp=mtp + ts_off,
            bits=bits,
            mempool=Mempool(),
            utxo=chain.utxo,
            miner_pubkey_hash=pkh,
        )

    a = build_at(pkh_a, 1)
    b = build_at(pkh_b, 2)
    mine_block(a)
    mine_block(b)
    err: list[BaseException] = []

    def accept(block):
        try:
            chain.accept_block(block)
        except BaseException as e:
            err.append(e)

    t1 = threading.Thread(target=accept, args=(a,))
    t2 = threading.Thread(target=accept, args=(b,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not err
    assert chain.height == 1
    assert chain.tip_hash in {a.block_hash(), b.block_hash()}
    # Second block stored as side or lost to equal-work keep — either is fine
    chain.info()  # must not raise
    chain.close()


def test_mining_start_stop_repeated(tmp_path: Path):
    data = tmp_path / "ss"
    kp = generate_keypair()
    addr = pubkey_hash_to_address(hash160(kp.public_key_compressed), hrp="mhc")
    ctrl = CoreController(network="regtest", data_dir=data)
    ctrl.start_node(host="127.0.0.1", port=28556, connect=[], skip_history_wait=True)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ctrl._node is not None and getattr(ctrl._node, "_running", False):
            break
        time.sleep(0.05)
    for _ in range(5):
        ctrl.start_mining(address=addr)
        time.sleep(0.15)
        ctrl.stop_mining()
        time.sleep(0.05)
        assert ctrl._mining is False
    ctrl.stop_node()


def test_tip_epoch_bumps_on_accept(tmp_path: Path):
    chain = Blockchain(tmp_path / "ep", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    e0 = chain.tip_epoch
    kp = generate_keypair()
    _mine_extending(chain, hash160(kp.public_key_compressed))
    assert chain.tip_epoch == e0 + 1
    chain.close()


def test_sqlite_stress_poll_while_mining(tmp_path: Path):
    """Concurrent tip reads + accept + mining abort checks — no shared-cursor crash."""
    chain = Blockchain(tmp_path / "stress", network="regtest")
    chain.init_with_genesis(get_network_genesis("regtest"))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            for _ in range(200):
                if stop.is_set():
                    break
                with chain._lock:
                    _ = chain.height
                    _ = chain.tip_hash
                    _ = chain.tip_epoch
                    _ = chain.info()
                time.sleep(0.001)
        except BaseException as e:
            errors.append(e)

    def extender():
        try:
            for _ in range(8):
                if stop.is_set():
                    break
                _mine_extending(chain, pkh)
                time.sleep(0.01)
        except BaseException as e:
            errors.append(e)

    def pow_loop():
        try:
            while not stop.is_set():
                tip = chain.tip_hash
                if tip is None:
                    time.sleep(0.01)
                    continue
                epoch = chain.tip_epoch
                bits = chain.get_next_work_for_parent(tip)
                block = build_block_template(
                    height=chain.height + 1,
                    previous_hash=tip,
                    timestamp=chain.median_time_past_for_parent(tip) + 1,
                    bits=bits,
                    mempool=Mempool(),
                    utxo=chain.utxo,
                    miner_pubkey_hash=pkh,
                )
                block.header.bits = 0x1D00FFFF
                try:
                    mine_block_cancellable(
                        block,
                        abort_check=lambda: stop.is_set()
                        or chain.tip_epoch != epoch
                        or chain.tip_hash != tip,
                        abort_every=200,
                    )
                except MiningAborted:
                    continue
        except BaseException as e:
            errors.append(e)

    threads = [
        threading.Thread(target=reader, daemon=True),
        threading.Thread(target=extender, daemon=True),
        threading.Thread(target=pow_loop, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors, errors
    assert chain.height >= 1
    chain.close()
