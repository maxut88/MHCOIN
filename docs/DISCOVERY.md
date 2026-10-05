# MHCOIN peer discovery — Bitcoin-style bootstrap

## Model (same idea as Bitcoin)

```text
hardcoded seeds + DNS seeds   ← first contact only (not consensus)
           │
           ▼
        AddrDB                ← peers.dat (Bitcoin-style peer address book)
           │
           ▼
   ADDR / GETADDR gossip      ← mesh grows without a central server
           │
           ▼
   VERSION / sync / mining
```

There is **no central coin server**. Seeds do not mint blocks and are not
trusted for consensus. Every node validates PoW and rules itself.

Today the published mainnet list may still be **small** (often one operator IP
until more seeds are published). That is a **bootstrap** risk for brand-new
installs with an empty `peers.dat` — not a consensus authority. Mitigations:

- Run additional public nodes and add them to `HARDCODED_SEEDS`
- Publish DNS seed names (`DNS_SEEDS` / `MHCOIN_DNS_SEEDS`)
- Users: `export MHCOIN_CONNECT=ip:8333,...` when the default seed is unreachable

Once a node has gossiped peers into `peers.dat`, it can reconnect without the
original seed.

## Bootstrap sources

1. `MHCOIN_CONNECT=host:port,...` — manual (like `bitcoind -connect`)
2. Hardcoded seeds in `mhcoin/network/seeds.py`
3. DNS seeds (`DNS_SEEDS` + `MHCOIN_DNS_SEEDS`) — resolve A/AAAA → many IPs

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

Hosts from peers are untrusted (bounded decode + hygiene).

## AddrDB

Persistent versioned JSON file (`peers.dat`). On first open, a legacy
`peers.sqlite` in the same directory is migrated (then renamed to
`peers.sqlite.bak`).

Fields per address:

- host, port, services, last_seen, last_try, attempts, source
- sources: `manual` (seeds / `--connect`), `gossip` (ADDR), `inbound`
- bounded by `MAX_ADDR_DB` (2500)

## Outbound / reconnect

- Target: `DEFAULT_OUTBOUND_TARGET` (8)
- Periodic reconnect from AddrDB
- Skips self, banned, recently attempted addresses

## Operator checklist (decentralize)

1. Run **2–3+** always-on listening nodes on different networks
2. Add their `IP:8333` to `HARDCODED_SEEDS`
3. Optionally publish DNS names with multiple A records (`DNS_SEEDS`)
4. Keep port **8333/tcp** open on those machines

## CLI

```bash
mhcoin node start --network mainnet
# uses built-in seeds when --connect is omitted

mhcoin node start --network mainnet --connect 203.0.113.10:8333
mhcoin node start --network mainnet --no-seed   # dial nothing until gossip/manual
```
