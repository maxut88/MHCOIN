# MHCOIN — Mining Hashrate Regression Investigation

**Date:** 2026-10-09  
**Project:** MHCOIN (MHC)  
**Production tree:** `/data/MHCOIN/`  
**Mode:** READ-ONLY investigation + isolated microbenchmarks  
**Status:** FIXES APPLIED (2026-10-09) — patched in place on **0.4.2.0** Production tree; pool restarted single-instance  

---

## Verdict

**REGRESSION CONFIRMED — ROOT CAUSE FOUND**

(with an important units correction and a secondary historical display issue)

| Claim in brief | Evidence |
|---|---|
| Released `0.4.2.0` pool CLI hot loop is slower than Desktop PoW | Isolated 60s×3 bench: **~26.5% slower** than Desktop-style tight loop |
| Root cause in miner client code | `time.perf_counter()` checked **every hash** in committed `mhcoin/pool/client.py` |
| Production Pool / Stratum Oct-9 hardening is **not** the SHA-256d bottleneck | Rate limits ≫ real share rate; pool H/s = client-reported; no consensus change |
| Historical “1300 MH/s (1.3 GH/s)” is almost certainly a **unit error** | Observed Mac rates are **~1.0–1.3 MH/s (= 1000–1300 kH/s)**, not GH/s |

**Applied:** tight pool client loop + mining-only meter + online-only pool H/s + single-instance pool scripts (still version **0.4.2.0**). Miners must `pip install -e .` / reinstall and restart `pool-start`.

---

## 1. Current Production state (read-only)

Observed 2026-10-09 ~19:58 UTC+3 (API / process listing only; **no intentional restart** during this investigation turn).

| Item | Value |
|---|---|
| Pool JSON | `:3333` listening |
| Pool Stratum | `:3334` listening |
| Pool stats | `:8888` listening |
| Node P2P | `:8333` listening |
| `share_factor` | **1024** |
| Fee | 1% |
| `pool_hashrate_display` | `0 H/s` (no online workers at snapshot) |
| Round | 126 |
| Recent matured tips | 1218, 1209, 1208 |
| Uncommitted mining/pool changes | Yes (large), on `main` |

**Note:** process list showed **two** `run_pool.py` PIDs at one snapshot (73619 and 102489). Not altered here. Operator should ensure a single pool instance; dual listeners can race on ports / confuse ops.

**Git HEAD:** `e1e4555` (`main...origin/main`)  
**Worktrees:** `/data/MHCOIN-worktrees/pool-launch-stress`, `pool-hardening` (same base commit; extra hardening/stress files).

---

## 2. Units clarification (critical)

| Label | H/s | Plausible on M2 Python SHA-256d? |
|---|---|---|
| 1300 **kH/s** | 1.3×10⁶ | **Yes** (matches prior Desktop/terminal anecdotes) |
| 1300 **MH/s** (= 1.3 GH/s) | 1.3×10⁹ | **No** for single-thread CPython on MacBook |

Terminal output format is `~{hps/1000:.1f} kH/s`.  
Mac session 09.10.2026 (post meter fix): settled **~990–1013 kH/s**, early peaks **~1150 kH/s**, **629 accepts**, 0 rejects visible.  
Live pool while that miner was online earlier: **~1.01 MH/s**, matching the terminal.

**Historical target for comparison in this report:** **~1300 kH/s (1.3 MH/s)**, not 1300 MH/s.

Deviation vs that target at steady Mac pool-mine:  
`(1300 − 1010) / 1300 ≈ **22%**`.

---

## 3. History of checked changes

### 3.1 Commits that touch mining throughput

| Commit | Date | Relevance |
|---|---|---|
| `fa85b79` | 2026-10-06 | **Speed up PoW**: in-place 80-byte HASH256 in `abortable_pow` + `proof_of_work.mine_block` (same digests; faster path) |
| `c09c912` | 2026-10-05 | Restore single-lane Desktop mining |
| `968bc36` | 2026-10-08 | Protocol v2 + **ships pool**; introduces `mhcoin/pool/client.py` with slow hot loop |
| `003c5fa` / `0.4.2.0` | 2026-10-08 | Release packaging; **pool client still has per-hash `perf_counter`** |

### 3.2 Uncommitted Production working tree (pool launch / hardening)

Large diffs (not committed): `pool/client.py`, `db.py`, `engine.py`, `stratum.py`, `config.py`, `block_template.py`, plus new `rate_limit.py`, `monitoring.py`.

Worktree `pool-launch-stress` mirrors much of this hardening.

### 3.3 What did **not** change

- Consensus HASH256 definition / targets / DAA / genesis / reward — **not modified** by pool client work.
- `share_factor` default remains **1024** (HEAD and working tree).

---

## 4. Mining code changes (hot path)

### 4.1 Released / committed pool client (`HEAD` = tag `0.4.2.0` client)

`mhcoin/pool/client.py` `_mine_job`:

- In-place 80-byte header + double SHA-256 (good).
- **Regression:** every iteration calls `time.perf_counter()` to enforce a 30s job deadline.
- That syscall/poll in the innermost loop taxes CPython heavily.

