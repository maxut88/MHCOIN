#!/usr/bin/env python3
"""PyInstaller entrypoint for MHCOIN Core Desktop.

End users double-click → native MHCOIN Core window (pywebview).
Release builds default to **mainnet** (T=0 live). Localnet remains
available via the in-app Network switcher.
"""

from __future__ import annotations

import os
import sys


def _prepare_env() -> None:
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    os.environ.setdefault("MHCOIN_DESKTOP_UI", "web")
    # Do NOT set MHCOIN_DATA here — each network uses ~/.mhcoin/<net>.
    # Release / frozen: mainnet. Dev source runs still default via prefs.
    if getattr(sys, "frozen", False):
        os.environ.setdefault("MHCOIN_NETWORK", "mainnet")
        # Immutable / gaming distros (Bazzite, Silverblue) rarely ship
        # WebKitGTK/PyQt for pywebview — skip the noisy probe and use browser UI.
        if sys.platform.startswith("linux"):
            os.environ.setdefault("MHCOIN_DESKTOP_BROWSER", "1")


def main() -> None:
    import multiprocessing as mp

    mp.freeze_support()
    _prepare_env()
    from mhcoin.desktop.webui import run_web_desktop

    run_web_desktop()


if __name__ == "__main__":
    main()
