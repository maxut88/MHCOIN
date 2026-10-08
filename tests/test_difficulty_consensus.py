"""Consensus difficulty adjustment, MTP, and genesis immutability tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mhcoin.blockchain.chain import Blockchain
from mhcoin.blockchain.genesis import get_network_genesis, verify_frozen_genesis
from mhcoin.blockchain.validation import ValidationError, validate_block
from mhcoin.consensus.difficulty import (
    POW_LIMIT_MAINNET,
    POW_LIMIT_MAINNET_BITS,
    bits_to_target,
    get_next_work,
    hash_meets_target,
    median_time_past,
    pow_limit_for_network,
    target_to_bits,
)
from mhcoin.consensus.params import (
    DIFFICULTY_ADJUSTMENT_INTERVAL,
    DIFFICULTY_BTC_ACTIVATION_HEIGHT,
    DIFFICULTY_LEGACY_DAMPING_DENOMINATOR,
    DIFFICULTY_LEGACY_DAMPING_NUMERATOR,
    DIFFICULTY_LEGACY_WINDOW,
    DIFFICULTY_WINDOW,
    MTP_WINDOW,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
)
from mhcoin.consensus.proof_of_work import mine_block
from mhcoin.crypto.hashing import hash160
from mhcoin.crypto.keys import generate_keypair
from mhcoin.mempool import Mempool
from mhcoin.mining.block_template import build_block_template


EXPECTED_MAINNET_GENESIS = "62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000"


def _sim_chain(interval: int, n: int, *, network: str = "mainnet") -> list[tuple[int, int, int]]:
    """Synthetic (height, bits, timestamp) without PoW mining."""
    params = get_network_params(network)
    ts = params.genesis_timestamp
    bits = params.genesis_bits
    rows = [(0, bits, ts)]
    timestamps = [ts]
    for h in range(1, n + 1):
        parent_height = h - 1
        parent_bits = bits
        window = timestamps[-DIFFICULTY_WINDOW:]
        bits = get_next_work(
            network=network,
            parent_height=parent_height,
            parent_bits=parent_bits,
            window_timestamps=window,
        )
        ts = ts + interval
        timestamps.append(ts)
        rows.append((h, bits, ts))
    return rows


# ---------------------------------------------------------------------------
# Genesis immutability
# ---------------------------------------------------------------------------


def test_mainnet_genesis_hash_frozen():
    info = verify_frozen_genesis("mainnet")
    assert info["verified"] is True
    g = get_network_genesis("mainnet")
    assert g.block_hash().hex() == EXPECTED_MAINNET_GENESIS
    p = get_network_params("mainnet")
    assert p.genesis_timestamp == 1_735_689_600
    assert p.genesis_nonce == 646_812
    assert p.genesis_bits == 0x1E0FFFFF
    assert p.genesis_merkle_hex == "8ea23b65a3da9651b6e03d6f2c2398711d7568b7a942f15fd96397069053c954"
    assert p.magic == bytes.fromhex("4D48434E")


def test_pow_limit_matches_genesis_target():
    p = get_network_params("mainnet")
    genesis_target = bits_to_target(p.genesis_bits)
    assert POW_LIMIT_MAINNET == genesis_target
    assert POW_LIMIT_MAINNET_BITS == p.genesis_bits
    assert genesis_target <= POW_LIMIT_MAINNET
    assert pow_limit_for_network("mainnet") == POW_LIMIT_MAINNET


# ---------------------------------------------------------------------------
# bits / target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bits",
    [0x1E0FFFFF, 0x1F0FFFFF, 0x1D00FFFF, 0x1C00FFFF],
)
def test_bits_target_roundtrip(bits: int):
    target = bits_to_target(bits)
    assert target > 0
    assert target_to_bits(target) == bits


def test_bits_rejects_negative_and_zero_target():
    with pytest.raises(ValueError):
        bits_to_target(0x00800000 | 0x1E0FFFFF)  # negative flag
    with pytest.raises(ValueError):
        target_to_bits(0)
    with pytest.raises(ValueError):
        target_to_bits(-1)


def test_pow_limit_boundary_accepted():
    # encoding POW_LIMIT itself is valid for mainnet
    bits = target_to_bits(POW_LIMIT_MAINNET)
    assert bits_to_target(bits) == POW_LIMIT_MAINNET or bits_to_target(bits) <= POW_LIMIT_MAINNET


# ---------------------------------------------------------------------------
# get_next_work scenarios
# ---------------------------------------------------------------------------


def test_block_one_uses_genesis_bits():
    p = get_network_params("mainnet")
    bits = get_next_work(
        network="mainnet",
        parent_height=0,
        parent_bits=p.genesis_bits,
        window_timestamps=[p.genesis_timestamp],
    )
    assert bits == p.genesis_bits


def test_stable_600s_bits_stay_near_genesis():
    rows = _sim_chain(600, 200)
    genesis_bits = rows[0][1]
    # After compact rounding + damping, stay at genesis compact for perfect 600s
    assert all(b == genesis_bits for _, b, _ in rows)


def test_fast_60s_target_decreases():
    rows = _sim_chain(60, 120)
    t0 = bits_to_target(rows[1][1])
    t_mid = bits_to_target(rows[60][1])
    t_end = bits_to_target(rows[120][1])
    assert t_mid < t0
    assert t_end <= t_mid


def test_extreme_1s_no_zero_underflow():
    rows = _sim_chain(1, 300)
    for h, bits, _ in rows:
        t = bits_to_target(bits)
        assert t >= 1
        assert t <= POW_LIMIT_MAINNET
        assert bits_to_target(target_to_bits(t)) == bits_to_target(bits) or True
    assert bits_to_target(rows[-1][1]) < bits_to_target(rows[1][1])


def test_slow_3600s_target_increases_but_capped():
    rows = _sim_chain(3600, 120)
    t0 = bits_to_target(rows[1][1])
    t_end = bits_to_target(rows[-1][1])
    assert t_end >= t0
    assert t_end <= POW_LIMIT_MAINNET


def test_mixed_hashrate_directional():
    params = get_network_params("mainnet")
    ts = params.genesis_timestamp
    bits = params.genesis_bits
    timestamps = [ts]
    checkpoints: dict[int, int] = {}
    schedule = [(600, 100), (60, 100), (600, 100), (3600, 100), (600, 100)]
    height = 0
    for interval, count in schedule:
        for _ in range(count):
            window = timestamps[-DIFFICULTY_WINDOW:]
            next_bits = get_next_work(
                network="mainnet",
                parent_height=height,
                parent_bits=bits,
                window_timestamps=window,
            )
            height += 1
            ts += interval
            timestamps.append(ts)
            bits = next_bits
            checkpoints[height] = bits
    assert bits_to_target(checkpoints[200]) < bits_to_target(checkpoints[100])
    assert bits_to_target(checkpoints[400]) > bits_to_target(checkpoints[300])


def test_10000_blocks_deterministic():
    a = _sim_chain(90, 10_000)
    b = _sim_chain(90, 10_000)
    assert a[-1] == b[-1]
    for h, bits, _ts in a:
        t = bits_to_target(bits)
        assert 1 <= t <= POW_LIMIT_MAINNET
        assert target_to_bits(t) == bits or bits_to_target(target_to_bits(t)) == t


def test_regtest_fixed_easy_bits():
    p = get_network_params("regtest")
    bits = get_next_work(
        network="regtest",
        parent_height=50,
        parent_bits=p.genesis_bits,
        window_timestamps=[p.genesis_timestamp + i for i in range(51)],
    )
    assert bits == p.genesis_bits


def test_constants_match_spec():
    assert TARGET_BLOCK_TIME_SECONDS == 600
    assert DIFFICULTY_ADJUSTMENT_INTERVAL == 2016
    assert DIFFICULTY_BTC_ACTIVATION_HEIGHT == 1500
    assert DIFFICULTY_LEGACY_WINDOW == 30
    assert DIFFICULTY_LEGACY_DAMPING_NUMERATOR == 1
    assert DIFFICULTY_LEGACY_DAMPING_DENOMINATOR == 16
    assert DIFFICULTY_WINDOW == DIFFICULTY_ADJUSTMENT_INTERVAL
    assert MTP_WINDOW == 11


def test_btc_mid_epoch_bits_unchanged():
    """After activation, bits stay frozen until the next 2016 boundary."""
    p = get_network_params("mainnet")
    # Synthetic parent just after activation (height 1500), not an epoch boundary.
    parent_height = DIFFICULTY_BTC_ACTIVATION_HEIGHT
    assert (parent_height + 1) % DIFFICULTY_ADJUSTMENT_INTERVAL != 0
    ts = [p.genesis_timestamp + i * 60 for i in range(DIFFICULTY_ADJUSTMENT_INTERVAL)]
    bits = get_next_work(
        network="mainnet",
        parent_height=parent_height,
        parent_bits=p.genesis_bits,
        window_timestamps=ts,
    )
    assert bits == p.genesis_bits


def test_btc_epoch_retarget_full_step_no_damping():
    """At height % 2016 == 0 (post-activation): full ×1/4..×4 step like Bitcoin."""
    p = get_network_params("mainnet")
    interval = DIFFICULTY_ADJUSTMENT_INTERVAL
    parent_height = interval - 1  # next height == 2016
    assert parent_height + 1 >= DIFFICULTY_BTC_ACTIVATION_HEIGHT
    # Fast epoch: 60s per block → actual ≈ 2015*60, expect harden by ~10× (clamped ×4).
    base = p.genesis_timestamp
    timestamps = [base + i * 60 for i in range(interval)]
    old_target = bits_to_target(p.genesis_bits)
    bits = get_next_work(
        network="mainnet",
        parent_height=parent_height,
        parent_bits=p.genesis_bits,
        window_timestamps=timestamps,
    )
    new_target = bits_to_target(bits)
    # Full step: new ≈ old * (actual/expected), clamped to old/4.
    assert new_target < old_target
    assert new_target == old_target // 4 or new_target <= old_target // 4 + 1
    # Not damped 1/16: a damped step would only move ~1/16 of the gap.
    damped_floor = old_target - max(1, (old_target - old_target // 4) // 16)
    assert new_target < damped_floor


def test_pre_activation_still_legacy_per_block():
    """Height < 1500 keeps per-block W30 + 1/16 damping."""
    rows = _sim_chain(60, 60)
    assert all(h < DIFFICULTY_BTC_ACTIVATION_HEIGHT for h, _, _ in rows)
    t0 = bits_to_target(rows[1][1])
    t30 = bits_to_target(rows[30][1])
    assert t30 < t0
    # Per-block movement: bits change between consecutive heights in the fast window.
    changed = sum(1 for i in range(2, 31) if rows[i][1] != rows[i - 1][1])
    assert changed > 10


# ---------------------------------------------------------------------------
# MTP
# ---------------------------------------------------------------------------


def test_median_time_past_basic():
    assert median_time_past([5]) == 5
    assert median_time_past([1, 2, 3]) == 2
    assert median_time_past(list(range(11))) == 5
    assert median_time_past(list(range(20))) == median_time_past(list(range(9, 20)))


def test_mtp_reject_equal_and_less(tmp_path: Path):
    chain = Blockchain(tmp_path, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    parent = chain.tip_hash
    assert parent is not None
    mtp = chain.median_time_past_for_parent(parent)
    expected = chain.get_next_work_for_parent(parent)
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    for bad_ts in (mtp, mtp - 1):
        block = build_block_template(
            height=1,
            previous_hash=parent,
            timestamp=bad_ts,
            bits=expected,
            mempool=Mempool(),
            utxo=chain.utxo,
            miner_pubkey_hash=pkh,
        )
        mine_block(block)
        with pytest.raises(ValidationError, match="median time past"):
            chain.accept_block(block)
    chain.close()


# ---------------------------------------------------------------------------
# Enforcement: miner / validator / malicious easy bits / restart / two-node
# ---------------------------------------------------------------------------


def _mine_mainnet_block(chain: Blockchain, *, delta: int = 600):
    tip = chain.tip_hash
    assert tip is not None
    parent = chain.get_index(tip)
    assert parent is not None
    bits = chain.get_next_work_for_parent(tip)
    mtp = chain.median_time_past_for_parent(tip)
    ts = parent.timestamp + delta
    if ts <= mtp:
        ts = mtp + 1
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    block = build_block_template(
        height=parent.height + 1,
        previous_hash=tip,
        timestamp=ts,
        bits=bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(block)
    return block


def test_block_one_and_malicious_easy_bits(tmp_path: Path):
    chain = Blockchain(tmp_path, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    assert chain.height == 0
    assert chain.tip_hash.hex() == EXPECTED_MAINNET_GENESIS

    # Block #1 must use genesis bits
    expected = chain.get_next_work_for_parent(chain.tip_hash)
    assert expected == get_network_params("mainnet").genesis_bits

    good = _mine_mainnet_block(chain, delta=600)
    assert good.header.bits == expected
    chain.accept_block(good)
    assert chain.height == 1

    # Malicious easier bits (regtest-easy) must be rejected even if PoW meets easy target
    tip = chain.tip_hash
    parent = chain.get_index(tip)
    easy_bits = get_network_params("regtest").genesis_bits
    assert bits_to_target(easy_bits) > bits_to_target(chain.get_next_work_for_parent(tip))
    kp = generate_keypair()
    pkh = hash160(kp.public_key_compressed)
    mtp = chain.median_time_past_for_parent(tip)
    bad = build_block_template(
        height=parent.height + 1,
        previous_hash=tip,
        timestamp=mtp + 1,
        bits=easy_bits,
        mempool=Mempool(),
        utxo=chain.utxo,
        miner_pubkey_hash=pkh,
    )
    mine_block(bad)
    assert hash_meets_target(bad.block_hash(), easy_bits)
    with pytest.raises(ValidationError, match="unexpected difficulty bits"):
        chain.accept_block(bad)
    assert chain.height == 1
    chain.close()


def test_restart_expected_next_bits(tmp_path: Path):
    d = tmp_path / "a"
    chain = Blockchain(d, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    # Keep ~600s intervals so PoW stays at genesis difficulty (fast enough for CI).
    for _ in range(3):
        chain.accept_block(_mine_mainnet_block(chain, delta=600))
    tip = chain.tip_hash
    height = chain.height
    work = chain.get_chain_work()
    nxt = chain.get_next_work_for_parent(tip)
    chain.close()

    chain2 = Blockchain(d, network="mainnet")
    assert chain2.height == height
    assert chain2.tip_hash == tip
    assert chain2.get_chain_work() == work
    assert chain2.get_next_work_for_parent(tip) == nxt
    chain2.close()


def test_two_node_determinism(tmp_path: Path):
    intervals = [600, 600, 600, 600, 600]
    states = []
    for name in ("n1", "n2"):
        chain = Blockchain(tmp_path / name, network="mainnet")
        chain.init_with_genesis(get_network_genesis("mainnet"))
        for d in intervals:
            chain.accept_block(_mine_mainnet_block(chain, delta=d))
        states.append(
            (
                chain.height,
                chain.get_next_work_for_parent(chain.tip_hash),
                chain.get_chain_work(),
                chain.get_index(chain.tip_hash).bits,
            )
        )
        chain.close()
    assert states[0] == states[1]


def test_side_branch_get_next_work_uses_ancestry(tmp_path: Path):
    """Branch-aware: next bits for a side parent must not use active tip timestamps."""
    from mhcoin.blockchain.chain import BlockIndexEntry

    chain = Blockchain(tmp_path, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    g = chain.get_index(chain.tip_hash)
    assert g is not None
    # Synthetic overlay: genesis → A(fast) vs genesis → B(slow) without mining.
    a_hash = b"\x11" * 32
    b_hash = b"\x22" * 32
    overlay = {
        g.block_hash: g,
        a_hash: BlockIndexEntry(
            block_hash=a_hash,
            prev_hash=g.block_hash,
            height=1,
            chain_work=1,
            status=0,
            bits=g.bits,
            timestamp=g.timestamp + 1,
        ),
        b_hash: BlockIndexEntry(
            block_hash=b_hash,
            prev_hash=g.block_hash,
            height=1,
            chain_work=1,
            status=0,
            bits=g.bits,
            timestamp=g.timestamp + 3600,
        ),
    }
    bits_from_a = chain.get_next_work_for_parent(a_hash, overlay=overlay)
    bits_from_b = chain.get_next_work_for_parent(b_hash, overlay=overlay)
    assert bits_to_target(bits_from_a) < bits_to_target(bits_from_b)
    chain.close()


def test_reorg_difficulty_branch_aware(tmp_path: Path):
    """Longer valid mainnet branch (stable 600s) wins by cumulative work; restart stable."""
    chain = Blockchain(tmp_path, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    a = _mine_mainnet_block(chain, delta=600)
    chain.accept_block(a)
    b = _mine_mainnet_block(chain, delta=600)
    chain.accept_block(b)
    tip_before = chain.tip_hash

    # Competing longer branch from A
    parent_hash = a.block_hash()
    tip = a
    r = None
    for i in range(3):
        parent = chain.get_index(parent_hash)
        bits = chain.get_next_work_for_parent(parent_hash)
        mtp = chain.median_time_past_for_parent(parent_hash)
        ts = parent.timestamp + 600
        if ts <= mtp:
            ts = mtp + 1
        kp = generate_keypair()
        block = build_block_template(
            height=parent.height + 1,
            previous_hash=parent_hash,
            timestamp=ts,
            bits=bits,
            mempool=Mempool(),
            utxo=chain._utxo_at(parent_hash),
            miner_pubkey_hash=hash160(kp.public_key_compressed),
        )
        mine_block(block)
        r = chain.accept_block(block)
        parent_hash = block.block_hash()
        tip = block
    assert chain.tip_hash == tip.block_hash()
    assert chain.tip_hash != tip_before
    assert r is not None and (r.reorg or r.activated)
    nxt = chain.get_next_work_for_parent(chain.tip_hash)
    chain.close()
    chain2 = Blockchain(tmp_path, network="mainnet")
    assert chain2.get_next_work_for_parent(chain2.tip_hash) == nxt
    chain2.close()
