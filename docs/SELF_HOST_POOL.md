# Self-host a MHCOIN mining pool

Run **your own** MHCOIN pool on your server. You download the same open-source software used in production; the UI looks the same. Your pool has **its own** coinbase address, SQLite ledger, wallet, and payouts — not a clone of someone else’s balances.

Native **HASH256 / UTXO** only. Not compatible with Ethereum / Ethash pools.

## What you get

| Port | Role |
|------|------|
| `3333` | JSON miner protocol (`mhcoin mining pool-start`) |
| `3334` | Stratum adapter |
| `8888` | Stats web UI (same layout as the reference pool) |

Rewards: **PROP** (proportional shares per round), minus your pool fee. Credits go **immature → matured** after N confirmations, then auto-payout when matured ≥ threshold.

## Requirements

- Linux x86_64 (or similar) with Python **3.10+**
- Outbound P2P to mainnet peers (default port **8333**)
- Open inbound (or NAT) for miners: **3333**, **3334**, and optionally **8888** for the stats UI
- Disk for chain sync under a **dedicated** pool datadir (do **not** share with Desktop solo mining)

## 1. Get the code

```bash
git clone https://github.com/maxut88/MHCOIN.git
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## 2. Create the pool wallet (once)

The pool node datadir holds the wallet that receives coinbases and sends payouts.
Use the **same path** for both variables: the pool runtime reads `MHCOIN_POOL_DATA`,
while `mhcoin wallet create` follows `MHCOIN_DATA`.

```bash
export MHCOIN_NETWORK=mainnet
export MHCOIN_POOL_DATA="$HOME/.mhcoin/pool-node"
export MHCOIN_DATA="$MHCOIN_POOL_DATA"   # wallet CLI → same datadir as the pool
mhcoin wallet create --network mainnet
```

Save the printed **address** and **password**. That address becomes `MHCOIN_POOL_ADDRESS`.
In `pool.env`, set `MHCOIN_POOL_DATA` to this same directory (see `packaging/pool/pool.env.example`).

## 3. Configure environment

```bash
mkdir -p "$HOME/.mhcoin"
cp packaging/pool/pool.env.example "$HOME/.mhcoin/pool.env"
chmod 600 "$HOME/.mhcoin/pool.env"
nano "$HOME/.mhcoin/pool.env"
```

Set at least:

- `MHCOIN_POOL_DATA` — same path as above (`~/.mhcoin/pool-node`)  
- `MHCOIN_POOL_ADDRESS` — your pool coinbase / fee address  
- `MHCOIN_POOL_PASSWORD` — wallet password for auto-payouts  
- `MHCOIN_CONNECT` — one or more `host:8333` mainnet peers (seed / friends)

Optional: fee %, share factor, mature confirms, and  
`MHCOIN_POOL_PAYOUT_THRESHOLD` in **sats** (example in `pool.env.example` is
`10000000` = **0.1 MHC**; set your own policy — e.g. `100000000000` = **1000 MHC**).

## 4. Start the pool

### Foreground (test)

```bash
cd /path/to/MHCOIN
source .venv/bin/activate
set -a && source "$HOME/.mhcoin/pool.env" && set +a
mhcoin pool start --address "$MHCOIN_POOL_ADDRESS" --network mainnet
# or: packaging/pool/run_pool.sh
```

### systemd (production)

```bash
sudo cp packaging/pool/mhcoin-pool.service.example /etc/systemd/system/mhcoin-pool.service
sudo nano /etc/systemd/system/mhcoin-pool.service   # User=, WorkingDirectory=, paths
sudo systemctl daemon-reload
sudo systemctl enable --now mhcoin-pool
sudo systemctl status mhcoin-pool --no-pager
```

Open **http://YOUR_HOST:8888** — UI matches the reference pool; your KPIs and operator address are yours.

## 5. Point miners at your pool

```bash
mhcoin mining pool-start \
  --url YOUR_HOST:3333 \
  --address mhc1YOUR_PAYOUT \
  --worker rig1
```

Stratum (optional): `stratum+tcp://YOUR_HOST:3334` — prefer JSON + `pool-start` when possible.

## 6. Verify

| Check | Expect |
|-------|--------|
| Stats UI `:8888` | Tip advances, Network panel shows peers |
| `/api/stats` | `mainnet_tip`, `pool_hashrate`, `blocks` |
| Pool log | `POOL BLOCK` when you find a block |
| Immature → matured | After `MHCOIN_POOL_MATURE_CONFIRMS` (default 20) |
| Payout | When matured ≥ `MHCOIN_POOL_PAYOUT_THRESHOLD` (sats; see §3) |

## Security checklist

- Dedicated datadir: `MHCOIN_POOL_DATA` (and matching `MHCOIN_DATA` when using wallet CLI) ≠ Desktop / solo miner datadir  
- `chmod 600` on `pool.env` (contains wallet password)  
- Firewall: only expose 3333/3334/8888 as needed; do not expose wallet files  
- Back up `~/.mhcoin/pool-node/` (wallet) and `~/.mhcoin/pool/pool.sqlite`  
- Fee and payout threshold are **your** policy — document them for your miners  

## What is shared vs private

| Shared (this repo) | Private (your server) |
|--------------------|------------------------|
| Pool software + UI | Wallet seed / password |
| Docs & systemd examples | `pool.env`, SQLite credits/payouts |
| Mainnet consensus rules | Your coinbase address & fee |

## Related docs

- [POOL.md](POOL.md) — operator overview  
- [MAINNET.md](MAINNET.md) · [DISCOVERY.md](DISCOVERY.md) — peers / seeds  
- [USER_QUICKSTART.md](USER_QUICKSTART.md) — CLI wallet & mining  

## Support

Issues: [github.com/maxut88/MHCOIN/issues](https://github.com/maxut88/MHCOIN/issues)
