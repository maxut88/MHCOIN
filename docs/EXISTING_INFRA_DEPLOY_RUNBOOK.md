# Controlled Deploy Runbook — Existing Host Only (activation=0)

**STOP:** Do not execute service stops or path swaps until the owner explicitly approves.  
**RC:** `c96d0a52a72075a9fa6d6ce5e93ce0627f3acd96`  
**Invariant:** `COINBASE_MATURITY_ACTIVATION["mainnet"] = 0`  
**Do not** set `MATURE_CONFIRMS` in this document’s node steps (pool is separate runbook).

---

## Current production map (audited 2026-10-22)

| Service | Unit | Exec | Code root (`PYTHONPATH`) | Data |
|---------|------|------|--------------------------|------|
| Seed node | `mhcoin-mainnet-node.service` | `rc1_ops/run_mainnet_node.py` | `/data/MHCOIN` | `/data/MHCOIN/mainnet/node-8333` |
| Pool | `mhcoin-pool.service` | `rc1_ops/run_pool.py` | `/data/MHCOIN` + `~/.mhcoin/pool.env` | pool DB `~/.mhcoin/pool/`; node `~/.mhcoin/pool-node` |
| Explorer | `mhcoin-explorer.service` | `rc1_ops/run_explorer.py` | `/data/MHCOIN` | (uses chain / env) |

Production Git: `568ff3d` — **no** maturity code. Dirty WIP may exist under `/data/MHCOIN`; **do not** deploy by merging WIP. Use a **clean RC tree**.

---

## Recommended layout (avoids mixing GPU/WIP)

```bash
# AFTER OWNER APPROVAL ONLY
sudo mkdir -p /data/MHCOIN-releases
git -C /data/MHCOIN-worktrees/coinbase-maturity-100-integration \
  archive --format=tar c96d0a52a72075a9fa6d6ce5e93ce0627f3acd96 \
  | sudo tar -x -C /data/MHCOIN-releases/c96d0a5
# or: git worktree add /data/MHCOIN-releases/c96d0a5 c96d0a5
echo c96d0a52a72075a9fa6d6ce5e93ce0627f3acd96 | sudo tee /data/MHCOIN-releases/c96d0a5/RELEASE_SHA
```

Verify:

```bash
PYTHONPATH=/data/MHCOIN-releases/c96d0a5 python3 -c \
  "from mhcoin.consensus.params import COINBASE_MATURITY, COINBASE_MATURITY_ACTIVATION as A; \
   assert COINBASE_MATURITY==100 and A['mainnet']==0; print('RC_OK', A)"
```

Keep `rc1_ops` launchers: either copy `run_*.py` from current `/data/MHCOIN/rc1_ops` into the release tree **or** leave ExecStart paths and only change `Environment=PYTHONPATH=...` so imports resolve to RC `mhcoin/`.

---

## Pre-flight (same window)

1. Record tip: `node_status.json` height + tip hash (seed + pool-node must match).  
2. Complete backup per `EXISTING_INFRA_BACKUP_RESTORE.md`.  
3. Confirm disk ≥15% free (host had ~40G free / 62% used).  
4. Agree maintenance window (pool miners will reconnect).  
5. Open intents: expect only terminal `sent` (audited); no pending incomplete intents.

---

## Sequence (owner-approved window)

```bash
# 0) Record state
cp /data/MHCOIN/mainnet/node-8333/node_status.json /tmp/pre_upgrade_node_status.json
cp /home/maxut88/.mhcoin/pool-node/node_status.json /tmp/pre_upgrade_pool_node_status.json

# 1) Stop dependent services (order matters)
sudo systemctl stop mhcoin-pool.service
sudo systemctl stop mhcoin-explorer.service
sudo systemctl stop mhcoin-mainnet-node.service

# 2) Point PYTHONPATH at clean RC (edit units or drop-in)
# Example drop-in:
sudo mkdir -p /etc/systemd/system/mhcoin-mainnet-node.service.d
sudo tee /etc/systemd/system/mhcoin-mainnet-node.service.d/rc-maturity.conf <<'EOF'
[Service]
Environment=PYTHONPATH=/data/MHCOIN-releases/c96d0a5
EOF
# Repeat for mhcoin-pool.service.d and mhcoin-explorer.service.d
sudo systemctl daemon-reload

# 3) Start seed first
sudo systemctl start mhcoin-mainnet-node.service
sleep 3
systemctl is-active mhcoin-mainnet-node.service
python3 -c "import json;print(json.load(open('/data/MHCOIN/mainnet/node-8333/node_status.json'))['height'], json.load(open('/data/MHCOIN/mainnet/node-8333/node_status.json'))['tip'])"

# 4) Start pool + explorer
sudo systemctl start mhcoin-pool.service
sudo systemctl start mhcoin-explorer.service

# 5) Verify activation still 0 inside running code
PYTHONPATH=/data/MHCOIN-releases/c96d0a5 python3 -c \
  "from mhcoin.consensus.params import COINBASE_MATURITY_ACTIVATION as A; print(A['mainnet'])"
```

### Post-checks

- Genesis hash unchanged vs pre status (`b832eaa2…00000000`).  
- Tip height ≥ pre-upgrade; tip hash consistent seed ↔ pool-node.  
- `peer_count` ≥ 1 (pool↔seed).  
- Mempool OK; stratum ports listening (`3333`/`3334`/`8888`).  
- No `COINBASE_MATURITY_ACTIVATION mainnet != 0`.  
- Mining / shares resume.

### Immediate rollback (pre-activation)

```bash
sudo systemctl stop mhcoin-pool mhcoin-explorer mhcoin-mainnet-node
sudo rm -f /etc/systemd/system/mhcoin-*.service.d/rc-maturity.conf
sudo systemctl daemon-reload
sudo systemctl start mhcoin-mainnet-node mhcoin-pool mhcoin-explorer
```

Datadir untouched → prior `/data/MHCOIN` code path restored.

---

## Out of scope here

- `MHCOIN_POOL_MATURE_CONFIRMS=100` → see pool migration runbook.  
- Setting activation height.  
- New VPS.  
- git push / public release.
