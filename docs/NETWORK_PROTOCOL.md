# MHCOIN Network Protocol

## Status

| Stage | Scope | Status |
|-------|--------|--------|
| 1 | Message envelope | **Done** |
| 2 | VERSION/VERACK/PING/PONG + TCP peers | **Done** |
| 3 | INV / GETDATA / TX relay | **Done** |
| 4 | BLOCK relay + full block validation | **Done** |
| 5 | HEADERS + chain sync | **Done** |
| 6 | Reorg / fork choice | **Done** |
| 7 | Peer discovery / bans / reconnect | **Done** |
| 8 | Security & adversarial audit | **Done** |
| 9 | Mainnet preparation (frozen genesis, versioning) | **Done** (no auto-launch) |

---

## Envelope (Stage 1)

`[ magic 4 ][ command 12 ][ length u32 LE ][ checksum 4 ][ payload ]`  
Checksum = `HASH256(payload)[:4]`. `MAX_PAYLOAD_SIZE` = 1_000_000.

---

## Inventory

| Type | Value |
|------|-------|
| TX | 1 |
| BLOCK | 2 |

---

## Stage 5–6 — synchronization & forks

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

Stage 6: headers need not extend the active tip if the first header's parent is
already known. Full blocks remain required for UTXO validation. Fork choice uses
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

### Not in Stage 6

HEADERS-only light clients, seed nodes, mainnet genesis, mining pools, SPV.

---

## Stage 7 — discovery & resilience

See [DISCOVERY.md](DISCOVERY.md).

- `GETADDR` / `ADDR` gossip (bounded)
- Persistent AddrDB + ban list
- Outbound target + reconnect
- Misbehavior scoring on invalid blocks / protocol abuse
- **No DNS seeds / no central peer directory**

---

## Stage 8 — security & adversarial audit

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

Pipeline: detect → score → disconnect → ban → kick host → continue.

Additional rate limits: INV, GETDATA, GETHEADERS, inbound connects.
Scores persist across reconnect (no handshake wipe).
