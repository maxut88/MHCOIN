# MHCOIN Mining Pool

Native HASH256 / UTXO pool for MHCOIN. Not compatible with Ethereum pools.

## Components

| Port | Role |
|------|------|
| `3333` | JSON miner protocol (CLI `mining pool-start`) |
| `3334` | Bitcoin-style Stratum (phase 2 adapter) |
| `8888` | Stats web UI |

Rewards: **PROP** (proportional shares per round), minus pool fee. Immature → matured after N confirmations, then auto-payout when balance ≥ threshold.

## Operator — start the pool

Use a **dedicated** chain datadir (do not share with Desktop solo mining):

```bash
cd /path/to/MHCOIN
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Pool coinbase + payout wallet must live in the pool node datadir (create once):
export MHCOIN_NETWORK=mainnet
export MHCOIN_DATA="$HOME/.mhcoin/pool-node"
mhcoin wallet create --network mainnet
# Note the printed address, then:

export MHCOIN_POOL_PASSWORD='your-wallet-password'   # auto-payouts
mhcoin pool start \
  --address mhc1YOUR_POOL_ADDRESS \
  --network mainnet \
  --data-dir "$HOME/.mhcoin/pool-node" \
  --pool-dir "$HOME/.mhcoin/pool" \
  --port 3333 \
  --stratum-port 3334 \
  --web-port 8888 \
  --fee-percent 1 \
  --share-factor 1024
```


Env alternatives: `MHCOIN_POOL_ADDRESS`, `MHCOIN_POOL_DATA`, `MHCOIN_POOL_DIR`, `MHCOIN_POOL_PORT`, `MHCOIN_POOL_WEB_PORT`, `MHCOIN_POOL_FEE_PERCENT`, `MHCOIN_POOL_SHARE_FACTOR`, `MHCOIN_POOL_PAYOUT_THRESHOLD` (sats; `0` disables auto-payout).

systemd example: [`rc1_ops/mhcoin-pool.service`](../rc1_ops/mhcoin-pool.service).

## Stats UI

Open **http://POOL_HOST:8888** — explorer-matched dashboard (KPIs, connect howto, workers, blocks, miner lookup).
Explorer nav **Pool** appears when `MHCOIN_POOL_URL` is set (default on LAN: `http://192.168.0.221:8888`).

## Miner — terminal only

No Desktop pool UI yet. Mine with CLI:

```bash
mhcoin mining pool-start \
  --url POOL_HOST:3333 \
  --address mhc1YOUR_PAYOUT \
  --worker rig1
```

The miner does **not** write a local chain; it only talks to the pool.

## Explorer link

Set on the explorer process:

```bash
export MHCOIN_POOL_URL=http://POOL_HOST:8888
```

A **Pool** nav link appears when this is set.

## Stratum notes

Port `3334` speaks a Bitcoin-like Stratum subset (`subscribe` / `authorize` / `notify` / `submit`).  
Job byte order matches MHCOIN (native), not necessarily every BTC miner’s endian conventions. Prefer the JSON protocol + `mhcoin mining pool-start` for now.

## Fee & payouts

- Coinbase pays the **pool address**.
- After a pool block, miner credits = `(reward × (1 − fee%)) × miner_shares / round_shares`.
- Credits start **immature**; after `MHCOIN_POOL_MATURE_CONFIRMS` (default 20) they mature.
- Auto-payout sends from the pool wallet in `--data-dir` when matured ≥ threshold.
