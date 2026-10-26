# MHCOIN — Two Independent VPS Full Nodes (preparation only)

**Status:** Configuration & runbook only. Do **not** order VPS or deploy without owner approval.  
**Release candidate SHA:** `c96d0a52a72075a9fa6d6ce5e93ce0627f3acd96`  
**Network:** `mainnet` · P2P default port `8333`

---

## Goals

- Two full nodes on **separate VPS**, preferably **different providers** and regions.
- Independent `datadir`, IP, firewall, logs, and systemd units.
- Strengthen P2P mesh before Coinbase Maturity activation (current seed mesh is thin).
- No mining keys / pool wallet keys on these nodes unless explicitly required later.

---

## Minimum requirements (per VPS)

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| OS | Ubuntu Server 22.04/24.04 LTS | 24.04 LTS |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 2 GiB | 4 GiB |
| Disk | 40 GiB SSD | 80+ GiB SSD |
| Network | Public IPv4, 8333/tcp inbound | Static IP |
| Python | 3.10+ | Match release CI |

Dependencies (typical): `python3`, `python3-venv`, `python3-pip`, build essentials if compiling optional extras, `git`.

---

## Security baseline

1. Create non-root user `mhcoin`; install code under `/opt/mhcoin` (release tarball of RC SHA only).
2. Firewall (`ufw`): allow `22/tcp` (admin IP only), `8333/tcp` (public P2P); **deny** RPC to the internet.
3. If RPC is enabled: bind `127.0.0.1` only + SSH tunnel; strong auth; no wallet unlocked at rest on public VPS.
4. Do **not** copy production pool mnemonic / payout keys onto VPS nodes.
5. Separate datadir: `/var/lib/mhcoin/mainnet/node-a` and `node-b` (example).
6. Logrotate for node logs; journald for systemd.

---

## Example layout

```text
/opt/mhcoin/                 # code @ c96d0a5 (or later agreed RC)
/var/lib/mhcoin/mainnet/vps1/
/etc/mhcoin/vps1.env
/etc/systemd/system/mhcoin-node-vps1.service
```

---

## Example `vps1.env`

```bash
MHCOIN_NETWORK=mainnet
MHCOIN_DATA_DIR=/var/lib/mhcoin/mainnet/vps1
MHCOIN_HOST=0.0.0.0
MHCOIN_PORT=8333
# Bootstrap to known seeds / controlled nodes (update at deploy time):
# MHCOIN_CONNECT=SEED1:8333,SEED2:8333
PYTHONPATH=/opt/mhcoin
```

---

## Example systemd unit

Adapt from `packaging/ops/mhcoin-mainnet-node.service`:

```ini
[Unit]
Description=MHCOIN Mainnet Full Node (VPS1)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mhcoin
Group=mhcoin
EnvironmentFile=/etc/mhcoin/vps1.env
WorkingDirectory=/opt/mhcoin
ExecStart=/usr/bin/python3 -m mhcoin node start
Restart=on-failure
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Exact CLI entrypoint must match the release (`mhcoin node start` vs `rc1_ops/run_mainnet_node.py`). Pin to the RC packaging docs at deploy time.

---

## Sync & monitoring checklist

After first start (when approved):

1. `mhcoin node info` — height climbing toward seed tip.
2. Tip hash matches controlled seed (`best block hash`).
3. ≥1 healthy outbound peer; prefer ≥2–3 diverse peers.
4. Disk growth stable; no repeated reorg storms.
5. Alert if height stalls > N× observed block interval.
6. After maturity-enabled release: reject logs may show `immature coinbase` for invalid peer blocks — expected when enforcing.

---

## Activation coordination

- Deploy **maturity-capable binary with `ACTIVATION_HEIGHT=0` first**.
- Only after owner sets a future height in a **new** release: upgrade VPS nodes **before** that height.
- VPS nodes should be online and synced ≥72 h before activation height is crossed (ops preference).

---

## Out of scope for this document

- Ordering cloud accounts / paying for VPS.
- Production deploy or DNS changes.
- Setting `COINBASE_MATURITY_ACTIVATION["mainnet"]`.
