# Activation height H* — proposal methodology (no value written to mainnet)

**Current code must keep** `COINBASE_MATURITY_ACTIVATION["mainnet"] = 0`.

## Inputs (refresh at announce time)

- `tip` from seed `node_status.json`
- `t_median` over last ≥50 best-chain blocks
- Independent full-node readiness (not stratum miner count)
- Wall-clock for: RC deploy, pool 20→100, desktop distribution, operator notice, buffer

## Formula

```
H* = tip_announce + max(2016, ceil(T_wall / t_median)) + buffer_ops
```

## Illustrative snapshot only (2026-10-22, NOT approved)

- tip ≈ 1702
- t_median ≈ 266 s
- T_wall = 7 days = 604800 s → ceil(604800/266) ≈ 2274
- raw ≈ 1702 + max(2016,2274) = 1702 + 2274 = **3976**
- With +1000 ops buffer → illustrative ~**5000**

Prior “8500” (from tip≈1270 / faster interval) is **obsolete** — recompute; do not auto-use 8500.

## Activation release plan (separate approval)

1. Network runs RC with activation 0 + pool at 100.
2. Owner locks H*.
3. New commit sets `COINBASE_MATURITY_ACTIVATION["mainnet"]=H*` only.
4. Upgrade all controlled nodes **before** H*; notify external operators (e.g. reachable peer 176.38.3.168).
5. Monitor rejects `immature coinbase` after H*.
