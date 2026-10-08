# MHCOIN Consensus

Authoritative source: `mhcoin/consensus/params.py`.

## Shared monetary / PoW model (consensus-critical)

| Parameter | Value |
|-----------|--------|
| Max supply | 21,000,000 MHC |
| Decimals | 8 |
| Initial subsidy | 50 MHC |
| Halving | every 210,000 blocks |
| Block interval target | 600 seconds |
| Difficulty (from height 1500) | Bitcoin-style: retarget every **2016** blocks, full step, timespan clamp ×1/4..×4, no damping |
| Difficulty (legacy, height &lt; 1500) | Per-block W30 + damping 1/16 (historical mainnet only) |
| Activation height | `DIFFICULTY_BTC_ACTIVATION_HEIGHT = 1500` |
| MTP window | 11 |
| Max future block time | 7200 seconds |
| Mainnet POW_LIMIT | genesis target (`bits = 0x1e0fffff`) |
| PoW | double SHA-256 (`HASH256` of header) |
| Ledger | UTXO |

**Mainnet consensus fingerprint** (canonical source-tree hash):

`5eb609540c366c94b05b817205bed137fb2496c1d8b48cf2e0081498269c33eb`

**Mainnet genesis:**

`62e078a7ca0dfeac4c6451e5852d5ec714c7aacbff4e48f1d8cc8aa9560a0000`

Changing any of these without a coordinated protocol upgrade is a hard fork.
All nodes must upgrade to protocol v2 **before** height 1500.

## Consensus-critical vs policy

**Consensus-critical (must match for the same chain):** shared monetary table above,
`BLOCK_VERSION` / `TX_VERSION`, max block size / max txs, and per-network genesis
fields + network magic.

**Not consensus-critical (node policy):** ban scores, rate limits, orphan caps,
reconnect intervals, AddrDB size. Divergent policy does not split the chain by itself.

## Per-network identity (consensus-critical)

| Network | Magic | Default port | Address HRP | Custom genesis bootstrap |
|---------|-------|--------------|-------------|--------------------------|
| mainnet | `4D48434E` (MHCN) | 8333 | `mhc` | **No** |
| testnet | `4D48544E` (MHTN) | 18333 | `mht` | **No** |
| regtest | `4D48524E` (MHRN) | 18444 | `mhc` | Yes (local tooling) |
| localnet | `4D484C4E` (MHLN) | 18444 | `mhc` | Yes (local tooling) |

Cross-network peering is rejected by wrong magic **and** VERSION `network` field.
Data directories are `~/.mhcoin/<network>/` by default.

## Protocol versioning

| Field | Value | Role |
|-------|-------|------|
| `PROTOCOL_VERSION` | 2 | Advertised wire version (BTC difficulty hard fork) |
| `MIN_SUPPORTED_PROTOCOL_VERSION` | 2 | Reject older peers |
| `MAX_SUPPORTED_PROTOCOL_VERSION` | 2 | Reject newer peers |
| `SOFTWARE_VERSION` | 0.4.2.0 | Informational user-agent |

### Compatibility rules

1. Peers outside `[MIN_SUPPORTED, MAX_SUPPORTED]` are disconnected (no handshake).
2. Raising `MIN_SUPPORTED` is a **deliberate** network upgrade — document in release notes
   and coordinate before shipping.
3. Do **not** change consensus rules without also bumping protocol bounds in the same
   release; otherwise nodes can stay connected while enforcing different rules (split risk).
4. Soft-fork style policy tightenings may keep the same protocol version if old nodes
   still accept the subset of valid blocks; prefer explicit version bumps for clarity.

## Transaction / block rules

Canonical serialization → `txid = HASH256(serialize(tx))`.

Fee: `sum(input values) - sum(outputs)` (non-negative).

Header: version, prev hash, merkle root, timestamp, bits, nonce.
First transaction must be coinbase; coinbase value ≤ subsidy(height) + fees.

See also: [GENESIS.md](GENESIS.md), [MAINNET.md](MAINNET.md), [CONSENSUS_FREEZE.md](CONSENSUS_FREEZE.md).
