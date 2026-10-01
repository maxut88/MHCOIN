#!/usr/bin/env python3
"""Stochastic PoW validation for candidate W30/D1/16 (analysis; no consensus edit)."""

from __future__ import annotations

import hashlib
import json
import random
import statistics
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.simulate_difficulty import (  # noqa: E402
    Hashrate,
    POW_LIMIT_MAINNET,
    TARGET_BLOCK_TIME_SECONDS,
    bits_to_target,
    candidate_get_next_work,
    expected_interval,
    get_network_params,
)

OUT = ROOT / "audit" / "difficulty_sweep"
W, DN, DD = 30, 1, 16
SEEDS = 50
BLOCKS = 10_000
TAIL = 2_000


def hr_at_factory(schedule: Callable[[int], Hashrate]) -> Callable[[int], Hashrate]:
    return schedule


def simulate(
    *,
    window: int,
    damp_num: int,
    damp_den: int,
    schedule: Callable[[int], Hashrate],
    blocks: int,
    seed: int,
    fixed_bits: int | None = None,
) -> list[dict]:
    """If fixed_bits set, skip retarget (ideal equilibrium control)."""
    rng = random.Random(seed)
    params = get_network_params("mainnet")
    genesis_target = bits_to_target(params.genesis_bits)
    ts = params.genesis_timestamp
    timestamps = [ts]
    rows = [
        {
            "height": 0,
            "bits": params.genesis_bits,
            "target": genesis_target,
            "interval": None,
            "rel": 1.0,
            "expected": None,
            "hashrate": 1.0,
        }
    ]
    for h in range(1, blocks + 1):
        parent_bits = rows[-1]["bits"]
        if fixed_bits is not None:
            bits = fixed_bits
        else:
            bits = candidate_get_next_work(
                parent_height=h - 1,
                parent_bits=parent_bits,
                window_timestamps=timestamps[-window:],
                window=window,
                damp_num=damp_num,
                damp_den=damp_den,
                pow_limit=POW_LIMIT_MAINNET,
            )
        target = bits_to_target(bits)
        if target < 1 or target > POW_LIMIT_MAINNET:
            raise RuntimeError(f"bad target {target} at h={h}")
        hr = schedule(h)
        mean_iv = expected_interval(target, genesis_target, hr, TARGET_BLOCK_TIME_SECONDS)
        iv = max(1, int(round(rng.expovariate(1.0 / mean_iv))))
        ts += iv
        timestamps.append(ts)
        rows.append(
            {
                "height": h,
                "bits": bits,
                "target": target,
                "interval": iv,
                "rel": genesis_target / target,
                "expected": mean_iv,
                "hashrate": hr.as_float(),
            }
        )
    return rows


def pct(xs: list[float], p: float) -> float:
    ys = sorted(xs)
    if not ys:
        return float("nan")
    k = (len(ys) - 1) * p / 100.0
    f = int(k)
    c = min(len(ys) - 1, f + 1)
    if f == c:
        return ys[f]
    return ys[f] * (c - k) + ys[c] * (k - f)


def summarize(rows: list[dict], *, start_h: int = 0, tail: int | None = TAIL) -> dict:
    body = [r for r in rows[1:] if r["height"] >= start_h]
    if tail is not None and len(body) > tail:
        body = body[-tail:]
    ivs = [float(r["interval"]) for r in body]
    exps = [float(r["expected"]) for r in body]
    rels = [float(r["rel"]) for r in body]
    return {
        "n": len(body),
        "actual_mean": statistics.fmean(ivs),
        "actual_median": statistics.median(ivs),
        "actual_p05": pct(ivs, 5),
        "actual_p95": pct(ivs, 95),
        "actual_p99": pct(ivs, 99),
        "expected_mean": statistics.fmean(exps),
        "expected_median": statistics.median(exps),
        "expected_p05": pct(exps, 5),
        "expected_p95": pct(exps, 95),
        "expected_std": statistics.pstdev(exps) if len(exps) > 1 else 0.0,
        "rel_mean": statistics.fmean(rels),
        "rel_median": statistics.median(rels),
        "rel_p05": pct(rels, 5),
        "rel_p95": pct(rels, 95),
        "rel_min": min(rels),
        "rel_max": max(rels),
        "rel_std": statistics.pstdev(rels) if len(rels) > 1 else 0.0,
        "final_bits": rows[-1]["bits"],
        "final_target": rows[-1]["target"],
        "final_rel": rows[-1]["rel"],
    }


def seq_hash(rows: list[dict]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['height']}:{r['bits']}:{r['interval']}:{r['target']}\n".encode())
    return h.hexdigest()


