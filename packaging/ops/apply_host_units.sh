#!/usr/bin/env bash
# One-shot: install updated units + move explorer/pool fully under systemd.
# Run:  sudo bash /data/MHCOIN/packaging/ops/apply_host_units.sh
set -euo pipefail
exec bash "$(cd "$(dirname "$0")" && pwd)/install_ops_systemd.sh"
