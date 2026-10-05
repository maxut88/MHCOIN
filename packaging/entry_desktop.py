#!/usr/bin/env python3
"""PyInstaller entrypoint for MHCOIN Core Desktop.

Critical for macOS feel: show a native window BEFORE importing mhcoin.*
(heavy crypto/chain modules). Dock bounce without a window = user thinks
the app is hung.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path


def _prepare_env() -> None:
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    os.environ.setdefault("MHCOIN_DESKTOP_UI", "web")
    if getattr(sys, "frozen", False):
        os.environ.setdefault("MHCOIN_NETWORK", "mainnet")
        if sys.platform.startswith("linux"):
            os.environ.setdefault("MHCOIN_DESKTOP_BROWSER", "1")


def _splash_html(version: str = "") -> str:
    ver = f" {version}" if version else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MHCOIN Core{ver}</title>
<style>
  html,body {{ margin:0; height:100%; background:#0c1210; color:#e8f0ea;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .wrap {{ min-height:100%; display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:14px; padding:24px; box-sizing:border-box; }}
  .logo {{ width:64px; height:64px; border-radius:50%; background:#1a8f5a;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 0 0 8px rgba(26,143,90,.15); }}
  .logo svg {{ width:34px; height:34px; }}
  h1 {{ margin:0; font-size:22px; letter-spacing:.04em; }}
  p {{ margin:0; color:#9bb0a2; font-size:13px; }}
  .bar {{ width:180px; height:4px; border-radius:99px; background:#1a2420;
    overflow:hidden; margin-top:8px; }}
  .bar > i {{ display:block; height:100%; width:40%; background:#1a8f5a;
    animation: slide 1.1s ease-in-out infinite; }}
  @keyframes slide {{
    0% {{ transform:translateX(-120%); }}
    100% {{ transform:translateX(320%); }}
  }}
</style></head><body>
<div class="wrap">
  <div class="logo" aria-hidden="true">
    <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
    </svg>
  </div>
  <h1>MHCOIN Core</h1>
  <p>Starting…</p>
  <div class="bar"><i></i></div>
</div>
</body></html>
"""


def _find_app_icon() -> str | None:
    """Locate mhcoin.icns/png without importing mhcoin (keeps splash fast)."""
    names = (
        ("mhcoin.icns", "mhcoin.png", "mhcoin-256.png")
        if sys.platform == "darwin"
        else ("mhcoin.png", "mhcoin-256.png", "mhcoin.ico", "mhcoin.icns")
    )
    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
            bases.append(Path(meipass) / "mhcoin" / "desktop" / "assets")
        exe = Path(sys.executable).resolve()
        bases.extend(
            [
                exe.parent,
                exe.parent / "assets",
                exe.parent.parent / "Resources",
                exe.parent.parent / "Resources" / "assets",
            ]
        )
    # Source / editable install
    here = Path(__file__).resolve()
    bases.extend(
        [
            here.parent / "icons",
            here.parent.parent / "mhcoin" / "desktop" / "assets",
        ]
    )
    for base in bases:
        for name in names:
            p = base / name
            if p.is_file():
                return str(p)
    return None


def _run_with_early_splash() -> None:
    """Show window first (webview only), then load mhcoin backend."""
    import webview  # type: ignore

    boot: dict = {
        "url": None,
        "state": None,
        "cleanup": None,
        "error": None,
        "navigated": False,
        "nav_started": False,
        "version": "",
    }
    ready = threading.Event()

    def prepare() -> None:
        try:
            # Heavy imports only after the splash window exists.
            from mhcoin.desktop.webui import start_desktop_backend

            url, state, cleanup, version = start_desktop_backend()
            boot["url"] = url
            boot["state"] = state
            boot["cleanup"] = cleanup
            boot["version"] = version or ""
        except Exception as e:  # noqa: BLE001
            boot["error"] = e
            print(f"Desktop boot failed: {e}", file=sys.stderr)
        finally:
            ready.set()

    win_title = "MHCOIN Core"
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass

    # Create the window FIRST — do not import mhcoin before this line.
    window = webview.create_window(
        win_title,
        html=_splash_html(),
        width=720,
        height=780,
        min_size=(480, 560),
        confirm_close=False,
        background_color="#0c1210",
    )
    # Backend boot in parallel only after the splash window object exists.
    threading.Thread(target=prepare, name="mhcoin-boot", daemon=True).start()

    def _force_quit() -> None:
        import time

        time.sleep(0.05)
        try:
            st = boot.get("state")
            if st is not None:
                st.ctrl.lock()
                st.ctrl.request_stop_mining()
        except Exception:
            pass
        os._exit(0)

    def _on_closing() -> bool:
        threading.Thread(target=_force_quit, daemon=True).start()
        return True

    try:
        window.events.closing += _on_closing
    except Exception:
        pass

    def navigate() -> None:
        ready.wait(timeout=180.0)
        if boot.get("navigated"):
            return
        boot["navigated"] = True
        err = boot.get("error")
        if err is not None:
            try:
                window.load_html(
                    "<html><body style='background:#0c1210;color:#fcc;"
                    "font-family:sans-serif;padding:40px'>"
                    "<h2>MHCOIN Core failed to start</h2>"
                    f"<p>{err}</p></body></html>"
                )
            except Exception:
                pass
            return
        try:
            # Wire close / save bridge used by the full UI.
            from mhcoin.desktop import webui as wui

            wui._GUI["window"] = window
            wui._GUI["state"] = boot["state"]
            wui._GUI["allow_quit"] = False
            ver = boot.get("version") or ""
            if ver:
                try:
                    window.set_title(f"MHCOIN Core {ver}")
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"GUI wire failed: {e}", file=sys.stderr)
        try:
            window.load_url(boot["url"])
        except Exception as e:  # noqa: BLE001
            print(f"load_url failed: {e}", file=sys.stderr)

    def on_loaded() -> None:
        if boot.get("nav_started"):
            return
        boot["nav_started"] = True
        threading.Thread(target=navigate, name="mhcoin-nav", daemon=True).start()

    try:
        window.events.loaded += on_loaded
    except Exception:
        threading.Thread(target=navigate, daemon=True).start()

    try:
        icon = _find_app_icon()
        if icon:
            webview.start(icon=icon)
        else:
            webview.start()
    finally:
        cleanup = boot.get("cleanup")
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass


def main() -> None:
    import multiprocessing as mp

    mp.freeze_support()
    _prepare_env()

    if _want_browser():
        # Linux AppImage / no WebKit: no early native splash.
        from mhcoin.desktop.webui import run_web_desktop

        run_web_desktop()
        return

    try:
        import webview  # noqa: F401
    except ImportError:
        from mhcoin.desktop.webui import run_web_desktop

        run_web_desktop()
        return

    _run_with_early_splash()


if __name__ == "__main__":
    main()
