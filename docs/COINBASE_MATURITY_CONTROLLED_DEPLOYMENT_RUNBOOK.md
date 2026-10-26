# Coinbase Maturity 100 — Controlled Mainnet Deployment Runbook

**Preparation only.** No phase below may be executed without separate MHCOIN owner approval.  
**RC SHA:** `c96d0a52a72075a9fa6d6ce5e93ce0627f3acd96`  
**Invariant until activation release:** `COINBASE_MATURITY_ACTIVATION["mainnet"] = 0`

Related docs: `COINBASE_MATURITY_ACTIVATION.md`, `POOL_MATURE_20_TO_100.md`, `VPS_INDEPENDENT_NODES.md`, `COINBASE_MATURITY_EMERGENCY_PLAN.md`.

---

## Activation height methodology (do not auto-fill)

Do **not** hard-code 8500 or any other height without a fresh owner decision.

1. Record `tip_announce` and observed median block interval `t_median` over ≥50 recent mainnet blocks.
2. Choose wall-clock lead time `T_wall` covering: controlled node upgrades, pool config change, Desktop distribution, third-party operators, buffer (≥72 h after last controlled upgrade recommended).
3. Compute:

\[
H^* = tip_{announce} + \max\!\big(2016,\ \lceil T_{wall}/t_{median}\rceil\big) + buffer_{ops}
\]

4. Publish provisional `H*` → upgrade all controlled components with **activation still 0** → recompute if tip moved too close → only then ship a **second** release that sets `COINBASE_MATURITY_ACTIVATION["mainnet"] = H*` with explicit approval.
5. Nodes that IBD from genesis after activation must enforce maturity for spends at heights ≥ `H*` while accepting historical early spends at heights &lt; `H*` (already covered by height-based soft-fork semantics).

---

## Phase A — Pre-deployment (checklist)

- [ ] Verify RC SHA and regression logs match readiness report.
- [ ] Confirm RC excludes unrelated GPU miner / production WIP.
- [ ] Backups: `chain.sqlite`, chainstate/UTXO, `wallet.json`, `pool.sqlite`, `pool.env` (integrity-checked).
- [ ] Inventory controlled nodes (host, version, tip, peers).
- [ ] Inventory known third-party / VPS peers; contact plan.
- [ ] Pool: document current `MATURE_CONFIRMS` (prod default 20 if unset) and threshold (1000 MHC).
- [ ] Agree provisional activation methodology; **do not** write height into mainnet code yet.
- [ ] Freeze scope: maturity + documented pool/wallet fixes only.

## Phase B — Coordinated rollout (activation still 0 unless noted)

1. Deploy RC binaries to **all controlled** full nodes; restart **only with approval**.
2. Verify tip hash equality across controlled nodes; peer count ≥ target.
3. Deploy compatible pool binary; set `MHCOIN_POOL_MATURE_CONFIRMS=100` **before or with** consensus activation (never after). Threshold stays 1000 MHC.
4. Publish Desktop builds (Win/macOS/Linux) with same consensus params; confirm network=`mainnet`.
5. Upgrade Explorer to RC (confirmations + future maturity UX).
6. Bring up independent VPS nodes per `VPS_INDEPENDENT_NODES.md`.
7. Announce upgrade deadline to third-party operators.
8. Only after approval: ship activation release with `ACTIVATION_HEIGHT=H*`; upgrade everyone **before** `H*`.

## Phase C — Activation monitoring (around `H*`)

- Watch tip height vs `H*`; log `immature coinbase` rejects.
- Best block hash + chain work across seeds/VPS.
- Mempool: no immature CB spends surviving on upgraded nodes.
- Pool: no payouts attempting spends of CB age &lt; 100; intents idempotent.
- Reorg depth / orphan rate; investigate any split immediately (see emergency plan).

## Phase D — Post-activation

- 24–72 h stability: tips agree, payouts succeed from consensus-liquid UTXOs.
- Explorer balances/confirmations sane.
- Write post-activation report; keep activation height fixed (do not silently flip to 0).

---

## Explicit non-actions for this preparation TZ

- No `git push` / PR / merge / deploy / production restart.
- No change to `/data/MHCOIN` production tree.
- No non-zero mainnet activation in the RC used for first binary rollout.
- No deletion of backup worktrees.
