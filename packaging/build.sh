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

# On macOS: allow building on a new OS while targeting older runtimes.
if [[ "$(uname -s)" == "Darwin" ]]; then
  MACOS_MIN="${MHCOIN_MACOS_MIN:-${MACOSX_DEPLOYMENT_TARGET:-11.0}}"
  export MACOSX_DEPLOYMENT_TARGET="${MACOS_MIN}"
  export MHCOIN_MACOS_MIN="${MACOS_MIN}"
  export CFLAGS="${CFLAGS:+$CFLAGS }-mmacosx-version-min=${MACOS_MIN}"
  export CXXFLAGS="${CXXFLAGS:+$CXXFLAGS }-mmacosx-version-min=${MACOS_MIN}"
  export LDFLAGS="${LDFLAGS:+$LDFLAGS }-mmacosx-version-min=${MACOS_MIN}"
  echo "MACOSX_DEPLOYMENT_TARGET=${MACOSX_DEPLOYMENT_TARGET}"
fi

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
    cat > "${STAGE}/README-LINUX.txt" <<'EOF'
MHCOIN Core — Linux notes
=========================

Run from a terminal:
  ./MHCOIN-Core

UI: http://127.0.0.1:18765/

Native window needs system Qt (PyQt6) or GTK (PyGObject). On immutable
desktops (Bazzite/Silverblue) those are often missing — the app then
serves the same UI in your browser. That is supported.

If an older build crashes when opening the browser with:
  libssl.so.3: version OPENSSL_3.2.0 not found
do NOT delete OpenSSL from _internal (crypto may need it). Newer builds
open the browser with a cleaned LD_LIBRARY_PATH. Workaround for old
tarballs only:
  mv _internal/libssl.so.3 _internal/libssl.so.3.bak
  mv _internal/libcrypto.so.3 _internal/libcrypto.so.3.bak
Then open http://127.0.0.1:18765/ manually if needed.

Stop: Ctrl+C in the terminal (or close the native window).
EOF
    cat > "${STAGE}/run-browser.sh" <<'EOF'
#!/usr/bin/env bash
# Force browser UI (skip pywebview) — good on Bazzite / immutable Fedora.
set -euo pipefail
cd "$(dirname "$0")"
export MHCOIN_DESKTOP_BROWSER=1
exec ./MHCOIN-Core "$@"
EOF
    chmod +x "${STAGE}/run-browser.sh"
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
    MACOS_MIN="${MHCOIN_MACOS_MIN:-${MACOSX_DEPLOYMENT_TARGET:-11.0}}"
    export MACOSX_DEPLOYMENT_TARGET="${MACOS_MIN}"
    export MHCOIN_MACOS_MIN="${MACOS_MIN}"
    echo "macOS minimum system: ${MACOS_MIN}"

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
  <key>LSMinimumSystemVersion</key><string>${MACOS_MIN}</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppSleepDisabled</key><true/>
</dict></plist>
EOF
    else
      # Ensure PyInstaller .app also carries the MHCOIN icon + min OS
      ICNS="${ROOT}/packaging/icons/mhcoin.icns"
      if [[ ! -f "${ICNS}" ]]; then
        ICNS="${ROOT}/mhcoin/desktop/assets/mhcoin.icns"
      fi
      if [[ -f "${ICNS}" ]]; then
        mkdir -p "${APP}/Contents/Resources"
        cp "${ICNS}" "${APP}/Contents/Resources/mhcoin.icns"
      fi
      /usr/libexec/PlistBuddy -c "Add :LSMinimumSystemVersion string ${MACOS_MIN}" \
        "${APP}/Contents/Info.plist" 2>/dev/null \
        || /usr/libexec/PlistBuddy -c "Set :LSMinimumSystemVersion ${MACOS_MIN}" \
          "${APP}/Contents/Info.plist" 2>/dev/null || true
    fi
    # legacy builds get a distinct artifact name for side-by-side testing
    if [[ "${MHCOIN_MACOS_LEGACY:-0}" == "1" ]]; then
      TAG="macos${MACOS_MIN//./}-legacy"
      DMG="${DIST}/release/${OUT_NAME}-${TAG}.dmg"
      VOL="MHCOIN Core ${MACOS_MIN}+"
    else
      DMG="${DIST}/release/${OUT_NAME}-macos.dmg"
      VOL="MHCOIN Core"
    fi
    mkdir -p "${DIST}/release"
    rm -f "${DMG}"
    hdiutil create -volname "${VOL}" -srcfolder "${APP}" -ov -format UDZO "${DMG}"
    echo "macOS DMG: ${DMG}"
    ls -la "${DMG}"
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
