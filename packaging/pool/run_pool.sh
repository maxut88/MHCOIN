#!/usr/bin/env bash
# Start MHCOIN pool from env file (used by systemd example).
# Usage:
#   export MHCOIN_POOL_ENV="$HOME/.mhcoin/pool.env"
#   packaging/pool/run_pool.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_FILE="${MHCOIN_POOL_ENV:-$HOME/.mhcoin/pool.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy packaging/pool/pool.env.example first" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${MHCOIN_POOL_ADDRESS:-}" || "$MHCOIN_POOL_ADDRESS" == mhc1YOUR_POOL_ADDRESS ]]; then
  echo "Set MHCOIN_POOL_ADDRESS in $ENV_FILE" >&2
  exit 2
fi

cd "$ROOT"
export PYTHONPATH="${PYTHONPATH:-$ROOT}"

if [[ -x "$ROOT/.venv/bin/mhcoin" ]]; then
  exec "$ROOT/.venv/bin/mhcoin" pool start \
    --address "$MHCOIN_POOL_ADDRESS" \
    --network "${MHCOIN_NETWORK:-mainnet}"
elif command -v mhcoin >/dev/null 2>&1; then
  exec mhcoin pool start \
    --address "$MHCOIN_POOL_ADDRESS" \
    --network "${MHCOIN_NETWORK:-mainnet}"
else
  exec python3 -m mhcoin.cli.main pool start \
    --address "$MHCOIN_POOL_ADDRESS" \
    --network "${MHCOIN_NETWORK:-mainnet}"
fi
