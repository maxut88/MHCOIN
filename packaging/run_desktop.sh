#!/usr/bin/env bash
# Launch MHCOIN Core Desktop (macOS / Linux).
# Prefer an activated venv; otherwise use python3 on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

export TK_SILENCE_DEPRECATION="${TK_SILENCE_DEPRECATION:-1}"
export MHCOIN_DESKTOP_UI="${MHCOIN_DESKTOP_UI:-web}"

PY=python3
if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

exec "${PY}" -m mhcoin.desktop "$@"