def aggregate(seed_stats: list[dict]) -> dict:
    def col(k: str) -> list[float]:
        return [float(s[k]) for s in seed_stats]

    out = {"seeds": len(seed_stats)}
    for k in (
        "actual_mean",
        "expected_mean",
        "expected_p05",
        "expected_p95",
        "expected_std",
        "rel_mean",
        "rel_p05",
        "rel_p95",
        "rel_std",
        "rel_min",
        "rel_max",
    ):
        xs = col(k)
        out[f"{k}_across_mean"] = statistics.fmean(xs)
        out[f"{k}_across_min"] = min(xs)
        out[f"{k}_across_max"] = max(xs)
    # fractions
    out["frac_expected_mean_480_720"] = sum(
        1 for s in seed_stats if 480 <= s["expected_mean"] <= 720
    ) / len(seed_stats)
    out["frac_expected_mean_540_660"] = sum(
        1 for s in seed_stats if 540 <= s["expected_mean"] <= 660
    ) / len(seed_stats)
    return out


def run_fixed(mult: int, *, seeds: int = SEEDS, blocks: int = BLOCKS) -> dict:
    hr = Hashrate(mult, 1)

    def sched(_h: int) -> Hashrate:
        return hr

    stats = []
    for seed in range(1, seeds + 1):
        rows = simulate(
            window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=blocks, seed=seed
        )
        stats.append(summarize(rows))
    return {"kind": f"fixed_x{mult}", "agg": aggregate(stats), "per_seed_sample": stats[0]}


def run_control(mult: int, *, seeds: int = SEEDS, blocks: int = BLOCKS) -> dict:
    """Fixed difficulty at equilibrium bits for hashrate=mult (ideal PoW variance)."""
    # Find equilibrium bits via deterministic closed-loop then freeze.
    from tools.simulate_difficulty import simulate_closed, hr_fixed

    det = simulate_closed(
        blocks=3000, window=W, damp_num=DN, damp_den=DD, hashrate_at=hr_fixed(float(mult))
    )
    eq_bits = det[-1]["bits"]
    hr = Hashrate(mult, 1)

    def sched(_h: int) -> Hashrate:
        return hr

    stats = []
    for seed in range(1, seeds + 1):
        rows = simulate(
            window=W,
            damp_num=DN,
            damp_den=DD,
            schedule=sched,
            blocks=blocks,
            seed=seed,
            fixed_bits=eq_bits,
        )
        stats.append(summarize(rows))
    return {
        "kind": f"control_fixed_diff_x{mult}",
        "eq_bits": f"0x{eq_bits:08x}",
        "agg": aggregate(stats),
    }


def run_step(
    before: int, after: int, at: int = 1000, *, seeds: int = SEEDS, blocks: int = BLOCKS
) -> dict:
    def sched(h: int) -> Hashrate:
        return Hashrate(before, 1) if h < at else Hashrate(after, 1)

    stats = []
    for seed in range(1, seeds + 1):
        rows = simulate(
            window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=blocks, seed=seed
        )
        stats.append(summarize(rows, start_h=at))  # post-transition tail of last TAIL
    return {"kind": f"step_{before}_to_{after}", "agg": aggregate(stats)}


def run_shock(*, seeds: int = SEEDS, blocks: int = BLOCKS) -> dict:
    def sched(h: int) -> Hashrate:
        if h < 1000:
            return Hashrate(1)
        if h < 3000:
            return Hashrate(100)
        if h < 5000:
            return Hashrate(10)
        if h < 7000:
            return Hashrate(50)
        if h < 8500:
            return Hashrate(2)
        return Hashrate(1)

    stats = []
    for seed in range(1, seeds + 1):
        rows = simulate(
            window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=blocks, seed=seed
        )
        stats.append(summarize(rows, start_h=8500, tail=min(TAIL, blocks - 8500)))
    return {"kind": "shock", "agg": aggregate(stats)}


