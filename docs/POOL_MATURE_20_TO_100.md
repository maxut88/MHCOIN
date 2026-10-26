# Pool maturity migration 20 → 100 (preparation only)

## Current production (read-only observation, 2026-10-20)

- `MHCOIN_POOL_MATURE_CONFIRMS` effective: **20** (config default / env)
- `MHCOIN_POOL_PAYOUT_THRESHOLD`: **100 MHC** (enabled)
- Tip ≈ **1270**
- Immature rounds: **20** (last 20 blocks)
- Matured ledger Σ ≈ **900 MHC**, immature Σ ≈ **990 MHC**, paid Σ ≈ **60480 MHC**
- Open `payout_intents`: **5 pending** all with `insufficient funds` (wallet/UTXO race — separate fix in this worktree)
- Consensus-spendable coinbase at tip+1: **~42400 MHC** age≥100; **~3100 MHC** age 20–99; **~850 MHC** age&lt;20

## What changing env to 100 does

`mature_confirms` only gates **immature → matured** transitions for rounds still `status=immature`.

| Already `matured` / `paid` credits | **Unchanged** — no automatic demotion |
| New immature rounds | Wait until `tip >= height + 100` |
| Ledger liabilities | Preserved — balances/payout history not wiped |

## Safe transition (recommended)

1. **Before consensus activation:** deploy wallet fix (`ensure_change_address` without second LMDB open) so payouts stop false `insufficient funds`.
2. Set `MHCOIN_POOL_MATURE_CONFIRMS=100` and restart pool **only with operator approval** (this TZ forbids restart now).
3. Do **not** reverse already-matured balances (avoids double-count / miner trust break).
4. Wallet/payout path must exclude coinbase with age &lt; 100 once consensus activation is live (policy + consensus).
5. Expect temporary: ledger `matured` may exceed consensus-liquid wallet funds if past credits matured at 20 while UTXOs are still &lt;100 deep — payouts then queue/backoff until coinbases age in, or spend non-coinbase UTXOs first.

## Optional (needs separate explicit permission — NOT default)

- Demote credits whose source round still has `&lt;100` confirms from `matured` back to `immature` (complex, risk of UX shock).
- Freeze auto-payout until liquid ≥ matured liabilities from consensus-mature UTXOs only.

## Double-pay / double-debit

- `payout_intents` + `complete_payout_intent` remain the idempotency boundary — migration must not clear intents or replay sent txids.
- Pending intents with `insufficient funds` should be left to retry after wallet fix; do not delete.

## Compatibility with consensus activation

| Order | Risk |
|-------|------|
| Raise pool to 100 **before** consensus activation | Low — accounting stricter than consensus |
| Activate consensus **before** raising pool | **High** — pool marks payable at 20, consensus rejects spends until 100 |
| Raise pool and activate same release | Preferred |

**Conclusion:** pool must be on **100 confirms before or with** consensus activation.
