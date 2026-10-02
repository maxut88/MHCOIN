# MHCOIN (MHC)

[![Desktop release artifacts](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml/badge.svg)](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Independent Proof-of-Work blockchain. **Not** a Bitcoin Core fork.

| | |
|---|---|
| Ticker | MHC |
| Maximum supply | 21,000,000 MHC |
| Decimals | 8 |
| Initial block reward | 50 MHC |
| Halving | every 210,000 blocks |
| PoW | HASH256 (double SHA-256) |
| Target block time | 600 seconds (10 minutes) |
| Difficulty | per-block retarget · window **30** · damping **1/16** |
| Address format | `mhc1…` (bech32, HRP `mhc`) |
| Ledger | UTXO |
| Mainnet P2P port | **8333** |
| Mainnet magic | `4D48434E` (`MHCN`) |
| Software | 0.3.1 |

## Mainnet identity (frozen)

| | |
|---|---|
| Genesis | `62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000` |
| Consensus fingerprint | `21f256498747b313795b65c9e26f0dd1cb23ee762813bc0d593c2c513e787ae9` |
| Bootstrap seed | `176.38.3.168:8333` |

The seed is **bootstrap only** (first P2P contact). Every node independently validates headers, PoW, difficulty (`get_next_work`), transactions, and UTXO. **Do not** copy another machine’s chain database.

Verify locally:

```bash
mhcoin genesis verify --network mainnet
mhcoin audit fingerprint
# expect 21f256498747b313795b65c9e26f0dd1cb23ee762813bc0d593c2c513e787ae9
```

## Features

- Full validating node (P2P sync, mempool, orphan/side-chain handling)
- Encrypted wallet · Send / Receive · multi-wallet
- Solo mining (Desktop GUI + CLI) with live tip-sync while P2P stays online
- Same **MHCOIN Core** Desktop UI on **Linux**, **macOS**, and **Windows**

## Install MHCOIN Core (users)

Download binaries from **[GitHub Releases](https://github.com/maxut88/MHCOIN/releases)** and verify `SHA256SUMS`.

### Linux

1. Download `MHCOIN-Core-0.3.1-x86_64.AppImage` **or** `MHCOIN-Core-0.3.1-linux-x86_64.tar.gz`.
2. AppImage: `chmod +x MHCOIN-Core-0.3.1-x86_64.AppImage && ./MHCOIN-Core-0.3.1-x86_64.AppImage`
3. tarball: extract and run the bundled `MHCOIN-Core` binary.
4. Allow sync from genesis, create or open a wallet, then Receive / Send / Mining.

Optional first-peer override:

```bash
export MHCOIN_CONNECT=176.38.3.168:8333
```

### macOS

1. Download `MHCOIN-Core-0.3.1-macos.dmg`.
2. Open the DMG and install **MHCOIN Core**.
3. Launch, sync from genesis, create/open a wallet.

Apple Silicon: current Release builds are native (`macos-14` / arm64).

### Windows

1. Download `MHCOIN-Core-0.3.1-windows-x86_64.exe`.
2. Launch **MHCOIN Core**, sync from genesis, create/open a wallet.

Wallet data defaults to `~/.mhcoin/mainnet/` (Windows: `%USERPROFILE%\.mhcoin\mainnet\`).

## Mining

**Desktop:** Mining tab → payout address `mhc1…` → Start (P2P stays online; templates rebuild if the tip moves).

**CLI:**

```bash
mhcoin mining start --network mainnet --address mhc1…
```

Notes:

- Block discovery is probabilistic; the network targets ~600 s **on average**.
- Difficulty adjusts every block (window 30, damping 1/16).
- Only **canonical** (active-chain) coinbases pay; stale/side blocks do not.

## Install from source (developers)

```bash
git clone https://github.com/maxut88/MHCOIN.git
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -U pip wheel
pip install -e ".[dev]"
pip install -e ".[desktop]"   # Desktop UI
pytest -q
```

Run Desktop from source:

```bash
python -m mhcoin.desktop --network mainnet
```

Build installers **on the target OS**:

```bash
bash packaging/build.sh                 # Linux / macOS
powershell -File packaging/build.ps1    # Windows
```

CI: [`.github/workflows/desktop-release.yml`](.github/workflows/desktop-release.yml) (Linux, macOS-14, Windows).

## Mainnet node (operators)

```bash
mhcoin node start --network mainnet --host 0.0.0.0 --port 8333
```

Peer discovery: hardcoded/DNS seeds, then ADDR gossip ([docs/DISCOVERY.md](docs/DISCOVERY.md)). No central block server.

## Documentation

- [Consensus](docs/CONSENSUS.md) — frozen parameters & fingerprint
- [Genesis](docs/GENESIS.md)
- [Wallet](docs/WALLET.md)
- [Node](docs/NODE.md)
- [P2P protocol](docs/NETWORK_PROTOCOL.md)
- [Desktop](docs/DESKTOP.md)
- [CLI quickstart](docs/USER_QUICKSTART.md)
- [Security](SECURITY.md)

## License

MIT — see [LICENSE](LICENSE).
