# Coinbase Maturity 100 — Mainnet Activation Preparation

**Status:** PREPARATION ONLY — `COINBASE_MATURITY_ACTIVATION["mainnet"]` remains **0**  
**Worktree:** `audit/coinbase-maturity-p2p`  
**Date:** 2026-10-20  
**Production:** not modified / not restarted / no real TX from this task

---

## 1. Mainnet audit (read-only)

| Item | Value |
|------|-------|
| Tip height | **1270** |
| Tip hash | `8e608a0927c0d71f…00000000` |
| Chain work | `5590455076510` |
| Peers | **1** (`192.168.0.221:8333`) — WARNING thin mesh |
| Mempool | **0** txs |
| Explorer height | 1270 |
| Historical early CB spends (age&lt;100) | **175** (prior audit) — remain valid pre-activation |
| UTXO entries | 1162 (927 coinbase / 235 non-cb) |
| CB value | ≈46350 MHC |
| CB age at next block &lt;20 / 20–99 / ≥100 | 17 / 62 / 848 (≈850 / 3100 / 42400 MHC) |
| Pool matured / immature / paid | ≈900 / 990 / 60480 MHC |
| Open payout intents | **5 pending** (`insufficient funds`) + 3 sent |
| Payouts | 265 |
| Auto-payout threshold | 100 MHC (already enabled in prod — not changed by this task) |
| Observed block interval (last ~50) | median **≈97 s**, mean ≈240 s |

---

## 2. Rule (code ready, inactive on mainnet)

```
COINBASE_MATURITY = 100
COINBASE_MATURITY_ACTIVATION["mainnet"] = 0   # DO NOT SET until approval
```

Semantics: spendable at spend_height ≥ coinbase_height + 100 (H+100 OK, H+99 reject).  
Enforced in: `validate_transaction` → block validation, mempool (tip+1), template, relay, reorg reconnect, wallet select (when tip/activation known).

Pre-activation CB spent post-activation: still needs age ≥ 100 (tested).

---

## 3. Recommended ACTIVATION_HEIGHT (proposal only)

**Do not write this into production.**

Observed mining is much faster than 600 s target. Lead time must use **wall clock**, converted with **observed** median interval:

\[
H^* = tip_{announce} + \max\!\big(2016,\ \lceil T_{wall}/t_{median}\rceil\big)
\]

With tip≈1270, \(t_{median}≈97s\), \(T_{wall}=7\) days:

- \(\lceil 604800/97\rceil ≈ 6235\) → \(H^* ≈ 1270+6235 = 7505\)
- Ops buffer (+~1000) → **recommended lock candidate: 8500**
- Wall time at median: \((8500-1270)×97/3600 ≈ 7.0\) days
- If hashrate drops toward 600 s/block, the same height is farther in calendar time (safer)

**Procedure:** announce formula + provisional 8500 → all controlled nodes upgrade → 24 h soak → **recompute** from then-current tip if tip already &gt; 8500−2000 → only then set activation in a release with separate approval.

Minimum wall clock after last controlled node upgraded: **72 h** before height is crossed.

---

## 4. Pool 20 → 100

See `POOL_MATURE_20_TO_100.md`.

**Must** raise `MHCOIN_POOL_MATURE_CONFIRMS=100` **before or with** consensus activation.  
Do not demote existing matured balances by default.  
Wallet fix required first (false insufficient funds).

---

## 5. Three-node / fork risk tests

| Scenario | New nodes | Old nodes |
|----------|-----------|-----------|
| Spend age 1/20/99 after activation | REJECT | ACCEPT |
| Spend age 100 | ACCEPT | ACCEPT |
| Historical block before activation | ACCEPT | ACCEPT |
| CB before act, spend after | needs age 100 | may accept early |

**Split risk:** HIGH if hashrate remains on old software after activation. Soft-fork style only if economic majority enforces.

Tests: `tests/test_maturity_activation_three_node.py`, `tests/test_coinbase_maturity.py`.

---

## 6. Deployment plan (after separate approval)

1. Backup: chainstate, chain.sqlite, wallet.json, pool.sqlite (Backup API), pool.env — verify integrity  
2. Ship release: maturity code + wallet `ensure_change_address` fix + pool docs; mainnet activation still 0  
3. Upgrade all controlled nodes/wallets/pool binaries  
4. Set pool `MATURE_CONFIRMS=100` (restart — needs approval)  
5. Clear/fix pending intents after wallet fix (no ledger wipe)  
6. Network soak: peers, tip match, no immature spends in mempool  
7. Lock `ACTIVATION_HEIGHT` in params for next release; upgrade again before height  
8. Monitor through activation: rejects `immature coinbase`, chainwork, peers, reorg depth, payouts  
9. **No blind rollback** of consensus after activation — see incident plan below  

---

## 7. Incident response (post-activation)

| Symptom | Action |
|---------|--------|
| Minority old nodes on side chain | Do not weaken maturity; urge upgrade; publish tip hash |
| Pool cannot pay | Confirm MATURE_CONFIRMS=100; check liquid ≥100-age CB; inspect intents |
| Accidental activation height too soon | Social emergency only — do not silently set activation=0 mid-flight |
| Reorg across activation | Re-validate with height-based rule; immature spends in post-act blocks stay invalid on new nodes |

---

## 8. READY / NOT READY

| Gate | Status |
|------|--------|
| Code + unit tests for rule | READY |
| Mainnet activation height set | **NOT set (correct)** |
| Pool 20→100 applied in prod | **NOT READY** |
| Wallet insufficient-funds fix in prod | **NOT READY** (fixed in worktree only) |
| ≥ majority nodes upgraded | **NOT READY** |
| P2P ≥3 public seeds | **NOT READY** (ops parallel track) |
| Operator approval for height | **PENDING** |

**Overall: NOT READY for activation — READY for coordinated prep / review.**
