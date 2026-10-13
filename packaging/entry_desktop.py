#!/usr/bin/env python3
"""PyInstaller entrypoint for MHCOIN Core Desktop.

Show a splash window before importing mhcoin.* (heavy crypto/chain). Backend
boot starts only after the UI loop is up (webview.start(func=...)).

Dock / taskbar icon: same approach as 0.3.7.3 — pass icon= into webview.start
when an mhcoin.icns/png is found. On failure, retry without icon so a bad path
cannot leave the app with no window.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any


def _prepare_env() -> None:
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    os.environ.setdefault("MHCOIN_DESKTOP_UI", "web")
    if getattr(sys, "frozen", False):
        os.environ.setdefault("MHCOIN_NETWORK", "mainnet")
        if sys.platform.startswith("linux"):
            os.environ.setdefault("MHCOIN_DESKTOP_BROWSER", "1")


def _splash_html() -> str:
    return """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MHCOIN Core</title>
<style>
  html,body { margin:0; height:100%; background:#0c1210; color:#e8f0ea;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  .wrap { min-height:100%; display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:14px; padding:24px; box-sizing:border-box; }
  .logo { width:64px; height:64px; border-radius:50%; background:#1a8f5a;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 0 0 8px rgba(26,143,90,.15); }
  .logo svg { width:34px; height:34px; }
  h1 { margin:0; font-size:22px; letter-spacing:.04em; }
  p { margin:0; color:#9bb0a2; font-size:13px; }
  .bar { width:180px; height:4px; border-radius:99px; background:#1a2420;
    overflow:hidden; margin-top:8px; }
  .bar > i { display:block; height:100%; width:40%; background:#1a8f5a;
    animation: slide 1.1s ease-in-out infinite; }
  @keyframes slide {
    0% { transform:translateX(-120%); }
    100% { transform:translateX(320%); }
  }
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
    """Locate MHCOIN Dock/window icon (prefer .icns on macOS — same as 0.3.7.3)."""
    if sys.platform == "darwin":
        names = ("mhcoin.icns", "mhcoin.png", "mhcoin-256.png")
    elif sys.platform == "win32":
        names = ("mhcoin.ico", "mhcoin.png", "mhcoin-256.png")
    else:
        names = ("mhcoin.png", "mhcoin-256.png", "mhcoin.ico", "mhcoin.icns")

    bases: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.extend(
                [
                    Path(meipass) / "mhcoin" / "desktop" / "assets",
                    Path(meipass) / "assets",
                    Path(meipass),
                ]
            )
        exe = Path(sys.executable).resolve()
        bases.extend(
            [
                exe.parent / "mhcoin" / "desktop" / "assets",
                exe.parent / "assets",
                exe.parent,
                exe.parent.parent / "Resources",
            ]
        )
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


class _SaveBridge:
    """Minimal JS bridge so Save As works before mhcoin is imported."""

    def save_text_file(self, filename: str, content: str) -> dict[str, Any]:
        import webview  # type: ignore

        wins = getattr(webview, "windows", None) or []
        window = wins[0] if wins else None
        if window is None:
            return {"ok": False, "error": "no window"}

        name = (filename or "MHCOIN-save.txt").replace("/", "_").replace("\\", "_")
        for ch in '<>:"|?*':
            name = name.replace(ch, "_")
        start_dir = str(Path.home() / "Downloads")
        if not Path(start_dir).is_dir():
            start_dir = str(Path.home())

        save_flag = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        if save_flag is None:
            save_flag = getattr(webview, "SAVE_DIALOG", 20)

        try:
            result = window.create_file_dialog(
                save_flag,
                directory=start_dir,
                save_filename=name,
                file_types=("JSON (*.json)", "Text (*.txt)", "All files (*.*)"),
            )
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else result
        try:
            Path(path).write_text(content if content is not None else "", encoding="utf-8")
            try:
                Path(path).chmod(0o600)
            except OSError:
                pass
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
        return {"ok": True, "path": str(path)}


def _want_browser() -> bool:
    return os.environ.get("MHCOIN_DESKTOP_BROWSER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "browser",
    )


def _run_classic() -> None:
    from mhcoin.desktop.webui import run_web_desktop

    run_web_desktop()


def _run_with_early_splash() -> None:
    import webview  # type: ignore

    boot: dict[str, Any] = {
        "cleanup": None,
        "state": None,
        "done": False,
        "ui_ready": False,
    }

    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:
        pass

    window = webview.create_window(
        "MHCOIN Core",
        html=_splash_html(),
        width=720,
        height=780,
        min_size=(480, 560),
        confirm_close=False,
        background_color="#0c1210",
        js_api=_SaveBridge(),
    )

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
        # macOS/WebKit sometimes fires closing while splash → load_url runs.
        # Cancelling until the UI is up stops the app from auto-quitting.
        if not boot.get("ui_ready"):
            return False
        threading.Thread(target=_force_quit, daemon=True).start()
        return True

    try:
        window.events.closing += _on_closing
    except Exception:
        pass

    def _boot_backend() -> None:
        if boot["done"]:
            return
        boot["done"] = True
        try:
            from mhcoin.desktop.webui import start_desktop_backend
            from mhcoin.desktop import webui as wui

            url, state, cleanup, version = start_desktop_backend()
            boot["cleanup"] = cleanup
            boot["state"] = state
            wui._GUI["window"] = window
            wui._GUI["state"] = state
            wui._GUI["allow_quit"] = False
            if version:
                try:
                    window.set_title(f"MHCOIN Core {version}")
                except Exception:
                    pass
            window.load_url(url)
            # Allow real window-close only after the main UI has been asked to load.
            boot["ui_ready"] = True
        except Exception as e:  # noqa: BLE001
            boot["ui_ready"] = True
            print(f"Desktop boot failed: {e}", file=sys.stderr)
            try:
                window.load_html(
                    "<html><body style='background:#0c1210;color:#fcc;"
                    "font-family:sans-serif;padding:40px'>"
                    "<h2>MHCOIN Core failed to start</h2>"
                    f"<p>{e}</p></body></html>"
                )
            except Exception:
                pass

    def _after_ui_ready() -> None:
        threading.Thread(target=_boot_backend, name="mhcoin-boot", daemon=True).start()

    icon = _find_app_icon()
    start_kwargs: dict[str, Any] = {"func": _after_ui_ready}
    if icon:
        start_kwargs["icon"] = icon

    try:
        try:
            webview.start(**start_kwargs)
        except Exception as e:  # noqa: BLE001
            if "icon" in start_kwargs:
                print(
                    f"webview.start(icon=) failed ({e}); retrying without icon",
                    file=sys.stderr,
                )
                webview.start(func=_after_ui_ready)
            else:
                raise
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
        _run_classic()
        return

    try:
        import webview  # noqa: F401
    except ImportError:
        _run_classic()
        return

    try:
        _run_with_early_splash()
    except Exception as e:  # noqa: BLE001
        print(f"Early splash failed ({e}); falling back to classic start", file=sys.stderr)
        _run_classic()


if __name__ == "__main__":
    main()
