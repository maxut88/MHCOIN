# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for MHCOIN Core Desktop.

Build (from repo root):
  pyinstaller packaging/mhcoin_core.spec --noconfirm --clean

Outputs under dist/:
  MHCOIN-Core/          (onedir — preferred for AppImage / .app bundling)
"""

import os
import sys
from pathlib import Path

block_cipher = None
_sp = Path(SPECPATH).resolve()
ROOT = _sp if _sp.is_dir() else _sp.parent  # packaging/
REPO = ROOT.parent

hiddenimports = [
    "mhcoin",
    "mhcoin.desktop",
    "mhcoin.desktop.app",
    "mhcoin.desktop.webui",
    "mhcoin.desktop.controller",
    "mhcoin.desktop.seeds",
    "mhcoin.desktop.prefs",
    "mhcoin.cli",
    "mhcoin.cli.main",
    "mhcoin.wallet",
    "mhcoin.wallet.wallet",
    "mhcoin.wallet.send",
    "mhcoin.wallet.storage",
    "mhcoin.wallet.addresses",
    "mhcoin.wallet.bip39",
    "mhcoin.wallet.hd",
    "mhcoin.wallet.wif",
    "mhcoin.mining",
    "mhcoin.mining.miner",
    "mhcoin.mining.block_template",
    "mhcoin.node",
    "mhcoin.node.local_node",
    "mhcoin.node.runtime",
    "mhcoin.blockchain",
    "mhcoin.blockchain.chain",
    "mhcoin.blockchain.blockstore",
    "webview",
    "webview.platforms",

    "mhcoin.blockchain.genesis",
    "mhcoin.consensus",
    "mhcoin.consensus.params",
    "mhcoin.consensus.difficulty",
    "mhcoin.consensus.proof_of_work",
    "mhcoin.consensus.chain_work",
    "mhcoin.consensus.block_reward",
    "mhcoin.network",
    "mhcoin.network.p2p",
    "mhcoin.network.seeds",
    "mhcoin.network.addrdb",
    "mhcoin.crypto",
    "mhcoin.crypto.hashing",
    "mhcoin.crypto.keys",
    "mhcoin.crypto.signatures",
    "mhcoin.utxo",
    "mhcoin.mempool",
    "cryptography",
    "Crypto",
    "click",
    "appdirs",
    "mnemonic",
    "plyvel",
    "tkinter",
    "_tkinter",
]

extra_binaries = []
extra_datas = []

# Bundle pywebview (native OS window) when installed in the build env
try:
    from PyInstaller.utils.hooks import collect_all

    _wv_datas, _wv_binaries, _wv_hidden = collect_all("webview")
    extra_datas += _wv_datas
    extra_binaries += _wv_binaries
    hiddenimports += list(_wv_hidden)
except Exception:
    pass

# LevelDB bindings (UTXO chainstate)
try:
    from PyInstaller.utils.hooks import collect_all

    _pl_datas, _pl_binaries, _pl_hidden = collect_all("plyvel")
    extra_datas += _pl_datas
    extra_binaries += _pl_binaries
    hiddenimports += list(_pl_hidden)
except Exception:
    pass

try:
    from PyInstaller.utils.hooks import collect_all

    _mn_datas, _mn_binaries, _mn_hidden = collect_all("mnemonic")
    extra_datas += _mn_datas
    extra_binaries += _mn_binaries
    hiddenimports += list(_mn_hidden)
except Exception:
    pass

# Bundle Tcl/Tk from optional local tree (Linux VPS without apt python3-tk)
tkroot = Path(os.environ.get("MHCOIN_TKROOT", Path.home() / ".local" / "tkroot"))
if tkroot.is_dir() and sys.platform.startswith("linux"):
    so_dirs = [
        tkroot / "usr" / "lib" / "x86_64-linux-gnu",
        tkroot / "usr" / "lib",
    ]
    for d in so_dirs:
        if not d.is_dir():
            continue
        for name in (
            "libtk8.6.so",
            "libtk8.6.so.0",
            "libtcl8.6.so",
            "libtcl8.6.so.0",
            "libBLT.2.5.so.8.6",
            "libBLTlite.2.5.so.8.6",
            "libXft.so.2",
            "libXss.so.1",
            "libXrender.so.1",
        ):
            p = d / name
            if p.is_file() or p.is_symlink():
                extra_binaries.append((str(p.resolve()), "."))
    for pyv in ("3.10", "3.11", "3.12"):
        so = (
            tkroot
            / "usr"
            / "lib"
            / f"python{pyv}"
            / "lib-dynload"
            / f"_tkinter.cpython-{pyv.replace('.', '')}-x86_64-linux-gnu.so"
        )
        if so.is_file():
            extra_binaries.append((str(so), "."))
            break
    tcl = tkroot / "usr" / "share" / "tcltk" / "tcl8.6"
    tk = tkroot / "usr" / "share" / "tcltk" / "tk8.6"
    if tcl.is_dir():
        extra_datas.append((str(tcl), "tcl8.6"))
    if tk.is_dir():
        extra_datas.append((str(tk), "tk8.6"))

# Bundle desktop assets (app icon / logo) so frozen builds aren't Python-branded
_assets = REPO / "mhcoin" / "desktop" / "assets"
if _assets.is_dir():
    for _name in (
        "mhcoin.png",
        "mhcoin-256.png",
        "mhcoin-64.png",
        "mhcoin-32.png",
        "mhcoin.ico",
        "mhcoin.icns",
        "mhcoin-logo.svg",
    ):
        _p = _assets / _name
        if _p.is_file():
            extra_datas.append((str(_p), "mhcoin/desktop/assets"))

_icon_png = REPO / "packaging" / "icons" / "mhcoin.png"
_icon_ico = REPO / "packaging" / "icons" / "mhcoin.ico"
_icon_icns = REPO / "packaging" / "icons" / "mhcoin.icns"
_exe_icon = None
if sys.platform == "win32" and _icon_ico.is_file():
    _exe_icon = str(_icon_ico)
elif sys.platform == "darwin" and _icon_icns.is_file():
    _exe_icon = str(_icon_icns)
elif _icon_png.is_file():
    _exe_icon = str(_icon_png)

a = Analysis(
    [str(ROOT / "entry_desktop.py")],
    pathex=[str(REPO)],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "rthook_tcltk.py")],
    excludes=["PySide6", "PyQt5", "PyQt6", "matplotlib", "numpy", "pandas"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Keep bundled OpenSSL on Linux — hashlib SHA256 PoW is much faster with it
# (~300 kH/s vs ~190 without on some hosts). Browser open must scrub
# LD_LIBRARY_PATH (see webui._open_system_browser) so xdg-open/kde-open
# do not load this older libssl against system libcurl.

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MHCOIN-Core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True if sys.platform == "darwin" else False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_exe_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="MHCOIN-Core",
)

if sys.platform == "darwin":
    # Build on newest macOS but still run on older ones when
    # MACOSX_DEPLOYMENT_TARGET / MHCOIN_MACOS_MIN is set (e.g. 11.0).
    _macos_min = os.environ.get("MHCOIN_MACOS_MIN") or os.environ.get(
        "MACOSX_DEPLOYMENT_TARGET", "11.0"
    )
    app = BUNDLE(
        coll,
        name="MHCOIN-Core.app",
        icon=str(_icon_icns) if _icon_icns.is_file() else None,
        bundle_identifier="org.mhcoin.core",
        info_plist={
            "CFBundleName": "MHCOIN Core",
            "CFBundleDisplayName": "MHCOIN Core",
            "CFBundleShortVersionString": "0.4.0.1",
            "CFBundleIconFile": "mhcoin.icns",
            "NSHighResolutionCapable": True,
            "NSAppSleepDisabled": True,
            "LSMinimumSystemVersion": str(_macos_min),
        },
    )
