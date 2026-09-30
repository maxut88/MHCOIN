# MHCOIN Stage 7 — Peer Discovery & Network Resilience

## Scope

Stage 7 makes MHCOIN nodes discover and reconnect to peers **without**
central servers, DNS seeds, or trusted directories.

```text
--connect (manual)
      │
      ▼
   AddrDB  ◄──── ADDR gossip from peers
      │
      ▼
 outbound selection + reconnect
      │
      ▼
 VERSION/VERACK … existing Stages 2–6
```

## Messages

### GETADDR

Empty payload. Requests known addresses from a peer.

### ADDR

```text
count: varint                 (max MAX_ADDR_ENTRIES = 1000)
repeated:
  timestamp: uint32
  services:  uint64
  host:      varint + UTF-8   (max MAX_ADDR_HOST_LEN = 64)
  port:      uint16
```

Hosts are untrusted text. Decode enforces bounds and basic character hygiene.

## AddrDB

Persistent SQLite (`peers.sqlite`):

- host, port, services, last_seen, last_try, attempts, source
- sources: `manual` (`--connect`), `gossip` (ADDR), `inbound`
- bounded by `MAX_ADDR_DB` (2500); oldest gossip entries evicted

## Ban / misbehavior

`bans.json` stores host bans and scores.

| Event | Typical points |
|-------|----------------|
| Protocol abuse | `MISBEHAVIOR_PROTOCOL` (10) |
| Invalid block | `MISBEHAVIOR_INVALID_BLOCK` (20) |
| ADDR flood | `MISBEHAVIOR_FLOOD` (5) |

Ban when score ≥ `BAN_SCORE_THRESHOLD` (100). Banned hosts are refused on
inbound accept and outbound connect.

## Outbound / reconnect

- Target: `DEFAULT_OUTBOUND_TARGET` (8), configurable
- Periodic tick tries AddrDB candidates not currently connected
- Skips self, banned, and recently attempted addresses
- **No DNS seeds**

## DoS limits

| Limit | Value |
|-------|-------|
| MAX_ADDR_ENTRIES | 1000 / message |
| MAX_ADDR_RATE_PER_PEER | 3 / 60s |
| MAX_GETADDR_RESPONSE | 32 |
| MAX_REQUESTED_TX / BLOCKS | 2048 / 1024 |
| MAX_ADDR_DB | 2500 |

## CLI

```bash
mhcoin node info --data-dir ...
mhcoin node peers --data-dir ...
mhcoin node addrs --data-dir ...
mhcoin node bans --data-dir ...
```

## Not in Stage 7

DNS seeds, seed nodes list, Tor/I2P, UPnP, mainnet genesis, public HTTP RPC,
mining pools, SPV.
