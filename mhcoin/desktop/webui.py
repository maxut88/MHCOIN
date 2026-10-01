"""MHCOIN Core Desktop — local UI (bypasses broken macOS system Tk).

Preferred: native OS window via pywebview (embedded WebKit/Chromium — not Safari).
Fallback: default browser if pywebview is missing or MHCOIN_DESKTOP_BROWSER=1.
Same CoreController / local HTTP API either way.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mhcoin.consensus.params import get_network_params
from mhcoin.desktop.controller import CoreController
from mhcoin.network.seeds import default_connect_peers
from mhcoin.wallet.wallet import WalletError

HOST = "127.0.0.1"
DEFAULT_PORT = 18765
_ASSETS = Path(__file__).resolve().parent / "assets"


def _want_browser_fallback() -> bool:
    return os.environ.get("MHCOIN_DESKTOP_BROWSER", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "browser",
    )


def _app_icon_path() -> str | None:
    """Resolve MHCOIN logo for window / Dock / taskbar (not the Python icon)."""
    # Prefer .icns on macOS (Dock / NSAlert), .ico on Windows, PNG elsewhere.
    if sys.platform == "darwin":
        names = ("mhcoin.icns", "mhcoin.png", "mhcoin-256.png", "mhcoin.ico")
    elif sys.platform == "win32":
        names = ("mhcoin.ico", "mhcoin.png", "mhcoin-256.png", "mhcoin.icns")
    else:
        names = ("mhcoin.png", "mhcoin-256.png", "mhcoin.ico", "mhcoin.icns")
    candidates: list[Path] = []
    # Dev / editable install
    for name in names:
        candidates.append(_ASSETS / name)
    # PyInstaller onedir / onefile
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        for name in names:
            candidates.append(base / "mhcoin" / "desktop" / "assets" / name)
            candidates.append(base / "assets" / name)
            candidates.append(base / name)
    # Next to frozen executable / .app Contents/MacOS
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for name in names:
            candidates.append(exe_dir / "mhcoin" / "desktop" / "assets" / name)
            candidates.append(exe_dir / "assets" / name)
            candidates.append(exe_dir / name)
        # .app/Contents/Resources
        resources = exe_dir.parent / "Resources"
        for name in ("mhcoin.icns", "mhcoin.png", "icon.icns"):
            candidates.append(resources / name)
    for p in candidates:
        if p.is_file():
            return str(p)
    return None


# Shared with pywebview close / shutdown.
_GUI: dict[str, Any] = {"window": None, "allow_quit": False, "state": None}


class _NativeBridge:
    """JS ↔ Python bridge for native Save As (blob downloads do not work in pywebview)."""

    def save_text_file(self, filename: str, content: str) -> dict[str, Any]:
        import webview  # type: ignore

        window = _GUI.get("window")
        if window is None:
            wins = getattr(webview, "windows", None) or []
            window = wins[0] if wins else None
        if window is None:
            return {"ok": False, "error": "no window"}

        name = (filename or "MHCOIN-save.txt").replace("/", "_").replace("\\", "_")
        # Strip characters illegal on Windows / some save dialogs.
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


def _open_native_window(url: str, state: Any | None = None) -> tuple[bool, str]:
    """Open a real desktop window (no browser chrome).

    Returns (ok, detail). detail explains skip/failure for logs.
    Window close (X) quits the app; use Settings → Lock wallet to stay open.
    """
    if _want_browser_fallback():
        return False, "MHCOIN_DESKTOP_BROWSER is set"
    try:
        import webview  # type: ignore
    except ImportError:
        return False, "pywebview not installed (pip install pywebview)"

    try:
        icon = _app_icon_path()
        _GUI["allow_quit"] = False
        _GUI["state"] = state
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        bridge = _NativeBridge()
        window = webview.create_window(
            "MHCOIN Core",
            url,
            width=720,
            height=780,
            min_size=(480, 560),
            confirm_close=False,
            background_color="#0c1210",
            js_api=bridge,
        )
        _GUI["window"] = window

        def _on_closing() -> bool:
            # pywebview: return False cancels close; True/None allows it.
            _GUI["allow_quit"] = True
            st = _GUI.get("state")

            def _force_quit() -> None:
                import time

                time.sleep(0.05)
                try:
                    if st is not None:
                        st.ctrl.lock()
                        st.ctrl.request_stop_mining()
                except Exception:
                    pass
                # Hard exit — do not wait for miner/node join (X must always quit).
                os._exit(0)

            # Quit out-of-band so a busy miner cannot keep the process alive.
            threading.Thread(target=_force_quit, name="mhcoin-force-quit", daemon=True).start()
            return True

        try:
            window.events.closing += _on_closing
        except Exception:
            pass

        if icon:
            webview.start(icon=icon)
        else:
            webview.start()
        return True, "quit" if _GUI.get("allow_quit") else "native window closed"
    except Exception as e:  # noqa: BLE001 — surface any GUI backend failure
        return False, f"pywebview failed: {type(e).__name__}: {e}"
    finally:
        _GUI["window"] = None

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MHCOIN Core</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='12' y1='8' x2='52' y2='56' gradientUnits='userSpaceOnUse'%3E%3Cstop stop-color='%235dffc0'/%3E%3Cstop offset='.42' stop-color='%232dd4a0'/%3E%3Cstop offset='1' stop-color='%230f7a55'/%3E%3C/linearGradient%3E%3C/defs%3E%3Ccircle cx='32' cy='32' r='30' fill='url(%23g)'/%3E%3Cpath d='M14 46V18h7.2l9.8 17.6L40.8 18H48v28h-5.8V27.6L36.2 46h-4.1L21.8 27.6V46H14z' fill='%2306261a'/%3E%3C/svg%3E"/>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700&family=JetBrains+Mono:wght@450;600&display=swap');
  :root {
    --bg0:#0c1210; --bg1:#121a17; --card:rgba(255,255,255,.04);
    --text:#eef3ef; --muted:#8b9a92; --accent:#2dd4a0; --accent2:#1aa876;
    --line:rgba(255,255,255,.08); --ok:#2dd4a0;
  }
  * { box-sizing: border-box; }
  body {
    margin:0; color: var(--text); font-size: 13.5px; line-height: 1.45;
    font-family: "DM Sans", -apple-system, BlinkMacSystemFont, sans-serif;
    background:
      radial-gradient(900px 420px at 10% -10%, rgba(45,212,160,.16), transparent 55%),
      radial-gradient(700px 360px at 110% 10%, rgba(34,120,90,.22), transparent 50%),
      linear-gradient(180deg, var(--bg0), var(--bg1) 40%, #0e1512);
    min-height: 100vh;
  }
  .wrap { max-width: 680px; margin: 0 auto; padding: 18px 14px 36px; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom: 6px; animation: rise .45s ease both; }
  .logo {
    --logo-size: 34px;
    width: var(--logo-size); height: var(--logo-size);
    position: relative; display:grid; place-items:center; flex-shrink:0;
  }
  .logo-ring, .logo-ring2 {
    position:absolute; inset:0; border-radius:50%;
    border: 1.5px solid transparent;
    border-top-color: var(--accent);
    border-right-color: rgba(45,212,160,.28);
    animation: spin 2.8s linear infinite;
  }
  .logo-ring2 {
    inset: 3px;
    border-top-color: rgba(45,212,160,.55);
    border-right-color: transparent;
    border-bottom-color: rgba(45,212,160,.2);
    animation: spin 4s linear infinite reverse;
  }
  .logo-coin {
    width: 74%; height: 74%; border-radius: 50%;
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.45), transparent 42%),
      linear-gradient(145deg, #5dffc0 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.4),
      0 0 18px rgba(45,212,160,.35),
      inset 0 -2px 4px rgba(0,40,25,.35),
      inset 0 2px 3px rgba(255,255,255,.35);
    display:grid; place-items:center;
    position: relative; overflow:hidden;
    animation: throb 2.4s ease-in-out infinite;
  }
  .logo-coin::after {
    content:""; position:absolute; inset:-40% -20%;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.5), transparent 70%);
    animation: sheen 3.2s ease-in-out infinite;
  }
  .logo-coin svg {
    width: 58%; height: 58%; position:relative; z-index:1;
    filter: drop-shadow(0 1px 0 rgba(0,40,25,.25));
  }
  .logo.lg { --logo-size: 56px; }
  .brand-text { display:flex; flex-direction:column; gap:1px; min-width:0; }
  .brand-text .tag {
    font-size: 10px; color: var(--muted); letter-spacing: .08em;
    text-transform: uppercase; font-weight: 650;
  }
  h1 { font-size: 20px; margin: 0; letter-spacing: -0.03em; font-weight: 700; }
  h2 { font-size: 16px; margin: 0 0 8px; letter-spacing: -0.02em; }
  h3 { font-size: 12px; margin: 14px 0 8px; font-weight: 650; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .sub { color: var(--muted); margin: 0 0 10px; font-size: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 14px; margin-bottom: 12px; backdrop-filter: blur(10px);
    box-shadow: 0 10px 40px rgba(0,0,0,.22); animation: rise .5s ease both;
  }
  .row { display:flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
  button, .tab {
    appearance:none; border:1px solid var(--line); background: rgba(255,255,255,.05); color:var(--text);
    padding: 7px 11px; border-radius: 8px; font-size: 12.5px; cursor:pointer; line-height: 1.2;
    transition: transform .12s ease, background .15s ease, border-color .15s ease;
  }
  button:hover, .tab:hover { background: rgba(255,255,255,.09); }
  button:active { transform: scale(.97); }
  button.primary {
    background: linear-gradient(180deg, #36e0aa, var(--accent2)); color:#06261a;
    border-color: transparent; font-weight:700; box-shadow: 0 6px 18px rgba(45,212,160,.25);
  }
  button.sm { padding: 4px 8px; font-size: 11px; border-radius: 6px; font-weight: 600; }
  button.linkish { background: transparent; border: none; color: var(--accent); padding: 0; font-size: 11px; font-weight: 650; }
  button:disabled { opacity: .45; cursor:not-allowed; }
  .tabs {
    display:flex; flex-wrap:nowrap; align-items:center; gap:3px;
    margin-bottom:10px; overflow-x:auto; -webkit-overflow-scrolling:touch;
    scrollbar-width: none;
  }
  .tabs::-webkit-scrollbar { display:none; }
  .tab {
    padding: 5px 7px; font-size: 11px; border-radius: 999px;
    white-space: nowrap; flex: 0 0 auto;
  }
  .tab.active { background: rgba(45,212,160,.16); color: var(--accent); border-color: rgba(45,212,160,.35); }
  input, textarea, select {
    width:100%; padding:8px 10px; font-size:12.5px; border:1px solid var(--line); border-radius:8px;
    margin: 4px 0 10px; font-family: "JetBrains Mono", ui-monospace, monospace;
    background: rgba(0,0,0,.25); color: var(--text);
  }
  label { display:block; color:var(--muted); font-size:11px; margin-top:2px; }
  .bal { font-size: 28px; font-weight: 700; margin: 2px 0 8px; letter-spacing: -0.03em; }
  .mono { word-break: break-all; overflow-wrap: anywhere; font-family: "JetBrains Mono", ui-monospace, monospace; }
  textarea.mono, input.mono {
    white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-all;
    overflow-x: hidden; max-width: 100%;
  }
  .msg { padding:8px 10px; border-radius:8px; margin:8px 0; display:none; font-size: 12px; }
  .msg.ok { display:block; background: rgba(45,212,160,.12); color: #9af0ce; border:1px solid rgba(45,212,160,.2); }
  .msg.err { display:block; background: rgba(240,113,103,.12); color: #ffb4ad; border:1px solid rgba(240,113,103,.25); }
  .hidden { display:none !important; }
  .status { color: var(--muted); font-size: 11.5px; }
  .pill {
    display:inline-flex; align-items:center; gap:6px; padding: 3px 8px; border-radius: 999px;
    border: 1px solid var(--line); background: rgba(255,255,255,.03); font-size: 11px; color: var(--muted);
  }
  .dot { width:6px; height:6px; border-radius:50%; background: var(--muted); }
  .dot.on { background: var(--ok); box-shadow: 0 0 0 3px rgba(45,212,160,.15); animation: pulse 1.6s ease-in-out infinite; }
  .act { border: 1px solid var(--line); border-radius: 8px; padding: 6px 8px; margin: 0 0 4px; background: rgba(0,0,0,.18); }
  .act-top { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
  .act-title { font-weight: 650; font-size: 12px; margin: 0; }
  .act-meta { color: var(--muted); font-size: 10.5px; margin: 1px 0 0; line-height: 1.3; }
  .act-amt { font-weight: 700; font-size: 12px; white-space: nowrap; font-family: "JetBrains Mono", monospace; }
  .act-amt.in { color: #5dffc0; }
  .act-amt.out { color: #ff8f86; }
  .act-badge { display:none; }
  .act-txid { margin-top: 4px; display:flex; align-items:center; gap:6px; }
  .act-txid .mono { font-size: 10px; color: var(--muted); flex:1; min-width:0; }
  .act-list { max-height: 340px; overflow: auto; margin-top: 4px; }
  .act-full { padding: 7px 8px; }
  .act-full .act-top { margin-bottom: 2px; }
  .act-grid {
    display:grid; grid-template-columns: 44px 1fr auto; gap: 2px 6px; align-items: start;
    margin-top: 5px; font-size: 10.5px;
  }
  .act-grid .k { color: var(--muted); text-transform: uppercase; letter-spacing: .03em; padding-top: 1px; }
  .act-grid .v {
    font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 10.5px; line-height: 1.35;
    overflow-wrap: anywhere; word-break: break-all; user-select: text; -webkit-user-select: text;
    min-width: 0;
  }
  .act-grid .v.clip {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; word-break: normal;
  }
  .act-grid .a { align-self: start; }
  .act-kv { margin-top: 8px; }
  .act-kv > span {
    display:block; color: var(--muted); font-size: 10.5px; text-transform: uppercase;
    letter-spacing: .04em; margin-bottom: 3px;
  }
  .act-kv textarea.mono {
    width: 100%; margin: 0; padding: 6px 8px; font-size: 11px; line-height: 1.35;
    min-height: 0; resize: vertical; box-sizing: border-box;
  }
  .act-kv .act-val {
    font-size: 12px; word-break: break-all; overflow-wrap: anywhere;
    user-select: text; -webkit-user-select: text;
  }
  .act-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top: 2px; }
  .mine-hero {
    position: relative; overflow: hidden; border-radius: 14px; padding: 16px 14px;
    border: 1px solid rgba(45,212,160,.2);
    background:
      radial-gradient(120px 80px at 80% 20%, rgba(45,212,160,.25), transparent 70%),
      linear-gradient(160deg, rgba(45,212,160,.08), rgba(0,0,0,.2));
    margin-bottom: 12px;
  }
  .mine-hero.live::before {
    content:""; position:absolute; inset:-40%;
    background: conic-gradient(from 0deg, transparent, rgba(45,212,160,.18), transparent 35%);
    animation: spin 4.5s linear infinite;
  }
  .mine-inner { position: relative; z-index: 1; }
  .mine-ring-wrap { display:flex; justify-content:center; margin: 6px 0 12px; }
  .mine-ring {
    width: 110px; height: 110px; border-radius: 50%;
    display:grid; place-items:center; position: relative;
    background: radial-gradient(circle at 50% 45%, rgba(45,212,160,.22), transparent 62%);
  }
  .mine-ring::before, .mine-ring::after {
    content:""; position:absolute; inset:0; border-radius:50%;
    border: 2px solid transparent; border-top-color: var(--accent); border-right-color: rgba(45,212,160,.25);
  }
  .mine-ring::after { inset: 10px; border-top-color: rgba(45,212,160,.55); border-right-color: transparent; }
  .mine-hero.live .mine-ring::before { animation: spin 1.4s linear infinite; }
  .mine-hero.live .mine-ring::after { animation: spin 2.2s linear infinite reverse; }
  .mine-ring .logo { --logo-size: 64px; }
  .mine-hero:not(.live) .mine-ring::before,
  .mine-hero:not(.live) .mine-ring::after { opacity: .35; }
  .mine-hero:not(.live) .mine-ring .logo-coin { animation: throb 2.8s ease-in-out infinite; }
  .mine-hero.live .mine-ring .logo-coin {
    animation: throb 1.2s ease-in-out infinite;
    box-shadow:
      0 0 28px rgba(45,212,160,.55),
      0 0 0 1px rgba(45,212,160,.45),
      inset 0 -2px 4px rgba(0,40,25,.35),
      inset 0 2px 3px rgba(255,255,255,.35);
  }
  .mine-stats { display:grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; margin-top: 8px; }
  .stat { border: 1px solid var(--line); border-radius: 10px; padding: 8px; background: rgba(0,0,0,.22); }
  .stat .k { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .stat .v { font-size: 12px; font-weight: 700; margin-top: 2px; font-family: "JetBrains Mono", monospace; }
  .bars { display:flex; align-items:flex-end; gap:3px; height: 28px; margin-top: 10px; }
  .bars span { flex:1; border-radius: 3px 3px 1px 1px; background: rgba(45,212,160,.25); height: 20%; }
  .mine-hero.live .bars span {
    animation: bar 1.05s ease-in-out infinite;
    background: linear-gradient(180deg, #5dffc0, rgba(45,212,160,.2));
  }
  .mine-hero.live .bars span:nth-child(2){animation-delay:.08s}
  .mine-hero.live .bars span:nth-child(3){animation-delay:.16s}
  .mine-hero.live .bars span:nth-child(4){animation-delay:.24s}
  .mine-hero.live .bars span:nth-child(5){animation-delay:.32s}
  .mine-hero.live .bars span:nth-child(6){animation-delay:.4s}
  .mine-hero.live .bars span:nth-child(7){animation-delay:.48s}
  .mine-hero.live .bars span:nth-child(8){animation-delay:.56s}
  @keyframes spin { to { transform: rotate(360deg); } }
  @keyframes throb { 0%,100% { transform: scale(1);} 50% { transform: scale(1.08);} }
  @keyframes pulse { 0%,100% { box-shadow: 0 0 0 0 rgba(45,212,160,0);} 50% { box-shadow: 0 0 0 5px rgba(45,212,160,.12);} }
  @keyframes bar { 0%,100% { height:18%; opacity:.55;} 50% { height:100%; opacity:1;} }
  @keyframes rise { from { opacity:0; transform: translateY(8px);} to { opacity:1; transform:none;} }
  @keyframes sheen { 0%,100% { transform: translateX(-30%) rotate(18deg);} 50% { transform: translateX(40%) rotate(18deg);} }
  .mine-log {
    margin-top: 8px; max-height: 220px; overflow: auto; resize: vertical;
    background: #060a08; border: 1px solid var(--line); border-radius: 8px;
    padding: 8px 10px; font-family: "JetBrains Mono", ui-monospace, monospace;
    font-size: 10.5px; line-height: 1.45; color: #d6dde8; white-space: pre-wrap;
    word-break: break-all; user-select: text; -webkit-user-select: text;
  }
  .mine-log .ml-dim { color: #7a8a82; }
  .mine-log .ml-cyan { color: #5eead4; }
  .mine-log .ml-green { color: #4ade80; font-weight: 650; }
  .mine-log .ml-yellow { color: #fbbf24; }
  .mine-log .ml-mag { color: #e879f9; }
  .mine-log .ml-red { color: #f87171; }
  .mine-log .ml-bold { color: #f1f5f9; font-weight: 700; }
  .mine-log .ml-ts { color: #64748b; }
  .modal-backdrop {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(4,10,8,.72); backdrop-filter: blur(8px);
    display:flex; align-items:center; justify-content:center; padding: 18px;
    animation: rise .2s ease both;
  }
  .modal-backdrop.hidden { display:none !important; }
  .modal {
    width: min(420px, 100%);
    max-width: 100%;
    box-sizing: border-box;
    background: linear-gradient(180deg, #15211c, #101815);
    border: 1px solid rgba(45,212,160,.28);
    border-radius: 16px;
    padding: 18px 16px 14px;
    box-shadow: 0 24px 60px rgba(0,0,0,.45), 0 0 0 1px rgba(255,255,255,.04);
    overflow: hidden;
  }
  .modal-head { display:flex; align-items:center; gap:10px; margin-bottom: 10px; }
  .modal-head .logo { --logo-size: 36px; }
  .modal-head h3 {
    margin:0; font-size: 15px; letter-spacing: -0.02em; text-transform: none;
    color: var(--text); font-weight: 700;
  }
  .modal-msg {
    color: var(--muted); font-size: 12.5px; white-space: pre-wrap; margin: 0 0 12px; line-height: 1.45;
    user-select: text; -webkit-user-select: text; cursor: text;
    overflow-wrap: anywhere; word-break: break-word; max-width: 100%;
  }
  .modal-copy {
    width: 100%; min-height: 96px; resize: vertical; margin: 0 0 10px;
    font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px;
    line-height: 1.4; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-all;
    overflow-x: hidden; max-width: 100%; box-sizing: border-box;
    user-select: text; -webkit-user-select: text; cursor: text;
  }
  .modal-copy.tall { min-height: 280px; }
  .modal-copy.hidden { display: none !important; }
  .modal input {
    width: 100%; margin: 0 0 12px; box-sizing: border-box;
  }
  .modal-actions { display:flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
  .modal-actions.left { justify-content: flex-start; margin-bottom: 8px; }
  .support-form label { display:block; margin: 8px 0 4px; color: var(--muted); font-size: 11.5px; }
  .support-form input, .support-form textarea, .support-form select {
    width: 100%; box-sizing: border-box; margin: 0 0 2px;
    background: #0a1210; border: 1px solid var(--line); border-radius: 8px;
    color: var(--text); padding: 8px 10px; font-size: 12.5px;
  }
  .support-form textarea { min-height: 110px; resize: vertical; font-family: inherit; line-height: 1.4; }
  .support-form .support-hp { position:absolute; left:-9999px; opacity:0; height:0; width:0; overflow:hidden; }
  .support-form .support-meta { color: var(--muted); font-size: 10.5px; margin: 8px 0 0; line-height: 1.35; }
  .support-form .support-status { min-height: 18px; margin: 8px 0 0; font-size: 12px; }
  .support-form .support-status.ok { color: #4ade80; }
  .support-form .support-status.err { color: #f87171; }
  .modal.support-modal { width: min(460px, 100%); }
  #welcome.busy { position: relative; pointer-events: none; opacity: .72; }
  #welcome.busy #btnSupportWelcome { pointer-events: auto; opacity: 1; }
  .welcome-load {
    margin: 10px 0 12px; padding: 10px 12px; border-radius: 10px;
    border: 1px solid rgba(45,212,160,.28);
    background: rgba(45,212,160,.07);
  }
  .welcome-load.hidden { display: none !important; }
  .welcome-load-title { font-size: 12.5px; font-weight: 650; margin: 0 0 4px; }
  .welcome-load-meta { color: var(--muted); font-size: 11px; margin: 0 0 8px; line-height: 1.35; }

  .boot-overlay {
    position: fixed; inset: 0; z-index: 2000;
    background: radial-gradient(120% 80% at 50% 20%, #15241e, #0c1210 70%);
    display:flex; align-items:center; justify-content:center; padding: 24px;
  }
  .boot-overlay.hidden { display:none !important; }
  .boot-card {
    width: min(420px, 100%); text-align:center;
    animation: rise .35s ease both;
  }
  .boot-card .logo { --logo-size: 72px; margin: 0 auto 14px; }
  .boot-card h2 { margin: 0 0 6px; font-size: 18px; letter-spacing: -0.02em; }
  .boot-sub { color: var(--muted); font-size: 12.5px; margin: 0 0 16px; line-height: 1.45; }
  .boot-spinner {
    width: 36px; height: 36px; margin: 0 auto 14px; border-radius: 50%;
    border: 3px solid rgba(45,212,160,.18); border-top-color: var(--accent);
    animation: spin 0.85s linear infinite;
  }
  .sync-box {
    margin: 8px 0 12px; padding: 12px 14px; border-radius: 12px;
    border: 1px solid rgba(45,212,160,.28);
    background:
      radial-gradient(120% 80% at 0% 0%, rgba(45,212,160,.14), transparent 55%),
      rgba(8,14,12,.55);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.04);
  }
  .sync-box.done {
    border-color: var(--line);
    background: rgba(255,255,255,.03);
    box-shadow: none;
  }
  .sync-box.done .sync-bar > i {
    box-shadow: 0 0 10px rgba(45,212,160,.25);
    animation: none;
  }
  .sync-box.done .sync-bar > i::after { animation: none; opacity: .35; }
  .sync-head {
    display:flex; align-items:baseline; justify-content:space-between; gap:10px;
    margin: 0 0 4px;
  }
  .sync-title { font-size: 12.5px; font-weight: 650; margin: 0; }
  .sync-pct {
    font-family: "JetBrains Mono", ui-monospace, monospace;
    font-size: 11px; font-weight: 700; color: #5dffc0;
    letter-spacing: 0.02em; min-width: 3.2em; text-align: right;
    text-shadow: 0 0 12px rgba(93,255,192,.35);
  }
  .sync-box.done .sync-pct { color: var(--muted); text-shadow: none; }
  .sync-meta { color: var(--muted); font-size: 11px; margin: 0 0 10px; line-height: 1.35; }
  .sync-bar {
    position: relative;
    height: 11px; border-radius: 999px;
    background:
      linear-gradient(180deg, rgba(0,0,0,.55), rgba(12,20,16,.75)),
      repeating-linear-gradient(90deg,
        rgba(45,212,160,.06) 0 1px,
        transparent 1px 8px);
    overflow: hidden;
    border: 1px solid rgba(45,212,160,.22);
    box-shadow:
      inset 0 1px 2px rgba(0,0,0,.45),
      0 0 0 1px rgba(0,0,0,.25);
  }
  .sync-bar > i {
    position: relative;
    display:block; height:100%; width:0%; border-radius: inherit;
    background:
      linear-gradient(180deg, rgba(255,255,255,.28), transparent 42%),
      linear-gradient(90deg, #0d8f62 0%, #1fd196 42%, #7dffd0 78%, #b8ffe8 100%);
    box-shadow:
      0 0 14px rgba(45,212,160,.55),
      0 0 28px rgba(45,212,160,.22),
      inset 0 -1px 0 rgba(0,0,0,.25);
    transition: width .4s cubic-bezier(.22,.85,.3,1);
    overflow: hidden;
  }
  .sync-bar > i::before {
    content:"";
    position:absolute; inset:0;
    background: repeating-linear-gradient(
      -55deg,
      rgba(255,255,255,.10) 0 6px,
      transparent 6px 12px
    );
    mix-blend-mode: soft-light;
    opacity: .55;
    animation: syncStripes 1.1s linear infinite;
  }
  .sync-bar > i::after {
    content:"";
    position:absolute; top:-40%; bottom:-40%; width:42%;
    left: -20%;
    background: linear-gradient(90deg,
      transparent 0%,
      rgba(255,255,255,.08) 35%,
      rgba(255,255,255,.45) 50%,
      rgba(255,255,255,.08) 65%,
      transparent 100%);
    transform: skewX(-18deg);
    animation: syncSheen 2.1s ease-in-out infinite;
  }
  .sync-bar.indeterminate > i {
    width: 36% !important;
    animation: syncSlide 1.35s cubic-bezier(.45,.05,.55,.95) infinite;
  }
  .sync-bar.indeterminate > i::before { animation-duration: .7s; }
  @keyframes syncSlide {
    0% { transform: translateX(-115%); }
    100% { transform: translateX(310%); }
  }
  @keyframes syncSheen {
    0%, 18% { left: -35%; opacity: 0; }
    35% { opacity: 1; }
    55%, 100% { left: 105%; opacity: 0; }
  }
  @keyframes syncStripes {
    0% { background-position: 0 0; }
    100% { background-position: 17px 0; }
  }
  .send-overlay {
    position: fixed; inset: 0; z-index: 2100;
    background: radial-gradient(120% 90% at 50% 15%, rgba(45,212,160,.22), #0c1210 68%);
    display:flex; align-items:center; justify-content:center; padding: 22px;
    animation: rise .25s ease both;
  }
  .send-overlay.hidden { display:none !important; }
  .send-card {
    width: min(440px, 100%); text-align:center;
    background: linear-gradient(180deg, rgba(21,33,28,.95), rgba(12,18,16,.98));
    border: 1px solid rgba(45,212,160,.3);
    border-radius: 18px; padding: 22px 18px 16px;
    box-shadow: 0 28px 70px rgba(0,0,0,.5);
  }
  .send-orbit {
    width: 120px; height: 120px; margin: 4px auto 14px; position: relative;
    display:grid; place-items:center;
  }
  .send-orbit::before, .send-orbit::after {
    content:""; position:absolute; inset:0; border-radius:50%;
    border: 2px solid transparent; border-top-color: var(--accent);
    border-right-color: rgba(45,212,160,.25);
  }
  .send-orbit::after { inset: 12px; border-top-color: rgba(45,212,160,.55); animation: spin 1.6s linear infinite reverse; }
  .send-orbit.sending::before { animation: spin 1.1s linear infinite; }
  .send-orbit.ok::before { animation: none; border-color: rgba(45,212,160,.35); }
  .send-orbit.ok::after { animation: none; border-color: rgba(45,212,160,.2); }
  .send-orbit .logo { --logo-size: 64px; }
  .send-orbit.sending .logo-coin { animation: throb 0.9s ease-in-out infinite; }
  .send-orbit.ok .logo-coin {
    animation: sendPop .55s cubic-bezier(.2,1.4,.4,1) both;
    box-shadow: 0 0 28px rgba(45,212,160,.55), 0 0 0 1px rgba(45,212,160,.4);
  }
  .send-check {
    position:absolute; inset:0; display:none; align-items:center; justify-content:center;
    pointer-events:none;
  }
  .send-orbit.ok .send-check { display:flex; }
  .send-check svg {
    width: 34px; height: 34px; filter: drop-shadow(0 0 8px rgba(45,212,160,.6));
    animation: sendPop .45s .15s cubic-bezier(.2,1.4,.4,1) both;
  }
  .send-title { margin: 0 0 4px; font-size: 18px; letter-spacing: -0.02em; }
  .send-sub { color: var(--muted); font-size: 12.5px; margin: 0 0 12px; line-height: 1.4; }
  .send-txid {
    width: 100%; min-height: 72px; resize: vertical; margin: 0 0 10px;
    font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px;
    line-height: 1.4; white-space: pre-wrap; word-break: break-all;
    box-sizing: border-box; user-select: text; -webkit-user-select: text;
  }
  .send-actions { display:flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
  .send-burst {
    position:absolute; inset:-8px; border-radius:50%; pointer-events:none; opacity:0;
  }
  .send-orbit.ok .send-burst {
    animation: sendBurst .7s ease-out both;
    background: radial-gradient(circle, rgba(45,212,160,.35), transparent 65%);
  }
  @keyframes sendPop {
    0% { transform: scale(.6); opacity: .2; }
    100% { transform: scale(1); opacity: 1; }
  }
  @keyframes sendBurst {
    0% { transform: scale(.4); opacity: .8; }
    100% { transform: scale(1.6); opacity: 0; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="brand">
    <div class="logo" aria-hidden="true">
      <span class="logo-ring"></span>
      <span class="logo-ring2"></span>
      <div class="logo-coin">
        <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
        </svg>
      </div>
    </div>
    <div class="brand-text">
      <h1>MHCOIN</h1>
      <span class="tag">Core Desktop</span>
    </div>
  </div>
  <div class="sub status" id="topStatus">MHCOIN Core · choosing network…</div>
  <div id="flash" class="msg"></div>

  <section id="welcome" class="card busy" aria-busy="true">
    <h2>Welcome</h2>
    <p class="sub" id="welcomeNet">Create or open an encrypted wallet for the selected network.</p>
    <div class="welcome-load" id="welcomeLoad">
      <div class="sync-head">
        <p class="welcome-load-title" id="welcomeLoadTitle">Loading wallets…</p>
        <span class="sync-pct" id="welcomeLoadPct">…</span>
      </div>
      <p class="welcome-load-meta" id="welcomeLoadMeta">Reading encrypted wallet list · buttons unlock when ready</p>
      <div class="sync-bar indeterminate" id="welcomeLoadBar"><i id="welcomeLoadFill" style="width:36%"></i></div>
    </div>
    <label>Network</label>
    <select id="welcomeNetSel" disabled>
      <option value="mainnet">Mainnet (real MHC)</option>
      <option value="localnet">Localnet (RC / solo test)</option>
    </select>
    <p class="sub" id="welcomeHint"></p>
    <div id="welcomeWallets"></div>
    <div class="row">
      <button class="primary" id="btnCreate" disabled>Create New Wallet</button>
      <button id="btnOpen" disabled>Open Existing Wallet</button>
      <button id="btnSupportWelcome">Support</button>
    </div>
  </section>

  <section id="app" class="hidden">
    <div class="tabs" id="tabs"></div>
    <div id="panel" class="card"></div>
  </section>
</div>

<div id="modalBackdrop" class="modal-backdrop hidden" role="dialog" aria-modal="true">
  <div class="modal">
    <div class="modal-head">
      <div class="logo" aria-hidden="true">
        <span class="logo-ring"></span>
        <span class="logo-ring2"></span>
        <div class="logo-coin">
          <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
          </svg>
        </div>
      </div>
      <h3 id="modalTitle">MHCOIN</h3>
    </div>
    <p class="modal-msg" id="modalMsg"></p>
    <textarea id="modalCopy" class="modal-copy mono hidden" readonly spellcheck="false"></textarea>
    <input id="modalInput" class="mono hidden" type="password" autocomplete="off" spellcheck="false"/>
    <div class="modal-actions left hidden" id="modalTools">
      <button type="button" id="modalCopyBtn">Copy</button>
      <button type="button" id="modalSaveBtn">Save to computer</button>
    </div>
    <div class="modal-actions">
      <button type="button" id="modalCancel">Cancel</button>
      <button type="button" class="primary" id="modalOk">OK</button>
    </div>
  </div>
</div>

<div id="bootOverlay" class="boot-overlay hidden" aria-live="polite">
  <div class="boot-card">
    <div class="logo" aria-hidden="true">
      <span class="logo-ring"></span>
      <span class="logo-ring2"></span>
      <div class="logo-coin">
        <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
          <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
        </svg>
      </div>
    </div>
    <div class="boot-spinner" aria-hidden="true"></div>
    <h2>Starting MHCOIN Core</h2>
    <p class="boot-sub" id="bootMsg">Opening wallet · connecting to network…</p>
    <div class="sync-box" id="bootSyncBox">
      <div class="sync-head">
        <p class="sync-title" id="bootSyncTitle">Preparing…</p>
        <span class="sync-pct" id="bootSyncPct">…</span>
      </div>
      <p class="sync-meta" id="bootSyncMeta">Like Bitcoin Core: blocks download from peers until you catch up.</p>
      <div class="sync-bar indeterminate" id="bootSyncBar"><i id="bootSyncFill"></i></div>
    </div>
  </div>
</div>

<div id="sendOverlay" class="send-overlay hidden" role="dialog" aria-modal="true" aria-live="polite">
  <div class="send-card">
    <div id="sendOrbit" class="send-orbit sending">
      <span class="send-burst" aria-hidden="true"></span>
      <div class="logo" aria-hidden="true">
        <span class="logo-ring"></span>
        <span class="logo-ring2"></span>
        <div class="logo-coin">
          <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
          </svg>
        </div>
      </div>
      <div class="send-check" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M5 13l4 4L19 7" stroke="#5dffc0" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
    </div>
    <h2 class="send-title" id="sendTitle">Sending…</h2>
    <p class="send-sub" id="sendSub">Broadcasting to mempool</p>
    <textarea id="sendTxid" class="send-txid mono hidden" readonly spellcheck="false"></textarea>
    <div class="send-actions hidden" id="sendActions">
      <button type="button" class="primary" id="sendCopyBtn">Copy TXID</button>
      <button type="button" id="sendDoneBtn">Done</button>
    </div>
  </div>
</div>

<div id="supportBackdrop" class="modal-backdrop hidden" role="dialog" aria-modal="true">
  <div class="modal support-modal">
    <div class="modal-head">
      <div class="logo" aria-hidden="true">
        <span class="logo-ring"></span>
        <span class="logo-ring2"></span>
        <div class="logo-coin">
          <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
          </svg>
        </div>
      </div>
      <h3>Contact Support</h3>
    </div>
    <p class="modal-msg">Fill the form — we email <b>support@imeigsx.com</b>. Reply-To will be your address.</p>
    <form id="supportForm" class="support-form" autocomplete="on">
      <label for="supName">Your name</label>
      <input id="supName" name="name" type="text" maxlength="120" required placeholder="Name"/>
      <label for="supEmail">Your email</label>
      <input id="supEmail" name="email" type="email" maxlength="200" required placeholder="you@example.com"/>
      <label for="supSubject">Subject</label>
      <input id="supSubject" name="subject" type="text" maxlength="180" required placeholder="Brief summary"/>
      <label for="supMessage">Message</label>
      <textarea id="supMessage" name="message" maxlength="8000" required placeholder="Describe the issue. Include steps if possible."></textarea>
      <input class="support-hp" id="supHp" name="hp" type="text" tabindex="-1" autocomplete="off"/>
      <p class="support-meta" id="supMetaHint">App details (network / height / peers / address) are attached automatically.</p>
      <p class="support-status" id="supStatus"></p>
      <div class="modal-actions" style="margin-top:10px">
        <button type="button" id="supCancel">Cancel</button>
        <button type="submit" class="primary" id="supSend">Send email</button>
      </div>
    </form>
  </div>
</div>

<script>
const tabs = ["Overview","History","Receive","Send","Mining","Network","Settings"];
let active = "Overview";
let unlocked = false;

function $(id){ return document.getElementById(id); }
function flash(text, ok=true){
  const el = $("flash");
  el.className = "msg " + (ok ? "ok" : "err");
  el.textContent = text;
  if (ok) setTimeout(()=>{ el.className="msg"; el.textContent=""; }, 4000);
}
async function api(path, body){
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(body)
  };
  const r = await fetch("/api/"+path, opts);
  const j = await r.json();
  if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}

/** In-app dialogs with MHCOIN logo — never use window.prompt/alert (Python icon). */
function showModal({ title, message, mode, okLabel, cancelLabel, placeholder, copyText, saveName }){
  return new Promise((resolve) => {
    const bd = $("modalBackdrop");
    const input = $("modalInput");
    const cancelBtn = $("modalCancel");
    const copyArea = $("modalCopy");
    const tools = $("modalTools");
    $("modalTitle").textContent = title || "MHCOIN";
    $("modalMsg").textContent = message || "";
    $("modalOk").textContent = okLabel || "OK";
    cancelBtn.textContent = cancelLabel || "Cancel";
    const needInput = mode === "password" || mode === "text";
    const needCancel = mode !== "alert" && mode !== "copy";
    const needCopy = mode === "copy" && !!copyText;
    input.classList.toggle("hidden", !needInput);
    cancelBtn.classList.toggle("hidden", !needCancel);
    copyArea.classList.toggle("hidden", !needCopy);
    tools.classList.toggle("hidden", !needCopy);
    if (needCopy) {
      copyArea.value = copyText;
      copyArea.onclick = () => { copyArea.focus(); copyArea.select(); };
    } else {
      copyArea.value = "";
      copyArea.onclick = null;
    }
    if (needInput) {
      input.type = mode === "password" ? "password" : "text";
      input.value = "";
      input.placeholder = placeholder || "";
    }
    bd.classList.remove("hidden");
    const finish = (val) => {
      bd.classList.add("hidden");
      $("modalOk").onclick = null;
      cancelBtn.onclick = null;
      $("modalCopyBtn").onclick = null;
      $("modalSaveBtn").onclick = null;
      input.onkeydown = null;
      bd.onkeydown = null;
      copyArea.onclick = null;
      resolve(val);
    };
    const copyAll = async () => {
      const text = copyArea.value || "";
      try {
        await navigator.clipboard.writeText(text);
        flash("Copied");
      } catch(e) {
        copyArea.focus(); copyArea.select();
        try { document.execCommand("copy"); flash("Copied"); }
        catch(e2){ flash("Select text and press Cmd/Ctrl+C", false); }
      }
    };
    $("modalCopyBtn").onclick = () => { copyAll(); };
    $("modalSaveBtn").onclick = async () => {
      const name = saveName || ("MHCOIN-wallet-details-" + Date.now() + ".txt");
      const r = await downloadTextFile(name, copyArea.value || "", "text/plain;charset=utf-8");
      if (r && r.cancelled) flash("Save cancelled", false);
      else if (r && r.path) flash("Saved: " + r.path);
      else if (r && r.ok) flash("Saved: " + name);
      else flash("Save failed", false);
    };
    $("modalOk").onclick = () => finish(needInput ? (input.value || "") : true);
    cancelBtn.onclick = () => finish(needInput ? "" : false);
    input.onkeydown = (e) => {
      if (e.key === "Enter") { e.preventDefault(); finish(input.value || ""); }
      if (e.key === "Escape") { e.preventDefault(); finish(""); }
    };
    bd.onkeydown = (e) => {
      if (e.key === "Escape") { e.preventDefault(); finish(needInput ? "" : (needCopy ? true : false)); }
    };
    setTimeout(() => {
      if (needCopy) { copyArea.focus(); copyArea.select(); }
      else if (needInput) input.focus();
      else $("modalOk").focus();
    }, 30);
  });
}
async function ask(msg){
  return await showModal({
    title: "MHCOIN",
    message: msg,
    mode: "password",
    okLabel: "Continue",
    cancelLabel: "Cancel",
  });
}
async function alertBox(msg){
  await showModal({
    title: "MHCOIN",
    message: msg,
    mode: "alert",
    okLabel: "OK",
  });
}
async function showMineTerminalHelp(address, network){
  const net = network || "mainnet";
  const addr = (address || "").trim() || "mhc1YOUR_ADDRESS";
  const text =
`MHCOIN — mine from Terminal (no Desktop mining)

IMPORTANT
• Stop mining in this app first (or Quit) — same data folder.
• Rewards go to the address below.
• Stop miner anytime with Ctrl+C.
• Mining does NOT need the LAN sync URL. That URL is only to download code on your home Wi‑Fi.

Your reward address:
${addr}

Network: ${net}
Data folder: ~/.mhcoin/${net}

────────────────────────────────
1) Get MHCOIN code (PUBLIC — anyone)
────────────────────────────────
git clone https://github.com/maxut88/MHCOIN.git
cd MHCOIN
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
export PYTHONPATH="$PWD"           # Windows CMD: set PYTHONPATH=%CD%

# Optional LAN-only sync (YOUR Wi‑Fi / office only — NOT public internet):
# curl -fsSL http://192.168.0.221:8765/private_rc/MHCOIN-desktop-mainnet-sync.tar.gz | tar -xz

────────────────────────────────
2) Start miner — macOS / Linux
────────────────────────────────
cd ~/MHCOIN   # or ~/src/MHCOIN — wherever you cloned
chmod +x packaging/mine_mainnet.sh
./packaging/mine_mainnet.sh ${addr}

# or one-liner:
python3 -m mhcoin.cli mining start --network ${net} --address ${addr}

────────────────────────────────
3) Start miner — Windows (CMD)
────────────────────────────────
cd %USERPROFILE%\\MHCOIN
packaging\\mine_mainnet.bat ${addr}

# or:
python -m mhcoin.cli mining start --network ${net} --address ${addr}

────────────────────────────────
4) Start miner — Windows (PowerShell)
────────────────────────────────
cd ~\\MHCOIN
.\\packaging\\mine_mainnet.ps1 ${addr}

────────────────────────────────
Notes
────────────────────────────────
• Public users: use GitHub clone (above). No API server required to mine.
• 192.168.0.221 = private sync for your LAN builds only.
• Blockchain peers use P2P (seeds like your node :8333), not the sync HTTP port.
• Solo mining today — not a stratum pool / ASIC pool yet.
`;
  const copyArea = $("modalCopy");
  copyArea.classList.add("tall");
  try {
    await showModal({
      title: "Terminal mining",
      message: "Copy commands → paste into Terminal. Public = GitHub. LAN URL is only for your local sync.",
      mode: "copy",
      okLabel: "Done",
      copyText: text,
      saveName: "MHCOIN-terminal-mining.txt",
    });
  } finally {
    copyArea.classList.remove("tall");
  }
}
async function confirmBox(msg){
  return !!(await showModal({
    title: "MHCOIN",
    message: msg,
    mode: "confirm",
    okLabel: "Continue",
    cancelLabel: "Cancel",
  }));
}
async function txSentBox(txid){
  await showSendSuccess(txid);
}
async function showSendSuccess(txid){
  const ov = $("sendOverlay");
  const orbit = $("sendOrbit");
  const title = $("sendTitle");
  const sub = $("sendSub");
  const ta = $("sendTxid");
  const actions = $("sendActions");
  if (!ov || !orbit) {
    await showModal({
      title: "Transaction sent",
      message: "TXID: " + (txid || ""),
      mode: "alert",
      okLabel: "OK",
    });
    return;
  }
  orbit.className = "send-orbit sending";
  title.textContent = "Sending…";
  sub.textContent = "Broadcasting to mempool";
  ta.value = "";
  ta.classList.add("hidden");
  actions.classList.add("hidden");
  ov.classList.remove("hidden");
  await new Promise(r => setTimeout(r, 700));
  orbit.className = "send-orbit ok";
  title.textContent = "Transaction sent";
  sub.textContent = "In mempool · waits for the next mined block";
  ta.value = txid || "";
  ta.classList.remove("hidden");
  actions.classList.remove("hidden");
  setTimeout(() => { try { ta.focus(); ta.select(); } catch(e){} }, 40);
  await new Promise((resolve) => {
    const finish = () => {
      $("sendCopyBtn").onclick = null;
      $("sendDoneBtn").onclick = null;
      ov.classList.add("hidden");
      resolve();
    };
    $("sendCopyBtn").onclick = async () => {
      try { await navigator.clipboard.writeText(txid || ""); flash("TXID copied"); }
      catch(e){
        ta.focus(); ta.select();
        try { document.execCommand("copy"); flash("TXID copied"); }
        catch(e2){ flash("Select TXID and copy", false); }
      }
    };
    $("sendDoneBtn").onclick = () => finish();
  });
}
async function showSupportHelp(){
  const bd = $("supportBackdrop");
  const form = $("supportForm");
  const status = $("supStatus");
  const sendBtn = $("supSend");
  status.className = "support-status";
  status.textContent = "";
  $("supHp").value = "";
  // Prefill meta hint from live status when possible
  let meta = { app: "MHCOIN Core Desktop", network: "", height: "", peers: "", address: "" };
  try {
    const s = await api("status");
    meta.network = String(s.network || "");
    meta.height = String(s.height ?? "");
    meta.peers = String(s.peers ?? "");
    meta.address = String(s.address || "");
    $("supMetaHint").textContent =
      "Attached: " + (meta.network || "?") +
      " · height " + (meta.height || "?") +
      " · peers " + (meta.peers || "?") +
      (meta.address ? (" · " + meta.address.slice(0, 18) + "…") : "");
  } catch(e) {
    $("supMetaHint").textContent = "App details will be attached when available.";
  }
  bd.classList.remove("hidden");
  setTimeout(() => { try { $("supName").focus(); } catch(e){} }, 30);

  return await new Promise((resolve) => {
    const close = () => {
      bd.classList.add("hidden");
      form.onsubmit = null;
      $("supCancel").onclick = null;
      resolve(true);
    };
    $("supCancel").onclick = () => close();
    bd.onclick = (ev) => { if (ev.target === bd) close(); };
    form.onsubmit = async (ev) => {
      ev.preventDefault();
      status.className = "support-status";
      status.textContent = "Sending…";
      sendBtn.disabled = true;
      const payload = {
        name: ($("supName").value || "").trim(),
        email: ($("supEmail").value || "").trim(),
        subject: ($("supSubject").value || "").trim(),
        message: ($("supMessage").value || "").trim(),
        hp: ($("supHp").value || "").trim(),
        meta,
      };
      try {
        const r = await fetch("https://imeigsx.com/mhcoin-support.php", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        let j = {};
        try { j = await r.json(); } catch(e) {}
        if (!r.ok || !j.ok) {
          throw new Error((j && j.error) || ("HTTP " + r.status));
        }
        status.className = "support-status ok";
        status.textContent = "Sent to support@imeigsx.com — check your inbox for replies.";
        flash("Support email sent");
        setTimeout(close, 900);
      } catch(e) {
        status.className = "support-status err";
        status.textContent = "Send failed: " + (e.message || e);
        flash(e.message || String(e), false);
      } finally {
        sendBtn.disabled = false;
      }
    };
  });
}
async function walletDetailsBox(address, password, network){
  const stamp = new Date().toISOString();
  const text =
    "MHCOIN wallet details\n" +
    "=====================\n" +
    "Created: " + stamp + "\n" +
    "Network: " + (network || "mainnet") + "\n" +
    "Address: " + address + "\n" +
    "Password: " + password + "\n" +
    "\n" +
    "KEEP THIS FILE PRIVATE.\n" +
    "Also download the encrypted wallet.json backup (next step).\n" +
    "Password is required to unlock — it is not stored inside wallet.json.\n";
  await showModal({
    title: "Save your wallet",
    message: "Select / copy / save these details before continuing.\nPassword is shown once — store it safely.",
    mode: "copy",
    copyText: text,
    saveName: "MHCOIN-" + (network || "mainnet") + "-wallet-details.txt",
    okLabel: "Continue",
  });
}

function shortTx(txid){
  if (!txid) return "";
  if (txid.length <= 28) return txid;
  return txid.slice(0,12) + "…" + txid.slice(-10);
}

async function downloadTextFile(filename, text, mime){
  // pywebview ignores <a download> — use native Save As via Python bridge.
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_text_file){
      const r = await window.pywebview.api.save_text_file(filename || "MHCOIN-save.txt", text || "");
      if (r && r.cancelled) return {ok:false, cancelled:true};
      if (r && r.ok) return {ok:true, path:r.path || null};
      return {ok:false, error:(r && r.error) || "save failed"};
    }
  } catch(e){}
  // Browser fallback
  try {
    const blob = new Blob([text], {type: mime || "application/octet-stream;charset=utf-8"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "MHCOIN-save.txt";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 500);
    return {ok:true, path:null};
  } catch(e){
    return {ok:false, error:String(e)};
  }
}

async function doWalletBackup(){
  const j = await api("wallet/backup", {});
  const dl = await downloadTextFile(j.filename, j.content);
  let line1;
  if (dl && dl.path) line1 = "1) Saved to:\n" + dl.path;
  else if (dl && dl.cancelled) line1 = "1) Save cancelled — choose a location next time (Downloads dialog).";
  else if (dl && dl.ok) line1 = "1) Download started: " + j.filename;
  else line1 = "1) Could not open Save dialog — use the copy in backups/ below.\n" + ((dl && dl.error) || "");
  await alertBox(
    "Wallet backup ready.\n\n" +
    line1 + "\n\n" +
    "2) Also saved on disk:\n" + j.saved_path + "\n\n" +
    "3) Keep your wallet PASSWORD separately — it is NOT in the file.\n\n" +
    "Restore later: copy the .json back as wallet.json in your data folder, then unlock with the same password."
  );
  return j;
}

function activityCard(t, i, idPrefix){
  const type = t.type || ((t.kind||"").toLowerCase().includes("mining") ? "mined" :
    (t.kind==="Sent" ? "sent" : (t.kind==="Received"||t.kind==="Receive" ? "received" : "other")));
  const title = t.title || (type==="mined" ? "Block found" : (t.kind || "Activity"));
  const sign = t.sign || (String(t.amount||"").startsWith("-") ? "-" : "+");
  const amtRaw = t.amount_mhc || String(t.amount||"").replace(/^[+-]/,"");
  const amtClass = sign === "-" ? "out" : "in";
  const amtText = (sign === "-" ? "−" : "+") + amtRaw + " MHC";
  const height = (t.height!=null && t.height!=="") ? ("Block #" + t.height) : "Mempool";
  const txid = t.txid || "";
  const txShort = t.txid_short || shortTx(txid);
  const tid = (idPrefix||"tx") + i;
  return `<div class="act">
    <div class="act-top">
      <div>
        <p class="act-title">${title}</p>
        <p class="act-meta">${height}</p>
      </div>
      <div class="act-amt ${amtClass}">${amtText}</div>
    </div>
    ${txid ? `<div class="act-txid">
      <div class="mono" id="${tid}" title="${txid}" style="user-select:text">${txShort}</div>
      <button type="button" class="linkish copyTx" data-txid="${txid}">Copy</button>
    </div>` : ""}
  </div>`;
}

function esc(s){
  return String(s==null?"":s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;");
}

function shortAddr(a){
  const s = String(a||"");
  if (s.length <= 22) return s;
  return s.slice(0, 10) + "…" + s.slice(-8);
}
function prettyFrom(list, type){
  if (!list || !list.length) return "—";
  return list.map(a => {
    const raw = String(a||"");
    if (/^coinbase$/i.test(raw)) return "Block Reward";
    return raw;
  }).join(", ");
}
function txTypeOf(t){
  return t.type || ((t.kind||"").toLowerCase().includes("mining") ? "mined" :
    ((t.kind||"").startsWith("Sent") ? "sent" : ((t.kind||"").startsWith("Received") ? "received" : "other")));
}
function colorizeMineLog(lines){
  const arr = Array.isArray(lines) ? lines : String(lines||"").split("\n");
  if (!arr.length || (arr.length===1 && !arr[0])) {
    return `<span class="ml-dim">Idle — Start mining to see live log.</span>`;
  }
  return arr.map(raw => {
    const line = String(raw ?? "");
    const m = line.match(/^\[(\d{2}:\d{2}:\d{2})\]\s?(.*)$/);
    const ts = m ? m[1] : "";
    const body = m ? m[2] : line;
    const prefix = ts ? `<span class="ml-ts">[${esc(ts)}]</span> ` : "";
    let inner;
    if (/BLOCK FOUND|▸/.test(body)) {
      inner = `<span class="ml-green">${esc(body)}</span>`;
    } else if (/^hash\b/i.test(body.trim()) || /\bhash\s+[0-9a-f]{20,}/i.test(body)) {
      inner = body.replace(/(hash\s+)([0-9a-f]+)/ig, (_, a, h) =>
        `${esc(a)}<span class="ml-yellow">${esc(h)}</span>`);
      if (inner === body) inner = `<span class="ml-yellow">${esc(body)}</span>`;
    } else if (/^reward\b/i.test(body.trim())) {
      inner = body.replace(/(reward\s+)([0-9.]+ MHC)/ig, (_, a, r) =>
        `${esc(a)}<span class="ml-green">${esc(r)}</span>`);
      if (inner === body) inner = `<span class="ml-green">${esc(body)}</span>`;
    } else if (/ERROR|WARN/i.test(body)) {
      inner = `<span class="ml-red">${esc(body)}</span>`;
    } else if (/MHCOIN Solo Miner|^═|^╔|^║|^╚/.test(body)) {
      inner = `<span class="ml-cyan ml-bold">${esc(body)}</span>`;
    } else if (/^session\b/i.test(body.trim()) || /Mining stopped|■/.test(body)) {
      inner = `<span class="ml-mag">${esc(body)}</span>`;
    } else if (/^·|nonce|kH\/s|H\/s|template|searching|stale|──|─{3,}/i.test(body)) {
      // progress / separators like terminal dim+cyan height
      let t = esc(body);
      t = t.replace(/(height\s+)(\d+)/g, `$1<span class="ml-cyan">$2</span>`);
      t = t.replace(/(#\d+)/g, `<span class="ml-cyan">$1</span>`);
      inner = `<span class="ml-dim">${t}</span>`;
    } else if (/^reward\s+mhc1|^data\s|^tip\b/i.test(body.trim())) {
      inner = `<span class="ml-dim">${esc(body)}</span>`;
    } else {
      inner = esc(body);
    }
    return prefix + inner;
  }).join("\n");
}
function setMineLogEl(el, lines){
  if (!el) return;
  const html = colorizeMineLog(lines);
  if (el.dataset.fp === html) return;
  const nearBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 24);
  el.innerHTML = html;
  el.dataset.fp = html;
  if (nearBottom) el.scrollTop = el.scrollHeight;
}
async function fillTxList(boxId, wantType, emptyMsg){
  const box = $(boxId);
  if (!box) return;
  const hadContent = !!box.dataset.fp;
  try {
    if (!hadContent) {
      box.innerHTML = "<p class='sub'>Loading wallet history in background…</p>";
    }
    const j = await api("history");
    if (j.building) {
      box.dataset.building = "1";
      if (!hadContent) {
        box.innerHTML = "<p class='sub'>Scanning blocks for your transactions… UI stays usable.</p>";
      }
      // Poll until build finishes (non-blocking API).
      setTimeout(() => fillTxList(boxId, wantType, emptyMsg), 1200);
    } else {
      box.dataset.building = "0";
    }
    const list = (j.txs || []).filter(t => txTypeOf(t) === wantType);
    const fp = list.map(t => (t.txid||"") + ":" + (t.kind||"") + ":" + (t.amount_mhc||"")).join("|")
      + (j.building ? ":b" : ":d");
    if (box.dataset.fp === fp) return;
    box.dataset.fp = fp;
    if (!list.length) {
      box.innerHTML = "<p class='sub'>"+(j.building ? "Still scanning…" : emptyMsg)+"</p>";
      return;
    }
    const note = j.building
      ? "<p class='sub'>Partial list — full scan still running…</p>"
      : (j.partial ? "<p class='sub'>Cached list (stop node to rescan full chain).</p>" : "");
    box.innerHTML = note + list.map((t,i) => historyCard(t,i)).join("");
    bindCopyTxButtons(box);
  } catch(e) {
    if (!hadContent) box.innerHTML = "<p class='sub'>Failed to load: "+(e.message||e)+"</p>";
  }
}
function syncPctLabel(syncing, pct){
  if (!syncing) return "100%";
  if (pct != null && !Number.isNaN(pct) && pct > 0) return pct.toFixed(1) + "%";
  return "…";
}
function syncProgressHtml(s, idPrefix){
  const syncing = !!s.syncing;
  const pct = (s.sync_percent != null) ? Number(s.sync_percent) : null;
  const cur = s.sync_progress != null ? s.sync_progress : s.height;
  const tgt = s.sync_target || 0;
  const title = syncing ? "Synchronizing with network…" : (s.sync || "Ready");
  const meta = syncing && tgt
    ? (`Block ${cur} of ${tgt}` +
       (s.sync_pending ? ` · ${s.sync_pending} blocks downloading` : "") +
       ` · peers ${s.peers||0}`)
    : ((s.sync || "") + (s.peers!=null ? ` · peers ${s.peers}` : ""));
  const barCls = "sync-bar" + (syncing && !(pct > 0) ? " indeterminate" : "");
  const width = syncing ? Math.max(2, Math.min(99, pct || 8)) : 100;
  const pctTxt = syncPctLabel(syncing, pct);
  return `<div class="sync-box ${syncing?"":"done"}" id="${idPrefix}SyncBox">
    <div class="sync-head">
      <p class="sync-title" id="${idPrefix}SyncTitle">${esc(title)}</p>
      <span class="sync-pct" id="${idPrefix}SyncPct">${esc(pctTxt)}</span>
    </div>
    <p class="sync-meta" id="${idPrefix}SyncMeta">${esc(meta)}</p>
    <div class="${barCls}" id="${idPrefix}SyncBar"><i id="${idPrefix}SyncFill" style="width:${width}%"></i></div>
  </div>`;
}
function patchSyncBox(prefix, s){
  const box = $(prefix + "SyncBox");
  if (!box) return;
  const syncing = !!s.syncing;
  const pct = (s.sync_percent != null) ? Number(s.sync_percent) : null;
  const cur = s.sync_progress != null ? s.sync_progress : s.height;
  const tgt = s.sync_target || 0;
  box.classList.toggle("done", !syncing);
  const title = $(prefix + "SyncTitle");
  const meta = $(prefix + "SyncMeta");
  const pctEl = $(prefix + "SyncPct");
  const bar = $(prefix + "SyncBar");
  const fill = $(prefix + "SyncFill");
  if (title) title.textContent = syncing ? "Synchronizing with network…" : (s.sync || "Ready");
  if (pctEl) pctEl.textContent = syncPctLabel(syncing, pct);
  if (meta) {
    meta.textContent = syncing && tgt
      ? (`Block ${cur} of ${tgt}` +
         (s.sync_pending ? ` · ${s.sync_pending} blocks downloading` : "") +
         ` · peers ${s.peers||0}`)
      : ((s.sync || "") + (s.peers!=null ? ` · peers ${s.peers}` : ""));
  }
  if (bar) bar.classList.toggle("indeterminate", syncing && !(pct > 0));
  if (fill) fill.style.width = (syncing ? Math.max(2, Math.min(99, pct || 8)) : 100) + "%";
}
function showBoot(msg){
  const ov = $("bootOverlay");
  if (!ov) return;
  ov.classList.remove("hidden");
  if (msg && $("bootMsg")) $("bootMsg").textContent = msg;
}
function hideBoot(){
  const ov = $("bootOverlay");
  if (ov) ov.classList.add("hidden");
}
async function bootAndEnter(){
  showBoot("Unlocking wallet · starting node…");
  unlocked = true;
  $("welcome").classList.add("hidden");
  $("app").classList.remove("hidden");
  const tb = $("tabs");
  tb.innerHTML = "";
  tabs.forEach(name => {
    const b = document.createElement("button");
    b.className = "tab" + (name===active?" active":"");
    b.textContent = name;
    b.onclick = () => { active = name; [...tb.children].forEach(x=>x.classList.remove("active")); b.classList.add("active"); render(); };
    tb.appendChild(b);
  });
  if (window._mhStatusTimer) clearInterval(window._mhStatusTimer);
  window._mhStatusTimer = setInterval(() => {
    if (!unlocked) return;
    refreshStatus();
  }, 1500);
  // Kick history build in background (non-blocking).
  api("history").catch(()=>{});
  try {
    await api("node/start", {});
    if ($("bootMsg")) $("bootMsg").textContent = "Connected · checking chain sync…";
  } catch(e) {
    if ($("bootMsg")) $("bootMsg").textContent = "Node start: " + (e.message||e);
  }
  // Show splash briefly with live sync numbers (Bitcoin Core style).
  const t0 = Date.now();
  for (let i = 0; i < 12; i++) {
    try {
      const s = await api("status");
      patchSyncBox("boot", s);
      if ($("bootSyncTitle")) {
        $("bootSyncTitle").textContent = s.syncing
          ? "Synchronizing with network…"
          : (s.sync || "Almost ready…");
      }
      $("topStatus").textContent = s.network + " · height " + s.height + " · peers " + s.peers +
        (s.syncing ? " · syncing" : "") + (s.mining ? " · mining" : "");
      // Leave splash once we have status and either synced or waited ~2.5s.
      if (!s.syncing && Date.now() - t0 > 900) break;
      if (Date.now() - t0 > 3500) break;
    } catch(e) {}
    await new Promise(r => setTimeout(r, 400));
  }
  hideBoot();
  render();
  flash("Wallet ready");
}
function historyCard(t, i){
  const type = t.type || ((t.kind||"").toLowerCase().includes("mining") ? "mined" :
    ((t.kind||"").startsWith("Sent") ? "sent" : ((t.kind||"").startsWith("Received") ? "received" : "other")));
  const title = t.title || (type==="mined" ? "Block found" : (t.kind || "Activity"));
  const sign = t.sign || (type==="sent" ? "-" : "+");
  const amtRaw = t.amount_mhc || String(t.amount||"").replace(/^[+-]/,"");
  const amtClass = sign === "-" ? "out" : "in";
  const amtText = (sign === "-" ? "−" : "+") + amtRaw + " MHC";
  const height = (t.height!=null && t.height!=="") ? ("#" + t.height) : "mempool";
  const conf = (t.confirmations!=null) ? (t.confirmations + "c") : "";
  const meta = [height, conf, t.time_utc || ""].filter(Boolean).join(" · ");
  const txid = t.txid || "";
  const fromList = (t.from && t.from.length) ? t.from : [];
  const toList = (t.to && t.to.length) ? t.to : [];
  const fromPretty = prettyFrom(fromList, type);
  const toPretty = toList.length ? toList.join(", ") : "—";
  const isBlockReward = type === "mined" || /block reward/i.test(fromPretty);
  const fee = t.fee_mhc != null ? (t.fee_mhc + " MHC") : "—";
  const fromLabel = "From";
  const toLabel = "To";
  const fromShown = isBlockReward ? "Block Reward" : fromPretty;
  const copyFrom = fromList.length && !/^coinbase$/i.test(fromList[0])
    ? `<button type="button" class="linkish copyAddr" data-addr="${esc(fromList[0])}">Copy</button>` : "";
  const copyTo = toList.length
    ? `<button type="button" class="linkish copyAddr" data-addr="${esc(toList[0])}">Copy</button>` : "";
  return `<div class="act act-full">
    <div class="act-top">
      <div style="min-width:0">
        <p class="act-title">${esc(title)}</p>
        <p class="act-meta">${esc(meta)}</p>
      </div>
      <div class="act-amt ${amtClass}">${amtText}</div>
    </div>
    <div class="act-grid">
      <div class="k">TXID</div>
      <div class="v" title="${esc(txid)}">${esc(txid || "—")}</div>
      <div class="a">${txid ? `<button type="button" class="linkish copyTx" data-txid="${esc(txid)}">Copy</button>` : ""}</div>
      <div class="k">${esc(fromLabel)}</div>
      <div class="v" title="${esc(fromShown)}">${esc(fromShown)}</div>
      <div class="a">${copyFrom}</div>
      <div class="k">${esc(toLabel)}</div>
      <div class="v" title="${esc(toPretty)}">${esc(toPretty)}</div>
      <div class="a">${copyTo}</div>
      <div class="k">Fee</div>
      <div class="v">${esc(fee)}</div>
      <div class="a"></div>
    </div>
  </div>`;
}

function bindCopyTxButtons(root){
  (root || document).querySelectorAll(".copyTx").forEach(btn => {
    btn.onclick = async () => {
      const text = btn.getAttribute("data-txid") || "";
      try { await navigator.clipboard.writeText(text); flash("TXID copied"); }
      catch(e){
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
        flash("TXID copied");
      }
    };
  });
  (root || document).querySelectorAll(".copyAddr").forEach(btn => {
    btn.onclick = async () => {
      const text = btn.getAttribute("data-addr") || "";
      try { await navigator.clipboard.writeText(text); flash("Address copied"); }
      catch(e){
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
        flash("Address copied");
      }
    };
  });
}

function applyWelcomeNet(s){
  const sel = $("welcomeNetSel");
  if (sel && s.network) sel.value = s.network;
  const hint = $("welcomeHint");
  if (hint) {
    if (s.network === "mainnet") {
      hint.textContent = "Data: " + (s.data_dir||"~/.mhcoin/mainnet") +
        " · Seed: " + ((s.seeds&&s.seeds[0])||"176.38.3.168:8333");
    } else {
      hint.textContent = "LOCALNET — not the public MHCOIN chain. Data: " + (s.data_dir||"");
    }
  }
  $("topStatus").textContent = (s.network||"?") + " · " + (s.data_dir||"");
  const box = $("welcomeWallets");
  if (!box) return;
  const list = s.wallets || [];
  if (!s.wallet_exists || !list.length) {
    box.innerHTML = "";
    return;
  }
  const active = s.address || "";
  const opts = list.map((w, i) => {
    const lab = (w.label && w.label !== "default") ? w.label : ("Wallet " + (i+1));
    const short = (w.address||"").slice(0,10) + "…" + (w.address||"").slice(-8);
    const mark = w.address === active ? " · last active" : "";
    return `<option value="${w.wallet_id}">${lab}: ${short}${mark}</option>`;
  }).join("");
  box.innerHTML =
    `<label>Wallet to open</label>` +
    `<select id="welcomeWalletSel">${opts}</select>` +
    `<p class="sub">Pick the wallet, then Open. Each key can have its own password.</p>`;
  const wsel = $("welcomeWalletSel");
  if (wsel && active) {
    const hit = list.find(w => w.address === active);
    if (hit) wsel.value = hit.wallet_id;
  }
}

$("welcomeNetSel").onchange = async () => {
  if (!welcomeReady) return;
  try {
    setWelcomeBusy(true, "Switching network…", "Closing previous datadir · loading wallets");
    const net = $("welcomeNetSel").value;
    await api("network/switch", {network: net});
    await loadWelcome({showBusy: true});
    flash(net === "mainnet" ? "Switched to Mainnet" : "Switched to Localnet");
  } catch(e){
    setWelcomeBusy(false);
    flash(e.message, false);
  }
};

$("btnCreate").onclick = async () => {
  if (!welcomeReady) { flash("Still loading — wait for the progress bar", false); return; }
  try {
    const a = await ask("Choose a wallet password:");
    if (!a) return;
    const b = await ask("Confirm password:");
    if (a !== b) return flash("Passwords do not match", false);
    const j = await api("wallet/create", {password:a});
    flash("New wallet is now active (previous keys stay in wallet.json)");
    let net = "mainnet";
    try { const s = await api("status"); net = s.network || net; } catch(e){}
    await walletDetailsBox(j.address, a, net);
    try {
      await doWalletBackup();
    } catch(be){
      flash("Wallet created, but backup failed: "+be.message, false);
    }
    enterApp();
  } catch(e){ flash(e.message, false); }
};
$("btnOpen").onclick = async () => {
  if (!welcomeReady) { flash("Still loading — wait for the progress bar", false); return; }
  try {
    const p = await ask("Wallet password:");
    if (!p) return;
    const wid = ($("welcomeWalletSel") && $("welcomeWalletSel").value) || "";
    const body = wid ? {password:p, wallet_id:wid} : {password:p};
    await api("wallet/unlock", body);
    flash("Wallet unlocked");
    enterApp();
  } catch(e){ flash(e.message, false); }
};
$("btnSupportWelcome").onclick = async () => { await showSupportHelp(); };

function enterApp(){
  bootAndEnter();
}

async function refreshStatus(){
  try {
    const s = await api("status");
    $("topStatus").textContent = s.network + " · height " + s.height + " · peers " + s.peers +
      (s.syncing ? " · syncing" : "") + (s.mining ? " · mining" : "");
    if (s.chain_error) {
      flash(s.chain_error, false);
    }
    patchSyncBox("ov", s);
    patchSyncBox("net", s);
    // Mining tab: live stats
    if (active === "Mining" && $("mineHero")) {
      $("mineHero").classList.toggle("live", !!s.mining);
      if ($("mineState")) $("mineState").textContent = s.mining ? "Mining" : "Idle";
      if ($("mineDot")) $("mineDot").className = "dot" + (s.mining ? " on" : "");
      if ($("statHash")) $("statHash").textContent = s.hashrate || "—";
      if ($("statBlocks")) $("statBlocks").textContent = String(s.blocks_found ?? 0);
      if ($("statRewards")) $("statRewards").textContent = s.rewards || "0";
      const logEl = $("mineLog");
      if (logEl) setMineLogEl(logEl, s.mine_log || []);
      // Keep a live activity strip on Mining too.
      const live = $("mineLiveActs");
      if (live) {
        const rows = (s.txs||[]).filter(t => (t.type==="mined") || (t.kind||"").toLowerCase().includes("mining")).slice(0,6);
        live.innerHTML = rows.map((t,i)=>historyCard(t,i)).join("") ||
          '<p class="sub">New blocks will stream here while mining…</p>';
        bindCopyTxButtons(live);
      }
      return;
    }
    // Overview: patch balance + activity live (avoid full rebuild flicker).
    if (active === "Overview" && $("recentList")) {
      const pill = document.querySelector("#panel .pill");
      if (pill) {
        pill.innerHTML = `<span class="dot ${s.mining?'on':''}"></span>${s.network} · block ${s.height} · peers ${s.peers}`;
      }
      const bal = document.querySelector("#panel .bal");
      if (bal) bal.textContent = s.balance;
      const sess = document.querySelector("#panel .sub.sessionStats");
      if (sess) {
        sess.textContent = (s.mining||s.blocks_found)
          ? (`Session · ${s.blocks_found||0} blocks · ${s.rewards||"0"}`)
          : "";
        sess.style.display = (s.mining||s.blocks_found) ? "block" : "none";
      }
      const box = $("recentList");
      box.innerHTML = (s.txs||[]).slice(0,12).map((t,i)=>historyCard(t,i)).join("") ||
        '<p class="sub">No activity yet — mined blocks and transfers will appear here.</p>';
      bindCopyTxButtons(box);
      return;
    }
    // Network only: live peer/height refresh. Do NOT rebuild Receive/Send
    // (that reloads history and makes TX lists flicker).
    if (active === "Network") {
      render(s);
    }
  } catch(e){}
}

let welcomeReady = false;
function setWelcomeBusy(busy, title, meta){
  const card = $("welcome");
  const load = $("welcomeLoad");
  if (card) {
    card.classList.toggle("busy", !!busy);
    card.setAttribute("aria-busy", busy ? "true" : "false");
  }
  if (load) load.classList.toggle("hidden", !busy);
  if (title && $("welcomeLoadTitle")) $("welcomeLoadTitle").textContent = title;
  if (meta && $("welcomeLoadMeta")) $("welcomeLoadMeta").textContent = meta;
  const dis = !!busy;
  ["welcomeNetSel","btnCreate","btnOpen"].forEach(id => {
    const el = $(id); if (el) el.disabled = dis;
  });
  const wsel = $("welcomeWalletSel");
  if (wsel) wsel.disabled = dis;
  welcomeReady = !busy;
}
async function loadWelcome(opts){
  const showBusy = !opts || opts.showBusy !== false;
  if (showBusy) setWelcomeBusy(true, "Loading wallets…", "Reading encrypted wallet list · buttons unlock when ready");
  const t0 = Date.now();
  let lastErr = null;
  for (let i = 0; i < 40; i++) {
    try {
      // Fast welcome endpoint — does not open chain.sqlite / UTXO.
      const s = await api("welcome");
      applyWelcomeNet(s);
      const waited = Date.now() - t0;
      setWelcomeBusy(false);
      $("topStatus").textContent = (s.network||"?") + " · ready" + (waited > 400 ? (" · " + (waited/1000).toFixed(1) + "s") : "");
      if (s.wallet_exists && s.unlocked) enterApp();
      return s;
    } catch(e) {
      lastErr = e;
      if ($("welcomeLoadMeta")) {
        $("welcomeLoadMeta").textContent = "Still loading… " + (e.message || e);
      }
      await new Promise(r => setTimeout(r, 250));
    }
  }
  setWelcomeBusy(false);
  flash((lastErr && lastErr.message) || "Failed to load wallets", false);
  return null;
}
loadWelcome();


async function render(pre){
  const s = pre || await api("status");
  const p = $("panel");
  if (active === "Overview") {
    const switcher = (s.wallets||[]).length > 1
      ? `<div class="row" style="margin:8px 0 16px">${(s.wallets||[]).map(w =>
          `<button class="tab ${w.address===s.address?'active':''}" data-wid="${w.wallet_id}">${w.address===s.address?'ACTIVE · ':''}${w.address.slice(0,14)}…</button>`
        ).join("")}</div>`
      : "";
    p.innerHTML = `
      <div class="pill"><span class="dot ${s.mining?'on':''}"></span>${s.network} · block ${s.height} · peers ${s.peers}${s.syncing?' · syncing':''}</div>
      ${syncProgressHtml(s, "ov")}
      ${s.mining?'<p class="msg ok" style="display:block">Miner is live in the background.</p>':''}
      ${s.network!=="mainnet"?'<p class="msg err" style="display:block">You are on <b>localnet</b> — not public MHCOIN Mainnet.</p>':''}
      <label style="margin-top:10px">Active wallet</label>
      <div class="mono" style="font-size:11.5px;word-break:break-all;margin:4px 0 8px">${s.address||"—"}</div>
      ${switcher}
      <label>Balance</label>
      <div class="bal">${s.balance}</div>
      ${s.balance_cached?'<p class="sub">Cached while node is running.</p>':''}
      ${s.mining||s.blocks_found?`<p class="sub sessionStats">Session · ${s.blocks_found||0} blocks · ${s.rewards||"0"}</p>`:`<p class="sub sessionStats" style="display:none"></p>`}
      <div class="row">
        <button class="sm" onclick="go('Receive')">Receive</button>
        <button class="sm" onclick="go('Send')">Send</button>
        <button class="primary sm" onclick="go('Mining')">Mining</button>
      </div>
      <h3>Activity</h3>
      <p class="sub">${(s.txs||[]).length} recent · full TX details</p>
      <div class="act-list" id="recentList" style="max-height:48vh">
        ${(s.txs||[]).slice(0,12).map((t,i)=>historyCard(t,i)).join("") || '<p class="sub">No activity yet — mined blocks and transfers will appear here.</p>'}
      </div>
      <div class="row"><button class="sm" onclick="go('History')">Full history</button></div>`;
    bindCopyTxButtons($("recentList"));
    document.querySelectorAll("[data-wid]").forEach(btn => {
      btn.onclick = async () => {
        try {
          const wid = btn.getAttribute("data-wid") || "";
          if (wid && !(s.wallets||[]).some(w => w.wallet_id===wid && w.address===s.address)) {
            const pwd = await ask("Password for this wallet:");
            if (!pwd) return;
            await api("wallet/unlock", {password: pwd, wallet_id: wid});
          }
          flash("Switched active wallet");
          go("Overview");
        } catch(e){ flash(e.message, false); }
      };
    });
  } else if (active === "History") {
    p.innerHTML = `
      <h2>History</h2>
      <p class="sub">Full TXID · From / To · time · fee</p>
      <p class="mono" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(s.address||"")}">${s.address||""}</p>
      <div id="histBox" class="act-list" style="max-height:68vh;overflow:auto"><p class="sub">Loading…</p></div>
      <div class="row"><button class="sm" id="histReload">Reload</button></div>`;
    const fill = async () => {
      const box = $("histBox");
      try {
        if (!box.dataset.fp) box.innerHTML = "<p class='sub'>Loading wallet history in background…</p>";
        const j = await api("history");
        if (j.building) setTimeout(fill, 1200);
        const list = j.txs || [];
        const fp = list.map(t => (t.txid||"")+":"+(t.kind||"")).join("|") + (j.building?":b":":d");
        if (box.dataset.fp === fp) return;
        box.dataset.fp = fp;
        if (!list.length) {
          box.innerHTML = "<p class='sub'>"+(j.building?"Scanning blocks…":"No activity for this wallet yet.")+"</p>";
          return;
        }
        const note = j.building
          ? "<p class='sub'>Partial list — full scan still running…</p>"
          : (j.partial ? "<p class='sub'>Cached history.</p>" : "");
        box.innerHTML = note + list.map((t,i) => historyCard(t,i)).join("");
        bindCopyTxButtons(box);
      } catch(e) {
        box.innerHTML = "<p class='sub'>Failed to load history: "+(e.message||e)+"</p>";
      }
    };
    $("histReload").onclick = () => { $("histBox").dataset.fp=""; fill(); };
    fill();
  } else if (active === "Receive") {
    p.innerHTML = `
      <h2>Receive MHCOIN</h2>
      <p class="sub">Share this address to receive MHC.</p>
      <textarea id="addr" rows="3" readonly class="mono">${s.address||""}</textarea>
      <div class="row"><button class="primary" id="copyBtn">Copy Address</button></div>
      <h3>Received</h3>
      <p class="sub">Incoming transfers with full TX details</p>
      <div id="recvList" class="act-list" style="max-height:42vh;overflow:auto"><p class="sub">Loading…</p></div>
      <div class="row"><button class="sm" id="recvReload">Reload</button></div>`;
    $("copyBtn").onclick = async () => {
      try { await navigator.clipboard.writeText(s.address||""); flash("Address copied"); }
      catch(e){ $("addr").select(); document.execCommand("copy"); flash("Address copied"); }
    };
    const loadRecv = () => fillTxList("recvList", "received", "No received transactions yet.");
    $("recvReload").onclick = () => loadRecv();
    loadRecv();
  } else if (active === "Send") {
    const last = s.last_txid
      ? `<p class="sub">Last TXID</p><textarea id="lastTx" rows="4" readonly class="mono">${s.last_txid}</textarea>
         <div class="row"><button id="copyTx">Copy TXID</button></div>`
      : `<p class="sub">After Send the full TXID will appear here.</p>`;
    p.innerHTML = `
      <h2>Send MHCOIN</h2>
      <p class="sub">To move MHC to your other wallet: open that wallet → Receive → copy address → switch back here and paste it.</p>
      <label>Address</label><input id="to" placeholder="mhc1..."/>
      <label>Amount (MHC)</label><input id="amt" placeholder="1.0"/>
      <label>Fee (MHC)</label><input id="fee" value="0.00001000"/>
      <div class="row"><button class="primary" id="sendBtn">Send</button></div>
      ${last}
      <h3>Sent</h3>
      <p class="sub">Outgoing transfers with full TX details</p>
      <div id="sentList" class="act-list" style="max-height:36vh;overflow:auto"><p class="sub">Loading…</p></div>
      <div class="row"><button class="sm" id="sentReload">Reload</button></div>`;
    if ($("copyTx")) {
      $("copyTx").onclick = async () => {
        try { await navigator.clipboard.writeText(s.last_txid||""); flash("TXID copied"); }
        catch(e){ $("lastTx").select(); document.execCommand("copy"); flash("TXID copied"); }
      };
    }
    $("sendBtn").onclick = async () => {
      try {
        const password = await ask("Wallet password:");
        if (!password) return;
        const j = await api("send", {to:$("to").value.trim(), amount:$("amt").value.trim(), fee:$("fee").value.trim(), password});
        await txSentBox(j.txid);
        flash("Sent OK");
        render();
      } catch(e){ flash(e.message, false); }
    };
    const loadSent = () => fillTxList("sentList", "sent", "No sent transactions yet.");
    $("sentReload").onclick = () => loadSent();
    loadSent();
  } else if (active === "Mining") {
    const localWarn = s.network !== "mainnet"
      ? `<p class="msg err" style="display:block">LOCALNET only — not real Mainnet MHC.</p>`
      : `<p class="sub">Tabs don’t stop mining. Only Stop (or Quit) does.</p>`;
    p.innerHTML = `
      <h2>Mining</h2>
      ${localWarn}
      <div id="mineHero" class="mine-hero ${s.mining?'live':''}">
        <div class="mine-inner">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div class="pill"><span id="mineDot" class="dot ${s.mining?'on':''}"></span><span id="mineState">${s.mining?'Mining':'Idle'}</span></div>
            <div class="status">${s.network} · h${s.height}</div>
          </div>
          <div class="mine-ring-wrap"><div class="mine-ring">
            <div class="logo lg" aria-hidden="true">
              <div class="logo-coin">
                <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>
                </svg>
              </div>
            </div>
          </div></div>
          <div class="bars"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
          <div class="mine-stats">
            <div class="stat"><div class="k">Hashrate</div><div class="v" id="statHash">${s.hashrate||"—"}</div></div>
            <div class="stat"><div class="k">Blocks</div><div class="v" id="statBlocks">${s.blocks_found||0}</div></div>
            <div class="stat"><div class="k">Rewards</div><div class="v" id="statRewards">${s.rewards||"0"}</div></div>
          </div>
        </div>
      </div>
      <label>Reward address</label>
      <input id="mineAddr" class="mono" value="${s.address||""}"/>
      <div class="row">
        <button class="primary" id="mineStart">Start mining</button>
        <button class="sm" id="mineStop">Stop</button>
        <button class="sm" id="mineHelp">Instructions</button>
      </div>
      <h3>Mining log</h3>
      <p class="sub">Live terminal-style output (same data as CLI miner).</p>
      <pre id="mineLog" class="mine-log"></pre>
      <p class="sub">Prefer Terminal? Open <b>Instructions</b> and paste the commands.</p>
      <h3>Live blocks</h3>
      <p class="sub">Streams as blocks are found (also on Overview → Activity).</p>
      <div class="act-list" id="mineLiveActs">
        ${(s.txs||[]).filter(t => (t.type==="mined") || (t.kind||"").toLowerCase().includes("mining")).slice(0,6).map((t,i)=>historyCard(t,i)).join("") || '<p class="sub">New blocks will stream here while mining…</p>'}
      </div>`;
    bindCopyTxButtons($("mineLiveActs"));
    setMineLogEl($("mineLog"), s.mine_log || []);
    if ($("mineLog") && (s.mine_log||[]).length) {
      $("mineLog").scrollTop = $("mineLog").scrollHeight;
    }
    $("mineStart").onclick = async () => {
      try {
        if (s.network !== "mainnet") {
          const ok = await confirmBox("You are on LOCALNET, not Mainnet.\n\nMine the local test chain anyway?");
          if (!ok) return;
        }
        await api("mine/start", {address:$("mineAddr").value.trim()});
        flash("Mining started");
        render();
      } catch(e){ flash(e.message, false); }
    };
    $("mineStop").onclick = async () => {
      try { await api("mine/stop", {}); flash("Mining stopped"); render(); }
      catch(e){ flash(e.message, false); }
    };
    $("mineHelp").onclick = async () => {
      const addr = ($("mineAddr") && $("mineAddr").value.trim()) || s.address || "";
      await showMineTerminalHelp(addr, s.network || "mainnet");
    };
  } else if (active === "Network") {
    p.innerHTML = `
      <h2>Network</h2>
      ${syncProgressHtml(s, "net")}
      <p>Network: <b>${s.network}</b></p>
      <p>Status: ${esc(s.sync||"")}</p>
      <p>Block height: ${s.height}</p>
      <p>Peers: ${s.peers}</p>
      <p>Node: ${s.node_running?"running":"stopped"} · listen :${s.listen_port||"-"}</p>
      <p class="mono">Seeds: ${(s.seeds||[]).join(", ")||"(none)"}</p>
      <p class="mono">Tip: ${s.tip||"-"}</p>
      <div class="row">
        <button class="primary" id="nodeStart">Start Node</button>
        <button id="nodeStop">Stop Node</button>
      </div>`;
    $("nodeStart").onclick = async () => { try { await api("node/start",{}); flash("Node started"); render(); } catch(e){ flash(e.message,false);} };
    $("nodeStop").onclick = async () => { try { await api("node/stop",{}); flash("Node stopped"); render(); } catch(e){ flash(e.message,false);} };
  } else if (active === "Settings") {
    const wl = (s.wallets||[]).map((w, i) =>
      `<div class="card" style="padding:12px;margin-bottom:10px">
        <div class="sub">${w.address===s.address?"ACTIVE wallet":"Wallet "+(i+1)} · ${w.label||""}</div>
        <textarea id="waddr${i}" rows="2" readonly class="mono" style="user-select:text;-webkit-user-select:text;cursor:text">${w.address}</textarea>
        <div class="row">
          <button class="primary copyW" data-addr="${w.address}">Copy</button>
          <button class="selW" data-wid="${w.wallet_id}">Use</button>
        </div>
      </div>`
    ).join("") || "<p class='sub'>No wallets</p>";
    p.innerHTML = `
      <h2>Settings</h2>
      <label>Network</label>
      <select id="setNet">
        <option value="mainnet" ${s.network==="mainnet"?"selected":""}>Mainnet</option>
        <option value="localnet" ${s.network==="localnet"?"selected":""}>Localnet</option>
      </select>
      <p class="sub">Mainnet uses ~/.mhcoin/mainnet. Localnet is for testing only.</p>
      <div class="row"><button class="primary" id="applyNet">Apply network</button></div>
      <label>Data directory</label>
      <textarea id="dataDir" rows="2" readonly class="mono" style="user-select:text;-webkit-user-select:text">${s.data_dir}</textarea>
      <div class="row"><button id="copyDir">Copy path</button></div>
      <h3>Wallets in this datadir</h3>
      <p class="sub">Select text or tap Copy. Shared chain; balance follows the active key only.</p>
      ${wl}
      <h3>Backup</h3>
      <p class="sub">Backup = encrypted <span class="mono">wallet.json</span> + your password (password is never stored in the file).</p>
      <label>Wallet file</label>
      <textarea id="walletPath" rows="2" readonly class="mono" style="user-select:text;-webkit-user-select:text">${s.wallet_path||""}</textarea>
      <div class="row">
        <button class="primary" id="btnBackup">Download wallet backup</button>
        <button id="copyWalletPath">Copy wallet path</button>
      </div>
      <p class="sub">A copy is also written to <span class="mono">…/backups/</span> inside the data directory.</p>
      <div class="row">
        <button id="ref">Refresh</button>
        <button id="newW">Create Another Wallet</button>
        <button id="lockBtn">Lock wallet</button>
        <button id="supportBtn">Support</button>
        <button id="quitBtn">Quit app</button>
      </div>`;
    const copyText = async (text) => {
      try { await navigator.clipboard.writeText(text); flash("Copied"); }
      catch(e){
        const ta = document.createElement("textarea");
        ta.value = text; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
        flash("Copied");
      }
    };
    $("copyDir").onclick = () => copyText(s.data_dir||"");
    $("dataDir").onclick = () => { $("dataDir").select(); };
    $("copyWalletPath").onclick = () => copyText(s.wallet_path||"");
    $("walletPath").onclick = () => { $("walletPath").select(); };
    $("btnBackup").onclick = async () => {
      try {
        if (!s.wallet_exists) return flash("No wallet to back up", false);
        await doWalletBackup();
        flash("Wallet backup downloaded");
      } catch(e){ flash(e.message, false); }
    };
    $("applyNet").onclick = async () => {
      try {
        const net = $("setNet").value;
        if (net === s.network) return flash("Already on "+net);
        const ok = await confirmBox("Switch to "+net+"?\n\nYou will need to unlock/create the wallet for that network.\nData stays separate: ~/.mhcoin/"+net);
        if (!ok) return;
        const j = await api("network/switch", {network: net});
        unlocked = false;
        $("app").classList.add("hidden");
        $("welcome").classList.remove("hidden");
        applyWelcomeNet(j);
        flash("Switched to "+j.network+" — unlock or create wallet");
      } catch(e){ flash(e.message, false); }
    };
    document.querySelectorAll(".copyW").forEach(btn => {
      btn.onclick = () => copyText(btn.getAttribute("data-addr")||"");
    });
    (s.wallets||[]).forEach((_, i) => {
      const el = $("waddr"+i);
      if (el) el.onclick = () => el.select();
    });
    $("ref").onclick = () => render();
    document.querySelectorAll(".selW").forEach(btn => {
      btn.onclick = async () => {
        try {
          const wid = btn.getAttribute("data-wid") || "";
          const pwd = await ask("Password for this wallet:");
          if (!pwd) return;
          await api("wallet/unlock", {password: pwd, wallet_id: wid});
          flash("Active wallet switched");
          go("Overview");
        } catch(e){ flash(e.message, false); }
      };
    });
    $("newW").onclick = async () => {
      try {
        const a = await ask("Password for NEW wallet:");
        if (!a) return;
        const b = await ask("Confirm password:");
        if (a !== b) return flash("Passwords do not match", false);
        const j = await api("wallet/create", {password:a});
        flash("New wallet active");
        let net = "mainnet";
        try { const s = await api("status"); net = s.network || net; } catch(e){}
        await walletDetailsBox(j.address, a, net);
        try { await doWalletBackup(); } catch(be){ flash("Backup failed: "+be.message, false); }
        render();
      } catch(e){ flash(e.message, false); }
    };
    $("lockBtn").onclick = async () => {
      leaveApp("Wallet locked — unlock or create a wallet");
      try {
        await api("wallet/lock", {});
      } catch(e){ flash(e.message, false); }
    };
    $("supportBtn").onclick = async () => { await showSupportHelp(); };
    $("quitBtn").onclick = async () => {
      const ok = await confirmBox("Quit MHCOIN Core completely?\n\nWindow X also quits. Use Lock wallet to return to Welcome without closing.");
      if (!ok) return;
      try { await api("shutdown", {}); } catch(e) {}
      document.body.innerHTML = "<div class='wrap'><h1>MHCOIN Core</h1><p>Stopped. You can close this window.</p></div>";
    };
  }
}
function leaveApp(msg){
  unlocked = false;
  active = "Overview";
  $("app").classList.add("hidden");
  $("welcome").classList.remove("hidden");
  $("tabs").innerHTML = "";
  if (msg) flash(msg);
  loadWelcome({showBusy: true});
}
function go(name){
  active = name;
  [...$("tabs").children].forEach(x => {
    x.classList.toggle("active", x.textContent === name);
  });
  render();
}
</script>
</body>
</html>
"""