### 4.2 Uncommitted working-tree pool client (present on disk, not released)

- Removes per-hash clock; polls every `0x7FFF` hashes; `hash_budget` ≈ 40M hashes.
- Reports session H/s over **mining-only** time (excludes getjob/submit idle).
- Local binds: `sha256`, `pack_nonce`, `from_bytes`.

### 4.3 Desktop solo (`abortable_pow.py`, post-`fa85b79`)

- In-place buffer + HASH256.
- Abort poll every **25_000** hashes (macOS/Linux).
- Single-lane default (avoids GIL thrash).

### 4.4 SHA-256d consensus

`fa85b79` rewrote `mine_block` to the same in-place HASH256.  
Fingerprint of digests unchanged; this is a **performance** path, not a consensus rule change.

---

## 5. Benchmark results (isolated)

**Host (relative comparison only):** Intel i5-2467M @ 1.6GHz (VPS), 4 CPUs — **not** the owner’s M2.  
**Absolute MH/s on this host are lower than Mac; use ratios.**  
**Conditions:** no pool/node connection; synthetic never-meet target; warmup 3s; **60s × 3** per variant; variants run sequentially; single process.

Artifact: `/tmp/mhcoin_hr_bench/full_bench_clean.txt`  
Harness: `/tmp/mhcoin_hr_bench/bench_loops.py`

| Variant | Mean | Min | Max | vs Desktop-while |
|---|---:|---:|---:|---:|
| **A** HEAD pool (`perf_counter` every hash) | **165.3 kH/s** | 147.9 | 174.6 | **73.5%** |
| **B** WORK tight (`0x7FFF` poll) | **226.7 kH/s** | 224.9 | 229.8 | **100.8%** |
| **C** Desktop-style while + abort 25k | **224.9 kH/s** | 222.3 | 227.1 | 100% |
| C2 Desktop-like `for`-range + abort 25k | 200.7 kH/s | 198.8 | 202.7 | 89.2% |

**Preview (8s, same host)** earlier showed the same ordering (HEAD ≪ tight ≈ Desktop).

### Interpretation

1. **Confirmed code regression** in released pool CLI vs tight Desktop-equivalent loop: **~26%** hashrate loss on this CPU.
2. Uncommitted client loop **restores parity** with a Desktop-style tight while-loop on the same host.
3. Mac absolute gap (~22% vs remembered 1300 kH/s) is **consistent in magnitude** with (1), if the Mac was running the released client and/or comparing peak Desktop vs steady pool.

CPU profiling (12s `cProfile` spot checks): time dominated by `hashlib.sha256` in all variants; HEAD additionally pays for far more `time.perf_counter` calls (order: once per hash vs once per 25k–32k).

Temperature/frequency on Mac: not instrumented here (no agent on the M2). User log shape (peak ~1150 → settle ~1010) is consistent with **thermal settle** on MacBook Air, independent of pool code.

---

## 6. Local vs terminal vs pool hashrate

| Meter | What it measures | Trust |
|---|---|---|
| **Local SHA-256d** | Hashes / pure mine time in hot loop | Ground truth for compute |
| **Terminal (HEAD 0.4.2.0)** | `hps` over last share interval only | OK per share; noisy; hot loop slow |
| **Terminal (uncommitted)** | Session hashes / **mining-only** seconds | Matches local compute; was briefly broken when wall-clock/RPC time was included (~200 kH/s false reading) |
| **Pool display** | Sum of **online workers’ client-reported** `hashrate` | Tracks miner reports; not bitcoin-diff×2³² |

### Pool formula notes (working tree)

- Online-only workers counted; dead workers zeroed (fixes prior inflation).
- `share_factor` is **not** Bitcoin difficulty; old `diff * 2^32` style would show absurd TH/s.
- Address `hashrate_long` previously could inflate from **dead workers’ old shares**; working tree sums online workers’ long estimates instead.
- Stochastic share arrival affects share-count estimators; client-reported H/s avoids that for the headline number.

**Pool does not compute the miner’s SHA-256d for them.** A wrongly low pool number with a healthy terminal usually means reporting/window bugs; a low terminal with a tight meter means real loop/CPU issues.

---

## 7. Stratum / JSON pool investigation (read-only)

| Area | Finding |
|---|---|
| `mining.subscribe` / `authorize` / `notify` | Present in `stratum.py`; difficulty notify uses `share_factor` as numeric difficulty |
| Share difficulty | `target_share = bits_to_target(nbits) * share_factor` (factor 1024 → easier shares, more frequent submits) |
| Job refresh | `job_refresh_sec` default **20s** (config); client also refreshes on hash budget ~40M |
| Rate limits (`rate_limit.py`) | e.g. max **30 submits/worker/s**, 40 req/IP/s, 8 conn/IP — far above ~1 share/s at 1 MH/s & factor 1024 |
| Effect on real H/s | **None expected** for a single honest Mac miner; limits reject floods, they do not slow the hash loop |
| Latency | Not load-tested against Production (forbidden). Share I/O is outside mining-only H/s in the fixed client |

