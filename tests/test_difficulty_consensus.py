"""Consensus difficulty adjustment (ASERT mainnet), MTP, and genesis immutability tests."""

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
    next_bits_aserti3_2d,
    pow_limit_for_network,
    target_to_bits,
    uses_asert_difficulty,
)
from mhcoin.consensus.params import (
    ASERT_ACTIVATION_HEIGHT,
    ASERT_HALF_LIFE_SECONDS,
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
    p = get_network_params("mainnet")
    if p.genesis_hash_hex == "00" * 32:
        pytest.skip("mainnet genesis v2 not frozen yet")
    info = verify_frozen_genesis("mainnet")
    assert info["verified"] is True
    g = get_network_genesis("mainnet")
    assert g.block_hash().hex() == p.genesis_hash_hex
    assert p.genesis_bits == 0x1D00FFFF
    assert p.genesis_coinbase_extra == b"MHCOIN/mainnet genesis v2"
    assert p.magic == bytes.fromhex("4D48434E")
    assert p.genesis_nonce != 0


def test_pow_limit_matches_genesis_target():
    p = get_network_params("mainnet")
    genesis_target = bits_to_target(p.genesis_bits)
    assert POW_LIMIT_MAINNET == genesis_target
    assert POW_LIMIT_MAINNET_BITS == p.genesis_bits
    assert genesis_target <= POW_LIMIT_MAINNET
    assert pow_limit_for_network("mainnet") == POW_LIMIT_MAINNET
    assert p.genesis_bits == 0x1D00FFFF


# ---------------------------------------------------------------------------
# bits / target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bits",
    [0x1E0FFFFF, 0x1F0FFFFF, 0x1D00FFFF, 0x1C00FFFF],
)
def test_bits_roundtrip(bits: int):
    t = bits_to_target(bits)
    assert t > 0
    assert bits_to_target(target_to_bits(t)) == t or target_to_bits(t) == bits


def test_negative_bits_rejected():
    with pytest.raises(ValueError):
        bits_to_target(0x00800000 | 0x1E0FFFFF)  # negative flag


def test_hash_meets_target_pow_limit():
    p = get_network_params("mainnet")
    # All-zero hash always meets any positive target.
    assert hash_meets_target(b"\x00" * 32, p.genesis_bits)
    bits = p.genesis_bits
    assert bits_to_target(bits) == POW_LIMIT_MAINNET or bits_to_target(bits) <= POW_LIMIT_MAINNET


# ---------------------------------------------------------------------------
# get_next_work / ASERT scenarios
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
    assert bits_to_target(rows[-1][1]) < bits_to_target(rows[1][1])


def test_slow_3600s_capped_at_pow_limit():
    """With genesis == POW_LIMIT, late blocks stay at limit (cannot ease further)."""
    rows = _sim_chain(3600, 120)
    for _, bits, _ in rows:
        assert bits_to_target(bits) <= POW_LIMIT_MAINNET
    assert bits_to_target(rows[-1][1]) == POW_LIMIT_MAINNET


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
    # After slow stretch, target eases vs the fast trough (may hit powLimit).
    assert bits_to_target(checkpoints[400]) >= bits_to_target(checkpoints[200])


def test_10000_blocks_deterministic():
    a = _sim_chain(90, 10_000)
    b = _sim_chain(90, 10_000)
    assert a[-1] == b[-1]
    for h, bits, _ts in a:
        t = bits_to_target(bits)
        assert 1 <= t <= POW_LIMIT_MAINNET


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
    assert ASERT_HALF_LIFE_SECONDS == 172_800
    assert ASERT_ACTIVATION_HEIGHT == 1
    assert uses_asert_difficulty("mainnet")
    assert not uses_asert_difficulty("regtest")
    # BTC activation retained for testnet tooling only; mainnet ignores freeze path.
    assert DIFFICULTY_BTC_ACTIVATION_HEIGHT == 1
    assert DIFFICULTY_LEGACY_WINDOW == 30
    assert DIFFICULTY_LEGACY_DAMPING_NUMERATOR == 1
    assert DIFFICULTY_LEGACY_DAMPING_DENOMINATOR == 16
    assert DIFFICULTY_WINDOW == DIFFICULTY_ADJUSTMENT_INTERVAL
    assert MTP_WINDOW == 11


def test_asert_every_block_updates_on_fast_schedule():
    """Unlike BTC epoch freeze, ASERT adjusts every block when intervals are off."""
    rows = _sim_chain(60, 40)
    changed = sum(1 for i in range(2, len(rows)) if rows[i][1] != rows[i - 1][1])
    assert changed > 10


def test_aserti3_2d_on_time_unchanged():
    p = get_network_params("mainnet")
    # Virtual parent of genesis: time_diff=600, height_diff=0 → exponent 0.
    bits = next_bits_aserti3_2d(
        p.genesis_bits,
        TARGET_BLOCK_TIME_SECONDS,
        0,
        pow_limit_bits=p.genesis_bits,
    )
    assert bits == p.genesis_bits


# ---------------------------------------------------------------------------
# MTP
# ---------------------------------------------------------------------------


def test_median_time_past_basic():
    assert median_time_past([5]) == 5
    assert median_time_past([1, 2, 3]) == 2
    assert median_time_past(list(range(11))) == 5
    assert median_time_past(list(range(20))) == median_time_past(list(range(9, 20)))


@pytest.mark.skipif(
    get_network_params("mainnet").genesis_hash_hex == "00" * 32,
    reason="mainnet genesis v2 not frozen yet",
)
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


@pytest.mark.skipif(
    get_network_params("mainnet").genesis_hash_hex == "00" * 32,
    reason="mainnet genesis v2 not frozen yet",
)
def test_chain_get_next_work_matches_module(tmp_path: Path):
    chain = Blockchain(tmp_path, network="mainnet")
    chain.init_with_genesis(get_network_genesis("mainnet"))
    tip = chain.tip_hash
    assert tip is not None
    expected = chain.get_next_work_for_parent(tip)
    p = get_network_params("mainnet")
    assert expected == get_next_work(
        network="mainnet",
        parent_height=0,
        parent_bits=p.genesis_bits,
        window_timestamps=[p.genesis_timestamp],
    )
    chain.close()


def test_side_branch_get_next_work_uses_ancestry(tmp_path: Path):
    """Branch-aware: next bits for a side parent must not use active tip timestamps."""
    from mhcoin.blockchain.chain import BlockIndexEntry

    p = get_network_params("mainnet")
    # Synthetic genesis index (no PoW / frozen hash required).
    g_hash = bytes.fromhex(p.genesis_hash_hex) if p.genesis_hash_hex != "00" * 32 else b"\xaa" * 32
    g = BlockIndexEntry(
        block_hash=g_hash,
        prev_hash=b"\x00" * 32,
        height=0,
        chain_work=1,
        status=0,
        bits=p.genesis_bits,
        timestamp=p.genesis_timestamp,
    )
    chain = Blockchain(tmp_path, network="mainnet")
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


# Mainnet PoW mining at bits 0x1d00ffff is too slow for CI (~MH/s·hours).
# Keep synthetic ASERT coverage above; mine-path enforcement is covered on regtest elsewhere.
@pytest.mark.skip(reason="mainnet genesis bits 0x1d00ffff too hard for CI PoW mining")
def test_block_one_and_malicious_easy_bits(tmp_path: Path):
    pass
