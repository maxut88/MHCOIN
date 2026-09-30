#!/usr/bin/env bash
# Wrap a PyInstaller onedir folder as an AppImage.
# Usage: make_appimage.sh <onedir-stage> <output.AppImage>
set -euo pipefail

STAGE="${1:?stage dir}"
OUT="${2:?output AppImage path}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TOOL="${ROOT}/packaging/linux/appimagetool"
if [[ ! -x "${TOOL}" ]]; then
  echo "appimagetool missing — run packaging/linux/fetch_appimagetool.sh"
  exit 1
fi

TMP="$(mktemp -d)"
APPDIR="${TMP}/MHCOIN-Core.AppDir"
mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/share/applications" \
         "${APPDIR}/usr/share/icons/hicolor/256x256/apps"

cp -a "${STAGE}/." "${APPDIR}/usr/bin/"

cat > "${APPDIR}/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/MHCOIN-Core" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

cat > "${APPDIR}/mhcoin-core.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MHCOIN Core
Exec=MHCOIN-Core
Icon=mhcoin
Categories=Finance;
Terminal=false
EOF
cp "${APPDIR}/mhcoin-core.desktop" "${APPDIR}/usr/share/applications/"

ICON_SRC="${ROOT}/packaging/icons/mhcoin-256.png"
if [[ ! -f "${ICON_SRC}" ]]; then
  ICON_SRC="${ROOT}/mhcoin/desktop/assets/mhcoin-256.png"
fi
if [[ ! -f "${ICON_SRC}" ]]; then
  ICON_SRC="${ROOT}/packaging/icons/mhcoin.png"
fi
if [[ -f "${ICON_SRC}" ]]; then
  cp "${ICON_SRC}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/mhcoin.png"
else
  echo "WARNING: MHCOIN icon PNG missing — AppImage will have a blank icon"
  python3 - <<PY
import struct, zlib, pathlib
p = pathlib.Path("${APPDIR}/usr/share/icons/hicolor/256x256/apps/mhcoin.png")
def chunk(tag, data):
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
rgb = bytes([0x2D, 0xD4, 0xA0])
w = h = 256
raw = b"".join(b"\x00" + rgb * w for _ in range(h))
p.write_bytes(
    b"\x89PNG\r\n\x1a\n"
    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    + chunk(b"IDAT", zlib.compress(raw, 9))
    + chunk(b"IEND", b"")
)
PY
fi
cp "${APPDIR}/usr/share/icons/hicolor/256x256/apps/mhcoin.png" "${APPDIR}/mhcoin.png"

mkdir -p "$(dirname "${OUT}")"
# Headless / no FUSE: extract-and-run
export APPIMAGE_EXTRACT_AND_RUN=1
ARCH=x86_64 "${TOOL}" "${APPDIR}" "${OUT}"
chmod +x "${OUT}"
echo "AppImage: ${OUT}"
rm -rf "${TMP}"
