# MHCOIN Genesis

## Rule

**Production genesis is frozen in source.** Nodes never mine a new mainnet/testnet
genesis at startup. They rebuild the template from `NetworkParams` and verify hash,
merkle, nonce, timestamp, bits, coinbase extra, and pubkey hash.

## Frozen mainnet genesis

| Field | Value |
|-------|--------|
| Timestamp | `1791838170` |
| Bits | `0x1d00ffff` |
| Nonce | `1296691697` |
| Merkle root | `77d5c1666adbea9dbed18d8b6854db07d2fe7299cb850f334f0468c3e3e027c7` |
| Genesis hash | `b832eaa2883f17d6965e39849009344409d5a778eb8edac59e35262800000000` |
| Coinbase extra | `MHCOIN/mainnet genesis v2` |
| Reward | 50 MHC |
| Network magic | `4D48434E` |
| Default port | 8333 |
| DAA | aserti3-2d ASERT from height 1 (τ = 172800 s), anchor = genesis |

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

```bash
PYTHONPATH=. python3 -m mhcoin.blockchain.genesis_mine mine-prepare \
  --network mainnet --bits 0x1d00ffff --i-understand \
  --extra "MHCOIN/mainnet genesis v2" --timestamp <unix> \
  --out /tmp/mainnet_genesis_candidate.json
# or parallel:
PYTHONPATH=. python3 tools/parallel_genesis_mine.py ...
```

Operators must review the JSON and manually freeze values into `params.py`.
