# MHCOIN Mainnet Preparation (Stage 9)

This document defines production readiness. **Stage 9 does not launch mainnet.**

## Goals

1. Freeze production consensus parameters.
2. Freeze reproducible mainnet genesis (hash, merkle, nonce, timestamp, bits, magic).
3. Hard-separate mainnet / testnet / localnet (magic, ports, genesis, data dirs).
4. Protocol versioning with explicit upgrade rules.
5. Genesis verification on every node start.
6. Document fresh-node bootstrap and launch procedure.
7. Complete the safety checklist before any real launch.

## Fresh-node bootstrap (no central blockchain server)

```
fresh VPS
   ↓
install MHCOIN (pip install -e .)
   ↓
configure mainnet (MHCOIN_NETWORK=mainnet, dedicated data dir)
   ↓
mhcoin genesis verify --network mainnet
   ↓
mhcoin node start --network mainnet --connect <peer1:8333> --connect <peer2:8333>
   ↓
discover peers (ADDR/GETADDR; no DNS seeds)
   ↓
sync from genesis (GETHEADERS / GETDATA)
   ↓
verify chain + UTXO supply
   ↓
ready
```

Example:

```bash
export MHCOIN_NETWORK=mainnet
export MHCOIN_DATA=$HOME/.mhcoin/mainnet
cd . && pip3 install -e ".[dev]" --user --break-system-packages

mhcoin genesis verify --network mainnet

mhcoin node start \
  --network mainnet \
  --host 0.0.0.0 \
  --data-dir "$MHCOIN_DATA/node-8333" \
  --connect 203.0.113.10:8333 \
  --connect 198.51.100.20:8333
```

Empty datadir → frozen mainnet genesis is installed (not mined). Wrong genesis on
disk → node refuses to start.

## Network separation guarantees

| Mechanism | Effect |
|-----------|--------|
| Distinct 4-byte magic | Wrong-network envelopes rejected before handshake |
| VERSION `network` field | Peer with mismatched name disconnected |
| Distinct genesis | Chain from another network fails genesis verify |
| Distinct default ports | Reduces accidental dials |
| Distinct data dirs | `~/.mhcoin/mainnet` vs `testnet` vs `localnet` |

It must be impossible to accidentally merge mainnet ↔ testnet or mainnet ↔ localnet.

## What Stage 9 does **not** do

- Does not auto-start a public mainnet ceremony.
- Does not invent a new genesis at runtime.
- Does not enable DNS seeds or a central “blockchain server”.
- Does not claim economic readiness (hashrate, exchanges, etc.).

See `docs/LAUNCH.md` and `docs/MAINNET_CHECKLIST.md`.
