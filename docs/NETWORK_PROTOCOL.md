# MHCOIN Network Protocol

Wire protocol for MHCOIN full nodes. Related: [DISCOVERY.md](DISCOVERY.md),
[REORG.md](REORG.md), [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

## Envelope

`[ magic 4 ][ command 12 ][ length u32 LE ][ checksum 4 ][ payload ]`  
Checksum = `HASH256(payload)[:4]`. `MAX_PAYLOAD_SIZE` = 1_000_000.

## Inventory

| Type | Value |
|------|-------|
| TX | 1 |
| BLOCK | 2 |

## Handshake & keepalive

`VERSION` / `VERACK` establish the session. `PING` / `PONG` keep the link alive.
Peers outside `[MIN_SUPPORTED_PROTOCOL_VERSION, MAX_SUPPORTED_PROTOCOL_VERSION]`
are disconnected.

## Transaction & block relay

`INV` → `GETDATA` → `TX` / `BLOCK`. Full blocks are validated (PoW, merkle,
UTXO rules) before acceptance. See [TRANSACTIONS.md](TRANSACTIONS.md).

## Synchronization & forks

### GETHEADERS

```text
locator_count: varint   (max MAX_LOCATOR_HASHES = 32)
locator_hashes: ×32     (newest first; exponential backsteps)
hash_stop: 32 bytes     (zeros = no stop)
```

### HEADERS

```text
count: varint           (max MAX_HEADERS = 2000)
headers: count × 80-byte BlockHeader
```

### Locator

Built from the **active** chain: tip, tip−1, … then exponential steps, always including genesis.
Peer locators are untrusted input.

### Sync flow

```text
handshake (peer start_height hint > ours — informational only)
  → GETHEADERS(locator)
  → HEADERS
  → validate linkage + PoW (parent may be tip OR known side/active ancestor)
  → GETDATA(BLOCK) batches (≤ MAX_SYNC_BLOCK_BATCH = 16)
  → BLOCK → accept_block (may activate, store side chain, or reorg)
  → repeat GETHEADERS until empty HEADERS
```

Headers need not extend the active tip if the first header's parent is already
known. Full blocks remain required for UTXO validation. Fork choice uses
**local cumulative work** only — see [REORG.md](REORG.md).

### Limits

| Constant | Value |
|----------|-------|
| MAX_HEADERS | 2000 |
| MAX_LOCATOR_HASHES | 32 |
| MAX_SYNC_BLOCK_BATCH | 16 |
| MAX_REORG_DEPTH | 100 (node policy) |
| MAX_ORPHAN_BLOCKS | 64 |
| MAX_ORPHAN_BYTES | 2_000_000 |
| headers/sync timeouts | 15s / 30s |

Not in scope for this protocol layer: HEADERS-only light clients, mining pools, SPV.

## Discovery & resilience

See [DISCOVERY.md](DISCOVERY.md).

- `GETADDR` / `ADDR` gossip (bounded)
- Persistent AddrDB + ban list
- Outbound target + reconnect
- Misbehavior scoring on invalid blocks / protocol abuse
- Optional DNS / hardcoded seeds for first contact only (not consensus)

## Security

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

Pipeline: detect → score → disconnect → ban → kick host → continue.

Additional rate limits: INV, GETDATA, GETHEADERS, inbound connects.
Scores persist across reconnect (no handshake wipe).
