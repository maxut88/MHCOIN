# MHCOIN Core 0.4.2.0

**Software version:** 0.4.2.0 · **Protocol:** 3

First public release of MHCOIN Core — full node, encrypted wallet, and Desktop miner.

## Features

- Cross-platform **MHCOIN Core** (Windows · macOS · Linux).
- Validating full node (P2P sync, mempool, reorgs).
- Encrypted wallet · BIP39 · multi-address receive · Send.
- Solo mining on the live mainnet tip (Desktop + Terminal).
- **Import / Show Private Key (WIF)** in Welcome + Settings.
- Optional native HASH256 pool: JSON `:3333`, Stratum `:3334`, stats `:8888` (PROP). See [POOL.md](POOL.md) and [SELF_HOST_POOL.md](SELF_HOST_POOL.md).

## Network

| | |
|---|---|
| Protocol | 3 |
| Difficulty | aserti3-2d ASERT from height 1 (τ = 172800 s) |
| Target block time | 600 s |
| Genesis bits | `0x1d00ffff` |
| Genesis hash | `b832eaa2883f17d6965e39849009344409d5a778eb8edac59e35262800000000` |
| PoW | HASH256 (double SHA-256) |
| Addresses | `mhc1…` (bech32) |

Verify:

```bash
mhcoin genesis verify --network mainnet
mhcoin audit fingerprint
```

## Downloads

[GitHub Releases — v0.4.2.0](https://github.com/maxut88/MHCOIN/releases/tag/v0.4.2.0)

| OS | File |
|----|------|
| Windows | `MHCOIN-Core-0.4.2.0-windows-x86_64.exe` (or `.zip`) |
| macOS | `MHCOIN-Core-0.4.2.0-macos.dmg` |
| Linux | `MHCOIN-Core-0.4.2.0-x86_64.AppImage` or `.tar.gz` |

Verify `SHA256SUMS` before running.
