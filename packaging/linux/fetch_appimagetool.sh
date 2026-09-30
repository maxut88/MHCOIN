#!/usr/bin/env bash
# Fetch appimagetool (x86_64) into packaging/linux/
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
OUT="${DIR}/appimagetool"
echo "Downloading appimagetool…"
curl -fsSL -o "${OUT}" "${URL}"
chmod +x "${OUT}"
echo "Saved ${OUT}"
