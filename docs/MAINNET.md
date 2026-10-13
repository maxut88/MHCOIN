# MHCOIN Mainnet

Live network identity and operator notes. Genesis and consensus parameters are
**frozen** in `mhcoin/consensus/params.py`.

## Identity

| | |
|---|---|
| Genesis | `b832eaa2883f17d6965e39849009344409d5a778eb8edac59e35262800000000` |
| Genesis bits | `0x1d00ffff` |
| Protocol | **3** (ASERT from height 1) |
| Software | **0.4.2.0** |
| Magic | `4D48434E` (`MHCN`) |
| Default port | **8333** |
| Data directory | `~/.mhcoin/mainnet/` |

```bash
mhcoin genesis verify --network mainnet
mhcoin audit fingerprint
```

## Fresh node bootstrap

```text
install MHCOIN Core (release) or pip install -e .
   ↓
MHCOIN_NETWORK=mainnet · dedicated data dir
   ↓
mhcoin genesis verify --network mainnet
   ↓
mhcoin node start --network mainnet
   ↓
seeds / MHCOIN_CONNECT → peers.dat → ADDR gossip
   ↓
sync from genesis (GETHEADERS / GETDATA)
   ↓
validate PoW + rules locally
```

```bash
export MHCOIN_NETWORK=mainnet
export MHCOIN_DATA=$HOME/.mhcoin/mainnet

mhcoin genesis verify --network mainnet

mhcoin node start \
  --network mainnet \
  --host 0.0.0.0 \
  --port 8333 \
  --data-dir "$MHCOIN_DATA/node-8333"
```

Optional first contact when the default seed is unreachable:

```bash
export MHCOIN_CONNECT=176.38.3.168:8333
```

Empty datadir → frozen mainnet genesis is installed (not mined). Wrong genesis on
disk → node refuses to start.

## Network separation

| Mechanism | Effect |
|-----------|--------|
| Distinct 4-byte magic | Wrong-network envelopes rejected before handshake |
| VERSION `network` field | Peer with mismatched name disconnected |
| Distinct genesis | Chain from another network fails genesis verify |
| Distinct default ports | Reduces accidental dials |
| Distinct data dirs | `~/.mhcoin/mainnet` vs `testnet` vs `localnet` |

It must be impossible to accidentally merge mainnet ↔ testnet or mainnet ↔ localnet.

## Bootstrap vs consensus

- Hardcoded seeds and optional DNS seeds are **first contact only**.
- They do not mint blocks and are not trusted for consensus.
- Every node validates PoW and rules itself.
- See [DISCOVERY.md](DISCOVERY.md).

## Related

- [GENESIS.md](GENESIS.md)
- [CONSENSUS.md](CONSENSUS.md)
- [CONSENSUS_FREEZE.md](CONSENSUS_FREEZE.md)
- [USER_FAQ.md](USER_FAQ.md)
