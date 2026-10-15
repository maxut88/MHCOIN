# How to mine on the MHCOIN pool

Terminal miner only (Desktop solo is separate). Same machine can solo **or** pool — not both on one datadir.

## Public endpoints

| Role | URL |
|------|-----|
| Website | https://mhcscan.com |
| Explorer | https://explorer.mhcscan.com |
| Pool stats | https://pool.mhcscan.com |
| JSON miners | `mine.mhcscan.com:3333` |
| Stratum | `stratum+tcp://mine.mhcscan.com:3334` |

Payouts use your `mhc1…` address. Pool fee **1%** · PROP shares · mature after **20** confirms · auto-payout threshold **100 MHC**.

## Connect (recommended)

```bash
mhcoin mining pool-start \
  --url mine.mhcscan.com:3333 \
  --address mhc1YOUR_PAYOUT \
  --worker rig1
```

Stratum (optional):

```text
stratum+tcp://mine.mhcscan.com:3334
```

Prefer the JSON client (`pool-start`) for best compatibility.

## Check your work

1. Open https://pool.mhcscan.com → **Live workers** (your address / worker should appear).
2. Paste your `mhc1…` in **Miner** lookup for hashrate, credits, and payouts.
3. Each found block has a **round audit** (`/round/<id>`) with PROP % and fee split.

## Solo vs pool

| | Solo (Desktop / `mining start`) | Pool (`mining pool-start`) |
|--|----------------------------------|----------------------------|
| Datadir | `~/.mhcoin/mainnet/` | none (talks only to pool) |
| Coinbase | your address | pool address → PROP credits to you |
| Connect | P2P `:8333` | `mine.mhcscan.com:3333` |

Do **not** run Desktop solo mining and a pool miner that share the same wallet datadir intent incorrectly — pool mining does not need a local chain.

## More

- Pool operator guide: [POOL.md](POOL.md)
- Self-host a pool: [SELF_HOST_POOL.md](SELF_HOST_POOL.md)
- CLI wallet / solo: [USER_QUICKSTART.md](USER_QUICKSTART.md)
