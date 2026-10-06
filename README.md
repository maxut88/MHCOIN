# MHCOIN (MHC)

[![Desktop release](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml/badge.svg)](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml)
[![Latest release](https://img.shields.io/github/v/release/maxut88/MHCOIN)](https://github.com/maxut88/MHCOIN/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Independent Proof-of-Work cryptocurrency with a full node, encrypted wallet, and Desktop miner.  
**Not** a Bitcoin Core fork.

**[Download MHCOIN Core 0.4.1.12](https://github.com/maxut88/MHCOIN/releases/tag/v0.4.1.12)** · [User FAQ](docs/USER_FAQ.md) · [Docs](docs/README.md)

---

## Specs

| | |
|---|---|
| Ticker | **MHC** |
| Max supply | 21,000,000 MHC (8 decimals) |
| Block reward | 50 MHC → halves every 210,000 blocks |
| PoW | HASH256 (double SHA-256) |
| Block time | ~10 minutes (target 600 s) |
| Difficulty | retarget every block · window 30 · damping 1/16 |
| Addresses | `mhc1…` (bech32) |
| Ledger | UTXO |
| P2P | port **8333** · magic `MHCN` |
| Software | **0.4.1.12** |

### Mainnet identity (frozen)

| | |
|---|---|
| Genesis | `62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000` |
| Consensus fingerprint | `21f256498747b313795b65c9e26f0dd1cb23ee762813bc0d593c2c513e787ae9` |
| Bootstrap seed | `176.38.3.168:8333` (discovery only — always validate yourself) |

```bash
mhcoin genesis verify --network mainnet
mhcoin audit fingerprint
```

---

## What you get

- Validating full node (P2P sync, mempool, reorgs)
- Encrypted wallet · BIP39 seed · multi-address receive · Send
- Solo mining on the live mainnet tip (Desktop + Terminal)
- Same **MHCOIN Core** app on **Windows**, **macOS**, and **Linux**

---

## Install (recommended)

Download the build for your OS from the [latest release](https://github.com/maxut88/MHCOIN/releases/latest), then verify `SHA256SUMS`.

| OS | File |
|----|------|
| **Windows** | `MHCOIN-Core-0.4.1.12-windows-x86_64.exe` (or `.zip`) |
| **macOS** (current) | `MHCOIN-Core-0.4.1.12-macos.dmg` |
| **macOS** (11+ / older Intel) | `MHCOIN-Core-0.4.1.12-macos110-legacy.dmg` |
| **Linux** | `MHCOIN-Core-0.4.1.12-x86_64.AppImage` or `.tar.gz` |

**First run**

1. Launch **MHCOIN Core** and let it sync from genesis.
2. Create or open a wallet (write down the BIP39 seed offline).
3. Use **Receive** / **Send** / **Mining**.

Data folder: `~/.mhcoin/mainnet/` · Windows: `%USERPROFILE%\.mhcoin\mainnet\`

**Tips**

- **macOS Gatekeeper:** right-click → Open, or allow in Privacy & Security.
- **Linux AppImage:** `chmod +x MHCOIN-Core-*.AppImage && ./MHCOIN-Core-*.AppImage`  
  If no native window opens, open **http://127.0.0.1:18765/** in a browser (normal on some desktops).
- **Upgrade from 0.3.7.3:** open the same data folder once — storage migrates automatically. Back up `wallet.json` first.
- Optional peer: `export MHCOIN_CONNECT=176.38.3.168:8333`

Older builds: [all releases](https://github.com/maxut88/MHCOIN/releases).

---

## Mining

Desktop and Terminal miners race the **same** live mainnet tip.

1. **Desktop:** Mining → paste payout `mhc1…` → **Start mining**.
2. Open **Instructions** for copy-paste Terminal commands (Mac/Windows/Linux).

```bash
mhcoin mining start --network mainnet --address mhc1…
```

Helpers: `packaging/mine_mainnet.sh` · `.bat` · `.ps1`

- Stop Desktop mining (or Quit) before Terminal mining on the **same** data folder.
- Finding a block is probabilistic (~10 min average for the **network**, not per miner).
- Only blocks on the active chain pay; stale/side blocks do not.

---

## Build from source

```bash
git clone https://github.com/maxut88/MHCOIN.git
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install -U pip wheel setuptools
python -m pip install -e ".[dev,desktop]"
pytest -q
python -m mhcoin.desktop --network mainnet
```

Mac note: if `cryptography` fails, install OpenSSL (`brew install openssl@3 pkgconf`) or use `pip install "cryptography>=42" --only-binary=:all:` — see Desktop → **Instructions**.

Packagers: `packaging/build.sh` (Linux/macOS) · `packaging/build.ps1` (Windows) · CI builds on tag `v*`.

---

## Run a node

```bash
mhcoin node start --network mainnet --host 0.0.0.0 --port 8333
```

Peer discovery: seeds → `peers.dat` → ADDR gossip. Details: [docs/DISCOVERY.md](docs/DISCOVERY.md).

---

## Documentation

| | |
|---|---|
| [User FAQ](docs/USER_FAQ.md) | Upgrade · backup · mining · peers |
| [CLI quickstart](docs/USER_QUICKSTART.md) | Wallet · mine · send |
| [Desktop](docs/DESKTOP.md) | App notes |
| [Wallet](docs/WALLET.md) | BIP39 / BIP84 · `mhc1…` |
| [Consensus](docs/CONSENSUS.md) · [Genesis](docs/GENESIS.md) | Frozen parameters |
| [Storage](docs/STORAGE.md) · [Node](docs/NODE.md) · [P2P](docs/NETWORK_PROTOCOL.md) | Internals |
| [Security](SECURITY.md) | Reporting & wallet safety |
| [Full index](docs/README.md) | All docs |

---

## License

MIT — see [LICENSE](LICENSE).
