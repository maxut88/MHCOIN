# Activation height H* — locked value

**Owner decision (2026-10-26):** `H* = 2000`

`COINBASE_MATURITY_ACTIVATION["mainnet"] = 2000` in this activation RC.

## Height rule

- Block / spend height **1999**: legacy (no maturity check)
- Block / spend height **2000+**: enforce `spend_height >= coinbase_height + 100`

## Methodology (historical)

Previous provisional formula (tip + lead time) is superseded by the owner lock at 2000. Recompute only if tip is already too close or past 2000 at deploy time → STOP and pick a new height with owner approval.

## Rollout order (production requires separate approval)

1. All controlled consensus nodes run this activation RC **before** tip 2000
2. Confirm live still had activation=0 until deploy window
3. Deploy activation code; verify `get_coinbase_maturity_activation("mainnet")==2000` on every validator
4. Do **not** resume mining until owner gate (C)
5. Monitor blocks 1999–2001 and first 100+ post-activation blocks