class DesktopState:
    def __init__(self, network: str) -> None:
        self.ctrl = CoreController(network=network)
        self.lock = threading.RLock()

    def _welcome_payload(self) -> dict[str, Any]:
        """Fast Welcome payload — never opens chain.sqlite / UTXO (avoids UI freeze).

        Caller must hold self.lock.
        """
        c = self.ctrl
        params = get_network_params(c.network)
        unlocked = c._password is not None
        addr = None
        try:
            addr = c.default_address()
        except WalletError:
            pass
        height = 0
        tip = None
        try:
            from mhcoin.node.runtime import NodeRuntime

            st = NodeRuntime.read_status(c.data_dir) or {}
            height = max(0, int(st.get("height") or 0))
            tip = str(st["tip"]) if st.get("tip") else None
        except Exception:
            pass
        return {
            "ok": True,
            "ready": True,
            "network": c.network,
            "height": height,
            "tip": tip,
            "peers": 0,
            "sync": "Ready to unlock",
            "syncing": False,
            "genesis_hash": params.genesis_hash_hex,
            "seeds": default_connect_peers(c.network),
            "node_running": c._node is not None,
            "listen_port": params.default_port,
            "address": addr,
            "wallet_exists": c.wallet_exists(),
            "unlocked": unlocked,
            "data_dir": str(c.data_dir),
            "wallet_path": str(c.wallet_file_path()),
            "wallets": c.list_wallets() if c.wallet_exists() else [],
        }

    def welcome_status(self) -> dict[str, Any]:
        with self.lock:
            return self._welcome_payload()

    def status(self) -> dict[str, Any]:
        with self.lock:
            c = self.ctrl
            unlocked = c._password is not None
            # Before unlock, keep status light so Welcome buttons stay responsive.
            if not unlocked and c._node is None:
                light = self._welcome_payload()
                light["mining"] = False
                light["hashrate"] = "-"
                light["blocks_found"] = 0
                light["rewards"] = "0"
                light["mine_log"] = []
                light["txs"] = []
                light["balance"] = "0.00000000 MHC"
                light["balance_cached"] = False
                light["last_txid"] = None
                light["chain_error"] = None
                light["sync_state"] = "IDLE"
                light["sync_progress"] = light.get("height") or 0
                light["sync_target"] = 0
                light["sync_pending"] = 0
                light["sync_percent"] = None
                return light
            info = c.chain_info()
            stats = c.mining_stats
            addr = None
            try:
                addr = c.default_address()
            except WalletError:
                pass
            node_running = bool(info.get("node_running"))
            txs: list[dict] = []
            balance = "0.00000000 MHC"
            if addr:
                if not node_running:
                    try:
                        balance = c.balance_text()
                    except Exception:
                        balance = c.cached_balance_text()
                else:
                    balance = c.cached_balance_text()
                try:
                    txs = c.recent_for_ui(25)
                except Exception:
                    txs = list(getattr(c, "_recent_cache", []) or [])[:25]
            hr = stats["hashrate"]
            return {
                "ok": True,
                "network": info["network"],
                "height": info["height"],
                "tip": info["tip"],
                "peers": info["peers"],
                "sync": info["sync"],
                "syncing": bool(info.get("syncing")),
                "sync_state": info.get("sync_state"),
                "sync_progress": info.get("sync_progress"),
                "sync_target": info.get("sync_target"),
                "sync_pending": info.get("sync_pending"),
                "sync_percent": info.get("sync_percent"),
                "genesis_hash": info.get("genesis_hash"),
                "seeds": info.get("seeds") or [],
                "node_running": node_running,
                "listen_port": info.get("listen_port"),
                "balance": balance,
                "balance_cached": bool(node_running and c._balance_cache_valid),
                "address": addr,
                "wallet_exists": c.wallet_exists(),
                "unlocked": unlocked,
                "mining": stats["mining"],
                "hashrate": f"{hr:,.0f} H/s" if hr else "-",
                "blocks_found": stats["blocks_found"],
                "rewards": stats["rewards_text"],
                "mine_log": stats.get("log") or [],
                "data_dir": str(c.data_dir),
                "wallet_path": str(c.wallet_file_path()),
                "txs": txs,
                "wallets": c.list_wallets() if c.wallet_exists() else [],
                "last_txid": c.last_txid(),
                "chain_error": info.get("chain_error"),
            }


