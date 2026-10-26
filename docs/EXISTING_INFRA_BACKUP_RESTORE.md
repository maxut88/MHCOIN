# Existing Infrastructure — Backup & Restore (pre-upgrade)

**Do not run production stops unless owner approved the upgrade window.**  
Recovery smoke (2026-10-22): SQLite online backup of `chain.sqlite` + `pool.sqlite` opened read-only with RC → **PASS**  
Sample recovery dir: `/tmp/mhcoin_maturity_recovery_smoke_20261022_192445`

---

## What to back up (before RC deploy)

| Asset | Path (current host) | Notes |
|-------|---------------------|-------|
| Seed chain DB | `/data/MHCOIN/mainnet/node-8333/chain.sqlite` (+ `-wal`/`-shm` via sqlite backup API) | Prefer `sqlite3.backup` while node running |
| Seed chainstate | `/data/MHCOIN/mainnet/node-8333/chainstate/` | LMDB UTXO |
| Seed blocks | `/data/MHCOIN/mainnet/node-8333/blocks/` | |
| Pool embedded node | `/home/maxut88/.mhcoin/pool-node/` (chain + chainstate + **wallet.json**) | Contains keys — encrypt backup at rest |
| Pool ledger | `/home/maxut88/.mhcoin/pool/pool.sqlite` | Includes payout_intents |
| Pool env | `/home/maxut88/.mhcoin/pool.env` | Secrets — never paste into chat/logs |
| Systemd units | `/etc/systemd/system/mhcoin-*.service` | |
| Production Git SHA | `git -C /data/MHCOIN rev-parse HEAD` | Today: `568ff3d…` |
| Network unit CONNECT | from `mhcoin-mainnet-node.service` | |

Suggested backup root: `/data/MHCOIN-backups/pre-maturity-rc-$(date -u +%Y%m%dT%H%M%SZ)/`

### Online SQLite backup (safe while services run)

```bash
python3 - <<'PY'
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
root = Path(f"/data/MHCOIN-backups/pre-maturity-rc-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
(root/"node").mkdir(parents=True)
(root/"pool").mkdir(parents=True)

def bak(src, dst):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d); d.close(); s.close()
    print(dst, dst.stat().st_size)

bak("/data/MHCOIN/mainnet/node-8333/chain.sqlite", root/"node"/"chain.sqlite")
bak("/home/maxut88/.mhcoin/pool/pool.sqlite", root/"pool"/"pool.sqlite")
print("ROOT", root)
PY
# Also: rsync -a chainstate blocks pool-node (exclude logs) into same ROOT — owner window
```

**Never** dump wallet mnemonics / private keys into reports.

---

## Restore (isolated first)

1. Copy backup into a **new** datadir (e.g. `/tmp/mhcoin_restore_test/`).
2. Point `PYTHONPATH` at RC worktree; open chain with `MHCOIN_NETWORK=mainnet` **without** binding public P2P (or use firewall drop) to avoid serving stale state.
3. Verify tip height/hash match backup metadata.
4. Open `PoolDB` on pool copy; confirm balance sums and intent counts.
5. Only after isolated verify: consider production restore (separate approval).

### Isolated verify commands

```bash
WT=/data/MHCOIN-worktrees/coinbase-maturity-100-integration
REC=<backup_or_smoke_dir>
PYTHONPATH=$WT python3 - <<PY
import sqlite3
from pathlib import Path
rec=Path("$REC")
con=sqlite3.connect(f"file:{rec}/node/chain.sqlite?mode=ro", uri=True)
print(con.execute("select height from block_index where status=1 order by height desc limit 1").fetchone())
con.close()
from mhcoin.pool.db import PoolDB
db=PoolDB(rec/"pool"/"pool.sqlite")
print(db._conn.execute("select count(*), coalesce(sum(matured_sats),0) from balances").fetchone())
db.close()
PY
```

---

## Rollback compatibility (pre-activation only)

While `COINBASE_MATURITY_ACTIVATION["mainnet"]=0`, rolling code back to previous production SHA is generally safe if datadir format unchanged.  
**After** a future activation height is crossed: do **not** auto-downgrade (see emergency plan).
