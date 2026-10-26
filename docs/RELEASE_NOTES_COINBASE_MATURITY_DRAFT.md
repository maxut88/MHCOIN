# MHCOIN 0.4.2.x — Coinbase Maturity 100 (DRAFT — NOT PUBLISHED)

**Status:** Local draft for owner review. Do **not** publish to GitHub until activation height `H*` is approved and a dedicated activation release is built.

## Consensus change (future activation release only)

- Soft-fork style: coinbase outputs may be spent only when `spend_height >= coinbase_height + 100`.
- Enforcement begins at `COINBASE_MATURITY_ACTIVATION["mainnet"] = H*` (**placeholder — not set in current RC**).
- Current production/RC keeps **`mainnet` activation = 0** (rule inactive).

## Current RC (c96d0a5) — pre-activation

- Ships maturity **code** with activation **off**.
- Pool operators should already use `MHCOIN_POOL_MATURE_CONFIRMS=100`.
- Payout threshold remains **1000 MHC** unless owner changes it.

## Upgrade guidance (operators)

1. Install RC / maturity-capable binary **before** any future `H*`.
2. Confirm tip hash with seed operators.
3. Do not run mixed old/new hashrate after `H*` without accepting split risk.
4. Desktop users: upgrade before `H*`; immature coinbase will not be spendable after activation.

## Breaking warning

After `H*`, blocks/txs that spend coinbase younger than 100 confirms are **invalid** on upgraded nodes. Old nodes may still accept them → **chain split risk**.

## H*

`H* = ________` (owner approval required; do not invent).  
Upgrade deadline: ________ (wall-clock before `H*`).

## Artifacts (to attach when publishing)

- Linux: `MHCOIN-Core-*-linux-x86_64.tar.gz` + SHA256
- Windows / macOS: **NOT VERIFIED** in this preparation — do not attach unverified builds as final
- `SHA256SUMS`

## Checksums

See `checksums/` in the owner-review package.
