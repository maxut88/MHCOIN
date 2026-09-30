"""MHCOIN Core Desktop — GUI over the same consensus core (no consensus changes)."""

from __future__ import annotations

import os
import sys

__all__ = ["run_app", "main"]


def main(argv=None):
    argv = list(argv or sys.argv[1:])
    # Default: native window (pywebview) or browser fallback — see webui.py.
    # Force Tk with MHCOIN_DESKTOP_UI=tk (not recommended on macOS; Apple system Tk is broken).
    ui = os.environ.get("MHCOIN_DESKTOP_UI", "").strip().lower()
    if ui == "tk" and sys.platform != "darwin":
        from mhcoin.desktop.app import main as tk_main

        return tk_main(argv)
    if ui == "tk" and sys.platform == "darwin":
        print(
            "WARNING: macOS system Tk is broken; using native/web UI instead.\n"
            "Set MHCOIN_DESKTOP_UI=web explicitly to silence this.",
            file=sys.stderr,
        )
    from mhcoin.desktop.prefs import resolve_launch_network
    from mhcoin.desktop.webui import run_web_desktop

    network = resolve_launch_network(argv=argv)
    return run_web_desktop(network=network)


def run_app() -> None:
    main()
