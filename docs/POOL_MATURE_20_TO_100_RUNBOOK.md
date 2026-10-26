# Pool Migration Runbook — `MATURE_CONFIRMS` 20 → 100

**Do not edit production `pool.env` until owner approval.**  
**Keep** `MHCOIN_POOL_PAYOUT_THRESHOLD=100000000000` (**1000 MHC**).

---

## Current production (RO audit 2026-10-22)

| Item | Value |
|------|-------|
| `MHCOIN_POOL_MATURE_CONFIRMS` in env | **absent → code default 20** |
| Immature rounds | 20 |
| Matured rounds | ~1670 |
| Orphaned rounds | 1 |
| Immature Σ / Matured Σ / Paid Σ (sats) | ≈9.90e10 / ≈1.10e11 / ≈8.16e12 |
| `payout_intents` | all **`sent`** (39) — no open pending |
| `payouts` | 301 `sent` |

No incomplete intents → **no special intent migration** required for the cutover itself.

---

## Semantics of the change

`mature_rounds(tip, confirms=100)` only advances rounds still `status=immature` when `tip >= height + 100`.

| Already `matured` / `paid` / completed intents | **Unchanged** — no demotion, no re-pay |
| New immature rounds | Wait 100 confirms |
| Historical miner balances | Preserved |

**Order vs consensus activation:** set pool to **100 before or with** consensus activation. Never leave pool at 20 after nodes enforce maturity 100.

---

## Approved cutover steps (after backup + node RC on activation=0)

```bash
# 1) Backup pool.sqlite (sqlite backup API) — see EXISTING_INFRA_BACKUP_RESTORE.md

# 2) Add env line (do not remove threshold)
# Append exactly:
# MHCOIN_POOL_MATURE_CONFIRMS=100
sudo grep -q '^MHCOIN_POOL_MATURE_CONFIRMS=' /home/maxut88/.mhcoin/pool.env \
  || echo 'MHCOIN_POOL_MATURE_CONFIRMS=100' | sudo tee -a /home/maxut88/.mhcoin/pool.env
# If key already exists, set to 100 explicitly with owner-reviewed edit.

# 3) Restart pool only
sudo systemctl restart mhcoin-pool.service
systemctl is-active mhcoin-pool.service

# 4) Verify process sees 100 (from pool logs / metrics) and threshold still 1000 MHC
grep MATURE /home/maxut88/.mhcoin/pool.env
grep THRESHOLD /home/maxut88/.mhcoin/pool.env
```

### Verify ledger unchanged

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path
con=sqlite3.connect(f"file:{Path.home()/'.mhcoin/pool/pool.sqlite'}?mode=ro", uri=True)
print(con.execute("select coalesce(sum(immature_sats),0), coalesce(sum(matured_sats),0), coalesce(sum(paid_sats),0) from balances").fetchone())
print(con.execute("select status, count(*) from rounds group by status").fetchall())
print(con.execute("select status, count(*) from payout_intents group by status").fetchall())
con.close()
PY
```

Expect: matured/paid sums **not reset**; immature rounds remain until depth≥100.

### Rollback env (pre-consensus-activation only)

Remove or set `MHCOIN_POOL_MATURE_CONFIRMS=20`, restart pool. Does not rewrite historical matured rows.

---

## Forbidden

- Manual SQL to zero balances or delete intents.  
- Re-creating payouts for already `sent` txids.  
- Spending coinbase UTXO younger than 100 once consensus is active.  
- Changing 1000 MHC threshold in this migration.
