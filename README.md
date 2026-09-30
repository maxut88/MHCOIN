# MHCOIN (MHC)

Independent Proof-of-Work cryptocurrency. Not a Bitcoin Core fork.

| | |
|---|---|
| Ticker | MHC |
| Supply | 21,000,000 |
| Block reward | 50 MHC (halving every 210,000 blocks) |
| PoW | double-SHA256 |
| Target block time | ~10 minutes |
| Address | `mhc1…` (bech32) |
| Ledger | UTXO |
| Default P2P port | 8333 |

## Features

- Full node (P2P sync, validation, mempool)
- Encrypted wallet + Send / Receive
- Mining
- CLI and Desktop app (Linux / macOS / Windows)

## Install (developers)

```bash
git clone https://github.com/maxut88/MHCOIN.git
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip wheel
pip install -e ".[dev]"
```

Desktop extras:

```bash
pip install -e ".[desktop]"
```

## Quick start (localnet)

```bash
export MHCOIN_NETWORK=localnet
export MHCOIN_DATA="$HOME/.mhcoin/localnet"

mhcoin wallet create
mhcoin wallet address
mhcoin mining start --address mhc1… --blocks 1
mhcoin wallet balance
```

## Mainnet node

```bash
mhcoin genesis verify --network mainnet

mhcoin node start --network mainnet
# listens on 0.0.0.0:8333 and dials built-in seeds (Bitcoin-style bootstrap)
```

Wallet data defaults to `~/.mhcoin/mainnet/`.

Peer discovery: hardcoded/DNS seeds for first contact, then ADDR gossip
(`docs/DISCOVERY.md`). No central block server.
## Desktop

```bash
python -m mhcoin.desktop --network mainnet
```

Build installers:

```bash
bash packaging/build.sh          # Linux / macOS
powershell -File packaging/build.ps1   # Windows
```

## Tests

```bash
pytest -q
```

## Docs

- [Consensus](docs/CONSENSUS.md)
- [Genesis](docs/GENESIS.md)
- [Wallet](docs/WALLET.md)
- [Node](docs/NODE.md)
- [P2P](docs/NETWORK_PROTOCOL.md)
- [Desktop](docs/DESKTOP.md)
- [CLI quickstart](docs/USER_QUICKSTART.md)
- [Security notes](SECURITY.md)

## License

MIT — see [LICENSE](LICENSE).
