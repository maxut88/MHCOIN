#!/usr/bin/env bash
# Launch MHCOIN Core Desktop (macOS / Linux).
# Prefer an activated venv; otherwise use python3 on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

export TK_SILENCE_DEPRECATION="${TK_SILENCE_DEPRECATION:-1}"
export MHCOIN_DESKTOP_UI="${MHCOIN_DESKTOP_UI:-web}"

# Never inherit a Linux-server datadir path (e.g. /data/MHCOIN/...) on a Mac/laptop.
# That happens if someone pasted RC1 server exports into the same shell.
if [[ -n "${MHCOIN_DATA:-}" ]]; then
  case "${MHCOIN_DATA}" in
    /data|/data/*)
      echo "Note: ignoring MHCOIN_DATA=${MHCOIN_DATA} (server path). Using ~/.mhcoin/<network>."
      unset MHCOIN_DATA
      ;;
  esac
fi
if [[ -n "${MHCOIN_DATA:-}" ]]; then
  if ! mkdir -p "${MHCOIN_DATA}" 2>/dev/null; then
    echo "Note: MHCOIN_DATA=${MHCOIN_DATA} is not writable. Using ~/.mhcoin/<network>."
    unset MHCOIN_DATA
  fi
fi

PY=python3
if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  PY="${VIRTUAL_ENV}/bin/python"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
fi

exec "${PY}" -m mhcoin.desktop "$@"