def _json(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    raw = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def make_handler(state: DesktopState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # quieter
            return

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                raw = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == "/api/welcome":
                _json(self, 200, state.welcome_status())
                return
            if path == "/api/status":
                _json(self, 200, state.status())
                return
            if path == "/api/history":
                c = state.ctrl
                try:
                    payload = c.history_for_api(limit=2000)
                    _json(self, 200, payload)
                except Exception as e:  # noqa: BLE001
                    if c._recent_cache:
                        _json(self, 200, {"ok": True, "txs": list(c._recent_cache), "count": len(c._recent_cache), "cached": True, "partial": True})
                        return
                    _json(self, 500, {"ok": False, "error": str(e)})
                return
            _json(self, 404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                _json(self, 400, {"ok": False, "error": "bad json"})
                return
            try:
                out = self._dispatch(path, body)
                _json(self, 200, out)
            except WalletError as e:
                _json(self, 400, {"ok": False, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                _json(self, 400, {"ok": False, "error": str(e)})

        def _dispatch(self, path: str, body: dict) -> dict:
            c = state.ctrl
            with state.lock:
                if path == "/api/wallet/create":
                    addr = c.create_wallet(str(body.get("password") or ""))
                    # Do not scan the full chain under the API lock — UI would freeze.
                    try:
                        c.request_history_build(full_chain=False)
                    except Exception:
                        pass
                    return {
                        "ok": True,
                        "address": addr,
                        "wallets": c.list_wallets(),
                    }
                if path == "/api/wallet/unlock":
                    if not c.wallet_exists():
                        raise WalletError("no wallet found")
                    wid = str(body.get("wallet_id") or "").strip() or None
                    addr = c.unlock(str(body.get("password") or ""), wallet_id=wid)
                    # Chain open + history happen after enterApp / node start (async).
                    try:
                        c.request_history_build(full_chain=False)
                    except Exception:
                        pass
                    return {"ok": True, "address": addr}
                if path == "/api/wallet/lock":
                    c.lock_session()
                    return {
                        "ok": True,
                        "network": c.network,
                        "data_dir": str(c.data_dir),
                        "wallet_exists": c.wallet_exists(),
                    }
                if path == "/api/wallet/list":
                    return {"ok": True, "wallets": c.list_wallets(), "active": c.default_address()}
                if path == "/api/wallet/select":
                    wid = str(body.get("wallet_id") or "")
                    addr = str(body.get("address") or "")
                    if wid:
                        selected = c.select_wallet(wid)
                    elif addr:
                        selected = c.select_wallet_by_address(addr)
                    else:
                        raise WalletError("wallet_id or address required")
                    return {"ok": True, "address": selected}
                if path == "/api/wallet/backup":
                    return c.backup_wallet()
                if path == "/api/network/switch":
                    net = str(body.get("network") or "").strip().lower()
                    if net not in ("mainnet", "localnet"):
                        raise RuntimeError("network must be mainnet or localnet")
                    out = c.switch_network(net)
                    out["ok"] = True
                    return out
                if path == "/api/send":
                    txid = c.send(
                        str(body.get("to") or ""),
                        str(body.get("amount") or ""),
                        str(body.get("password") or ""),
                        fee_mhc=str(body.get("fee") or "") or None,
                    )
                    return {"ok": True, "txid": txid}
                if path == "/api/mine/start":
                    addr = str(body.get("address") or "") or None
                    c.start_mining(addr)
                    return {"ok": True}
                if path == "/api/mine/stop":
                    c.stop_mining()
                    return {"ok": True}
                if path == "/api/node/start":
                    c.start_node()
                    return {"ok": True, "seeds": c.chain_info().get("seeds") or []}
                if path == "/api/node/stop":
                    c.stop_node()
                    return {"ok": True}
                if path == "/api/shutdown":
                    def _bye() -> None:
                        import time

                        time.sleep(0.15)
                        _GUI["allow_quit"] = True
                        w = _GUI.get("window")
                        if w is not None:
                            try:
                                w.destroy()
                            except Exception:
                                pass
                        c.shutdown()
                        os._exit(0)

                    threading.Thread(target=_bye, daemon=True).start()
                    return {"ok": True}
            raise RuntimeError(f"unknown endpoint {path}")

    return Handler


def run_web_desktop(network: str | None = None, port: int | None = None) -> None:
    from mhcoin.desktop.prefs import resolve_launch_network, save_preferred_network

    net = resolve_launch_network(explicit=network)
    save_preferred_network(net)
    port = int(port or os.environ.get("MHCOIN_DESKTOP_PORT", DEFAULT_PORT))
    state = DesktopState(net)

    httpd = None
    last_err: Exception | None = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((HOST, p), make_handler(state))
            port = p
            break
        except OSError as e:
            last_err = e
            continue
    if httpd is None:
        raise SystemExit(f"cannot bind desktop port ({last_err})")

    url = f"http://{HOST}:{port}/"
    frozen = getattr(__import__("sys"), "frozen", False)
    print(f"MHCOIN Core Desktop")
    print(f"UI: {url}")
    print(f"Network: {net}")
    print(f"Data: {state.ctrl.data_dir}")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    try:
        ok, detail = _open_native_window(url, state=state)
        if not ok:
            print(f"Shell: browser fallback — {detail}")
            if not frozen:
                print("Fix native window:  pip install 'pywebview>=5.0'")
                print("Then relaunch. Force browser: MHCOIN_DESKTOP_BROWSER=1")
                print("Dev mode — Press Ctrl+C to stop.")
            threading.Timer(0.35, lambda: webbrowser.open(url)).start()
            try:
                server_thread.join()
            except KeyboardInterrupt:
                print("\nStopping…")
        else:
            print(f"Shell: native window ({detail})")
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
        state.ctrl.shutdown()
        httpd.server_close()


def main(argv: list[str] | None = None) -> None:
    run_web_desktop()


if __name__ == "__main__":
    main()