def run_alt(lo: int, hi: int, period: int, *, seeds: int = SEEDS, blocks: int = BLOCKS) -> dict:
    def sched(h: int) -> Hashrate:
        bucket = ((h - 1) // period) % 2
        return Hashrate(lo if bucket == 0 else hi)

    stats = []
    runaway = 0
    for seed in range(1, seeds + 1):
        rows = simulate(
            window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=blocks, seed=seed
        )
        st = summarize(rows)
        # runaway if rel grows without bound relative to max(lo,hi)*5
        if st["rel_max"] > max(lo, hi) * 20 or st["rel_min"] <= 0:
            runaway += 1
        stats.append(st)
    agg = aggregate(stats)
    agg["runaway_seeds"] = runaway
    return {"kind": f"alt_{lo}_{hi}_p{period}", "agg": agg}


def determinism_check() -> dict:
    seeds = [1, 7, 21, 42, 50]
    hr = Hashrate(10, 1)

    def sched(_h: int) -> Hashrate:
        return hr

    out = {}
    ok = True
    for seed in seeds:
        a = simulate(window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=3000, seed=seed)
        b = simulate(window=W, damp_num=DN, damp_den=DD, schedule=sched, blocks=3000, seed=seed)
        same = (
            seq_hash(a) == seq_hash(b)
            and a[-1]["bits"] == b[-1]["bits"]
            and a[-1]["target"] == b[-1]["target"]
        )
        out[str(seed)] = same
        ok = ok and same
    return {"ok": ok, "seeds": out}


def pass_fixed(agg: dict, target_rel: float) -> tuple[bool, str]:
    """
    Use EXPECTED interval (controller output), not raw exponential draws.
    Natural PoW variance is large; controller should keep expected ~600.
    """
    reasons = []
    if agg["frac_expected_mean_480_720"] < 0.85:
        reasons.append(
            f"expected_mean band frac={agg['frac_expected_mean_480_720']:.2f} < 0.85"
        )
    # relative difficulty near target
    rel = agg["rel_mean_across_mean"]
    if abs(rel - target_rel) / target_rel > 0.25:
        reasons.append(f"rel_mean {rel:.2f} far from {target_rel}")
    # expected interval std across time should not be huge vs control later
    if agg["expected_std_across_mean"] > 250 and target_rel >= 10:
        # allow some movement; hard fail only if extreme
        if agg["expected_std_across_mean"] > 800:
            reasons.append(f"expected_std {agg['expected_std_across_mean']:.1f} extreme")
    # no invalid extremes in rel across seeds
    if agg["rel_min_across_min"] <= 0:
        reasons.append("non-positive rel")
    return (len(reasons) == 0, "; ".join(reasons) or "ok")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {
        "candidate": {"W": W, "D": f"{DN}/{DD}"},
        "seeds": SEEDS,
        "blocks": BLOCKS,
        "tail": TAIL,
        "scenarios": {},
    }
    print(f"W{W}/D{DN}/{DD} stochastic validation: seeds={SEEDS} blocks={BLOCKS}")

    # 1x
    print("... 1×")
    s1 = run_fixed(1)
    ok1, why1 = pass_fixed(s1["agg"], 1.0)
    # for 1x also require rel stays near 1
    if s1["agg"]["rel_mean_across_mean"] > 1.2:
        ok1, why1 = False, f"rel drifted to {s1['agg']['rel_mean_across_mean']:.3f}"
    s1["pass"] = ok1
    s1["why"] = why1
    report["scenarios"]["x1"] = s1
    print("  ", ok1, why1, f"exp_mean={s1['agg']['expected_mean_across_mean']:.1f}")

    # ×10 / ×100 + controls
    for mult in (10, 100):
        print(f"... ×{mult}")
        sx = run_fixed(mult)
        cx = run_control(mult)
        ok, why = pass_fixed(sx["agg"], float(mult))
        # compare controller expected_std to control expected_std (control should be ~0)
        ctrl_exp_std = cx["agg"]["expected_std_across_mean"]
        cand_exp_std = sx["agg"]["expected_std_across_mean"]
        # controller may move a bit; fail if >> natural (control is flat expected)
        if cand_exp_std > 400:
            ok, why = False, f"controller expected_std {cand_exp_std:.1f} too high vs control {ctrl_exp_std:.1f}"
        sx["control"] = cx
        sx["pass"] = ok
        sx["why"] = why
        report["scenarios"][f"x{mult}"] = sx
        print(
            "  ",
            ok,
            why,
            f"exp_mean={sx['agg']['expected_mean_across_mean']:.1f}",
            f"rel={sx['agg']['rel_mean_across_mean']:.2f}",
            f"exp_std={cand_exp_std:.1f}",
        )

    # step-up
    print("... step-up")
    step_up_ok = True
    for b, a in ((1, 2), (1, 5), (1, 10), (1, 100)):
        st = run_step(b, a, at=1000)
        ok, why = pass_fixed(st["agg"], float(a))
        st["pass"] = ok
        st["why"] = why
        report["scenarios"][f"step_{b}_to_{a}"] = st
        step_up_ok = step_up_ok and ok
        print(f"  {b}→{a}", ok, why)

    # step-down (warm-up by starting high from genesis is imperfect; use long pre via step from high)
    print("... step-down")
    step_down_ok = True
    for b, a in ((100, 50), (100, 10), (100, 1), (10, 5), (10, 2), (10, 1)):
        # warm: run at `b` from start, step to `a` at 3000
        st = run_step(b, a, at=3000)
        # for drop to 1x, POW_LIMIT may keep rel>=1; expected ~600 if rel~1
        target = float(max(a, 1))
        ok, why = pass_fixed(st["agg"], target if a >= 1 else 1.0)
        # when dropping to 1, rel should approach ~1 and expected~600
        if a == 1:
            ok = st["agg"]["frac_expected_mean_480_720"] >= 0.85
            why = "ok" if ok else f"post-drop expected band {st['agg']['frac_expected_mean_480_720']:.2f}"
        st["pass"] = ok
        st["why"] = why
        report["scenarios"][f"step_{b}_to_{a}"] = st
        step_down_ok = step_down_ok and ok
        print(f"  {b}→{a}", ok, why)

    print("... shock")
    sh = run_shock()
    # final segment is 1× → expected ~600, rel~1
    ok = sh["agg"]["frac_expected_mean_480_720"] >= 0.80 and sh["agg"]["rel_mean_across_mean"] < 1.5
    sh["pass"] = ok
    sh["why"] = "ok" if ok else "final 1× segment not recovered"
    report["scenarios"]["shock"] = sh
    print("  ", ok, sh["why"])

    print("... volatile")
    vol_ok = True
    for lo, hi, p in ((10, 100, 100), (10, 100, 30), (2, 20, 100)):
        alt = run_alt(lo, hi, p)
        ok = alt["agg"]["runaway_seeds"] == 0 and alt["agg"]["rel_min_across_min"] > 0
        alt["pass"] = ok
        alt["why"] = "ok" if ok else f"runaway={alt['agg']['runaway_seeds']}"
        report["scenarios"][f"alt_{lo}_{hi}_p{p}"] = alt
        vol_ok = vol_ok and ok
        print(f"  alt {lo}/{hi} p{p}", ok)

    print("... determinism")
    det = determinism_check()
    report["determinism"] = det
    print("  ", det)

    # POW_LIMIT note
    report["pow_limit_floor"] = {
        "1x": 600,
        "0.1x": 6000,
        "0.01x": 60000,
        "changed": False,
    }

    overall = (
        report["scenarios"]["x1"]["pass"]
        and report["scenarios"]["x10"]["pass"]
        and report["scenarios"]["x100"]["pass"]
        and step_up_ok
        and step_down_ok
        and report["scenarios"]["shock"]["pass"]
        and vol_ok
        and det["ok"]
    )
    report["overall"] = "PASS" if overall else "FAIL"

    # console report
    def line(name: str, sc: dict) -> None:
        a = sc["agg"]
        print(f"## {name}")
        print(f"- actual interval mean: {a['actual_mean_across_mean']:.1f}")
        print(f"- expected interval tail mean: {a['expected_mean_across_mean']:.1f}")
        print(
            f"- expected interval tail p05/p95: "
            f"{a['expected_p05_across_mean']:.1f}/{a['expected_p95_across_mean']:.1f}"
        )
        print(
            f"- difficulty tail mean/p05/p95: "
            f"{a['rel_mean_across_mean']:.3f}/"
            f"{a['rel_p05_across_mean']:.3f}/{a['rel_p95_across_mean']:.3f}"
        )
        if "control" in sc:
            c = sc["control"]["agg"]
            print(
                f"- control expected mean/std: "
                f"{c['expected_mean_across_mean']:.1f}/{c['expected_std_across_mean']:.1f}"
            )
        print(f"- PASS/FAIL: {'PASS' if sc['pass'] else 'FAIL'} ({sc.get('why','')})")

    print()
    print("# W30/D1/16 STOCHASTIC VALIDATION")
    print(f"- Seeds: {SEEDS}")
    print(f"- Blocks per seed: {BLOCKS}")
    line("1×", report["scenarios"]["x1"])
    line("×10", report["scenarios"]["x10"])
    line("×100", report["scenarios"]["x100"])
    print(f"## STEP-UP\n- PASS/FAIL: {'PASS' if step_up_ok else 'FAIL'}")
    print(f"## STEP-DOWN\n- PASS/FAIL: {'PASS' if step_down_ok else 'FAIL'}")
    print(f"## SHOCK\n- PASS/FAIL: {'PASS' if report['scenarios']['shock']['pass'] else 'FAIL'}")
    print(f"## VOLATILE\n- PASS/FAIL: {'PASS' if vol_ok else 'FAIL'}")
    print(f"## DETERMINISM\n- PASS/FAIL: {'PASS' if det['ok'] else 'FAIL'}")
    print(f"\nOVERALL STOCHASTIC:\n    {report['overall']}")

    (OUT / "stochastic_W30_D16_final.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8"
    )
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
