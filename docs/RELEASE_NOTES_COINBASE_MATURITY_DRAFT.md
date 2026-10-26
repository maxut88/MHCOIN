# MHCOIN — Coinbase Maturity 100 / H*=2000 (DRAFT — NOT PUBLISHED)

**Status:** Local activation release-candidate draft for owner review.  
**Do not** push, tag, or publish to GitHub until owner approval gate (B).  
**Do not** deploy to production until owner approval gate (A).

## Consensus change

- Soft-fork style: coinbase outputs may be spent only when `spend_height >= coinbase_height + 100`
- Enforcement begins at `COINBASE_MATURITY_ACTIVATION["mainnet"] = 2000`
- Height **1999** = last legacy; height **2000** = first enforced
- Unchanged: `COINBASE_MATURITY=100`, PoW SHA-256d, 50 MHC subsidy, halving 210000, target 600s, max supply 21M
- Other networks remain activation `0`

## Upgrade guidance (operators)

1. Install this activation RC on **all** consensus validators / block producers **before** tip reaches 2000
2. Confirm identical tip/hash across controlled nodes after deploy
3. Do not run mixed legacy/upgraded hashrate after H* without accepting split risk
4. Desktop: rebuild/redistribute only if activation commit content differs from owner-confirmed binaries (see owner report)

## Breaking warning

After H*=2000, blocks/txs that spend coinbase younger than 100 confirms are **invalid** on upgraded nodes. Old nodes may still accept them → **chain split risk**.

## Pool

- Keep `MHCOIN_POOL_MATURE_CONFIRMS=100` (independent of consensus activation)
- Payout threshold remains **1000 MHC** unless owner changes it (gate D)

## Artifacts (local RC only until gate B)

- Linux source/tree SHA + `SHA256SUMS` + test logs in the local RC package
- Windows / macOS Desktop: owner previously confirmed OK on pre-activation builds — reassess if packaging includes this commit’s consensus params

## Checksums

See local RC `SHA256SUMS` (not published by this TZ).