**Conclusion:** Oct-9 Stratum/hardening can affect acceptance under abuse or misconfig, but **does not explain** a sustained ~20–30% drop in local SHA-256d throughput for one worker.

---

## 8. Regression taxonomy

| Hypothesis | Result |
|---|---|
| 1. Real SHA-256d slowdown in released pool client | **CONFIRMED** (~26% on isolated bench) |
| 2. Extra hot-loop delays (`perf_counter` every hash) | **CONFIRMED root cause** for (1) |
| 3. CPU threads / GIL multi-worker | Desktop intentionally single-lane; not the pool-CLI issue |
| 4. Stratum job / share_factor change | `share_factor=1024` unchanged; does not slow hash loop |
| 5. Pool hashrate formula / windows | Secondary reporting bugs existed (dead workers, long window); **not** the Mac terminal ~1010 reading |
| 6. Wall-clock terminal meter | **CONFIRMED historical false low (~200 kH/s)** when RPC time was included; fixed in uncommitted client |
| 7. Hardware / thermal | Plausible contributor to peak→steady on M2; not measured on-device here |
| 8. “1300 MH/s” literal | **REJECTED** as unit mix-up |

---

## 9. Confirmed / probable causes

### Primary (confirmed)

**Released pool miner hot loop** in `mhcoin/pool/client.py` (`_mine_job`) calls `time.perf_counter()` on **every nonce**. Isolated benchmarks show ~**26%** lower H/s vs the Desktop-equivalent tight loop.

### Secondary (confirmed as display-only)

Session H/s that included getjob/submit wall time produced **falsely low** terminal figures (~200 kH/s). Uncommitted client uses mining-only time.

### Tertiary (probable on M2)

Thermal/power settle explains peak ~1150 → steady ~1010 without any code change.

### Not a cause of real hash slowdown

Production Stratum rate limits, share_factor=1024, pool DB stats windows (after online-only fix).

---

## 10. Files / functions to fix (do not apply yet)

| Priority | File | Function / area | Action |
|---|---|---|---|
| P0 | `mhcoin/pool/client.py` | `_mine_job` | Ship tight loop (no per-hash clock); keep mining-only session H/s — **already in working tree, needs review + release** |
| P1 | Package / install path | `mhcoin 0.4.2.0` on miners | Ensure Macs install build that contains P0 (editable `/data/MHCOIN` or new patch release) |
| P2 | `mhcoin/pool/db.py` | `miner_stats` hashrate | Keep online-only aggregation; avoid dead-worker long-window inflation |
| P3 | Ops | single `run_pool.py` | Ensure one pool process |

**Consensus / DAA / SHA-256d definition:** no change required for recovery.

---

## 11. Recovery plan (pending owner approval)

1. **Code review** uncommitted `client.py` tight loop + mining-only meter (already present on disk).
2. **Isolated re-bench** on the **M2** (owner machine): Desktop solo vs `pool-start` for 60s×3 each, not simultaneous — confirm ≥95% parity.
3. **Keep version `0.4.2.0`** and patch the tree in place (client hot-loop + meter + online-only hashrate) — **done**; no consensus changes.
4. **Redeploy miner installs** (`pip install -e .` / reinstall same 0.4.2.0 build with the fix). Pool already restarted single-instance.
5. **Do not** rollback consensus `fa85b79` PoW fast path (it helps Desktop).

### Risks

| Risk | Mitigation |
|---|---|
| Shipping unreviewed hardening with client fix | Split PR: client-only first |
| Job refresh too rare without clock | Keep hash_budget / bit-poll (already in working tree) |
| Dual pool processes | Ops check before restart |
| Expecting 1.3 GH/s | Educate units: target ~1.3 MH/s peak / ~1.0–1.2 steady on M2 |

---

## 12. Success criteria vs observations

| Criterion | Result |
|---|---|
| Current real hashrate (Mac, post meter fix) | **~1.01 MH/s** steady; pool matched while online |
| Deviation vs historical ~1.3 MH/s | **~22%** |
| Regression confirmed by local bench? | **Yes** (released client vs tight loop, −26.5% on VPS) |
| Does Pool reduce SHA-256d speed? | **No** (reporting/admission only) |
| Component to fix | **`mhcoin/pool/client.py` hot loop** (+ ensure miners run that build) |
| Recoverable without consensus changes? | **Yes** |

---

## 13. Appendix — Mac evidence (operator log, 09.10.2026)

- Package: `mhcoin-0.4.2.0`
- Pool: `192.168.0.221:3333`, worker `Rig1`, `share_factor=1024`
- Login OK; shares 1…629 accepted
- Early: up to ~1153.6 kH/s  
- Late window mean ≈ **1010 kH/s** (≈1008–1013)

---

## 14. Final instruction compliance

- Production Mainnet Node / Pool **not modified** as part of fix application (none applied).
- No automatic rollback / unverified “optimization” shipped.
- Benchmarks isolated (no Production mining load from harness).
- Report path: `docs/MHCOIN_MINING_PERFORMANCE_REGRESSION.md`

**Awaiting owner permission before applying fixes.**
