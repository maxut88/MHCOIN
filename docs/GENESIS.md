# MHCOIN Genesis (Stage 9)

## Rule

**Production genesis is frozen in source.** Nodes never mine a new mainnet/testnet
genesis at startup. They rebuild the template from `NetworkParams` and verify hash,
merkle, nonce, timestamp, bits, coinbase extra, and pubkey hash.

## Frozen mainnet (MHCOIN MAINNET GENESIS)

| Field | Value |
|-------|--------|
| Timestamp | `1735689600` (2025-01-01 00:00:00 UTC) |
| Bits | `0x1e0fffff` |
| Nonce | `646812` |
| Merkle root | `8ea23b65a3da9651b6e03d6f2c2398711d7568b7a942f15fd96397069053c954` |
| Genesis hash | `62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000` |
| Coinbase extra | `MHCOIN/mainnet genesis` |
| Reward | 50 MHC |
| Network magic | `4D48434E` |
| Default port | 8333 |

Authoritative copy: `mhcoin.consensus.params` → `_MAINNET`.

## Verify (required before launch)

```bash
cd .
PYTHONPATH=. python3 -m mhcoin.blockchain.genesis_mine verify --network mainnet
PYTHONPATH=. python3 -m mhcoin.blockchain.genesis_mine verify-all
# or CLI:
mhcoin genesis verify --network mainnet
mhcoin genesis show --network mainnet
```

On node start, `Blockchain` loads height 0 and rejects any mismatch with the expected
genesis for `--network`.

## Mine-prepare (candidate only — does not freeze)

To produce a **candidate** JSON for review (does **not** write into `params.py`,
does **not** start mainnet):

```bash
PYTHONPATH=. python3 -m mhcoin.blockchain.genesis_mine mine-prepare \
  --network mainnet --i-understand --out /tmp/mainnet_candidate.json
```

Operators must manually review and commit frozen fields into `params.py`, then
`verify` again. Never paste a candidate into production without dual review.

## Regtest / localnet custom genesis

Local wallet bootstrap may mine a different coinbase pubkey (same bits/timestamp):

```bash
export MHCOIN_NETWORK=localnet
export MHCOIN_DATA=/tmp/mhcoin_regtest
export MHCOIN_WALLET_PASSWORD='secret'
mhcoin wallet create
mhcoin blockchain init   # forbidden on mainnet/testnet
```

`node start` on an empty mainnet/testnet/localnet datadir installs the **frozen**
shared genesis for that network (not a random mine).
