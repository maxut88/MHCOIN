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

from mhcoin.desktop.controller import CoreController
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
            # Allow the OS close button to quit the process.
            _GUI["allow_quit"] = True
            st = _GUI.get("state")
            if st is not None:
                try:
                    # Clear unlock in-memory; full shutdown runs after webview.start returns.
                    st.ctrl.lock()
                except Exception:
                    pass
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
  .act { border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; margin: 0 0 6px; background: rgba(0,0,0,.18); }
  .act-top { display:flex; justify-content:space-between; align-items:center; gap:8px; }
  .act-title { font-weight: 650; font-size: 12.5px; margin: 0; }
  .act-meta { color: var(--muted); font-size: 11px; margin: 1px 0 0; }
  .act-amt { font-weight: 700; font-size: 12.5px; white-space: nowrap; font-family: "JetBrains Mono", monospace; }
  .act-amt.in { color: #5dffc0; }
  .act-amt.out { color: #ff8f86; }
  .act-badge { display:none; }
  .act-txid { margin-top: 6px; display:flex; align-items:center; gap:8px; }
  .act-txid .mono { font-size: 10px; color: var(--muted); flex:1; min-width:0; }
  .act-list { max-height: 340px; overflow: auto; margin-top: 4px; }
  .act-full { padding: 10px 12px; }
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
  .act-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top: 6px; }
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
    overflow-wrap: anywhere; word-break: break-all; max-width: 100%;
  }
  .modal-copy {
    width: 100%; min-height: 96px; resize: vertical; margin: 0 0 10px;
    font-family: "JetBrains Mono", ui-monospace, monospace; font-size: 11px;
    line-height: 1.4; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-all;
    overflow-x: hidden; max-width: 100%; box-sizing: border-box;
    user-select: text; -webkit-user-select: text; cursor: text;
  }
  .modal-copy.hidden { display: none !important; }
  .modal input {
    margin: 0 0 12px;
  }
  .modal-actions { display:flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }
  .modal-actions.left { justify-content: flex-start; margin-bottom: 8px; }
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

  <section id="welcome" class="card">
    <h2>Welcome</h2>
    <p class="sub" id="welcomeNet">Create or open an encrypted wallet for the selected network.</p>
    <label>Network</label>
    <select id="welcomeNetSel">
      <option value="mainnet">Mainnet (real MHC)</option>
      <option value="localnet">Localnet (RC / solo test)</option>
    </select>
    <p class="sub" id="welcomeHint"></p>
    <div id="welcomeWallets"></div>
    <div class="row">
      <button class="primary" id="btnCreate">Create New Wallet</button>
      <button id="btnOpen">Open Existing Wallet</button>
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
  const text =
    "MHCOIN transaction\n" +
    "=================\n" +
    "Status: sent to mempool\n" +
    "TXID: " + (txid || "") + "\n" +
    "\n" +
    "Next: Mining → +1 block, then refresh / switch wallet on Overview.\n";
  await showModal({
    title: "Transaction sent",
    message: "Full TXID below — copy or save it. It stays inside the window.",
    mode: "copy",
    copyText: text,
    saveName: "MHCOIN-txid-" + String(txid||"tx").slice(0,16) + ".txt",
    okLabel: "OK",
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

function historyCard(t, i){
  const type = t.type || ((t.kind||"").toLowerCase().includes("mining") ? "mined" :
    ((t.kind||"").startsWith("Sent") ? "sent" : ((t.kind||"").startsWith("Received") ? "received" : "other")));
  const title = t.title || (type==="mined" ? "Block found" : (t.kind || "Activity"));
  const sign = t.sign || (type==="sent" ? "-" : "+");
  const amtRaw = t.amount_mhc || String(t.amount||"").replace(/^[+-]/,"");
  const amtClass = sign === "-" ? "out" : "in";
  const amtText = (sign === "-" ? "−" : "+") + amtRaw + " MHC";
  const height = (t.height!=null && t.height!=="") ? ("Block #" + t.height) : "Mempool (unconfirmed)";
  const conf = (t.confirmations!=null) ? (t.confirmations + " conf") : "";
  const meta = [height, conf, t.time_utc || ""].filter(Boolean).join(" · ");
  const txid = t.txid || "";
  const fromList = (t.from && t.from.length) ? t.from : [];
  const toList = (t.to && t.to.length) ? t.to : [];
  const fromText = fromList.length ? fromList.join("\n") : "—";
  const toText = toList.length ? toList.join("\n") : "—";
  const fromRows = Math.min(4, Math.max(1, fromList.length || 1));
  const toRows = Math.min(4, Math.max(1, toList.length || 1));
  const fee = t.fee_mhc != null ? (t.fee_mhc + " MHC") : "—";
  const tid = "htx" + i;
  const fromLabel = type === "mined" ? "From" : (type === "sent" ? "From (your wallet)" : "From");
  const toLabel = type === "sent" ? "To (recipient)" : (type === "received" ? "To (your wallet)" : "To");
  return `<div class="act act-full">
    <div class="act-top">
      <div>
        <p class="act-title">${esc(title)}</p>
        <p class="act-meta">${esc(meta)}</p>
      </div>
      <div class="act-amt ${amtClass}">${amtText}</div>
    </div>
    <div class="act-kv">
      <span>Transaction ID</span>
      <textarea id="${tid}" rows="2" readonly class="mono">${esc(txid)}</textarea>
      <div class="act-actions">
        <button type="button" class="linkish copyTx" data-txid="${esc(txid)}">Copy TXID</button>
      </div>
    </div>
    <div class="act-kv">
      <span>Time</span>
      <div class="act-val">${esc(t.time_utc || (txid ? "pending / unknown" : "—"))}</div>
    </div>
    <div class="act-kv">
      <span>${esc(fromLabel)}</span>
      <textarea rows="${fromRows}" readonly class="mono">${esc(fromText)}</textarea>
      ${fromList.length ? `<div class="act-actions"><button type="button" class="linkish copyAddr" data-addr="${esc(fromList[0])}">Copy from</button></div>` : ""}
    </div>
    <div class="act-kv">
      <span>${esc(toLabel)}</span>
      <textarea rows="${toRows}" readonly class="mono">${esc(toText)}</textarea>
      ${toList.length ? `<div class="act-actions"><button type="button" class="linkish copyAddr" data-addr="${esc(toList[0])}">Copy to</button></div>` : ""}
    </div>
    <div class="act-kv">
      <span>Fee</span>
      <div class="act-val mono">${esc(fee)}</div>
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
  try {
    const net = $("welcomeNetSel").value;
    const j = await api("network/switch", {network: net});
    applyWelcomeNet(j);
    flash(j.network === "mainnet" ? "Switched to Mainnet" : "Switched to Localnet");
  } catch(e){ flash(e.message, false); }
};

$("btnCreate").onclick = async () => {
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

function enterApp(){
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
  // Mainnet: auto-connect to public seed so Height/Peers appear.
  api("node/start", {}).then(() => flash("Node started · connecting to seed…")).catch(()=>{});
  render();
  if (window._mhStatusTimer) clearInterval(window._mhStatusTimer);
  window._mhStatusTimer = setInterval(() => {
    if (!unlocked) return;
    refreshStatus();
  }, 1500);
}

async function refreshStatus(){
  try {
    const s = await api("status");
    $("topStatus").textContent = s.network + " · height " + s.height + " · peers " + s.peers +
      (s.mining ? " · mining" : "");
    if (s.chain_error) {
      flash(s.chain_error, false);
    }
    // Mining tab: live stats
    if (active === "Mining" && $("mineHero")) {
      $("mineHero").classList.toggle("live", !!s.mining);
      if ($("mineState")) $("mineState").textContent = s.mining ? "Mining" : "Idle";
      if ($("mineDot")) $("mineDot").className = "dot" + (s.mining ? " on" : "");
      if ($("statHash")) $("statHash").textContent = s.hashrate || "—";
      if ($("statBlocks")) $("statBlocks").textContent = String(s.blocks_found ?? 0);
      if ($("statRewards")) $("statRewards").textContent = s.rewards || "0";
      // Keep a live activity strip on Mining too.
      const live = $("mineLiveActs");
      if (live) {
        const rows = (s.txs||[]).filter(t => (t.type==="mined") || (t.kind||"").toLowerCase().includes("mining")).slice(0,6);
        live.innerHTML = rows.map((t,i)=>activityCard(t,i,"m")).join("") ||
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
      box.innerHTML = (s.txs||[]).slice(0,12).map((t,i)=>activityCard(t,i,"r")).join("") ||
        '<p class="sub">No activity yet — mined blocks and transfers will appear here.</p>';
      bindCopyTxButtons(box);
      return;
    }
    if (active === "Network" || active === "Receive") {
      render(s);
    }
  } catch(e){}
}

api("status").then(s => {
  applyWelcomeNet(s);
  if (s.wallet_exists && s.unlocked) enterApp();
}).catch(()=>{});


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
      <div class="pill"><span class="dot ${s.mining?'on':''}"></span>${s.network} · block ${s.height} · peers ${s.peers}</div>
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
      <p class="sub">${(s.txs||[]).length} recent · newest block first · open History for full list</p>
      <div class="act-list" id="recentList">
        ${(s.txs||[]).slice(0,12).map((t,i)=>activityCard(t,i,"r")).join("") || '<p class="sub">No activity yet — mined blocks and transfers will appear here.</p>'}
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
      <p class="sub">Full TXID, time, from/to addresses — like a block explorer for this wallet.</p>
      <p class="mono" style="font-size:12px;word-break:break-all">${s.address||""}</p>
      <div id="histBox" class="act-list" style="max-height:62vh;overflow:auto"><p class="sub">Loading…</p></div>
      <div class="row"><button class="sm" id="histReload">Reload</button></div>`;
    const fill = async () => {
      const box = $("histBox");
      try {
        box.innerHTML = "<p class='sub'>Loading activity…</p>";
        const j = await api("history");
        const list = j.txs || [];
        if (!list.length) {
          box.innerHTML = "<p class='sub'>No activity for this wallet yet.</p>";
          return;
        }
        box.innerHTML = list.map((t,i) => historyCard(t,i)).join("");
        bindCopyTxButtons(box);
      } catch(e) {
        box.innerHTML = "<p class='sub'>Failed to load history: "+(e.message||e)+"</p>";
      }
    };
    $("histReload").onclick = () => fill();
    fill();
  } else if (active === "Receive") {
    p.innerHTML = `
      <h2>Receive MHCOIN</h2>
      <p class="sub">Share this address to receive MHC.</p>
      <textarea id="addr" rows="3" readonly class="mono">${s.address||""}</textarea>
      <div class="row"><button class="primary" id="copyBtn">Copy Address</button></div>`;
    $("copyBtn").onclick = async () => {
      try { await navigator.clipboard.writeText(s.address||""); flash("Address copied"); }
      catch(e){ $("addr").select(); document.execCommand("copy"); flash("Address copied"); }
    };
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
      ${last}`;
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
      </div>
      <h3>Live blocks</h3>
      <p class="sub">Streams as blocks are found (also on Overview → Activity).</p>
      <div class="act-list" id="mineLiveActs">
        ${(s.txs||[]).filter(t => (t.type==="mined") || (t.kind||"").toLowerCase().includes("mining")).slice(0,6).map((t,i)=>activityCard(t,i,"m")).join("") || '<p class="sub">New blocks will stream here while mining…</p>'}
      </div>`;
    bindCopyTxButtons($("mineLiveActs"));
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
  } else if (active === "Network") {
    p.innerHTML = `
      <h2>Network</h2>
      <p>Network: <b>${s.network}</b></p>
      <p>Status: ${s.sync}</p>
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
  api("status").then(applyWelcomeNet).catch(()=>{});
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

    def status(self) -> dict[str, Any]:
        with self.lock:
            c = self.ctrl
            info = c.chain_info()
            stats = c.mining_stats
            addr = None
            unlocked = c._password is not None
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
            if path == "/api/status":
                _json(self, 200, state.status())
                return
            if path == "/api/history":
                c = state.ctrl
                txs = []
                try:
                    if c._node is not None:
                        # Node owns datadir — serve cached recent; ask user to Stop Node for full scan.
                        txs = list(c._recent_cache)
                    else:
                        for r in c.wallet_history(2000, full_chain=True):
                            item = c._txrow_to_dict(r)
                            txs.append(item)
                        c._recent_cache = list(txs)[:50]
                except Exception as e:  # noqa: BLE001
                    if c._recent_cache:
                        _json(self, 200, {"ok": True, "txs": list(c._recent_cache), "count": len(c._recent_cache), "cached": True})
                        return
                    _json(self, 500, {"ok": False, "error": str(e)})
                    return
                _json(self, 200, {"ok": True, "txs": txs, "count": len(txs)})
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
                    try:
                        c.balance_sats()
                        c.refresh_recent_cache(limit=50, full_chain=True)
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
                    c.ensure_chain()
                    try:
                        c.balance_sats()
                        c.refresh_recent_cache(limit=50, full_chain=True)
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
