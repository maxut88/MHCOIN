#!/usr/bin/env python3
"""
MHCOIN difficulty parameter sweep — analysis only.

Does NOT modify production consensus constants.
Candidate WINDOW/DAMPING are passed only into simulator-local copies of the
integer retarget math (including compact-bits quantization).

Usage:
  PYTHONPATH=. python3 tools/simulate_difficulty.py --mode sweep
  PYTHONPATH=. python3 tools/simulate_difficulty.py --mode closed --blocks 800
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mhcoin.consensus.difficulty import (  # noqa: E402
    POW_LIMIT_MAINNET,
    bits_to_target,
    get_next_work as production_get_next_work,
    target_to_bits,
)
from mhcoin.consensus.params import (  # noqa: E402
    DIFFICULTY_DAMPING_DENOMINATOR,
    DIFFICULTY_DAMPING_NUMERATOR,
    DIFFICULTY_WINDOW,
    TARGET_BLOCK_TIME_SECONDS,
    get_network_params,
)

OUT_DIR = ROOT / "audit" / "difficulty_sweep"

# ---------------------------------------------------------------------------
# Simulator-local candidate retarget (mirrors production math; injectable knobs)
# ---------------------------------------------------------------------------


def candidate_get_next_work(
    *,
    parent_height: int,
    parent_bits: int,
    window_timestamps: list[int],
    window: int,
    damp_num: int,
    damp_den: int,
    pow_limit: int,
    target_block_time: int = TARGET_BLOCK_TIME_SECONDS,
) -> int:
    """Integer-only retarget with compact-bits round-trip; params are local."""
    if parent_height <= 0:
        return parent_bits
    n = len(window_timestamps)
    if n < 2:
        return parent_bits
    if n > window:
        window_timestamps = window_timestamps[-window:]
        n = len(window_timestamps)

    intervals = n - 1
    expected_timespan = intervals * target_block_time
    if expected_timespan <= 0:
        return parent_bits

    actual_timespan = int(window_timestamps[-1]) - int(window_timestamps[0])
    if actual_timespan < 0:
        actual_timespan = 0

    min_timespan = expected_timespan // 4
    max_timespan = expected_timespan * 4
    if min_timespan < 1:
        min_timespan = 1
    adjusted = actual_timespan
    if adjusted < min_timespan:
        adjusted = min_timespan
    elif adjusted > max_timespan:
        adjusted = max_timespan

    old_target = bits_to_target(parent_bits)
    if old_target <= 0:
        raise ValueError("invalid parent target")
    if old_target > pow_limit:
        old_target = pow_limit

    calculated = old_target * adjusted // expected_timespan
    if calculated < 1:
        calculated = 1
    if calculated > pow_limit:
        calculated = pow_limit

    if calculated < old_target:
        delta = old_target - calculated
        adj = max(1, delta * damp_num // damp_den)
        new_target = old_target - adj
    elif calculated > old_target:
        delta = calculated - old_target
        adj = max(1, delta * damp_num // damp_den)
        new_target = old_target + adj
    else:
        new_target = old_target

    if new_target < 1:
        new_target = 1
    if new_target > pow_limit:
        new_target = pow_limit

    # Compact quantization — what real consensus stores/validates.
    return target_to_bits(new_target)


@dataclass(frozen=True)
class Hashrate:
    """Relative hashrate as rational num/den vs genesis baseline."""

    num: int
    den: int = 1

    def as_float(self) -> float:
        return self.num / self.den


def expected_interval(current_target: int, genesis_target: int, hr: Hashrate, target_bt: int) -> int:
    # Integer division; min 1 second.
    num = target_bt * genesis_target * hr.den
    den = current_target * hr.num
    if den <= 0:
        raise ValueError("bad interval denominator")
    iv = num // den
    if iv < 1:
        iv = 1
    return iv


def simulate_closed(
    *,
    blocks: int,
    window: int,
    damp_num: int,
    damp_den: int,
    hashrate_at: Callable[[int], Hashrate],
    target_bt: int = TARGET_BLOCK_TIME_SECONDS,
    pow_limit: int = POW_LIMIT_MAINNET,
) -> list[dict]:
    """
    hashrate_at(height_being_mined) -> Hashrate for that block.
    height_being_mined is 1..blocks.
    """
    params = get_network_params("mainnet")
    genesis_target = bits_to_target(params.genesis_bits)
    ts = params.genesis_timestamp
    timestamps = [ts]
    rows: list[dict] = [
        {
            "height": 0,
            "interval": None,
            "bits": params.genesis_bits,
            "target": genesis_target,
            "rel_difficulty": 1.0,
            "hashrate": 1.0,
        }
    ]
    for h in range(1, blocks + 1):
        parent_bits = rows[-1]["bits"]
        win_ts = timestamps[-window:]
        bits = candidate_get_next_work(
            parent_height=h - 1,
            parent_bits=parent_bits,
            window_timestamps=win_ts,
            window=window,
            damp_num=damp_num,
            damp_den=damp_den,
            pow_limit=pow_limit,
            target_block_time=target_bt,
        )
        target = bits_to_target(bits)
        if target < 1:
            raise RuntimeError(f"target < 1 at height {h}")
        if target > pow_limit:
            raise RuntimeError(f"target > POW_LIMIT at height {h}")
        hr = hashrate_at(h)
        iv = expected_interval(target, genesis_target, hr, target_bt)
        ts += iv
        timestamps.append(ts)
        rows.append(
            {
                "height": h,
                "interval": iv,
                "bits": bits,
                "target": target,
                "rel_difficulty": genesis_target / target,
                "hashrate": hr.as_float(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    k = (len(ys) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return ys[int(k)]
    return ys[f] * (c - k) + ys[c] * (k - f)


def tail_metrics(rows: list[dict], tail: int) -> dict:
    body = [r for r in rows[1:] if r["interval"] is not None]
    use = body[-tail:] if len(body) >= tail else body
    ivs = [float(r["interval"]) for r in use]
    if not ivs:
        return {}
    mean = statistics.fmean(ivs)
    med = statistics.median(ivs)
    std = statistics.pstdev(ivs) if len(ivs) > 1 else 0.0
    p05 = _pct(ivs, 5)
    p95 = _pct(ivs, 95)
    mn, mx = min(ivs), max(ivs)
    return {
        "n": len(ivs),
        "mean": mean,
        "median": med,
        "std": std,
        "p05": p05,
        "p95": p95,
        "min": mn,
        "max": mx,
        "p95_p05_ratio": (p95 / p05) if p05 > 0 else float("inf"),
        "max_overshoot": max(0.0, mx - 600.0),
        "max_undershoot": max(0.0, 600.0 - mn),
    }


def oscillation_flags(rows: list[dict], tail: int) -> dict:
    body = [r for r in rows[1:] if r["interval"] is not None]
    use = body[-tail:] if len(body) >= tail else body
    ivs = [float(r["interval"]) for r in use]
    if len(ivs) < 20:
        return {"persistent_oscillation": False, "reason": "short"}
    signs = []
    for v in ivs:
        d = v - 600.0
        if abs(d) < 1e-9:
            signs.append(0)
        else:
            signs.append(1 if d > 0 else -1)
    sign_changes = 0
    prev = 0
    for s in signs:
        if s == 0:
            continue
        if prev != 0 and s != prev:
            sign_changes += 1
        prev = s

    half = len(ivs) // 2
    a1, a2 = ivs[:half], ivs[half:]
    amp1 = (max(a1) - min(a1)) if a1 else 0.0
    amp2 = (max(a2) - min(a2)) if a2 else 0.0
    std = statistics.pstdev(ivs)
    # Peak-to-trough
    amp = max(ivs) - min(ivs)
    # Persistent if many crossings + large amp that does not decay much
    decay = amp2 / amp1 if amp1 > 1 else 1.0
    persistent = (
        sign_changes >= max(10, len(ivs) // 20)
        and amp >= 300.0
        and std >= 120.0
        and decay >= 0.7
    )
    return {
        "persistent_oscillation": persistent,
        "sign_changes": sign_changes,
        "amplitude": amp,
        "amp_first_half": amp1,
        "amp_second_half": amp2,
        "amp_decay_ratio": decay,
        "std": std,
    }


def settling_block(rows: list[dict], start_h: int) -> int | None:
    """
    Settled into NORMAL when ≥90% of next 100 blocks in 540–660
    AND 100-block mean in 570–630. Returns absolute height of end of window.
    """
    by_h = {r["height"]: r for r in rows}
    max_h = max(by_h)
    for h0 in range(start_h, max_h - 99):
        window = [by_h[h]["interval"] for h in range(h0, h0 + 100)]
        if any(v is None for v in window):
            continue
        in_band = sum(1 for v in window if 540 <= v <= 660)
        mean = sum(window) / 100.0
        if in_band >= 90 and 570 <= mean <= 630:
            return h0 + 99
    return None


def window_stats(rows: list[dict], h0: int, h1: int) -> dict:
    ivs = [
        float(r["interval"])
        for r in rows
        if r["height"] >= h0 and r["height"] <= h1 and r["interval"] is not None
    ]
    if not ivs:
        return {}
    return {
        "mean": statistics.fmean(ivs),
        "min": min(ivs),
        "max": max(ivs),
        "std": statistics.pstdev(ivs) if len(ivs) > 1 else 0.0,
        "swing": (max(ivs) - min(ivs)) / statistics.fmean(ivs) if statistics.fmean(ivs) else 0,
    }


def classify(tm: dict, osc: dict, settled: int | None) -> str:
    if not tm:
        return "UNSTABLE"
    mean = tm["mean"]
    ratio = tm["p95_p05_ratio"]
    if osc.get("persistent_oscillation"):
        if abs(mean - 600) <= 30 and ratio < 8:
            return "MARGINAL"
        return "UNSTABLE"
    if abs(mean - 600) <= 30 and ratio <= 2.5 and settled is not None:
        return "STABLE"
    if abs(mean - 600) <= 60 and ratio <= 4.0:
        return "MARGINAL"
    return "UNSTABLE"


def sequence_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['height']}:{r['bits']}:{r['interval']}\n".encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def hr_fixed(mult: float) -> callable:
    if mult >= 1:
        num, den = int(round(mult)), 1
        # handle exact ints
        if abs(mult - int(mult)) < 1e-12:
            num, den = int(mult), 1
    else:
        # 0.1 -> 1/10, 0.01 -> 1/100
        den = int(round(1 / mult))
        num = 1

    def _f(_h: int) -> Hashrate:
        return Hashrate(num, den)

    return _f


def hr_step(before: Hashrate, after: Hashrate, at: int) -> callable:
    def _f(h: int) -> Hashrate:
        return before if h < at else after

    return _f


def hr_piecewise(segments: list[tuple[int, int, Hashrate]]) -> callable:
    """segments: (start_inclusive, end_inclusive, hr); last end may be large."""

    def _f(h: int) -> Hashrate:
        for a, b, hr in segments:
            if a <= h <= b:
                return hr
        return segments[-1][2]

    return _f


def hr_alternate(lo: Hashrate, hi: Hashrate, period: int) -> callable:
    def _f(h: int) -> Hashrate:
        # blocks 1..period -> lo, period+1..2period -> hi, ...
        bucket = ((h - 1) // period) % 2
        return lo if bucket == 0 else hi

    return _f


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

WINDOWS = [10, 20, 30, 40, 60, 90, 120]
DAMPINGS = [(1, 2), (1, 4), (1, 8), (1, 16), (1, 32), (1, 64)]
REQUIRED = [
    (60, 1, 8),
    (60, 1, 16),
    (60, 1, 32),
    (30, 1, 8),
    (30, 1, 16),
    (30, 1, 32),
    (20, 1, 16),
    (20, 1, 32),
    (90, 1, 16),
    (120, 1, 16),
]
FIXED_MULTS = [1, 2, 5, 10, 25, 50, 100]
BLOCKS = 10000
TAIL = 2000


def run_sweep(blocks: int = BLOCKS, tail: int = TAIL) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []

    print(f"Sweep: {len(WINDOWS)}×{len(DAMPINGS)} combos, {blocks} blocks, tail={tail}")
    for w in WINDOWS:
        for dn, dd in DAMPINGS:
            for mult in FIXED_MULTS:
                rows = simulate_closed(
                    blocks=blocks,
                    window=w,
                    damp_num=dn,
                    damp_den=dd,
                    hashrate_at=hr_fixed(float(mult)),
                )
                tm = tail_metrics(rows, tail)
                osc = oscillation_flags(rows, tail)
                # settling from height 1 (fixed from start)
                settled = settling_block(rows, 1)
                settling_count = (settled - 0) if settled else None
                cls = classify(tm, osc, settled)
                rec = {
                    "window": w,
                    "damp": f"{dn}/{dd}",
                    "damp_num": dn,
                    "damp_den": dd,
                    "scenario": f"fixed_x{mult}",
                    "hashrate": mult,
                    "initial_interval": rows[1]["interval"],
                    "settling_height": settled,
                    "settling_blocks": settling_count,
                    "classification": cls,
                    **{f"tail_{k}": v for k, v in tm.items()},
                    **{f"osc_{k}": v for k, v in osc.items()},
                    "final_bits": f"0x{rows[-1]['bits']:08x}",
                    "final_target": rows[-1]["target"],
                    "seq_hash": sequence_hash(rows),
                }
                rows_out.append(rec)
            print(f"  done W={w} D={dn}/{dd}")

    # Write sweep.csv
    csv_path = OUT_DIR / "sweep.csv"
    if rows_out:
        keys = list(rows_out[0].keys())
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            wri = csv.DictWriter(f, fieldnames=keys)
            wri.writeheader()
            wri.writerows(rows_out)

    return {"fixed": rows_out, "blocks": blocks, "tail": tail}


def run_extra_scenarios(window: int, dn: int, dd: int, blocks: int = BLOCKS) -> dict:
    """Step/shock/alternating for one candidate."""
    out: dict = {}

    # Step-up
    for mult in (2, 5, 10, 100):
        rows = simulate_closed(
            blocks=blocks,
            window=window,
            damp_num=dn,
            damp_den=dd,
            hashrate_at=hr_step(Hashrate(1), Hashrate(mult), 500),
        )
        settled = settling_block(rows, 500)
        # decay windows after transition
        decay = {}
        for label, a, b in [
            ("1_100", 500, 599),
            ("101_200", 600, 699),
            ("201_400", 700, 899),
            ("401_800", 900, 1299),
            ("801_1600", 1300, 2099),
        ]:
            decay[label] = window_stats(rows, a, b)
        out[f"step_up_1_to_{mult}"] = {
            "settling_height": settled,
            "settling_after": (settled - 499) if settled else None,
            "tail": tail_metrics(rows, TAIL),
            "osc": oscillation_flags(rows, TAIL),
            "decay": decay,
            "seq_hash": sequence_hash(rows),
        }

    # Step-down (from elevated)
    for start, end in [(100, 50), (100, 10), (100, 1), (10, 5), (10, 2), (10, 1)]:
        # warm up at start for 500 blocks then drop
        def make_hr(s=start, e=end):
            return hr_step(Hashrate(s), Hashrate(e), 500)

        rows = simulate_closed(
            blocks=blocks,
            window=window,
            damp_num=dn,
            damp_den=dd,
            hashrate_at=make_hr(),
        )
        settled = settling_block(rows, 500)
        out[f"step_down_{start}_to_{end}"] = {
            "settling_height": settled,
            "settling_after": (settled - 499) if settled else None,
            "tail": tail_metrics(rows, TAIL),
            "osc": oscillation_flags(rows, TAIL),
            "seq_hash": sequence_hash(rows),
        }

    # Floor
    for mult_den in (10, 100):
        rows = simulate_closed(
            blocks=min(blocks, 3000),
            window=window,
            damp_num=dn,
            damp_den=dd,
            hashrate_at=hr_fixed(1.0 / mult_den),
        )
        out[f"floor_div{mult_den}"] = {
            "tail_mean": tail_metrics(rows, min(1000, len(rows) - 1)).get("mean"),
            "expected_floor_interval": 600 * mult_den,
            "final_rel": rows[-1]["rel_difficulty"],
        }

    # Shock
    shock = hr_piecewise(
        [
            (1, 499, Hashrate(1)),
            (500, 1499, Hashrate(100)),
            (1500, 2499, Hashrate(10)),
            (2500, 3499, Hashrate(50)),
            (3500, 4499, Hashrate(2)),
            (4500, blocks, Hashrate(1)),
        ]
    )
    rows = simulate_closed(
        blocks=blocks, window=window, damp_num=dn, damp_den=dd, hashrate_at=shock
    )
    out["shock"] = {
        "tail": tail_metrics(rows, TAIL),
        "osc": oscillation_flags(rows, TAIL),
        "settling_after_last": settling_block(rows, 4500),
        "seq_hash": sequence_hash(rows),
    }

    # Alternating
    for name, lo, hi, period in [
        ("alt_10_100_p100", Hashrate(10), Hashrate(100), 100),
        ("alt_10_100_p30", Hashrate(10), Hashrate(100), 30),
        ("alt_10_100_p10", Hashrate(10), Hashrate(100), 10),
        ("alt_2_20_p100", Hashrate(2), Hashrate(20), 100),
    ]:
        rows = simulate_closed(
            blocks=blocks,
            window=window,
            damp_num=dn,
            damp_den=dd,
            hashrate_at=hr_alternate(lo, hi, period),
        )
        out[name] = {
            "tail": tail_metrics(rows, TAIL),
            "osc": oscillation_flags(rows, TAIL),
            "min": min(r["interval"] for r in rows[1:]),
            "max": max(r["interval"] for r in rows[1:]),
            "final_target": rows[-1]["target"],
            "seq_hash": sequence_hash(rows),
        }

    return out


def pick_shortlist(fixed_rows: list[dict]) -> list[tuple[int, int, int]]:
    """Pareto-ish shortlist from ×10 and ×100 fixed results."""
    by_key: dict[tuple[int, int, int], dict] = {}
    for r in fixed_rows:
        key = (r["window"], r["damp_num"], r["damp_den"])
        by_key.setdefault(key, {})[int(r["hashrate"])] = r

    scored = []
    for key, m in by_key.items():
        x10, x100 = m.get(10), m.get(100)
        if not x10 or not x100:
            continue
        # Prefer: settled, low ratio, mean near 600, not persistent osc
        def score(r):
            mean_pen = abs(r["tail_mean"] - 600) / 600
            ratio_pen = max(0.0, (r["tail_p95_p05_ratio"] or 99) - 1.5)
            osc_pen = 5.0 if r["osc_persistent_oscillation"] else 0.0
            settle_pen = 0.0 if r["settling_blocks"] else 3.0
            settle_speed = (r["settling_blocks"] or 5000) / 5000
            return mean_pen + 0.4 * ratio_pen + osc_pen + settle_pen + 0.3 * settle_speed

        s = score(x10) + score(x100)
        cls_rank = {"STABLE": 0, "MARGINAL": 1, "UNSTABLE": 2}
        worst = max(cls_rank.get(x10["classification"], 2), cls_rank.get(x100["classification"], 2))
        scored.append((worst, s, key, x10, x100))

    scored.sort(key=lambda t: (t[0], t[1]))
    # Always include baseline first in comparison; shortlist up to 5 unique non-identical
    shortlist = []
    baseline = (60, 1, 8)
    seen = set()
    # Prefer STABLE then MARGINAL
    for worst, s, key, x10, x100 in scored:
        if key in seen:
            continue
        if worst >= 2 and len(shortlist) >= 3:
            continue
        shortlist.append(key)
        seen.add(key)
        if len(shortlist) >= 5:
            break
    if baseline not in seen and len(shortlist) < 5:
        shortlist.insert(0, baseline)
    # Ensure baseline present for comparison even if not "best"
    if baseline not in shortlist:
        shortlist = [baseline] + [k for k in shortlist if k != baseline][:4]
    return shortlist[:5]


def write_reports(sweep: dict) -> None:
    fixed = sweep["fixed"]
    by_key: dict[tuple[int, int, int], dict] = {}
    for r in fixed:
        key = (r["window"], r["damp_num"], r["damp_den"])
        by_key.setdefault(key, {})[int(r["hashrate"])] = r

    shortlist_keys = pick_shortlist(fixed)

    # baseline.txt
    bl = by_key[(60, 1, 8)]
    with (OUT_DIR / "baseline.txt").open("w", encoding="utf-8") as f:
        f.write("BASELINE W=60 D=1/8\n")
        for mult in (10, 100):
            r = bl[mult]
            f.write(f"\n×{mult}\n")
            for k in (
                "initial_interval",
                "settling_blocks",
                "tail_mean",
                "tail_median",
                "tail_p05",
                "tail_p95",
                "tail_std",
                "tail_min",
                "tail_max",
                "osc_persistent_oscillation",
                "classification",
            ):
                f.write(f"  {k}: {r.get(k)}\n")

    # shortlist + extras
    shortlist_rows = []
    extras_all = {}
    for key in shortlist_keys:
        w, dn, dd = key
        extras = run_extra_scenarios(w, dn, dd, blocks=BLOCKS)
        extras_all[f"W{w}_D{dn}_{dd}"] = extras
        x10 = by_key[key][10]
        x100 = by_key[key][100]
        shortlist_rows.append(
            {
                "window": w,
                "damp": f"{dn}/{dd}",
                "x10_settling": x10.get("settling_blocks"),
                "x100_settling": x100.get("settling_blocks"),
                "x10_mean": x10.get("tail_mean"),
                "x100_mean": x100.get("tail_mean"),
                "x10_p05": x10.get("tail_p05"),
                "x10_p95": x10.get("tail_p95"),
                "x100_p05": x100.get("tail_p05"),
                "x100_p95": x100.get("tail_p95"),
                "x10_osc": x10.get("osc_persistent_oscillation"),
                "x100_osc": x100.get("osc_persistent_oscillation"),
                "x10_class": x10.get("classification"),
                "x100_class": x100.get("classification"),
                "shock_settled": extras["shock"].get("settling_after_last"),
                "shock_osc": extras["shock"]["osc"].get("persistent_oscillation"),
            }
        )
        # determinism check
        rows_a = simulate_closed(
            blocks=2000, window=w, damp_num=dn, damp_den=dd, hashrate_at=hr_fixed(10.0)
        )
        rows_b = simulate_closed(
            blocks=2000, window=w, damp_num=dn, damp_den=dd, hashrate_at=hr_fixed(10.0)
        )
        shortlist_rows[-1]["determinism_ok"] = sequence_hash(rows_a) == sequence_hash(rows_b)

    with (OUT_DIR / "shortlist.csv").open("w", newline="", encoding="utf-8") as f:
        if shortlist_rows:
            wri = csv.DictWriter(f, fieldnames=list(shortlist_rows[0].keys()))
            wri.writeheader()
            wri.writerows(shortlist_rows)

    with (OUT_DIR / "shortlist_extras.json").open("w", encoding="utf-8") as f:
        json.dump(extras_all, f, indent=2, default=str)

    # pow_limit_floor.txt
    with (OUT_DIR / "pow_limit_floor.txt").open("w", encoding="utf-8") as f:
        f.write("POW_LIMIT FLOOR ANALYSIS\n")
        f.write(f"POW_LIMIT = {hex(POW_LIMIT_MAINNET)}\n")
        f.write("Minimum relative difficulty ≈ 1× (genesis target)\n")
        f.write("At 1× hashrate: expected interval ≈ 600s\n")
        f.write("At 0.1× hashrate: expected interval ≈ 6000s (cannot ease)\n")
        f.write("At 0.01× hashrate: expected interval ≈ 60000s (cannot ease)\n")
        f.write("WINDOW/DAMPING cannot solve this — separate consensus decision.\n")
        # verify via sim
        for den in (10, 100):
            rows = simulate_closed(
                blocks=500,
                window=60,
                damp_num=1,
                damp_den=8,
                hashrate_at=hr_fixed(1.0 / den),
            )
            f.write(
                f"sim div{den}: mean_interval={tail_metrics(rows, 400)['mean']:.1f} "
                f"rel={rows[-1]['rel_difficulty']:.4f}\n"
            )

    # summary.txt
    unstable = sum(1 for r in fixed if r["classification"] == "UNSTABLE" and r["hashrate"] in (10, 100))
    marginal = sum(1 for r in fixed if r["classification"] == "MARGINAL" and r["hashrate"] in (10, 100))
    stable = sum(1 for r in fixed if r["classification"] == "STABLE" and r["hashrate"] in (10, 100))
    with (OUT_DIR / "summary.txt").open("w", encoding="utf-8") as f:
        f.write("MHCOIN DIFFICULTY PARAMETER SWEEP\n\n")
        f.write(f"Blocks={sweep['blocks']} Tail={sweep['tail']}\n")
        f.write(f"Combinations={len(WINDOWS)*len(DAMPINGS)}\n")
        f.write(f"×10/×100 STABLE count rows={stable} MARGINAL={marginal} UNSTABLE={unstable}\n\n")
        f.write("BASELINE W=60 D=1/8\n")
        for mult in (10, 100):
            r = bl[mult]
            f.write(
                f"  ×{mult}: settle={r['settling_blocks']} mean={r['tail_mean']:.1f} "
                f"p05={r['tail_p05']:.1f} p95={r['tail_p95']:.1f} "
                f"osc={r['osc_persistent_oscillation']} class={r['classification']}\n"
            )
        f.write("\nSHORTLIST\n")
        for row in shortlist_rows:
            f.write(
                f"  W={row['window']} D={row['damp']} "
                f"x10_settle={row['x10_settling']} x100_settle={row['x100_settling']} "
                f"x10_mean={row['x10_mean']:.1f} x100_mean={row['x100_mean']:.1f} "
                f"x10_osc={row['x10_osc']} x100_osc={row['x100_osc']} "
                f"class={row['x10_class']}/{row['x100_class']} det={row['determinism_ok']}\n"
            )

    # quick closed_loop dumps for baseline + top non-baseline
    for key in shortlist_keys[:3]:
        w, dn, dd = key
        rows = simulate_closed(
            blocks=BLOCKS, window=w, damp_num=dn, damp_den=dd, hashrate_at=hr_fixed(10.0)
        )
        path = OUT_DIR / f"closed_loop_W{w}_D{dd}_x10.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            wri = csv.DictWriter(
                f, fieldnames=["height", "interval", "bits", "rel_difficulty", "hashrate"]
            )
            wri.writeheader()
            for r in rows[:: max(1, BLOCKS // 500)]:  # downsample
                wri.writerow(
                    {
                        "height": r["height"],
                        "interval": r["interval"],
                        "bits": f"0x{r['bits']:08x}",
                        "rel_difficulty": r["rel_difficulty"],
                        "hashrate": r["hashrate"],
                    }
                )

    return {
        "shortlist_keys": shortlist_keys,
        "shortlist_rows": shortlist_rows,
        "extras": extras_all,
        "counts": {"stable": stable, "marginal": marginal, "unstable": unstable},
        "baseline": bl,
        "by_key": by_key,
    }


def verify_candidate_matches_production_baseline() -> bool:
    """Sanity: simulator local knobs match production get_next_work on same inputs."""
    params = get_network_params("mainnet")
    ts = [params.genesis_timestamp + i * 60 for i in range(30)]
    a = production_get_next_work(
        network="mainnet",
        parent_height=29,
        parent_bits=params.genesis_bits,
        window_timestamps=ts,
    )
    b = candidate_get_next_work(
        parent_height=29,
        parent_bits=params.genesis_bits,
        window_timestamps=ts,
        window=DIFFICULTY_WINDOW,
        damp_num=DIFFICULTY_DAMPING_NUMERATOR,
        damp_den=DIFFICULTY_DAMPING_DENOMINATOR,
        pow_limit=POW_LIMIT_MAINNET,
    )
    return a == b


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("closed", "open", "both", "sweep"), default="closed")
    ap.add_argument("--blocks", type=int, default=800)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.mode != "sweep":
        # Keep prior closed-loop CLI for quick checks
        from importlib import import_module

        # Minimal closed demo using candidate with production knobs
        print("Quick closed-loop with production W/D (use --mode sweep for full audit)")
        for mult in (1.0, 10.0, 100.0):
            rows = simulate_closed(
                blocks=args.blocks,
                window=DIFFICULTY_WINDOW,
                damp_num=DIFFICULTY_DAMPING_NUMERATOR,
                damp_den=DIFFICULTY_DAMPING_DENOMINATOR,
                hashrate_at=hr_fixed(mult),
            )
            tm = tail_metrics(rows, min(1000, args.blocks // 2))
            osc = oscillation_flags(rows, min(1000, args.blocks // 2))
            print(
                f"×{mult:g}: start={rows[1]['interval']} tail_mean={tm.get('mean',0):.1f} "
                f"p05={tm.get('p05',0):.1f} p95={tm.get('p95',0):.1f} "
                f"osc={osc.get('persistent_oscillation')}"
            )
        return 0

    assert verify_candidate_matches_production_baseline(), "simulator≠production baseline"
    print("Simulator W60/D1/8 matches production get_next_work: OK")

    sweep = run_sweep(blocks=BLOCKS, tail=TAIL)
    report = write_reports(sweep)

    # Console summary
    bl10 = report["baseline"][10]
    bl100 = report["baseline"][100]
    print()
    print("MHCOIN DIFFICULTY PARAMETER SWEEP")
    print()
    print("BASELINE:")
    print(f"W=60 D=1/8")
    print(
        f"  ×10 settle={bl10['settling_blocks']} mean={bl10['tail_mean']:.1f} "
        f"p05/p95={bl10['tail_p05']:.0f}/{bl10['tail_p95']:.0f} "
        f"osc={bl10['osc_persistent_oscillation']} {bl10['classification']}"
    )
    print(
        f"  ×100 settle={bl100['settling_blocks']} mean={bl100['tail_mean']:.1f} "
        f"p05/p95={bl100['tail_p05']:.0f}/{bl100['tail_p95']:.0f} "
        f"osc={bl100['osc_persistent_oscillation']} {bl100['classification']}"
    )
    print()
    print("STABLE CANDIDATES:")
    for row in report["shortlist_rows"]:
        if row["x10_class"] == "STABLE" and row["x100_class"] == "STABLE":
            print(f"  W={row['window']} D={row['damp']}")
    print("MARGINAL:")
    for row in report["shortlist_rows"]:
        if "MARGINAL" in (row["x10_class"], row["x100_class"]) and row["x10_class"] != "UNSTABLE":
            print(f"  W={row['window']} D={row['damp']} ({row['x10_class']}/{row['x100_class']})")
    print(f"UNSTABLE COUNT (×10/×100 rows): {report['counts']['unstable']}")
    print()
    print("POW_LIMIT FLOOR: min rel≈1× → 0.1×≈6000s, 0.01×≈60000s; not tunable via W/D")
    print("CONSENSUS MODIFIED: NO (sweep uses simulator-local knobs)")
    print("GENESIS: 62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000")
    print("COMMIT: NOT CREATED")
    print("MAINNET MINING: NOT STARTED")
    print(f"Artifacts: {OUT_DIR}")

    # Save machine-readable report for the agent
    with (OUT_DIR / "report_meta.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "shortlist": report["shortlist_rows"],
                "counts": report["counts"],
                "baseline_x10": {k: bl10[k] for k in bl10 if k != "seq_hash"},
                "baseline_x100": {k: bl100[k] for k in bl100 if k != "seq_hash"},
            },
            f,
            indent=2,
            default=str,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
