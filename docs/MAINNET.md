# MHCOIN Mainnet

Live network identity and operator notes. Genesis and consensus parameters are
**frozen** in `mhcoin/consensus/params.py`.

## Identity

| | |
|---|---|
| Genesis | `62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000` |
| Consensus fingerprint | `5eb609540c366c94b05b817205bed137fb2496c1d8b48cf2e0081498269c33eb` |
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
