# Coinbase Maturity 100 — Future Activation Plan (NOT EXECUTED)

**Status:** documentation only. `COINBASE_MATURITY_ACTIVATION["mainnet"]` remains **0**.  
**Branch:** `feature/coinbase-maturity-100-integration`  
**Do not** set height 8500 (or any other value) without separate operator approval.

## Preconditions

1. All controlled nodes upgraded to a build that includes this integration (activation still 0).
2. Pool `MHCOIN_POOL_MATURE_CONFIRMS` raised **20 → 100** before or with consensus activation (restart needs approval).
3. Desktop Windows / macOS / Linux rebuilds published so wallets filter immature coinbase after activation.
4. Wallet/UTXO/payout fixes from PR #1–3 remain present (`utxo.reload()`, `ensure_change_address(utxo=)`, payout intents).
5. Network soak: tip match, peers healthy, no immature spends in mempool while activation=0 (baseline).

## Ordered rollout (after separate approval)

1. Backup chainstate, chain.sqlite, wallet, pool.sqlite, pool.env — verify integrity.
2. Ship release with maturity code; **mainnet activation still 0**.
3. Upgrade all controlled nodes, pool binary, Desktop clients.
4. Set pool `MHCOIN_POOL_MATURE_CONFIRMS=100` (operator-approved restart).
5. Do **not** demote already-matured ledger credits by default; do **not** clear payout intents.
6. Announce and lock a **future** `ACTIVATION_HEIGHT` using wall-clock lead time from then-current tip (recompute; do not blindly reuse provisional 8500).
7. Upgrade again before the height; monitor rejects (`immature coinbase`), chainwork, reorg depth, pool liquidity.
8. Minimum wall clock after last controlled node upgraded: **72 h** before height is crossed.

## Incident notes

- Old nodes after activation may still accept immature spends → split risk until upgraded.
- Do not silently set activation back to 0 mid-flight.
- Pool payout failures: confirm mature_confirms=100 and consensus-liquid UTXOs age ≥100.

## Explicitly out of scope for this document

Production deploy, env changes, push/merge, and setting a non-zero mainnet activation height.
