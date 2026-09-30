#!/usr/bin/env bash
# Generate SHA256SUMS for MHCOIN Core Desktop release artifacts.
# Usage: bash packaging/sha256sums.sh [dir=dist/release]
set -euo pipefail

DIR="${1:-dist/release}"
cd "${DIR}"

NAMES=(
  MHCOIN-Core-*-x86_64.AppImage
  MHCOIN-Core-*-linux-x86_64.tar.gz
  MHCOIN-Core-*-macos.dmg
  MHCOIN-Core-*-windows-x86_64.exe
  MHCOIN-Core-*-windows-x86_64.zip
)

OUT="SHA256SUMS"
rm -f "${OUT}"
shopt -s nullglob
for pattern in "${NAMES[@]}"; do
  for f in ${pattern}; do
    [[ -f "${f}" ]] || continue
    sha256sum "${f}" >> "${OUT}"
  done
done
shopt -u nullglob

if [[ ! -s "${OUT}" ]]; then
  echo "ERROR: no release artifacts matched in ${DIR}" >&2
  exit 1
fi

echo "Wrote $(pwd)/${OUT}"
cat "${OUT}"
