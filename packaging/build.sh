#!/usr/bin/env bash
# Build MHCOIN Core Desktop with PyInstaller.
# Run on the TARGET OS (Linux → AppImage, macOS → .app/.dmg, Windows → use build.ps1).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

# Optional local tkinter tree (no root install): ~/.local/tkroot from python3-tk debs
TKROOT="${MHCOIN_TKROOT:-${HOME}/.local/tkroot}"
if [[ -d "${TKROOT}/usr/lib/python3.10/tkinter" ]]; then
  export PYTHONPATH="${TKROOT}/usr/lib/python3.10/lib-dynload:${TKROOT}/usr/lib/python3.10${PYTHONPATH:+:$PYTHONPATH}"
  export LD_LIBRARY_PATH="${TKROOT}/usr/lib/x86_64-linux-gnu:${TKROOT}/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export TCL_LIBRARY="${TKROOT}/usr/share/tcltk/tcl8.6"
  export TK_LIBRARY="${TKROOT}/usr/share/tcltk/tk8.6"
  echo "Using local Tk root: ${TKROOT}"
fi

VERSION="$(python3 -c 'from mhcoin import __version__; print(__version__)')"
DIST="${ROOT}/dist"
OUT_NAME="MHCOIN-Core-${VERSION}"

echo "=== MHCOIN Core Desktop build ${VERSION} ($(uname -s)) ==="

python3 -m pip install -U pip wheel setuptools pyinstaller >/dev/null
python3 -m pip install -e ".[desktop]" >/dev/null 2>&1 \
  || { python3 -m pip install -r requirements.txt >/dev/null; python3 -m pip install 'pywebview>=5.0' >/dev/null; }

# Sanity: tkinter still imported as optional fallback; warn only
python3 - <<'PY'
import sys
try:
    import tkinter
    print("tkinter OK", getattr(tkinter, "TkVersion", "?"))
except ModuleNotFoundError:
    print("NOTE: tkinter missing — native/web UI only (OK for release packages)")
try:
    import webview
    print("pywebview OK", getattr(webview, "__version__", "?"))
except ModuleNotFoundError:
    print("WARNING: pywebview missing — packaged app will fall back to system browser")
PY

rm -rf build/MHCOIN-Core "${DIST}/MHCOIN-Core" "${DIST}/MHCOIN-Core.app" 2>/dev/null || true

python3 -m PyInstaller packaging/mhcoin_core.spec --noconfirm --clean

mkdir -p "${DIST}/release"
OS="$(uname -s)"

case "${OS}" in
  Linux)
    STAGE="${DIST}/release/${OUT_NAME}-linux-x86_64"
    rm -rf "${STAGE}"
    mkdir -p "${STAGE}"
    cp -a "${DIST}/MHCOIN-Core/." "${STAGE}/"
    cat > "${STAGE}/mhcoin-core.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=MHCOIN Core
Comment=MHCOIN wallet, node and miner
Exec=MHCOIN-Core
Icon=mhcoin
Categories=Finance;Network;
Terminal=false
EOF
    if [[ -f "${ROOT}/packaging/icons/mhcoin-256.png" ]]; then
      cp "${ROOT}/packaging/icons/mhcoin-256.png" "${STAGE}/mhcoin.png"
    elif [[ -f "${ROOT}/mhcoin/desktop/assets/mhcoin-256.png" ]]; then
      cp "${ROOT}/mhcoin/desktop/assets/mhcoin-256.png" "${STAGE}/mhcoin.png"
    fi
    (
      cd "${DIST}/release"
      tar -czf "${OUT_NAME}-linux-x86_64.tar.gz" "${OUT_NAME}-linux-x86_64"
    )
    echo "Linux tarball: ${DIST}/release/${OUT_NAME}-linux-x86_64.tar.gz"

    if command -v appimagetool >/dev/null 2>&1 || [[ -x "${ROOT}/packaging/linux/appimagetool" ]]; then
      bash "${ROOT}/packaging/linux/make_appimage.sh" "${STAGE}" "${DIST}/release/${OUT_NAME}-x86_64.AppImage"
    else
      echo "NOTE: appimagetool not found — skip AppImage (tarball is ready)."
      echo "      Or: bash packaging/linux/fetch_appimagetool.sh && re-run build."
    fi
    # Keep only packaged artifacts under dist/release (CI uploads this tree).
    rm -rf "${STAGE}"
    ;;
  Darwin)
    APP="${DIST}/MHCOIN-Core.app"
    if [[ ! -d "${APP}" ]]; then
      APP="${DIST}/release/${OUT_NAME}.app"
      rm -rf "${APP}"
      mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
      cp -a "${DIST}/MHCOIN-Core/." "${APP}/Contents/MacOS/"
      ICNS="${ROOT}/packaging/icons/mhcoin.icns"
      if [[ ! -f "${ICNS}" ]]; then
        ICNS="${ROOT}/mhcoin/desktop/assets/mhcoin.icns"
      fi
      if [[ -f "${ICNS}" ]]; then
        cp "${ICNS}" "${APP}/Contents/Resources/mhcoin.icns"
      fi
      cat > "${APP}/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>MHCOIN-Core</string>
  <key>CFBundleIdentifier</key><string>org.mhcoin.core</string>
  <key>CFBundleName</key><string>MHCOIN Core</string>
  <key>CFBundleDisplayName</key><string>MHCOIN Core</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleIconFile</key><string>mhcoin</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
    else
      # Ensure PyInstaller .app also carries the MHCOIN icon
      ICNS="${ROOT}/packaging/icons/mhcoin.icns"
      if [[ ! -f "${ICNS}" ]]; then
        ICNS="${ROOT}/mhcoin/desktop/assets/mhcoin.icns"
      fi
      if [[ -f "${ICNS}" ]]; then
        mkdir -p "${APP}/Contents/Resources"
        cp "${ICNS}" "${APP}/Contents/Resources/mhcoin.icns"
      fi
    fi
    DMG="${DIST}/release/${OUT_NAME}-macos.dmg"
    mkdir -p "${DIST}/release"
    rm -f "${DMG}"
    hdiutil create -volname "MHCOIN Core" -srcfolder "${APP}" -ov -format UDZO "${DMG}"
    echo "macOS DMG: ${DMG}"
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows*)
    echo "On Windows use: powershell -File packaging/build.ps1"
    exit 2
    ;;
  *)
    echo "Unsupported OS: ${OS}"
    exit 2
    ;;
esac

echo "=== Done ==="
ls -la "${DIST}/release/" || true
