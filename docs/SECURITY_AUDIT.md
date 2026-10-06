# MHCOIN Security & Adversarial Audit

## Goal

Hardening so that adversarial peers cannot:

- mutate consensus state with invalid data
- exhaust memory / request tables
- bypass bans by reconnecting
- collapse an otherwise honest localnet

Target pipeline:

```text
attack
  → detect
  → penalty (misbehavior score)
  → disconnect
  → ban (threshold)
  → kick remaining sockets for that host
  → honest peers continue
```

## Clarification on “limits”

Bootstrap seeds (hardcoded / optional DNS) are **not** consensus authorities.
There is no central directory that nodes must trust for chain validity.

There **are** explicit safety limits, including:

| Area | Limits |
|------|--------|
| Envelope | `MAX_PAYLOAD_SIZE`, magic, checksum |
| INV / GETDATA | `MAX_INV_ITEMS`, `MAX_GETDATA_ITEMS`, request caps, **rate limits** |
| TX / BLOCK | `MAX_TX_SIZE`, `MAX_BLOCK_SIZE`, validation |
| Headers | `MAX_HEADERS`, `MAX_LOCATOR_HASHES`, GETHEADERS rate limit |
| Orphans | `MAX_ORPHAN_BLOCKS`, `MAX_ORPHAN_BYTES` |
| Reorg | `MAX_REORG_DEPTH` (policy) |
| ADDR | `MAX_ADDR_ENTRIES`, AddrDB cap, ADDR rate limit |
| Peers | `DEFAULT_MAX_PEERS`, inbound-per-host, connect rate |
| Abuse | ban score threshold, host bans, kick-on-ban |

## Hardenings

1. **Ban bypass fix** — misbehavior scores are **not** cleared on handshake.
2. **Unified penalty path** — malformed INV/GETDATA/TX/BLOCK/HEADERS/GETHEADERS
   and invalid blocks/txs increment score; ban closes the peer.
3. **Kick-on-ban** — `BanManager.on_ban` disconnects all sockets for that host.
4. **Rate limits** — INV, GETDATA, GETHEADERS, inbound connect, ADDR.
5. **Inbound slot limit** — `MAX_INBOUND_PER_HOST`.

## Attack coverage (tests)

`tests/network/test_adversarial.py` exercises, among others:

- oversized / wrong-magic / bad-checksum envelopes
- oversized INV / ADDR counts
- invalid PoW, merkle, bits (too easy), future timestamp
- double-spend / bad signature / excessive coinbase
- orphan flooding (bounded)
- malicious lower-work fork ignored
- deep reorg failure atomicity
- restart after reorg
- AddrDB isolation after corruption of an unrelated file
- duplicate connections rejected
- peer lying about height (no tip change)
- score persistence across reconnect
- repeated invalid blocks accumulate penalties

## Honest network continuity

Localhost tests share `127.0.0.1`, so host-bans affect all local peers.
Production deployments on distinct IPs keep honest peers connected while the
attacker host is banned.

## Out of scope / future policy

- Public HTTP RPC / exchange APIs
- Tor / I2P transports
- Mining pool protocols

Consensus genesis, difficulty schedule, and fingerprint are already frozen —
see [CONSENSUS.md](CONSENSUS.md) and [GENESIS.md](GENESIS.md).
