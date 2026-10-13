#!/usr/bin/env bash
# Build MHCOIN Core Desktop for older macOS (default: 11.0 Big Sur+).
#
# Run THIS SCRIPT ON A MAC. Building on Linux cannot produce a macOS .app/.dmg.
# Intended for testing beside the “new OS only” DMG — does NOT push to GitHub.
#
# Usage:
#   bash packaging/build_macos_legacy.sh
#   MHCOIN_MACOS_MIN=12.0 bash packaging/build_macos_legacy.sh
#
# Output:
#   dist/release/MHCOIN-Core-<ver>-macos110-legacy.dmg   (name depends on min)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: macOS legacy build must run on a Mac (this host is $(uname -s))." >&2
  echo "Use: rc1_ops/mac/build_dmg_legacy_local.sh  (pulls sources + builds locally)." >&2
  exit 2
fi

# Big Sur (11.0) covers most “older than macOS 26” machines.
# Override: MHCOIN_MACOS_MIN=12.0 / 13.0 / 10.15 (10.15 needs older Python).
export MHCOIN_MACOS_MIN="${MHCOIN_MACOS_MIN:-11.0}"
export MACOSX_DEPLOYMENT_TARGET="${MHCOIN_MACOS_MIN}"
export MHCOIN_MACOS_LEGACY=1

echo "=== Legacy macOS desktop/miner build (min ${MHCOIN_MACOS_MIN}) ==="
echo "Python: $(command -v python3) ($(python3 -V 2>&1))"
echo "Tip: prefer official Python 3.11/3.12 macOS universal2, not only the newest 3.13+."

bash "${ROOT}/packaging/build.sh"

DMG="$(ls -1t "${ROOT}/dist/release"/MHCOIN-Core-*-macos*-legacy.dmg 2>/dev/null | head -1 || true)"
if [[ -z "${DMG}" ]]; then
  DMG="$(ls -1t "${ROOT}/dist/release"/MHCOIN-Core-*-macos.dmg 2>/dev/null | head -1 || true)"
fi
echo "=== Legacy artifact ==="
ls -la "${DMG}"
echo "Not uploaded to GitHub. Copy/test this DMG on the older Mac."
