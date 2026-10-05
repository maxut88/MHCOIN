# MHCOIN (MHC)

[![Desktop release artifacts](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml/badge.svg)](https://github.com/maxut88/MHCOIN/actions/workflows/desktop-release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/maxut88/MHCOIN)](https://github.com/maxut88/MHCOIN/releases/latest)

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
| Software | **0.4.1.9** |

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

- Full validating node (P2P sync, mempool, orphan / reorg handling)
- Encrypted wallet · **BIP39 seed** recovery · Send / Receive · multi-wallet
- Bitcoin-style storage: `peers.dat` · flat `blocks/blk*.dat` · LMDB `chainstate/`
- Solo mining (Desktop + CLI) on the live mainnet tip while P2P stays online
- Same **MHCOIN Core** Desktop UI on **Linux**, **macOS**, and **Windows**

## Install MHCOIN Core (users)

**Current:** [MHCOIN Core 0.4.1.9](https://github.com/maxut88/MHCOIN/releases/tag/v0.4.1.9) · verify `SHA256SUMS`  
**Previous:** [MHCOIN Core 0.3.7.3](https://github.com/maxut88/MHCOIN/releases/tag/v0.3.7.3) (still available)

### Linux

1. Download `MHCOIN-Core-0.4.1.9-x86_64.AppImage` **or** `MHCOIN-Core-0.4.1.9-linux-x86_64.tar.gz`.
2. AppImage: `chmod +x MHCOIN-Core-0.4.1.9-x86_64.AppImage && ./MHCOIN-Core-0.4.1.9-x86_64.AppImage`
3. tarball: extract and run `./MHCOIN-Core`.
4. Allow sync from genesis, create or open a wallet, then Receive / Send / Mining.

If no native window opens (common on immutable desktops), the UI is at **http://127.0.0.1:18765/** in your browser — that is normal.

Optional first-peer override:

```bash
export MHCOIN_CONNECT=176.38.3.168:8333
```

### macOS

1. Download `MHCOIN-Core-0.4.1.9-macos.dmg`.
2. Open the DMG → install **MHCOIN Core** → sync from genesis → create/open a wallet.
3. If Gatekeeper blocks the app: right-click → **Open**, or allow it in System Settings → Privacy & Security.

### Windows

1. Download `MHCOIN-Core-0.4.1.9-windows-x86_64.exe` (or the `.zip`).
2. Launch **MHCOIN Core**, sync from genesis, create/open a wallet.

Wallet data defaults to `~/.mhcoin/mainnet/` (Windows: `%USERPROFILE%\.mhcoin\mainnet\`).

Upgrading from **0.3.7.3**: open the same data folder once — storage migrates automatically (`utxo.sqlite` → `chainstate/`, blocks → `blk*.dat`). Keep a backup of `wallet.json` first.

## Mining

Desktop and terminal miners race the **same live mainnet tip** (P2P sync + block broadcast).

**Desktop:** Mining tab → payout `mhc1…` → **Start mining**.  
Open **Instructions** for copy-paste terminal commands.

**CLI (live on mainnet):**

```bash
mhcoin mining start --network mainnet --address mhc1…
# expect: MHCOIN Live Miner · mode live · synced to network tip
```

Helpers: `packaging/mine_mainnet.sh` · `.bat` · `.ps1`

Notes:

- Stop Desktop mining (or Quit) before using the terminal on the **same** data folder.
- Mainnet CLI is **online** by default (`--offline` is for local tests only).
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
- [Storage](docs/STORAGE.md) — peers · flat blocks · LMDB chainstate
- [Wallet](docs/WALLET.md) — BIP39 / BIP32 · `mhc1…`
- [Node](docs/NODE.md)
- [P2P protocol](docs/NETWORK_PROTOCOL.md)
- [Desktop](docs/DESKTOP.md)
- [CLI quickstart](docs/USER_QUICKSTART.md)
- [Security](SECURITY.md)

## License

MIT — see [LICENSE](LICENSE).
