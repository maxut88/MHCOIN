# Coinbase Maturity Activation Plan (NOT activated on mainnet)

## Rule

- `COINBASE_MATURITY = 100`
- Coinbase created in block **H** may be spent in block **H+100** or later
- Spending in **H+99** is rejected **after** activation
- `COINBASE_MATURITY_ACTIVATION["mainnet"] = 0` → **inactive** (current production-safe default)

## Soft-fork character

Enforcement only applies when `spend_height >= ACTIVATION_HEIGHT`.

- Historical blocks with early coinbase spends remain valid (not re-litigated)
- After activation, new blocks that spend immature coinbase are rejected by upgraded nodes
- This is a **coordinated consensus upgrade** (soft-fork style restriction). Old nodes that do not enforce the rule may accept immature spends → risk of temporary split until economic majority upgrades

## Activation checklist (requires separate operator approval)

1. Ship release with maturity code + `ACTIVATION_HEIGHT = 0` (already done in this branch).
2. Agree future height `A` with ≥ pool operators + independent nodes (recommend tip + ≥ 2016 blocks lead time).
3. Publish release notes: set `COINBASE_MATURITY_ACTIVATION["mainnet"] = A`, bump `PROTOCOL_VERSION` / release notes.
4. Upgrade **all** mining nodes, pool nodes, explorers, and wallets **before** height `A`.
5. At height `A−1`, verify no immature spends in mempool; after `A`, monitor reject logs for `immature coinbase`.
6. **Do not** activate without explicit go-ahead. Hard/soft fork without coordination can split the network.

## Pool impact (critical)

Pool `mature_confirms` defaults to **20**. After consensus activation at 100:

| Layer | Depth | Effect |
|---|---|---|
| Pool ledger maturity | 20 | Credits become “matured” in SQLite |
| Consensus / wallet spend | 100 | UTXO still immature → payout TX rejected |

**Recommendation:** before or with activation, set pool `MHCOIN_POOL_MATURE_CONFIRMS=100` (or ≥100) so accounting cannot mark funds payable before they are consensus-spendable. This is an **ops/config** change, not a consensus change, but it is required for safe auto-payouts.

## Wallet / miner impact

- Wallet coin select should exclude immature coinbase when activation is live (`tip_height` + activation args).
- Solo CPU mining unchanged (HASH256/ASERT/ports untouched).
- Auto-payouts that try to spend young coinbases will fail validation after activation until 100 confirms.

## Rollback

If activation height was never reached: set activation back to `0` and redeploy (no chain rewrite).
If activation already passed and a split occurred: social consensus / reorg procedure — out of scope here.
