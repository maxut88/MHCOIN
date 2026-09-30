# MHCOIN Node

Stage 7: nodes gossip peer addresses, reconnect to known peers, and ban abusive
hosts — still without DNS seeds or a central directory.

## Run multiple nodes

```bash
mhcoin node start --network localnet --port 18444 --data-dir ~/.mhcoin/localnet/node1
mhcoin node start --network localnet --port 18445 --data-dir ~/.mhcoin/localnet/node2 \
  --connect 127.0.0.1:18444
mhcoin node start --network localnet --port 18446 --data-dir ~/.mhcoin/localnet/node3 \
  --connect 127.0.0.1:18445
```

Each node uses its **own** data directory.

## Inspect

```bash
mhcoin node info --data-dir ~/.mhcoin/localnet/node3
mhcoin node peers --data-dir ~/.mhcoin/localnet/node3
mhcoin node addrs --data-dir ~/.mhcoin/localnet/node3
mhcoin node bans --data-dir ~/.mhcoin/localnet/node3
mhcoin blockchain forks --data-dir ~/.mhcoin/localnet/node3
```

## Rules

- `--connect` seeds AddrDB as `manual` sources.
- Peer ADDR messages are untrusted; limits apply.
- `peer.start_height` remains a sync **hint**, never consensus.
- Fork choice remains highest cumulative PoW (Stage 6).
- Banned hosts cannot connect until ban expiry.
- Misbehavior scores **persist across reconnect** (Stage 8).

See [DISCOVERY.md](DISCOVERY.md), [SECURITY_AUDIT.md](SECURITY_AUDIT.md),
[REORG.md](REORG.md), [NETWORK_PROTOCOL.md](NETWORK_PROTOCOL.md).
