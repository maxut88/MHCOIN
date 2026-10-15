#!/usr/bin/env bash
# Install / refresh MHCOIN public ops units (seed + explorer + pool + download + upload).
# Requires sudo once:
#   bash /data/MHCOIN/packaging/ops/install_ops_systemd.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OPS="$(cd "$(dirname "$0")" && pwd)"
RC1="${ROOT}/rc1_ops"

UNITS=(
  mhcoin-mainnet-node.service
  mhcoin-explorer.service
  mhcoin-pool.service
  mhcoin-download.service
)

# Optional LAN upload helper if present
if [[ -f "${OPS}/mhcoin-upload.service" ]] || [[ -f "${RC1}/mhcoin-upload.service" ]]; then
  UNITS+=(mhcoin-upload.service)
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Re-running with sudo…"
  exec sudo -E bash "$0" "$@"
fi

echo "Installing units from ${OPS} (and ${RC1} fallback)"
for u in "${UNITS[@]}"; do
  src="${OPS}/${u}"
  if [[ ! -f "${src}" && -f "${RC1}/${u}" ]]; then
    src="${RC1}/${u}"
  fi
  if [[ ! -f "${src}" ]]; then
    echo "  skip missing ${u}"
    continue
  fi
  install -m 644 "${src}" "/etc/systemd/system/${u}"
  echo "  + /etc/systemd/system/${u}"
done

systemctl disable mhcoin-mainnet.service 2>/dev/null || true

echo "Stopping crash-loop / freeing ports from ad-hoc processes…"
systemctl stop mhcoin-pool.service mhcoin-explorer.service 2>/dev/null || true
pkill -f 'rc1_ops/run_pool.py' 2>/dev/null || true
pkill -f 'rc1_ops/run_explorer.py' 2>/dev/null || true
# download/upload/seed: only kill if we are about to restart them via systemd
sleep 1

systemctl daemon-reload
systemctl enable --now mhcoin-mainnet-node.service mhcoin-explorer.service mhcoin-pool.service mhcoin-download.service
if systemctl cat mhcoin-upload.service >/dev/null 2>&1; then
  systemctl enable --now mhcoin-upload.service || true
fi

echo
systemctl --no-pager --full status mhcoin-mainnet-node mhcoin-explorer mhcoin-pool mhcoin-download || true
echo
ss -tlnp | grep -E '8333|3333|3334|8765|8766|8767|8888' || true
echo
echo "OK — units enabled. Check:"
echo "  systemctl status mhcoin-mainnet-node mhcoin-explorer mhcoin-pool mhcoin-download"
echo "  journalctl -u mhcoin-pool -f"
