# MHCOIN Core 0.4.2.0

**Hard fork — upgrade all nodes before height 1500.**

## Consensus

- **Protocol version 2** (v1 peers are rejected).
- From height **1500**: epoch difficulty — retarget every **2016** blocks, full step, timespan clamp ×1/4…×4, no damping.
- Heights **&lt; 1500**: legacy W30 + 1/16 damping (historical chain stays valid).
- Bits frozen **1500–2015**; first epoch retarget at height **2016**.

## Desktop

- **Import Private Key (WIF)** — Welcome + Settings (WIF or 64-char hex).
- **Show Private Key (WIF)** — Settings, for the active address (password + confirm).

## Mining pool

- Native HASH256 pool: JSON `:3333`, Stratum `:3334`, stats `:8888` (PROP). See [POOL.md](POOL.md).
- Pool CLI hot loop: no per-hash `time.perf_counter()`; abort poll every 25 000 hashes (Desktop parity); terminal H/s is mining-only time.
- Pool stats: online-only worker hashrate (dead workers do not inflate totals).
- Ops: `stop_pool.sh` sweeps orphan `run_pool.py`; `start_pool.sh` avoids dual instances.

## Upgrade

1. Back up `wallet.json` (and password / seed).
2. Install 0.4.2.0 (or `git pull` + `pip install -e .` on `main`).
3. Restart Core / node — confirm peers show `protocol: 2`.
4. Do not keep any live node on 0.4.1.x after others have upgraded.
