# Coinbase Maturity Activation (H*=2000)

## Rule

- `COINBASE_MATURITY = 100`
- Coinbase created in block **H** may be spent in block **H+100** or later
- Spending in **H+99** is rejected **after** activation
- `COINBASE_MATURITY_ACTIVATION["mainnet"] = 2000` (owner lock 2026-10-26)
- Other networks remain `0` (inactive) unless separately set

## Height semantics (exact)

| Spend height | Rule |
|---|---|
| ≤ 1999 | Legacy — maturity **not** enforced |
| ≥ 2000 | Maturity enforced for **all** coinbase UTXOs regardless of creation date |

Historical blocks/spends at height &lt; 2000 are **not** re-litigated. Soft-fork style: enforcement is by spend height only.

Same semantics in validation, mempool (tip+1), wallet coin selection / send guard, block template, and reorg reconnect paths.

## Soft-fork character

- Upgraded nodes reject immature coinbase spends in blocks at height ≥ 2000
- Legacy nodes (activation=0) may still accept them → **chain split risk** until economic majority upgrades
- Coordinated upgrade of all consensus-validating / block-producing processes **before** tip reaches 2000 is required

## Pool

Pool `MHCOIN_POOL_MATURE_CONFIRMS=100` is **independent** accounting policy (already expected live). Consensus activation does not change pool payout threshold (1000 MHC) or ledger balances.

## Rollback

- **Before tip ≥ 2000:** postponing is possible by shipping a new build that never activates (or activates later) and deploying it before H* — separate owner approval
- **After tip ≥ 2000:** simply setting activation back to `0` on a subset of nodes can create a chain split — requires a separate emergency decision, not a silent rollback
