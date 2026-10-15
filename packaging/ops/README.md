# MHCOIN host ops (systemd)

Reference units for the public mainnet host behind **mhcscan.com**.

| Unit | Port | Public |
|------|------|--------|
| `mhcoin-mainnet-node` | 8333 | P2P seed |
| `mhcoin-explorer` | 8766 | https://explorer.mhcscan.com |
| `mhcoin-pool` | 3333 / 3334 / 8888 | mine.mhcscan.com · https://pool.mhcscan.com |
| `mhcoin-download` | 8765 | https://mhcscan.com |

```bash
# once (sudo):
bash /data/MHCOIN/packaging/ops/install_ops_systemd.sh
```

Secrets and public URL overrides: `~/.mhcoin/pool.env`  
(`MHCOIN_POOL_URL=https://pool.mhcscan.com`, `MHCOIN_POOL_PUBLIC_HOST=mine.mhcscan.com`, …).

Do **not** also run `nohup` / `start_ops.sh` copies of the same services — ports will conflict and systemd will crash-loop.

## TLS (Let's Encrypt)

Public HTTPS is terminated by **Nginx Proxy Manager** (Docker `nginx-proxy-manager`).

- Certificate: `mhcscan.com` (+ `www`, `explorer`, `pool`) via Let's Encrypt HTTP-01
- Expiry (DB): ~2027-01-13 — NPM renews automatically before expiry
- Renewal config inside NPM: `/etc/letsencrypt/renewal/npm-4.conf`
- Host `certbot.timer` is enabled (twice daily) for any host-managed certs; NPM manages its own ACME jobs

Quick check:

```bash
docker exec nginx-proxy-manager cat /etc/letsencrypt/renewal/npm-4.conf | head
# or NPM UI → SSL Certificates → mhcscan.com
```

## One-time apply on this host

```bash
sudo bash /data/MHCOIN/packaging/ops/install_ops_systemd.sh
```

This installs the units from `packaging/ops/`, stops crash-loops, and ensures explorer/pool/download run only under systemd (not `nohup`).
