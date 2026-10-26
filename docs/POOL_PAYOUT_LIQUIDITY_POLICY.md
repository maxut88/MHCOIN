# Pool payout liquidity policy (Coinbase Maturity 100)

**Status:** preparation for owner review — **not** applied to production in this stage.  
**Do not demote** historical `matured_sats` without a separate migration approval.

## Problem

After pool `MATURE_CONFIRMS=100`, **new** rounds wait 100 confirms.  
Ledger may still hold **`matured_sats` from the previous depth-20 era** whose backing coinbase UTXOs have only ~27–99 confirms.  
Once consensus activation height `H*` is live, those UTXOs are **not consensus-spendable** until age ≥ 100.

## Safe policy (recommended)

1. **Keep ledger credits** (no automatic demotion).  
2. **Liquid probe must exclude immature CB** (aligned with wallet send) — prepared in worktree: `spendable_total(..., tip_height, activation)` + `PayoutEngine._liquid_sats`.  
3. When `liquid < threshold` or intent cannot build: **defer** via existing `mark_intent_failed` / backoff — **no double complete**.  
4. Optional ops near `H*`: raise temporary payout threshold or set env freeze flag (if added) — owner approval required.  
5. Do **not** craft manual spends of young coinbase.

## Freeze / unfreeze (ops checklist — execute only with approval)

| Action | How |
|--------|-----|
| Soft freeze | Set `MHCOIN_POOL_PAYOUT_THRESHOLD` temporarily above any balance (e.g. huge value); keep 1000 MHC documented as policy default to restore |
| Unfreeze | Restore `MHCOIN_POOL_PAYOUT_THRESHOLD=100000000000` |
| Verify | No new intents while frozen; after unfreeze, intents only when liquid ≥ threshold |

## Tests

- Intent create/complete idempotency: `tests/test_payout_intent_recovery.py`  
- Liquid maturity filter: covered by wallet maturity exclusion + unit check in owner-review matrix  

## After H*

Expect temporary “matured on books / not liquid on chain” until coinbases age. Monitor `insufficient liquid` deferrals; do not wipe intents.
