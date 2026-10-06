#!/usr/bin/env bash
# MHCOIN live miner via terminal (same mainnet tip as Desktop: ~/.mhcoin/mainnet).
#
# IMPORTANT: Stop mining in Desktop (or quit the app) first — only one process
# may write the same datadir at a time.
#
# Usage:
#   ./packaging/mine_mainnet.sh
#   ./packaging/mine_mainnet.sh mhc1youraddress…
#   MHCOIN_MINER_ADDRESS=mhc1… ./packaging/mine_mainnet.sh
#
# Stop: Ctrl+C
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

export MHCOIN_NETWORK="${MHCOIN_NETWORK:-mainnet}"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

PY=python3
if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

ADDR="${1:-${MHCOIN_MINER_ADDRESS:-}}"
if [[ -z "${ADDR}" ]]; then
  echo "MHCOIN terminal miner — network=${MHCOIN_NETWORK}"
  echo "Data: \${HOME}/.mhcoin/${MHCOIN_NETWORK}"
  echo "Copy your address from Desktop → Receive, then paste below."
  echo
  read -r -p "Reward address (mhc1…): " ADDR
fi
ADDR="$(echo "${ADDR}" | tr -d '[:space:]')"
if [[ -z "${ADDR}" ]]; then
  echo "No address — abort." >&2
  exit 1
fi

if ! "${PY}" -c "import click, cryptography, lmdb, mhcoin" >/dev/null 2>&1; then
  echo "Missing Python deps in: ${PY}" >&2
  echo "From repo root (folder with packaging/):" >&2
  echo "  python3 -m venv .venv && source .venv/bin/activate" >&2
  echo "  python -m pip install -U pip setuptools wheel && python -m pip install -e ." >&2
  echo "Mac cryptography fail → brew install openssl@3 pkgconf, then retry (see Desktop → Instructions)." >&2
  exit 1
fi

echo
echo "Starting live miner…"
echo "  network: ${MHCOIN_NETWORK}"
echo "  address: ${ADDR}"
echo "  stop:    Ctrl+C"
echo

exec "${PY}" -m mhcoin.cli mining start --network "${MHCOIN_NETWORK}" --address "${ADDR}"
