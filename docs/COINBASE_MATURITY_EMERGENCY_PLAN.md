# Coinbase Maturity 100 — Emergency / Rollback Plan

**Scope:** Contingency documentation only. Does not authorize production actions.

---

## Critical principle

After the activation height has been **crossed** on the economic majority chain, simply downgrading software or setting `COINBASE_MATURITY_ACTIVATION["mainnet"]=0` on a subset of nodes **does not** safely roll back consensus. Old software may accept immature coinbase spends that upgraded nodes reject → **chain split risk**.

Treat pre-activation and post-activation differently.

---

## A. Before activation height is reached

| Incident | Response |
|----------|----------|
| Bug found in maturity code | Halt rollout; keep activation at 0; ship fix; re-test; **do not** set height |
| Activation height chosen too soon | Publish delay: ship new release with later `H*` **before** old `H*` is reached; coordinate all operators |
| Pool misconfigured (still 20) | Pause auto-payouts; set `MATURE_CONFIRMS=100`; do not wipe balances |
| Desktop ships wrong network | Recall build; revoke download links; re-release mainnet-only |
| Controlled node fails upgrade | Fix node before continuing; do not activate while majority hashrate is old |

**Safe rollback window:** while tip &lt; `H*`, activation can be postponed by releasing a new build that never activates (or activates later). No chain rewrite.

---

## B. After activation height is reached

| Symptom | Do | Do not |
|---------|----|--------|
| Minority peers on “easy spend” fork | Publish canonical tip hash/work; urge upgrade; mine/relay only on enforcing chain | Weaken maturity on majority nodes to “follow” minority |
| Consensus mismatch / split | Identify which tip has more work **and** valid maturity rules for upgraded majority; coordinate operators | Auto-reorg by deleting datadir without analysis |
| Block production stall | Check templates/mempool rejects; peer connectivity; difficulty | Reset genesis / wipe UTXO |
| Mass TX rejects | Confirm rejects are immature CB only; communicate confirm requirement | Disable validation |
| Wrong maturity calculation | Emergency patch release; coordinate; document affected txs | Silently change history |
| Pool payout failures | Pause payouts; verify liquid CB age≥100; inspect intents (no double complete) | Replay completed intents |
| Desktop cannot spend | Confirm immature exclusion; upgrade wallet; never force raw immature spend | Instruct users to use old non-enforcing nodes |
| UTXO corruption suspicion | Stop writes; restore from **pre-incident** backup on a staging node; compare fingerprints | Run experimental repairs on sole production copy |

**Temporary pool pause:** set payout threshold unreachable or disable autopayout flag (ops) until liquid consensus-mature funds cover liabilities. Preserve `payout_intents` rows.

---

## C. Communication template (operators)

1. Current tip height + hash + chain work.
2. Whether incident is pre- or post-activation.
3. Required software SHA.
4. Whether to pause mining / payouts.
5. Next checkpoint time.

---

## D. Data preservation

Always retain: chain.sqlite, chainstate, undo, pool.sqlite, wallet backups, and reject logs showing `immature coinbase` before any recovery attempt.
