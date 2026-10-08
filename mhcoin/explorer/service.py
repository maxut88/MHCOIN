"""MHCOIN block explorer — stdlib HTTP (read-only)."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import mimetypes
import re
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.explorer import decode as D
from mhcoin.wallet.addresses import validate_address
from mhcoin.wallet.send import format_mhc

logger = logging.getLogger("mhcoin.explorer")

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
ASSETS_DIR = Path(__file__).resolve().parents[1] / "desktop" / "assets"
STATIC_FILES = {
    "logo.svg": ASSETS_DIR / "mhcoin-logo.svg",
    "logo.png": ASSETS_DIR / "mhcoin-256.png",
    "favicon.ico": ASSETS_DIR / "mhcoin.ico",
}

# Home/stats TTL — home tabs poll often; avoid full-chain rescans every hit.
_HOME_CACHE_TTL_SEC = 3.0
_RICH_CACHE_TTL_SEC = 30.0
# Per-client limits only for expensive scans (not HTML / tip poll / static).
_RATE_LIMIT_WINDOW_SEC = 60.0
_RATE_LIMIT_ADDRESS = 60  # /address/* chain scans
_RATE_LIMIT_API_HOME = 90  # full /api/ dumps (tip poll is exempt)

# Same mark as Desktop webui (coin + M) — animated rings / throb / sheen.
_LOGO_SVG_M = (
    '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>'
    "</svg>"
)


def _logo_html(*, size_class: str = "") -> str:
    cls = "logo" + (f" {size_class}" if size_class else "")
    return (
        f'<div class="{cls}" aria-hidden="true">'
        f'<span class="logo-ring"></span>'
        f'<span class="logo-ring2"></span>'
        f'<div class="logo-coin">{_LOGO_SVG_M}</div>'
        f"</div>"
    )


def _mint_mark(*, title: str = "MHC Mined") -> str:
    """Tiny MHCOIN coin mark for mined / minted rows (animated in Live Blocks)."""
    return (
        f'<span class="mint" title="{_esc(title)}">'
        f'<span class="mint-ring"></span>'
        f'<span class="mint-ring2"></span>'
        f'<span class="mint-coin">{_LOGO_SVG_M}</span>'
        f"</span>"
    )


def _type_pill(coinbase: bool) -> str:
    """Site-wide type badge — soft sky MHC Mined or teal Transfer."""
    if coinbase:
        return '<span class="pill minted">MHC Mined</span>'
    return '<span class="pill xfer">Transfer</span>'


def _flow_pill(*, received: bool) -> str:
    """Address flow badge — green Received / red Sent."""
    if received:
        return '<span class="pill recv">Received</span>'
    return '<span class="pill sent">Sent</span>'


def _short_addr(addr: str | None, n: int = 10) -> str:
    if not addr:
        return "—"
    if addr == "coinbase":
        return "coinbase"
    if len(addr) <= n * 2 + 1:
        return addr
    return f"{addr[:n]}…{addr[-n:]}"


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _page(title: str, body: str, *, tip: int | None = None, hero: bool = False) -> bytes:
    tip_bit = f" · tip #{tip}" if tip is not None and tip >= 0 else ""
    _ = hero  # branding is always a single header
    import os

    pool_url = (os.environ.get("MHCOIN_POOL_URL") or "").strip()
    pool_nav = (
        f'\n          <a href="{_esc(pool_url)}" rel="noopener">Pool</a>'
        if pool_url
        else ""
    )
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#ffffff" id="metaThemeColor"/>
<script>
(function(){{
  // Blocking (pre-paint) theme init — avoids a light/dark flash on load.
  try {{
    var saved = localStorage.getItem('mhcoin-theme');
    var dark = saved ? saved === 'dark' : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (dark) document.documentElement.setAttribute('data-theme', 'dark');
  }} catch (e) {{}}
}})();
</script>
<meta name="description" content="MHCOIN block explorer — live mainnet tip, blocks, transactions, and addresses."/>
<meta name="robots" content="index,follow"/>
<meta property="og:title" content="{_esc(title)} — MHCOIN Explorer"/>
<meta property="og:description" content="Independent HASH256 Proof-of-Work explorer for MHCOIN mainnet."/>
<meta property="og:type" content="website"/>
<link rel="icon" href="/favicon.ico?v=3" sizes="any"/>
<link rel="icon" type="image/svg+xml" href="/static/logo.svg?v=3"/>
<link rel="apple-touch-icon" href="/static/logo.png?v=3"/>
<link rel="shortcut icon" href="/favicon.ico?v=3"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link rel="preload" as="style" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@450;600&display=optional"/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@450;600&display=optional" rel="stylesheet"/>
<title>{_esc(title)} — MHCOIN Explorer</title>
<style>
  :root {{
    --bg: #f4f5f7; --panel: #ffffff; --text: #111418; --muted: #6b7280;
    --link: #111418; --line: #e5e7eb; --soft: #f9fafb;
    --header-bg: rgba(255,255,255,.94); --hover-bg: #ffffff; --invert: #000000;
    --val: #374151; --tbl-link: #111418;
    --sans: "DM Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
  }}
  [data-theme="dark"] {{
    --bg: #0b0d10; --panel: #15181d; --text: #e7eaee; --muted: #9aa3af;
    --link: #e7eaee; --line: #262b33; --soft: #1b1f26;
    --header-bg: rgba(11,13,16,.92); --hover-bg: #1e222a; --invert: #ffffff;
    --val: #c7ccd4; --tbl-link: #e7eaee;
  }}
  * {{ box-sizing: border-box; }}
  html {{
    background: var(--bg); color-scheme: light;
    overflow-x: clip; max-width: 100%;
  }}
  [data-theme="dark"] html, html[data-theme="dark"] {{ color-scheme: dark; }}
  body {{
    margin: 0; font-family: var(--sans); color: var(--text); line-height: 1.5;
    background: var(--bg); min-height: 100vh; max-width: 100%;
    overflow-x: clip;
    transition: background-color .15s ease, color .15s ease;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ color: var(--invert); text-decoration: underline; }}
  header.top {{
    position: sticky; top: 0; z-index: 20;
    background: var(--header-bg);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--line);
    padding: 1rem 1.5rem .9rem; width: 100%;
  }}
  .header-inner {{
    display: flex; flex-wrap: wrap; gap: 1rem 1.5rem;
    align-items: center; justify-content: space-between;
  }}
  .brand-row {{ display: flex; align-items: center; gap: .95rem; min-width: 0; }}
  .logo {{
    --logo-size: 52px; width: var(--logo-size); height: var(--logo-size);
    position: relative; display: grid; place-items: center; flex-shrink: 0;
  }}
  .logo-ring, .logo-ring2 {{
    position: absolute; inset: 0; border-radius: 50%;
    border: 1.5px solid transparent; border-top-color: #2dd4a0;
    border-right-color: rgba(45,212,160,.28);
    animation: spin 2.8s linear infinite;
  }}
  .logo-ring2 {{
    inset: 3px; border-top-color: rgba(45,212,160,.55);
    border-right-color: transparent; border-bottom-color: rgba(45,212,160,.2);
    animation: spin 4s linear infinite reverse;
  }}
  .logo-coin {{
    width: 74%; height: 74%; border-radius: 50%;
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.45), transparent 42%),
      linear-gradient(145deg, #5dffc0 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.35), 0 0 14px rgba(45,212,160,.22),
      inset 0 -2px 4px rgba(0,40,25,.35), inset 0 2px 3px rgba(255,255,255,.35);
    display: grid; place-items: center; position: relative; overflow: hidden;
    animation: throb 2.4s ease-in-out infinite;
  }}
  .logo-coin::after {{
    content: ""; position: absolute; inset: -40% -20%;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.5), transparent 70%);
    animation: sheen 3.2s ease-in-out infinite;
  }}
  .logo-coin svg {{
    width: 58%; height: 58%; display: block; position: relative; z-index: 1;
    filter: drop-shadow(0 1px 0 rgba(0,40,25,.25));
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes throb {{ 0%,100% {{ transform: scale(1); }} 50% {{ transform: scale(1.04); }} }}
  @keyframes sheen {{
    0%,100% {{ transform: translateX(-30%) rotate(12deg); opacity: .35; }}
    50% {{ transform: translateX(30%) rotate(12deg); opacity: .7; }}
  }}
  .brand-text {{ min-width: 0; }}
  .brand-text .eyebrow {{
    margin: 0; text-transform: uppercase; letter-spacing: .12em;
    font-size: .68rem; font-weight: 700; color: var(--muted);
  }}
  .brand-text .title {{
    margin: .12rem 0 0; font-size: 1.35rem; font-weight: 800;
    letter-spacing: -.03em; line-height: 1.15;
  }}
  .brand-text .title a {{ color: var(--text); text-decoration: none; }}
  .brand-text .title a:hover {{ color: var(--link); }}
  .brand-text .meta {{ margin: .15rem 0 0; color: var(--muted); font-size: .88rem; }}
  header nav {{ display: flex; flex-wrap: wrap; gap: .35rem .95rem; margin-top: .65rem; }}
  header nav a {{
    color: var(--muted); font-size: .9rem; font-weight: 600;
    padding: .15rem 0; border-bottom: 2px solid transparent; text-decoration: none;
  }}
  header nav a:hover {{ color: var(--text); border-bottom-color: var(--text); text-decoration: none; }}
  form.search {{ display: flex; gap: .5rem; width: min(420px, 100%); }}
  form.search input {{
    flex: 1; padding: .65rem .9rem; border-radius: 10px;
    border: 1px solid var(--line); background: var(--soft); color: var(--text);
    font: inherit; outline: none;
  }}
  form.search input:focus {{
    border-color: #9ca3af; background: var(--hover-bg); box-shadow: 0 0 0 3px rgba(17,20,24,.06);
  }}
  form.search button {{
    padding: .65rem 1.05rem; border-radius: 10px; border: 0;
    background: var(--text); color: var(--bg); font-weight: 700; cursor: pointer; font: inherit;
  }}
  form.search button:hover {{ background: var(--invert); color: var(--bg); }}
  .theme-toggle {{
    flex: 0 0 auto; width: 2.55rem; height: 2.55rem; padding: 0; border-radius: 999px;
    border: 1px solid var(--line); background: transparent; cursor: pointer;
    display: grid; place-items: center; position: relative; overflow: visible;
    transition: transform .25s ease, border-color .2s ease, box-shadow .25s ease;
  }}
  .theme-toggle:hover {{ transform: scale(1.08); border-color: #2dd4a0; }}
  .theme-toggle:active {{ transform: scale(.94) rotate(-18deg); }}
  .theme-toggle:focus-visible {{ outline: 2px solid #2dd4a0; outline-offset: 2px; }}
  .theme-coin {{
    --tc-size: 1.85rem; width: var(--tc-size); height: var(--tc-size);
    position: relative; display: grid; place-items: center; border-radius: 50%;
  }}
  .theme-coin .tc-ring, .theme-coin .tc-ring2 {{
    position: absolute; inset: -3px; border-radius: 50%;
    border: 1.5px solid transparent; pointer-events: none;
  }}
  .theme-coin .tc-core {{
    width: 100%; height: 100%; border-radius: 50%;
    display: grid; place-items: center; position: relative; overflow: hidden;
    z-index: 1;
  }}
  .theme-coin .tc-core svg {{
    width: 58%; height: 58%; display: block; position: relative; z-index: 2;
    filter: drop-shadow(0 1px 0 rgba(0,40,25,.3));
  }}
  .theme-coin .tc-core::after {{
    content: ""; position: absolute; inset: -45% -25%; z-index: 1;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.55), transparent 70%);
  }}
  /* Day = sunny PoW — bright mint, fast hash rings, warm glow */
  html:not([data-theme="dark"]) .theme-coin .tc-core {{
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.55), transparent 42%),
      linear-gradient(145deg, #7dffc8 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.4),
      0 0 16px rgba(250,204,21,.35),
      0 0 10px rgba(45,212,160,.35),
      inset 0 -2px 3px rgba(0,40,25,.3), inset 0 2px 2px rgba(255,255,255,.4);
    animation: tc-day-throb 1.8s ease-in-out infinite;
  }}
  html:not([data-theme="dark"]) .theme-coin .tc-core::after {{
    animation: tc-day-sheen 1.6s ease-in-out infinite;
  }}
  html:not([data-theme="dark"]) .theme-coin .tc-ring {{
    border-top-color: #f59e0b; border-right-color: rgba(245,158,11,.35);
    animation: tc-mine-spin 1.1s linear infinite;
  }}
  html:not([data-theme="dark"]) .theme-coin .tc-ring2 {{
    inset: 0; border-bottom-color: #2dd4a0; border-left-color: rgba(45,212,160,.25);
    animation: tc-mine-spin 1.7s linear infinite reverse;
  }}
  html:not([data-theme="dark"]) .theme-toggle:hover {{
    box-shadow: 0 0 14px rgba(245,158,11,.35);
  }}
  /* Night = deep mine — cooler coin, slow nonce search rings, cyan tick */
  [data-theme="dark"] .theme-coin .tc-core {{
    background:
      radial-gradient(circle at 30% 26%, rgba(165,243,252,.35), transparent 40%),
      linear-gradient(145deg, #1a9b72 0%, #0d5c44 48%, #06261a 100%);
    box-shadow:
      0 0 0 1px rgba(34,211,238,.35),
      0 0 18px rgba(34,211,238,.28),
      0 0 8px rgba(45,212,160,.2),
      inset 0 -2px 4px rgba(0,0,0,.55), inset 0 2px 2px rgba(165,243,252,.15);
    animation: tc-night-pulse 2.8s ease-in-out infinite;
  }}
  [data-theme="dark"] .theme-coin .tc-core::after {{
    background: linear-gradient(115deg, transparent 28%, rgba(165,243,252,.4), transparent 72%);
    animation: tc-night-sheen 3.4s ease-in-out infinite;
  }}
  [data-theme="dark"] .theme-coin .tc-ring {{
    border-top-color: #22d3ee; border-right-color: rgba(34,211,238,.3);
    animation: tc-mine-spin 2.4s linear infinite;
  }}
  [data-theme="dark"] .theme-coin .tc-ring2 {{
    inset: -1px; border-bottom-color: #a78bfa; border-left-color: rgba(167,139,250,.25);
    animation: tc-mine-spin 3.6s linear infinite reverse;
  }}
  [data-theme="dark"] .theme-toggle:hover {{
    box-shadow: 0 0 16px rgba(34,211,238,.4);
    border-color: #22d3ee;
  }}
  .theme-toggle.tc-found .theme-coin {{
    animation: tc-block-found .7s cubic-bezier(.2,.8,.2,1);
  }}
  .theme-toggle.tc-found::before {{
    content: ""; position: absolute; inset: -6px; border-radius: 50%;
    border: 2px solid #2dd4a0; opacity: 0;
    animation: tc-hash-burst .7s ease-out;
    pointer-events: none;
  }}
  @keyframes tc-mine-spin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes tc-day-throb {{
    0%,100% {{ transform: scale(1); }}
    50% {{ transform: scale(1.06); }}
  }}
  @keyframes tc-day-sheen {{
    0%,100% {{ transform: translateX(-35%) rotate(14deg); opacity: .4; }}
    50% {{ transform: translateX(35%) rotate(14deg); opacity: .85; }}
  }}
  @keyframes tc-night-pulse {{
    0%,100% {{ transform: scale(1); filter: brightness(1); }}
    50% {{ transform: scale(1.04); filter: brightness(1.12); }}
  }}
  @keyframes tc-night-sheen {{
    0%,100% {{ transform: translateX(-40%) rotate(10deg); opacity: .25; }}
    50% {{ transform: translateX(40%) rotate(10deg); opacity: .65; }}
  }}
  @keyframes tc-block-found {{
    0% {{ transform: rotateY(0) scale(1); }}
    35% {{ transform: rotateY(180deg) scale(1.18); }}
    70% {{ transform: rotateY(320deg) scale(1.05); }}
    100% {{ transform: rotateY(360deg) scale(1); }}
  }}
  @keyframes tc-hash-burst {{
    0% {{ transform: scale(.6); opacity: .9; }}
    100% {{ transform: scale(1.8); opacity: 0; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .theme-coin .tc-ring, .theme-coin .tc-ring2, .theme-coin .tc-core,
    .theme-coin .tc-core::after, .theme-toggle.tc-found .theme-coin,
    .theme-toggle.tc-found::before {{ animation: none !important; }}
  }}
  .search-row {{ display: flex; gap: .5rem; align-items: center; }}
  main {{ width: 100%; max-width: 100%; margin: 0; padding: 1rem 1.5rem 2.75rem; min-width: 0; }}
  .shell {{ width: 100%; max-width: 100%; min-width: 0; }}
  .kpi {{
    display: grid; grid-template-columns: repeat(8, minmax(0, 1fr));
    gap: .5rem; margin: 0 0 .9rem;
  }}
  @media (max-width: 1280px) {{ .kpi {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }} }}
  @media (max-width: 720px) {{ .kpi {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
  .kpi .stat {{
    --accent: #64748b;
    --accent-soft: rgba(100, 116, 139, .08);
    position: relative;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: .4rem .55rem .38rem .65rem;
    min-height: 0;
    display: flex; flex-direction: column; min-width: 0;
    overflow: hidden;
    box-shadow: 0 1px 0 rgba(17, 20, 24, .03);
    transition: border-color .15s ease, box-shadow .15s ease;
  }}
  .kpi .stat::before {{
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: var(--accent);
  }}
  .kpi .stat::after {{
    content: ""; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(135deg, var(--accent-soft), transparent 55%);
  }}
  .kpi .stat:hover {{
    border-color: color-mix(in srgb, var(--accent) 35%, var(--line));
    box-shadow: 0 4px 14px rgba(17, 20, 24, .05);
  }}
  [data-theme="dark"] .kpi .stat {{
    box-shadow: 0 1px 0 rgba(0,0,0,.25);
  }}
  [data-theme="dark"] .kpi .stat:hover {{
    box-shadow: 0 6px 18px rgba(0,0,0,.28);
  }}
  .kpi .stat.k-height {{ --accent: #0ea5e9; --accent-soft: rgba(14,165,233,.10); }}
  .kpi .stat.k-hash {{ --accent: #16a34a; --accent-soft: rgba(22,163,74,.10); }}
  .kpi .stat.k-diff {{ --accent: #d97706; --accent-soft: rgba(217,119,6,.10); }}
  .kpi .stat.k-eta {{ --accent: #2563eb; --accent-soft: rgba(37,99,235,.10); }}
  .kpi .stat.k-peers {{ --accent: #7c3aed; --accent-soft: rgba(124,58,237,.10); }}
  .kpi .stat.k-blocks {{ --accent: #475569; --accent-soft: rgba(71,85,105,.10); }}
  .kpi .stat.k-txs {{ --accent: #4f46e5; --accent-soft: rgba(79,70,229,.10); }}
  .kpi .stat.k-mint {{ --accent: #38bdf8; --accent-soft: rgba(56,189,248,.14); }}
  .kpi .stat.k-supply {{ --accent: #0f766e; --accent-soft: rgba(15,118,110,.10); }}
  .kpi .stat.k-halving {{ --accent: #b45309; --accent-soft: rgba(180,83,9,.10); }}
  .kpi .lbl {{
    position: relative; z-index: 1;
    color: var(--muted); font-size: .54rem; font-weight: 720;
    text-transform: uppercase; letter-spacing: .06em; line-height: 1.15;
  }}
  .kpi .val {{
    position: relative; z-index: 1;
    font-family: var(--mono); font-size: .92rem; font-weight: 700;
    margin-top: .12rem; letter-spacing: -.03em;
    line-height: 1.12; min-height: 0; color: var(--text);
    font-variant-numeric: tabular-nums;
  }}
  .kpi .hint {{
    position: relative; z-index: 1;
    color: var(--muted); font-size: .6rem; margin-top: .2rem;
    line-height: 1.25; min-height: 0;
    border-top: 1px solid var(--line); padding-top: .2rem;
  }}
  .kpi .hint.overdue, .kpi .hint.warn {{ color: #b45309; }}
  [data-theme="dark"] .kpi .hint.overdue, [data-theme="dark"] .kpi .hint.warn {{ color: #fbbf24; }}
  .blocks-row {{
    display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(300px, .95fr);
    gap: .75rem; align-items: start; margin: 0 0 1rem;
  }}
  @media (max-width: 1100px) {{
    .blocks-row {{ grid-template-columns: 1fr; }}
    /* Keep visual order: Latest blocks above Live Blocks on narrow screens. */
    .blocks-row > .card.latest-blocks {{ grid-column: 1; grid-row: 1; }}
    .blocks-row > .blocks-side {{ grid-column: 1; grid-row: 2; }}
  }}
  .blocks-side {{
    display: flex; flex-direction: column; gap: .65rem; min-width: 0;
    /* Paint Live Blocks before the heavy Latest-blocks table (DOM order), keep right column visually. */
    grid-column: 2;
    grid-row: 1;
  }}
  .blocks-row > .card.latest-blocks {{
    grid-column: 1;
    grid-row: 1;
    margin: 0; min-width: 0;
  }}
  /* Fixed Live Blocks box: exactly two full cards visible (compact rows). */
  .blocks-row .term {{
    margin: 0;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    height: 30rem;
    min-height: 30rem;
    max-height: 30rem;
    contain: layout size;
    padding: .75rem .85rem;
    font-size: .8rem;
    line-height: 1.32;
  }}
  .blocks-row .term .term-head {{
    flex-wrap: nowrap;
    gap: .5rem;
    margin-bottom: .4rem;
    align-items: center;
  }}
  .blocks-row .term .term-head h1 {{ font-size: 1rem; }}
  .blocks-row .term .term-head .muted {{
    font-size: .68rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }}
  .blocks-row .term .term-feed {{
    flex: 1 1 auto;
    min-height: 0;
    overflow-x: hidden;
    overflow-y: scroll;
    scrollbar-gutter: stable;
    gap: 0;
  }}
  .blocks-row .term .found {{
    padding-top: .4rem;
    border-top: 1px solid var(--line);
  }}
  .blocks-row .term .found:first-child {{
    padding-top: 0;
    border-top: 0;
  }}
  .blocks-row .term .found > div {{
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    line-height: 1.32;
  }}
  .blocks-row .term .found .title {{ line-height: 1.3; margin-bottom: .05rem; }}
  .blocks-row .term .found .hash,
  .blocks-row .term .found .hash a {{
    font-size: .68rem;
    letter-spacing: -0.03em;
  }}
  .blocks-row .term .found .rule {{
    margin-top: .15rem;
    letter-spacing: -0.04em;
  }}
  .blocks-row > .card {{ margin: 0; min-width: 0; }}
  .blocks-row > .card .table-wrap {{ overflow-x: auto; }}
  .blocks-row > .card table {{ font-size: .74rem; }}
  .blocks-row > .card th, .blocks-row > .card td {{ padding: .38rem .32rem; }}
  .peers-panel.card {{ padding: .75rem .8rem; }}
  .peers-panel {{ font-size: .72rem; min-width: 0; }}
  .peers-panel .card-head {{ margin-bottom: .4rem; }}
  .peers-panel .table-wrap {{ overflow-x: auto; }}
  .peers-panel table {{
    font-size: .7rem; width: 100%;
    border-collapse: collapse; table-layout: auto;
  }}
  .peers-panel th, .peers-panel td {{
    vertical-align: middle; white-space: nowrap;
    padding: .28rem .28rem; overflow: visible;
  }}
  .peers-panel th:nth-child(2), .peers-panel td:nth-child(2),
  .peers-panel th:nth-child(5), .peers-panel td:nth-child(5),
  .peers-panel th:nth-child(6), .peers-panel td:nth-child(6) {{ text-align: center; }}
  .peers-panel th:nth-child(3), .peers-panel td:nth-child(3),
  .peers-panel th:nth-child(4), .peers-panel td:nth-child(4) {{ text-align: right; }}
  .peers-panel .ip {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-weight: 650; white-space: nowrap; font-size: .68rem;
  }}
  .peers-panel .dir {{
    display: inline-block; font-size: .6rem; font-weight: 700; letter-spacing: .03em;
    text-transform: uppercase; padding: .08rem .25rem; border-radius: 5px;
    background: var(--soft); color: var(--muted);
  }}
  .peers-panel .dir.in {{ background: #e8f5e9; color: #1b5e20; }}
  .peers-panel .dir.out {{ background: #e3f2fd; color: #0d47a1; }}
  .peers-panel .peers-total {{
    margin: .4rem 0 0; font-size: .74rem; font-weight: 650;
  }}
  .peers-panel > .muted {{ margin: 0 0 .35rem !important; font-size: .7rem !important; }}
  .card {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.1rem; margin: 0 0 1rem;
    max-width: 100%; min-width: 0; overflow-wrap: anywhere;
  }}
  .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: .75rem; }}
  .card-head h1 {{ margin: 0; font-size: 1.05rem; }}
  .card-head .more {{ font-size: .85rem; font-weight: 650; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .85rem; font-weight: 750; letter-spacing: -.02em; }}
  .table-wrap {{ width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .78rem; }}
  /* Full hashes / wallets on one line; scroll sideways instead of wrapping under. */
  table.data-table {{
    width: max-content; min-width: 100%; table-layout: auto;
  }}
  table.data-table th, table.data-table td {{
    white-space: nowrap; vertical-align: middle;
  }}
  table.data-table .mono {{
    white-space: nowrap; word-break: normal; overflow-wrap: normal;
  }}
  table.data-table .copy-wrap {{
    display: inline-flex; align-items: center; gap: .3rem;
    max-width: none; flex-wrap: nowrap;
  }}
  table.data-table .row-ico {{
    display: inline-flex; align-items: center; gap: .4rem; flex-wrap: nowrap;
  }}
  th, td {{ text-align: left; padding: .55rem .45rem; border-bottom: 1px solid var(--line); vertical-align: middle; }}
  th {{ color: var(--muted); font-weight: 700; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }}
  tbody tr:hover {{ background: var(--soft); }}

  .row-ico {{ display: inline-flex; align-items: center; gap: .45rem; }}
  .mint {{
    --mint: 16px; width: var(--mint); height: var(--mint);
    aspect-ratio: 1 / 1;
    display: inline-flex; align-items: center; justify-content: center;
    flex: 0 0 var(--mint); flex-shrink: 0; vertical-align: middle;
    position: relative; border-radius: 50%;
  }}
  .mint-ring, .mint-ring2 {{
    display: none; /* rings looked square in Live Blocks; glow via box-shadow instead */
  }}
  .mint-coin {{
    width: 100%; height: 100%;
    border-radius: 50%;
    clip-path: circle(50%);
    background: linear-gradient(145deg, #5dffc0 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow: 0 0 0 1px rgba(15,118,110,.25);
    display: grid; place-items: center; position: relative; z-index: 1;
    overflow: hidden;
    animation: throb 2.6s ease-in-out infinite;
  }}
  .mint-coin::after {{
    content: ""; position: absolute; inset: -40% -20%; z-index: 0;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.45), transparent 70%);
    animation: sheen 3.2s ease-in-out infinite;
  }}
  .mint-coin svg {{ width: 58%; height: 58%; display: block; position: relative; z-index: 1; }}
  /* Live Blocks — perfect circle, day/night mining glow (no square rings) */
  .term-feed .mint {{ --mint: 15px; margin-right: .15rem; }}
  .term-feed .found .title .row-ico {{ gap: .4rem; align-items: center; }}
  html:not([data-theme="dark"]) .term-feed .mint-coin {{
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.55), transparent 42%),
      linear-gradient(145deg, #7dffc8 0%, #2dd4a0 45%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.45),
      0 0 0 0 rgba(250,204,21,.0);
    animation: lb-day-glow 1.8s ease-in-out infinite;
  }}
  html:not([data-theme="dark"]) .term-feed .mint-coin::after {{
    animation: lb-day-sheen 1.6s ease-in-out infinite;
  }}
  [data-theme="dark"] .term-feed .mint-coin {{
    background:
      radial-gradient(circle at 30% 26%, rgba(165,243,252,.35), transparent 42%),
      linear-gradient(145deg, #1a9b72 0%, #0d5c44 48%, #06261a 100%);
    box-shadow:
      0 0 0 1px rgba(34,211,238,.4),
      0 0 0 0 rgba(34,211,238,.0);
    animation: lb-night-glow 2.5s ease-in-out infinite;
  }}
  [data-theme="dark"] .term-feed .mint-coin::after {{
    background: linear-gradient(115deg, transparent 28%, rgba(165,243,252,.4), transparent 72%);
    animation: lb-night-sheen 3.2s ease-in-out infinite;
  }}
  .term-feed .found:nth-child(2) .mint-coin {{ animation-delay: .15s; }}
  .term-feed .found:nth-child(3) .mint-coin {{ animation-delay: .3s; }}
  .term-feed .found:nth-child(4) .mint-coin {{ animation-delay: .45s; }}
  .term-feed .found:nth-child(5) .mint-coin {{ animation-delay: .6s; }}
  @keyframes lb-day-glow {{
    0%,100% {{
      transform: scale(1);
      box-shadow: 0 0 0 1px rgba(45,212,160,.45), 0 0 4px rgba(250,204,21,.25);
    }}
    50% {{
      transform: scale(1.06);
      box-shadow: 0 0 0 1px rgba(45,212,160,.55), 0 0 9px rgba(250,204,21,.45);
    }}
  }}
  @keyframes lb-day-sheen {{
    0%,100% {{ transform: translateX(-35%) rotate(12deg); opacity: .35; }}
    50% {{ transform: translateX(35%) rotate(12deg); opacity: .85; }}
  }}
  @keyframes lb-night-glow {{
    0%,100% {{
      transform: scale(1);
      box-shadow: 0 0 0 1px rgba(34,211,238,.4), 0 0 5px rgba(34,211,238,.25);
    }}
    50% {{
      transform: scale(1.06);
      box-shadow: 0 0 0 1px rgba(34,211,238,.55), 0 0 11px rgba(34,211,238,.45);
    }}
  }}
  @keyframes lb-night-sheen {{
    0%,100% {{ transform: translateX(-40%) rotate(10deg); opacity: .22; }}
    50% {{ transform: translateX(40%) rotate(10deg); opacity: .65; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .term-feed .mint-coin, .term-feed .mint-coin::after {{ animation: none !important; }}
  }}
  .pill {{
    display: inline-flex; align-items: center; padding: .16rem .55rem; border-radius: 999px;
    font-size: .72rem; font-weight: 800; color: #fff; white-space: nowrap;
    letter-spacing: .02em;
  }}
  /* Mined = soft sky; Received = green; Sent = red; Transfer = teal */
  .pill.minted {{
    background: #e0f2fe; color: #0369a1;
    box-shadow: 0 0 0 1px rgba(2,132,199,.22);
  }}
  .pill.xfer {{ background: #0f766e; box-shadow: 0 0 0 1px rgba(15,118,110,.22); }}
  .pill.recv {{ background: #16a34a; box-shadow: 0 0 0 1px rgba(22,163,74,.25); }}
  .pill.sent {{ background: #dc2626; box-shadow: 0 0 0 1px rgba(220,38,38,.28); }}
  [data-theme="dark"] .pill.minted {{
    background: rgba(56,189,248,.18); color: #7dd3fc;
    box-shadow: 0 0 0 1px rgba(56,189,248,.28);
  }}
  .alert {{
    border: 1px solid #fcd34d; background: #fffbeb; color: #92400e;
    border-radius: 12px; padding: .85rem 1rem; margin: 0 0 1rem; font-size: .92rem;
  }}
  .alert strong {{ color: #78350f; }}
  .term {{
    background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.1rem; margin: 0 0 1rem; font-family: var(--mono); font-size: .86rem;
  }}
  .term-head {{ display: flex; flex-wrap: wrap; gap: .75rem; align-items: baseline; justify-content: space-between; margin-bottom: .75rem; }}
  .term-head h1 {{ margin: 0; color: var(--text); font-family: var(--sans); font-size: 1.05rem; }}
  .term-live {{ color: #16a34a; font-weight: 700; font-size: .78rem; letter-spacing: .04em; }}
  .term-live::before {{
    content: ""; display: inline-block; width: .55rem; height: .55rem; border-radius: 50%;
    background: #22c55e; margin-right: .4rem; vertical-align: middle;
    animation: pulse-dot 1.6s ease-out infinite;
  }}
  @keyframes pulse-dot {{
    0% {{ box-shadow: 0 0 0 0 rgba(34,197,94,.45); }}
    70% {{ box-shadow: 0 0 0 8px rgba(34,197,94,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(34,197,94,0); }}
  }}
  .term-feed {{ display: flex; flex-direction: column; gap: .15rem; }}
  .found {{ border-top: 1px solid var(--line); padding-top: .65rem; }}
  .found:first-child {{ border-top: 0; padding-top: 0; }}
  .found .title {{ color: #0ea5e9; font-weight: 800; }}
  .found .k {{ color: #9ca3af; }}
  .found .hash, .found .hash a {{
    color: #ca8a04; text-decoration: none;
    font-size: .76rem; letter-spacing: -0.02em;
    white-space: nowrap; word-break: normal;
  }}
  .found .hash a:hover {{ color: #a16207; text-decoration: underline; }}
  .found .reward {{ color: #0ea5e9; font-weight: 700; }}
  [data-theme="dark"] .found .title,
  [data-theme="dark"] .found .reward {{ color: #7dd3fc; }}
  table .reward, .kv .reward, .stat .reward {{ color: #16a34a; font-weight: 700; }}
  /* Terminal-style accents (light panel, same roles as Desktop mine log). */
  .found .h, .found .h a {{ color: #0891b2; font-weight: 700; text-decoration: none; }}
  .found .h a:hover {{ text-decoration: underline; }}
  .found .val {{ color: var(--val); }}
  .found .v-time {{ color: #0891b2; }}
  .found .v-size {{ color: #6b7280; }}
  .found .v-diff {{ color: #a21caf; }}
  .found .v-hps {{ color: #0d9488; font-weight: 700; }}
  .found .v-utc {{ color: #6b7280; }}
  .found .cyan, .found .cyan a {{ color: #0891b2; text-decoration: none; }}
  .found .cyan a:hover {{ text-decoration: underline; }}
  .found a {{ color: inherit; }}
  .found .rule {{ color: var(--line); margin-top: .35rem; }}
  table a, table a.mono, .row-ico a {{ color: var(--tbl-link); }}
  table a:hover, table a.mono:hover, .row-ico a:hover {{ color: var(--invert); }}
  .kpi .val a {{ color: var(--tbl-link); }}
  .kpi .val a:hover {{ color: var(--invert); }}
  .mono {{
    font-family: var(--mono); font-size: .72rem;
    overflow-wrap: anywhere; word-break: break-word;
  }}
  .nowrap {{ white-space: nowrap; }}
  th.num, td.num {{ text-align: center; white-space: nowrap; }}
  th.num-conf, td.num-conf {{
    text-align: center; width: 1%; max-width: 3.25rem; white-space: nowrap;
    padding-left: .25rem; padding-right: .25rem;
  }}
  th.hash-col, td.hash-col {{
    width: auto; min-width: 0; max-width: none;
  }}
  td.hash-col .copy-btn {{ flex-shrink: 0; }}
  .muted {{ color: var(--muted); }}
  .err {{ color: #b42318; }}
  .ok {{ color: #0f766e; font-weight: 650; }}
  .kv {{
    display: grid; grid-template-columns: minmax(96px, 150px) minmax(0, 1fr);
    gap: .4rem .85rem; font-size: .93rem; max-width: 100%;
  }}
  .kv > div {{ min-width: 0; overflow-wrap: anywhere; word-break: break-word; }}
  .kv div:nth-child(odd) {{ color: var(--muted); }}
  .stats {{
    display: grid; gap: .55rem;
    grid-template-columns: repeat(auto-fit, minmax(min(132px, 100%), 1fr));
    container-type: inline-size; container-name: stats;
    max-width: 100%; min-width: 0;
  }}
  .stat {{
    background: var(--soft); border: 1px solid var(--line); border-radius: 10px;
    padding: .7rem .75rem; min-width: 0;
  }}
  .stat .lbl {{
    color: var(--muted); font-size: .68rem; font-weight: 650;
    text-transform: uppercase; letter-spacing: .04em; line-height: 1.2;
  }}
  .stat .val {{
    font-size: 1.02rem; font-weight: 750; margin-top: .2rem; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums; line-height: 1.25; word-break: break-word;
  }}
  .stat .val .unit {{
    font-size: .72em; font-weight: 600; color: var(--muted); margin-left: .2rem;
  }}
  /* Dense when many tiles share one wide row */
  .stats.stats-compact {{
    grid-template-columns: repeat(auto-fit, minmax(min(108px, 100%), 1fr));
    gap: .45rem;
  }}
  .stats.stats-compact .stat {{ padding: .5rem .55rem; border-radius: 8px; }}
  .stats.stats-compact .stat .lbl {{ font-size: .58rem; letter-spacing: .03em; }}
  .stats.stats-compact .stat .val {{ font-size: .84rem; margin-top: .12rem; }}
  .stats.stats-compact .stat .val .unit {{ font-size: .68em; }}
  .stats.stats-primary {{
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    margin-bottom: .55rem;
  }}
  .stats.stats-primary .stat {{
    padding: .8rem .85rem; border-color: color-mix(in srgb, var(--line) 70%, #16a34a 30%);
  }}
  .stats.stats-primary .stat .val {{ font-size: 1.2rem; }}
  .stats.stats-primary .stat.s-fee .val {{ color: #b45309; }}
  [data-theme="dark"] .stats.stats-primary .stat.s-fee .val {{ color: #fbbf24; }}
  @container stats (min-width: 980px) {{
    .stats.stats-compact .stat .lbl {{ font-size: .55rem; }}
    .stats.stats-compact .stat .val {{ font-size: .78rem; }}
  }}
  details.io-more {{ margin-top: .35rem; }}
  details.io-more > summary {{
    cursor: pointer; color: var(--muted); font-size: .85rem; font-weight: 650;
    list-style: none; user-select: none; padding: .35rem 0;
  }}
  details.io-more > summary::-webkit-details-marker {{ display: none; }}
  details.io-more > summary:hover {{ color: var(--text); }}

  .pill-spent, .pill-unspent {{
    display: inline-block; padding: .12rem .45rem; border-radius: 999px;
    font-size: .68rem; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
    vertical-align: middle; white-space: nowrap;
  }}
  .pill-unspent {{ background: #dcfce7; color: #166534; }}
  .pill-spent {{ background: #e5e7eb; color: #4b5563; }}
  [data-theme="dark"] .pill-unspent {{ background: rgba(22,163,74,.22); color: #86efac; }}
  [data-theme="dark"] .pill-spent {{ background: rgba(148,163,184,.18); color: #94a3b8; }}
  .io-row.focus-addr, li.focus-addr {{
    background: color-mix(in srgb, #16a34a 12%, transparent);
    box-shadow: inset 3px 0 0 #16a34a;
    border-radius: 8px; padding-left: .45rem !important;
  }}
  [data-theme="dark"] .io-row.focus-addr, [data-theme="dark"] li.focus-addr {{
    background: color-mix(in srgb, #16a34a 18%, transparent);
  }}
  /* Fixed overlay — never changes document flow (no CLS / page jump on scroll). */
  .tx-sticky {{
    position: fixed; left: 1.5rem; right: 1.5rem; top: 4.35rem; z-index: 18;
    display: flex; flex-wrap: wrap; gap: .4rem .65rem; align-items: center;
    padding: .45rem .7rem; margin: 0; max-width: calc(100vw - 3rem);
    background: color-mix(in srgb, var(--panel) 94%, transparent);
    backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
    border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 6px 18px rgba(15,23,42,.08);
    opacity: 0; pointer-events: none; visibility: hidden;
    transform: translateY(-6px);
    transition: opacity .15s ease, transform .15s ease, visibility .15s;
  }}
  .tx-sticky.is-on {{
    opacity: 1; pointer-events: auto; visibility: visible; transform: translateY(0);
  }}
  [data-theme="dark"] .tx-sticky {{ box-shadow: 0 6px 18px rgba(0,0,0,.35); }}
  .tx-sticky .tx-sticky-id {{ min-width: 0; flex: 1 1 160px; overflow: hidden; }}
  .tx-sticky .tx-sticky-id .mono {{
    display: inline-block; max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom;
  }}
  .tx-sticky .tx-sticky-meta {{
    display: flex; flex-wrap: wrap; gap: .3rem .55rem; align-items: center; font-size: .8rem;
  }}
  .tx-sticky .tx-sticky-meta .badge {{ font-size: .68rem; }}
  @media (max-width: 720px) {{
    .tx-sticky {{ left: 1rem; right: 1rem; top: 4.1rem; max-width: calc(100vw - 2rem); }}
  }}
  .flow-bars {{ display: grid; gap: .45rem; margin: .85rem 0 0; }}
  .flow-row {{ display: grid; grid-template-columns: 72px 1fr auto; gap: .5rem; align-items: center; font-size: .85rem; }}
  .flow-row .flow-lbl {{ color: var(--muted); font-weight: 650; }}
  .flow-row .flow-track {{ height: 8px; background: var(--soft); border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }}
  .flow-row .flow-fill {{ height: 100%; border-radius: 999px; }}
  .flow-row.f-recv .flow-fill {{ background: #16a34a; }}
  .flow-row.f-sent .flow-fill {{ background: #dc2626; }}
  .flow-row.f-fee .flow-fill {{ background: #b45309; }}
  .flow-row.f-bal .flow-fill {{ background: #2563eb; }}
  .flow-row .flow-amt {{ font-variant-numeric: tabular-nums; font-weight: 650; white-space: nowrap; }}
  .bal-spark {{ margin-top: .85rem; }}
  .bal-spark .spark {{ max-width: 100%; width: 100%; height: 56px; }}
  .pager {{ display: flex; flex-wrap: wrap; gap: .65rem; align-items: center; margin: .75rem 0 0; }}
  .pager a {{
    padding: .35rem .75rem; border-radius: 8px; border: 1px solid var(--line);
    background: var(--soft); font-weight: 650; font-size: .88rem; color: var(--text); text-decoration: none;
  }}
  .pager a:hover {{ background: var(--hover-bg); border-color: #9ca3af; text-decoration: none; }}
  .badge {{
    display: inline-block; padding: .18rem .5rem; border-radius: 999px; background: var(--text); color: var(--bg);
    font-size: .75rem; font-weight: 700; vertical-align: middle;
  }}
  .badge.warn {{ background: #fef3c7; color: #92400e; }}
  ul.plain {{ list-style: none; padding: 0; margin: 0; }}
  ul.plain li {{ padding: .55rem 0; border-bottom: 1px solid var(--line); }}
  ul.plain li.io-row {{
    display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .35rem .85rem;
    align-items: start;
  }}
  ul.plain li.io-row .io-main {{ min-width: 0; word-break: break-all; }}
  ul.plain li.io-row .io-amt {{ white-space: nowrap; text-align: right; font-variant-numeric: tabular-nums; }}
  ul.plain li.io-row .io-meta {{ grid-column: 1 / -1; font-size: .8rem; }}
  @media (max-width: 720px) {{
    ul.plain li.io-row {{ grid-template-columns: 1fr; }}
    ul.plain li.io-row .io-amt {{ text-align: left; }}
  }}
  footer.site {{ width: 100%; margin: 0; padding: 0 1.5rem 2rem; color: var(--muted); font-size: .82rem; }}
  @media (max-width: 720px) {{
    .kv {{ grid-template-columns: 1fr; }}
    header.top {{ padding: .85rem 1rem; }}
    main {{ padding: 1rem; }}
    .logo {{ --logo-size: 44px; }}
    header nav {{ gap: .3rem .7rem; }}
    .blocks-row .term {{ height: 22rem; min-height: 22rem; max-height: 22rem; }}
  }}
  @media (max-width: 520px) {{
    .kpi {{ grid-template-columns: 1fr 1fr; }}
    .header-inner {{ flex-direction: column; align-items: stretch; }}
    .search-row {{ width: 100%; }}
    form.search {{ width: auto; flex: 1 1 auto; }}
    .kv {{ grid-template-columns: minmax(96px, 40%) 1fr; }}
    .card {{ padding: .85rem .9rem; }}
  }}
  @media (max-width: 380px) {{
    .kpi {{ grid-template-columns: 1fr; }}
  }}

  .copy-wrap {{
    display: inline-flex; align-items: center; gap: .35rem;
    max-width: 100%; min-width: 0; flex-wrap: wrap;
  }}
  .copy-wrap > a.mono, .copy-wrap > span.mono {{
    min-width: 0; max-width: 100%; overflow-wrap: anywhere; word-break: break-word;
  }}
  .copy-btn {{
    border: 1px solid var(--line); background: var(--soft); color: var(--muted);
    border-radius: 6px; padding: .05rem .35rem; cursor: pointer; font-size: .7rem; line-height: 1.2;
  }}
  .copy-btn:hover {{ color: var(--text); background: var(--hover-bg); }}
  .copy-btn.ok {{ color: #16a34a; border-color: #86efac; }}
  .spark {{ display: block; width: 100%; max-width: 320px; height: 48px; color: var(--text); }}
  [data-theme="dark"] .spark {{ color: #e7eaee; }}
  .meta-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .75rem; margin: 0 0 1rem; }}
  .mini-stat .val {{ font-size: 1.05rem; }}
  table .mono {{ font-size: .72rem; }}
  .hex-pre {{
    margin: 0; padding: .85rem 1rem; border-radius: 10px; border: 1px solid var(--line);
    background: var(--soft); font-family: var(--mono); font-size: .68rem; line-height: 1.45;
    white-space: pre-wrap; word-break: break-all; max-height: 28rem; overflow: auto;
  }}
  .subtabs {{ display: flex; flex-wrap: wrap; gap: .45rem; margin: 0 0 .75rem; }}
  .subtab {{
    border: 1px solid var(--line); background: var(--soft); color: var(--muted);
    border-radius: 8px; padding: .35rem .75rem; font-weight: 650; font-size: .85rem; cursor: pointer;
  }}
  .subtab.on {{ background: var(--text); color: var(--bg); border-color: var(--text); }}
  .qr-row {{ display: flex; flex-wrap: wrap; gap: 1.25rem; align-items: flex-start; margin-top: 1rem; }}
  .qr-box {{
    flex: 0 0 auto; padding: .65rem; border: 1px solid var(--line); border-radius: 12px; background: #fff;
  }}
  .qr-box svg {{ display: block; width: 148px; height: 148px; }}
  .link-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .65rem; margin: 0;
  }}
  .link-grid a {{
    display: block; padding: .65rem .85rem; border-radius: 10px; border: 1px solid var(--line);
    background: var(--soft); font-weight: 650; text-decoration: none;
  }}
  .link-grid a:hover {{ background: var(--hover-bg); border-color: #9ca3af; text-decoration: none; }}
  [data-theme="dark"] .alert {{
    background: #2a2208; border-color: #78350f; color: #fcd34d;
  }}
  [data-theme="dark"] .alert strong {{ color: #fde68a; }}
  [data-theme="dark"] .badge.warn {{ background: #4a3c0a; color: #fcd34d; }}
  /* QR codes stay on a fixed white tile (scanner contrast) regardless of theme. */
</style>
</head>
<body>
<header class="top">
  <div class="header-inner">
    <div class="brand-row">
      <a href="/" aria-label="MHCOIN home">{_logo_html()}</a>
      <div class="brand-text">
        <p class="eyebrow">Independent Proof-of-Work · MHC</p>
        <div class="title"><a href="/">MHCOIN Explorer</a></div>
        <div class="meta" id="hdrMeta">Live mainnet · read-only{_esc(tip_bit)}</div>
        <nav>
          <a href="/">Home</a>
          <a href="/blocks">Blocks</a>
          <a href="/transactions">Transactions</a>
          <a href="/richlist">Rich list</a>
          <a href="/stats">Stats</a>
          <a href="/charts">Charts</a>
          <a href="/mempool">Mempool</a>
          <a href="/supply">Supply</a>
          <a href="/orphans">Orphans</a>
          <a href="/block/0">Genesis</a>{pool_nav}
        </nav>
      </div>
    </div>
    <div class="search-row">
      <form class="search" method="get" action="/search">
        <input name="q" placeholder="height · block / txid · mhc1…" autocomplete="off"/>
        <button type="submit">Search</button>
      </form>
      <button type="button" id="themeToggle" class="theme-toggle" aria-label="Toggle day / night" title="Toggle day / night (PoW coin)" aria-pressed="false">
        <span class="theme-coin" aria-hidden="true">
          <span class="tc-ring"></span>
          <span class="tc-ring2"></span>
          <span class="tc-core"><svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/></svg></span>
        </span>
      </button>
    </div>
  </div>
</header>
<main>
{body}
</main>
<footer class="site">MHCOIN · HASH256 PoW · target 10m · read-only explorer</footer>

<script>
(function(){{
  // Light/dark toggle — persisted in localStorage, defaults to system preference.
  var THEME_KEY = 'mhcoin-theme';
  var root = document.documentElement;
  var toggleBtn = document.getElementById('themeToggle');
  var metaColor = document.getElementById('metaThemeColor');
  function currentTheme() {{
    return root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  }}
  function applyTheme(theme) {{
    if (theme === 'dark') root.setAttribute('data-theme', 'dark');
    else root.removeAttribute('data-theme');
    if (toggleBtn) {{
      var night = theme === 'dark';
      toggleBtn.setAttribute('aria-pressed', night ? 'true' : 'false');
      toggleBtn.setAttribute('title', night ? 'Night mine · switch to day' : 'Day mine · switch to night');
      toggleBtn.setAttribute('aria-label', night ? 'Switch to day theme' : 'Switch to night theme');
    }}
    if (metaColor) metaColor.setAttribute('content', theme === 'dark' ? '#0b0d10' : '#ffffff');
  }}
  applyTheme(currentTheme());
  if (toggleBtn) {{
    toggleBtn.addEventListener('click', function () {{
      var next = currentTheme() === 'dark' ? 'light' : 'dark';
      toggleBtn.classList.remove('tc-found');
      void toggleBtn.offsetWidth;
      toggleBtn.classList.add('tc-found');
      setTimeout(function() {{ toggleBtn.classList.remove('tc-found'); }}, 750);
      applyTheme(next);
      try {{ localStorage.setItem(THEME_KEY, next); }} catch (e) {{}}
    }});
  }}
  function flash(btn, mark) {{
    btn.classList.add('ok');
    btn.textContent = mark;
    setTimeout(function() {{ btn.classList.remove('ok'); btn.textContent = '⎘'; }}, 1100);
  }}
  function execCopy(text) {{
    // Must stay synchronous inside the click handler (user gesture).
    // navigator.clipboard is unavailable on plain http://192.168.x.x.
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.setAttribute('aria-hidden', 'true');
    // Keep in-viewport: some browsers reject off-screen execCommand('copy').
    ta.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;padding:0;margin:0;border:0;outline:none;box-shadow:none;background:transparent;opacity:0;z-index:-1;';
    document.body.appendChild(ta);
    ta.focus({{ preventScroll: true }});
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = false;
    try {{ ok = document.execCommand('copy'); }} catch (err) {{ ok = false; }}
    document.body.removeChild(ta);
    if (ok) return true;
    // Selection API fallback
    var span = document.createElement('span');
    span.textContent = text;
    span.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none;white-space:pre;';
    document.body.appendChild(span);
    var range = document.createRange();
    range.selectNodeContents(span);
    var sel = window.getSelection();
    if (sel) {{
      sel.removeAllRanges();
      sel.addRange(range);
      try {{ ok = document.execCommand('copy'); }} catch (err2) {{ ok = false; }}
      sel.removeAllRanges();
    }}
    document.body.removeChild(span);
    return !!ok;
  }}
  function showManualCopy(text, btn) {{
    var old = document.getElementById('mh-copy-fallback');
    if (old) old.remove();
    var box = document.createElement('div');
    box.id = 'mh-copy-fallback';
    box.style.cssText = 'position:fixed;z-index:99999;left:50%;top:20%;transform:translateX(-50%);background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:12px 14px;box-shadow:0 10px 30px rgba(0,0,0,.18);max-width:min(92vw,520px);font:13px/1.4 ui-sans-serif,system-ui,sans-serif;';
    box.innerHTML = '<div style="margin:0 0 8px;color:#64748b">Press Ctrl+C / ⌘C to copy</div>';
    var inp = document.createElement('input');
    inp.type = 'text';
    inp.value = text;
    inp.readOnly = true;
    inp.style.cssText = 'width:100%;box-sizing:border-box;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;padding:8px;border:1px solid #94a3b8;border-radius:6px;';
    box.appendChild(inp);
    document.body.appendChild(box);
    inp.focus();
    inp.select();
    function close() {{ if (box.parentNode) box.parentNode.removeChild(box); }}
    box.addEventListener('keydown', function(ev) {{ if (ev.key === 'Escape') close(); }});
    setTimeout(close, 8000);
    document.addEventListener('click', function once(ev) {{
      if (!box.contains(ev.target) && ev.target !== btn) {{
        close();
        document.removeEventListener('click', once, true);
      }}
    }}, true);
  }}
  function markDone(btn) {{ flash(btn, '✓'); }}
  document.addEventListener('click', function(e) {{
    var b = e.target && e.target.closest && e.target.closest('.copy-btn');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    var t = b.getAttribute('data-copy') || '';
    if (!t) return;
    // Prefer Clipboard API only in secure contexts (https / localhost).
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(t).then(function() {{ markDone(b); }}).catch(function() {{
        if (execCopy(t)) markDone(b);
        else {{ flash(b, '!'); showManualCopy(t, b); }}
      }});
      return;
    }}
    if (execCopy(t)) markDone(b);
    else {{ flash(b, '!'); showManualCopy(t, b); }}
  }}, true);
}})();
</script>
</body>
</html>
"""
    return doc.encode("utf-8")



def _peer_public_id(addr: str) -> str:
    """Stable anonymous peer label — never expose raw IP:port publicly."""
    digest = hashlib.sha256(addr.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return f"peer-{digest}"


class ExplorerApp:
    def __init__(self, data_dir: Path, *, hrp: str = "mhc"):
        self.data_dir = Path(data_dir)
        self.hrp = hrp
        self._home_cache: dict[str, Any] | None = None
        self._home_cache_at: float = 0.0
        self._home_lock = threading.Lock()
        self._rich_cache: dict[str, Any] | None = None
        self._rich_cache_at: float = 0.0
        self._rich_lock = threading.Lock()
        self._rate: dict[str, deque[float]] = defaultdict(deque)
        self._rate_lock = threading.Lock()

    def chain(self) -> ReadOnlyChain:
        c = ReadOnlyChain(self.data_dir)
        c.refresh_tip()
        return c

    def allow_request(self, client: str, *, bucket: str, limit: int) -> bool:
        """Token window per client+bucket. Normal page views are not rate-limited."""
        now = time.time()
        key = f"{client or 'unknown'}:{bucket}"
        with self._rate_lock:
            q = self._rate[key]
            while q and now - q[0] > _RATE_LIMIT_WINDOW_SEC:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def node_status(self) -> dict[str, Any]:
        path = self.data_dir / "node_status.json"
        out: dict[str, Any] = {
            "peer_count": None,
            "height": None,
            "sync_state": None,
            "updated": None,
            "peers": [],
            "listen": None,
        }
        if not path.is_file():
            return out
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out["peer_count"] = int(raw.get("peer_count") or 0)
            out["height"] = raw.get("height")
            # Public explorer: never expose listen bind / raw endpoints.
            out["listen"] = None
            sync = raw.get("sync") or {}
            out["sync_state"] = sync.get("state")
            out["updated"] = int(path.stat().st_mtime)
            out["updated_age"] = D.format_age(int(path.stat().st_mtime), now=int(time.time()))
            peers_out: list[dict[str, Any]] = []
            for p in raw.get("peers") or []:
                addr = str(p.get("addr") or "")
                peer_id = _peer_public_id(addr or str(p.get("agent") or id(p)))
                mining = bool(p.get("mining"))
                hps = int(p.get("hps") or 0) if mining else 0
                peers_out.append(
                    {
                        # Anonymized public fields only (same table shape).
                        "id": peer_id,
                        "ip": peer_id,
                        "port": "",
                        "addr": peer_id,
                        "inbound": bool(p.get("inbound")),
                        "direction": "in" if p.get("inbound") else "out",
                        "state": p.get("state") or "—",
                        "height": p.get("height"),
                        "agent": p.get("agent") or "—",
                        "network": p.get("network") or "",
                        "protocol": p.get("protocol"),
                        "mining": mining,
                        "hps": hps,
                        "hashrate": D.format_hps(float(hps)) if mining and hps > 0 else None,
                        "status_age": p.get("status_age"),
                    }
                )
            reported_hps = sum(int(p.get("hps") or 0) for p in peers_out if p.get("mining"))
            reported_n = sum(1 for p in peers_out if p.get("mining"))
            out["reported_hashrate_hps"] = int(reported_hps)
            out["reported_hashrate"] = D.format_hps(float(reported_hps)) if reported_hps > 0 else None
            out["reported_miners"] = int(reported_n)
            peers_out.sort(key=lambda x: (0 if x["inbound"] else 1, x.get("id") or ""))
            out["peers"] = peers_out
            miner = raw.get("miner") or {}
            if miner.get("hashrate_hps"):
                out["local_miner_hps"] = miner.get("hashrate_hps")
                out["local_miner_hashrate"] = D.format_hps(float(miner["hashrate_hps"]))
        except Exception:
            pass
        return out

    def home(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._home_lock:
            if (
                not force
                and self._home_cache is not None
                and (now - self._home_cache_at) < _HOME_CACHE_TTL_SEC
            ):
                return self._home_cache
        c = self.chain()
        try:
            data = D.chain_stats(c, hrp=self.hrp)
            data["node"] = self.node_status()
            data["mempool"] = D.load_mempool(self.data_dir, hrp=self.hrp, chain=c)
        finally:
            c.close()
        with self._home_lock:
            self._home_cache = data
            self._home_cache_at = time.time()
        return data

    def tip_status(self) -> dict[str, Any]:
        """Lightweight poll payload for the home live tick."""
        home = self.home()
        node = home.get("node") or {}
        return {
            "tip_height": home.get("tip_height"),
            "tip_hash": home.get("tip_hash"),
            "tip_age": home.get("tip_age"),
            "tip_age_seconds": home.get("tip_age_seconds"),
            "tip_difficulty_display": home.get("tip_difficulty_display"),
            "network_hashrate": home.get("network_hashrate"),
            "network_hashrate_label": home.get("network_hashrate_label"),
            "next_block_eta": home.get("next_block_eta"),
            "next_block_eta_seconds": home.get("next_block_eta_seconds"),
            "next_block_overdue": home.get("next_block_overdue"),
            "next_block_hint": home.get("next_block_hint"),
            "minted_mhc": home.get("minted_mhc"),
            "total_blocks": home.get("total_blocks"),
            "total_transactions": home.get("total_transactions"),
            "transfer_transactions": home.get("transfer_transactions"),
            "avg_block_interval_seconds": home.get("avg_block_interval_seconds"),
            "node": {
                "peer_count": node.get("peer_count"),
                "sync_state": node.get("sync_state"),
                "reported_hashrate": node.get("reported_hashrate"),
                "reported_hashrate_hps": node.get("reported_hashrate_hps"),
                "reported_miners": node.get("reported_miners"),
                "peers": node.get("peers") or [],
                "updated_age": node.get("updated_age"),
            },
            "mempool_count": (home.get("mempool") or {}).get("count"),
            "live_finds": home.get("live_finds"),
            "cached": True,
        }

    def health(self) -> dict[str, Any]:
        chain_path = self.data_dir / "chain.sqlite"
        tip = None
        err = None
        try:
            c = self.chain()
            try:
                tip = c.height
            finally:
                c.close()
        except Exception as exc:
            err = str(exc)
        node = self.node_status()
        ok = tip is not None and chain_path.is_file() and err is None
        return {
            "ok": ok,
            "service": "mhcoin-explorer",
            "tip_height": tip,
            "datadir": str(self.data_dir),
            "chain_sqlite": chain_path.is_file(),
            "node_updated_age": node.get("updated_age"),
            "peer_count": node.get("peer_count"),
            "error": err,
            "time": int(time.time()),
        }

    def blocks(self, page: int = 1) -> dict[str, Any]:
        c = self.chain()
        try:
            return D.blocks_page(c, page=page, hrp=self.hrp)
        finally:
            c.close()

    def transactions(self, page: int = 1, *, transfers_only: bool = False) -> dict[str, Any]:
        c = self.chain()
        try:
            return D.transactions_page(
                c, page=page, hrp=self.hrp, transfers_only=transfers_only
            )
        finally:
            c.close()

    def block(self, key: str) -> dict[str, Any] | None:
        c = self.chain()
        try:
            block = None
            height = None
            if key.isdigit():
                height = int(key)
                block = c.get_block_by_height(height)
            elif HEX64.match(key):
                bh = bytes.fromhex(key)
                block = c.get_block_by_hash(bh)
                height = c.get_height_of_hash(bh)
            if block is None:
                return None
            return D.summarize_block(
                c, block, height=height, tip=c.height, hrp=self.hrp, include_tx_summaries=True
            )
        finally:
            c.close()

    def tx(self, txid: str) -> dict[str, Any] | None:
        if not HEX64.match(txid):
            return None
        c = self.chain()
        try:
            return D.find_tx(
                c, txid.lower(), hrp=self.hrp, data_dir=self.data_dir
            )
        finally:
            c.close()

    def address(
        self, addr: str, *, page: int = 1, per_page: int = D.ADDRESS_PER_PAGE
    ) -> dict[str, Any] | None:
        if not validate_address(addr, hrp=self.hrp):
            return None
        c = self.chain()
        try:
            return D.address_history(
                c, addr, hrp=self.hrp, page=page, per_page=per_page
            )
        finally:
            c.close()

    def richlist(self, *, limit: int = 100, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._rich_lock:
            if (
                not force
                and self._rich_cache is not None
                and (now - self._rich_cache_at) < _RICH_CACHE_TTL_SEC
            ):
                return self._rich_cache
        c = self.chain()
        try:
            data = D.rich_list(self.data_dir, chain=c, hrp=self.hrp, limit=limit)
        finally:
            c.close()
        with self._rich_lock:
            self._rich_cache = data
            self._rich_cache_at = time.time()
        return data

    def stats(self) -> dict[str, Any]:
        return self.home()

    def charts(self) -> dict[str, Any]:
        home = self.home()
        return {
            "tip_height": home.get("tip_height"),
            "charts": home.get("charts") or {},
            "halving": home.get("halving") or {},
            "avg_block_interval_seconds": home.get("avg_block_interval_seconds"),
            "network_hashrate": home.get("network_hashrate"),
            "network_hashrate_window": home.get("network_hashrate_window"),
            "network_hashrate_short": home.get("network_hashrate_short"),
            "target_block_time_seconds": home.get("target_block_time_seconds"),
        }

    def mempool(self) -> dict[str, Any]:
        c = self.chain()
        try:
            return D.load_mempool(self.data_dir, hrp=self.hrp, chain=c)
        finally:
            c.close()

    def supply(self) -> dict[str, Any]:
        home = self.home()
        return D.supply_info(
            home.get("tip_height") or 0,
            home.get("minted_sats") or 0,
            avg_interval_seconds=home.get("avg_block_interval_seconds"),
        )

    def orphans(self, *, limit: int = 100) -> dict[str, Any]:
        c = self.chain()
        try:
            return D.orphan_blocks(c, hrp=self.hrp, limit=limit)
        finally:
            c.close()


def _pager(
    page: int,
    total_pages: int,
    base: str = "/blocks",
    *,
    chain_links: bool = True,
) -> str:
    sep = "&" if "?" in base else "?"
    prev_l = (
        f'<a href="{base}{sep}page={page - 1}">← newer</a>'
        if page > 1
        else '<span class="muted">← newer</span>'
    )
    next_l = (
        f'<a href="{base}{sep}page={page + 1}">older →</a>'
        if page < total_pages
        else '<span class="muted">older →</span>'
    )
    extras = ""
    if chain_links:
        extras = (
            f'<a href="{base}{sep}page={total_pages}">genesis page</a>'
            f'<a href="/block/0">block #0</a>'
        )
    return (
        f'<div class="pager">{prev_l}'
        f'<span class="muted">page {page} / {total_pages}</span>'
        f"{next_l}{extras}</div>"
    )



def _short_hash(h: str | None, n: int = 10) -> str:
    if not h:
        return "—"
    h = str(h)
    if len(h) <= n * 2 + 1:
        return h
    return f"{h[:n]}…{h[-n:]}"


def _is_wallet_addr(text: str | None) -> bool:
    """Bech32 wallet addresses (mhc1… / mhct1…) — always show in full on the site."""
    if not text:
        return False
    s = str(text).strip().lower()
    return s.startswith("mhc1") or s.startswith("mhct1")


def _copyable(text: str | None, *, short: bool = True, href: str | None = None, css: str = "mono") -> str:
    if not text:
        return '<span class="muted">—</span>'
    # Wallet addresses are never abbreviated (Miner / From / To / Top miners).
    do_short = bool(short) and not _is_wallet_addr(text)
    shown = _short_hash(text) if do_short else text
    link = (
        f'<a class="{css}" href="{_esc(href)}" title="{_esc(text)}">{_esc(shown)}</a>'
        if href
        else f'<span class="{css}" title="{_esc(text)}">{_esc(shown)}</span>'
    )
    return (
        f'<span class="copy-wrap">{link}'
        f'<button type="button" class="copy-btn" data-copy="{_esc(text)}" title="Copy">⎘</button></span>'
    )


def _sparkline(values: list, *, width: int = 220, height: int = 48, stroke: str = "currentColor") -> str:
    if not values:
        return '<span class="muted">—</span>'
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return '<span class="muted">—</span>'
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    n = len(nums)
    pts = []
    for i, v in enumerate(nums):
        x = 0 if n == 1 else i * (width - 2) / (n - 1) + 1
        y = height - 2 - ((v - lo) / span) * (height - 4)
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true"><polyline fill="none" stroke="{stroke}" stroke-width="1.6" '
        f'points="{poly}"/></svg>'
    )


def _fmt_interval(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    nb = " "
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{nb}{s % 60}s"
    if s < 86_400:
        return f"{s // 3600}h{nb}{(s % 3600) // 60}m"
    return f"{s // 86400}d"


def _inferred_tip(height: Any, conf: Any) -> int | None:
    try:
        if height is None or conf is None:
            return None
        return int(height) + int(conf) - 1
    except (TypeError, ValueError):
        return None


def _fmt_size(n: Any) -> str:
    try:
        b = int(n)
    except (TypeError, ValueError):
        return "—"
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024 * 1024):.2f} MB"


def _blocks_table(blocks: list[dict[str, Any]]) -> str:
    rows = []
    for b in blocks:
        h = b.get("height")
        miner = b.get("miner")
        miner_html = (
            _copyable(miner, href=f"/address/{miner}")
            if miner
            else '<span class="muted">—</span>'
        )
        rows.append(
            "<tr>"
            f'<td class="num"><span class="row-ico">{_mint_mark(title="MHC Mined")}'
            f'<a href="/block/{h}"><strong>{h}</strong></a></span></td>'
            f'<td class="hash-col">{_copyable(b.get("hash"), short=False, href="/block/" + str(b.get("hash")))}</td>'
            f'<td class="num">{b.get("tx_count")}</td>'
            f'<td class="num"><strong class="reward">{_esc(b.get("reward_mhc") or "—")} MHC</strong></td>'
            f"<td>{_type_pill(True)}</td>"
            f'<td class="muted nowrap num">{_esc(_fmt_interval(b.get("interval_seconds")))}</td>'
            f'<td class="muted nowrap num">{_esc(b.get("age") or "—")}</td>'
            f"<td>{miner_html}</td>"
            f'<td class="muted num-conf">{_esc(b.get("confirmations"))}</td>'
            "</tr>"
        )
    return f"""
    <div class="table-wrap"><table class="data-table">
      <tr>
        <th class="num">Height</th><th class="hash-col">Hash</th><th class="num">Txs</th><th class="num">Reward</th><th>Type</th>
        <th class="num">Time</th><th class="num">Age</th><th>Miner</th><th class="num-conf" title="Confirmations">Conf</th>
      </tr>
      {"".join(rows)}
    </table></div>
    """


def _txs_table(txs: list[dict[str, Any]], *, empty: str = "No transactions yet.") -> str:
    if not txs:
        return f'<p class="muted">{_esc(empty)}</p>'
    rows = []
    for t in txs:
        cb = bool(t.get("coinbase"))
        mark = _mint_mark(title="MHC Mined" if cb else "Transfer")
        pill = _type_pill(cb)

        fr = t.get("from")
        to = t.get("to")
        if fr == "coinbase":
            fr_html = '<span class="muted">coinbase</span>'
        elif fr:
            fr_html = _copyable(fr, href=f"/address/{fr}")
        else:
            fr_html = '<span class="muted">—</span>'
        to_html = _copyable(to, href=f"/address/{to}") if to else '<span class="muted">—</span>'
        fee = t.get("fee_mhc")
        rows.append(
            "<tr>"
            f'<td class="hash-col"><span class="row-ico">{mark}'
            f'{_copyable(t.get("txid"), short=False, href="/tx/" + str(t.get("txid")))}</span></td>'
            f"<td>{pill}</td>"
            f'<td><a href="/block/{t.get("height")}">#{t.get("height")}</a></td>'
            f"<td>{fr_html}</td>"
            f"<td>{to_html}</td>"
            f'<td><strong class="reward">{_esc(t.get("amount_mhc") or t.get("output_value_mhc"))} MHC</strong></td>'
            f'<td class="muted">{_esc(fee if fee is not None else "—")}</td>'
            f'<td class="muted nowrap">{_esc(t.get("age") or "—")}</td>'
            f'<td class="muted num-conf">{_esc(t.get("confirmations"))}</td>'
            "</tr>"
        )
    return f"""
    <div class="table-wrap"><table class="data-table">
      <tr>
        <th class="hash-col">Txid</th><th>Type</th><th>Block</th><th>From</th><th>To</th>
        <th>Amount</th><th>Fee</th><th>Age</th><th class="num-conf">Confirmations</th>
      </tr>
      {"".join(rows)}
    </table></div>
    """


def _found_terminal(blocks: list[dict[str, Any]]) -> str:
    """Latest real tip blocks — no fake miner-session totals."""
    if not blocks:
        return '<p class="muted">Waiting for the next block…</p>'
    cards = []
    for b in blocks:
        h = b.get("height")
        dt = b.get("interval_seconds")
        # On-chain interval since previous tip (not miner PoW stopwatch).
        time_s = f"{dt:.2f}s" if isinstance(dt, (int, float)) else "—"
        bh = b.get("hash") or ""
        bits = b.get("bits") or ""
        diff = b.get("difficulty_display") or "—"
        if bits:
            diff = f"{diff} ({bits})"
        cards.append(
            "<div class='found' data-height='"
            + _esc(h)
            + "'>"
            "<div class='title'><span class='row-ico'>"
            + _mint_mark(title="MHC Mined")
            + "▸ MHC Mined</span></div>"
            "<div>  <span class='k'>height</span>     <span class='h'><a href='/block/"
            + _esc(h)
            + "'>#"
            + _esc(h)
            + "</a></span></div>"
            "<div>  <span class='k'>hash</span>       <span class='hash'><a href='/block/"
            + _esc(bh)
            + "'>"
            + _esc(bh)
            + "</a></span></div>"
            "<div>  <span class='k'>reward</span>     <span class='reward'>"
            + _esc(b.get("reward_mhc") or "—")
            + " MHC</span></div>"
            "<div>  <span class='k'>time</span>       <span class='val v-time'>"
            + _esc(time_s)
            + "</span></div>"
            "<div>  <span class='k'>size</span>       <span class='val v-size'>"
            + _esc(_fmt_size(b.get("size_bytes")))
            + "</span></div>"
            "<div>  <span class='k'>difficulty</span> <span class='val v-diff'>"
            + _esc(diff)
            + "</span></div>"
            "<div>  <span class='k'>h/s</span>        <span class='val v-hps'>"
            + _esc(b.get("implied_hashrate") or "—")
            + "</span></div>"
            "<div>  <span class='k'>utc</span>        <span class='val v-utc'>"
            + _esc(b.get("time_utc") or "—")
            + "</span></div>"
            "<div class='rule'>──────────────────────────────────────────────────────────────</div>"
            "</div>"
        )
    return f'<div class="term-feed" id="liveFinds">{"".join(cards)}</div>'


def _peers_panel(
    peers: list[dict[str, Any]] | None,
    *,
    peer_count: Any = None,
    total_hps: Any = None,
    total_hashrate: Any = None,
) -> str:
    rows: list[str] = []
    sum_hps = 0
    for p in peers or []:
        peer_lbl = p.get("id") or p.get("ip") or "—"
        direction = "in" if p.get("inbound") else "out"
        dir_lbl = "in" if direction == "in" else "out"
        height = p.get("height")
        height_s = "—" if height is None else f"#{height}"
        agent = p.get("agent") or "—"
        state = p.get("state") or "—"
        hr = p.get("hashrate") or ("—" if not p.get("mining") else "…")
        mine = "yes" if p.get("mining") else "no"
        if p.get("mining"):
            sum_hps += int(p.get("hps") or 0)
        rows.append(
            "<tr>"
            f'<td class="ip" title="{_esc(peer_lbl)}">{_esc(peer_lbl)}</td>'
            f'<td><span class="dir {direction}">{dir_lbl}</span></td>'
            f"<td>{_esc(height_s)}</td>"
            f"<td>{_esc(hr)}</td>"
            f"<td>{mine}</td>"
            f"<td>{_esc(agent)}</td>"
            f"<td>{_esc(state)}</td>"
            "</tr>"
        )
    n = peer_count if peer_count is not None else len(peers or [])
    total_s = total_hashrate or D.format_hps(float(sum_hps)) or "—"
    if total_hps is None:
        total_hps = sum_hps
    body = (
        f"""
    <div class="table-wrap peers-panel"><table>
      <thead><tr>
        <th>Peer</th><th>Dir</th><th>Height</th><th>H/s</th><th>Mining</th><th>Version</th><th>State</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table></div>
    <p class="peers-total" id="peersTotal">Total live: {_esc(total_s)} · {_esc(sum(1 for p in (peers or []) if p.get("mining")))} miners</p>
    """
        if rows
        else '<p class="muted" id="peersEmpty">No peers connected to the seed.</p>'
    )
    return (
        f'<section class="card peers-panel" id="connectedPeers">'
        f'<div class="card-head">'
        f"<h1>Connected peers</h1>"
        f'<span class="muted" style="font-size:.85rem" id="peersCount">{_esc(n)} online</span>'
        f"</div>"
        f'<p class="muted" style="margin:0 0 .55rem;font-size:.75rem">'
        f"Live H/s from each miner (STATUS). Total = sum of rows."
        f"</p>"
        f'<div id="peersTable">{body}</div>'
        f"</section>"
    )


def _render_home(data: dict[str, Any]) -> bytes:
    avg = data.get("avg_block_interval_seconds")
    avg_s = _fmt_interval(avg) if avg is not None else "—"
    node = data.get("node") or {}
    peers = node.get("peer_count")
    peers_s = "—" if peers is None else str(peers)
    hr_implied = data.get("network_hashrate") or "—"
    hr_win = data.get("network_hashrate_window")
    hr_short = data.get("network_hashrate_short")
    reported_hps = int(node.get("reported_hashrate_hps") or 0)
    reported_hr = node.get("reported_hashrate")
    reported_n = int(node.get("reported_miners") or 0)
    # KPI main = live miner sum when available; implied stays in the hint.
    if reported_hps > 0 and reported_hr:
        hr_lbl = "Hashrate (live)"
        hr = reported_hr
        hr_hint = (
            f"total {reported_n} miners · implied {hr_implied}"
            + (f" · obs {hr_win}" if hr_win else "")
            + (f" · recent {hr_short}" if hr_short else "")
        )
    else:
        hr_lbl = "Hashrate (implied)"
        hr = hr_implied
        hr_hint = (
            "live — · implied @10m"
            + (f" · obs {hr_win}" if hr_win else "")
            + (f" · recent {hr_short}" if hr_short else "")
        )
    halv = data.get("halving") or {}
    mp = data.get("mempool") or {}
    eta_hint = data.get("next_block_hint") or "—"
    overdue = bool(data.get("next_block_overdue"))
    eta_cls = "err" if overdue else "muted"
    peers_panel = _peers_panel(
        node.get("peers") or [],
        peer_count=peers,
        total_hps=reported_hps,
        total_hashrate=reported_hr,
    )
    body = f"""
    <div class="shell" id="explorerHome" data-tip="{_esc(data.get("tip_height"))}">
      <div class="kpi">
        <div class="stat k-height">
          <div class="lbl">Block height</div>
          <div class="val" id="kpiHeight"><a href="/block/{data.get("tip_height")}">{data.get("tip_height")}</a></div>
          <div class="hint" id="kpiAge">{_esc(data.get("tip_age") or "—")} ago</div>
        </div>
        <div class="stat k-hash">
          <div class="lbl" id="kpiHashLbl">{_esc(hr_lbl)}</div>
          <div class="val" id="kpiHashrate">{_esc(hr)}</div>
          <div class="hint" id="kpiHashHint">{_esc(hr_hint)}</div>
        </div>
        <div class="stat k-diff">
          <div class="lbl">Difficulty</div>
          <div class="val" id="kpiDiff">{_esc(data.get("tip_difficulty_display") or "—")}</div>
          <div class="hint" id="kpiBits">bits {_esc(data.get("tip_bits") or "—")}</div>
        </div>
        <div class="stat k-eta">
          <div class="lbl">Next block</div>
          <div class="val" id="kpiEta">{_esc(data.get("next_block_eta") if not overdue else "overdue")}</div>
          <div class="hint {eta_cls}" id="kpiEtaHint">{_esc(eta_hint)}</div>
        </div>
        <div class="stat k-peers">
          <div class="lbl">Peers</div>
          <div class="val" id="kpiPeers">{_esc(peers_s)}</div>
          <div class="hint" id="kpiSync">{_esc(node.get("sync_state") or "—")}</div>
        </div>
        <div class="stat k-blocks">
          <div class="lbl">Total blocks</div>
          <div class="val" id="kpiTotal"><a href="/blocks">{data.get("total_blocks")}</a></div>
          <div class="hint" id="kpiTotalHint">&nbsp;</div>
        </div>
        <div class="stat k-txs">
          <div class="lbl">Transactions</div>
          <div class="val" id="kpiTxs">{data.get("total_transactions")}</div>
          <div class="hint" id="kpiXfer">{data.get("transfer_transactions")} transfers</div>
        </div>
        <div class="stat k-mint">
          <div class="lbl">Minted supply</div>
          <div class="val" id="kpiMint">{_esc(data.get("minted_mhc"))}</div>
          <div class="hint">subsidy {_esc(halv.get("current_subsidy_mhc") or "50")} MHC</div>
        </div>
      </div>

      <div class="blocks-row">
        <div class="blocks-side">
          <section class="term">
            <div class="term-head">
              <h1>Live Blocks</h1>
              <span class="muted" style="font-size:.78rem">latest on chain · time = since previous block</span>
            </div>
            {_found_terminal(data.get("live_finds") or [])}
          </section>
          {peers_panel}
        </div>
        <section class="card latest-blocks">
          <div class="card-head">
            <h1>Latest blocks</h1>
            <a class="more" href="/blocks">View all →</a>
          </div>
          <div id="latestBlocks"
               data-page="1"
               data-per-page="100"
               data-total-blocks="{_esc(data.get("total_blocks") or 0)}">{_blocks_table((data.get("recent") or [])[:25])}</div>
          <div class="pager" id="latestBlocksPager"></div>
        </section>
      </div>

      <section class="card">
        <div class="card-head">
          <h1>Latest transactions</h1>
          <a class="more" href="/transactions">View all →</a>
        </div>
        <p class="muted" style="margin:0 0 .75rem;font-size:.85rem" id="txMeta">{data.get("transfer_transactions")} transfers · {data.get("coinbase_transactions")} mined · {data.get("total_transactions")} total</p>
        <div id="latestTxs"
             data-page="1"
             data-per-page="100"
             data-total-txs="{_esc(data.get("total_transactions") or 0)}">{_txs_table((data.get("recent_txs") or [])[:25])}</div>
        <div class="pager" id="latestTxsPager"></div>
      </section>

      <section class="card">
        <div class="card-head">
          <h1>Transfers</h1>
          <span class="muted" style="font-size:.85rem">sends between addresses</span>
        </div>
        <div id="xferOnly">{_txs_table(
            data.get("recent_transfers") or [],
            empty="No transfers yet — only mined rewards so far.",
        )}</div>
      </section>

      <section class="card">
        <div class="card-head">
          <h1>Network dashboards</h1>
          <span class="muted" style="font-size:.85rem">detailed views</span>
        </div>
        <div class="link-grid">
          <a href="/stats">Stats &amp; miners</a>
          <a href="/charts">Charts</a>
          <a href="/mempool">Mempool ({_esc(mp.get("count") or 0)})</a>
          <a href="/richlist">Rich list</a>
        </div>
        <p class="muted" style="margin:.85rem 0 0;font-size:.85rem">
          Avg interval {_esc(avg_s)} · halving in {_esc(halv.get("blocks_to_halving"))} blocks ·
          <a href="/charts">view charts →</a>
        </p>
      </section>
    </div>
    <script>
    (function () {{
      var lastTip = Number(document.getElementById('explorerHome').dataset.tip || -1);
      function esc(s) {{
        return String(s == null ? '' : s)
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;');
      }}

      function mintMark(title) {{
        return '<span class="mint" title="' + esc(title || '') + '">' +
          '<span class="mint-ring"></span><span class="mint-ring2"></span>' +
          '<span class="mint-coin">' +
          '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">' +
          '<path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>' +
          '</svg></span></span>';
      }}
      function shortHash(h) {{
        if (!h) return '—';
        h = String(h);
        if (h.length <= 21) return h;
        return h.slice(0,10) + '…' + h.slice(-10);
      }}
      function isWalletAddr(a) {{
        if (!a) return false;
        a = String(a).trim().toLowerCase();
        return a.indexOf('mhc1') === 0 || a.indexOf('mhct1') === 0;
      }}
      function copyable(text, href, full) {{
        if (!text) return '<span class="muted">—</span>';
        // Full wallet addresses everywhere; block hashes use full=true.
        var shown = (full || isWalletAddr(text)) ? String(text) : shortHash(text);
        var link = href
          ? '<a class="mono" href="' + href + '" title="' + esc(text) + '">' + esc(shown) + '</a>'
          : '<span class="mono" title="' + esc(text) + '">' + esc(shown) + '</span>';
        return '<span class="copy-wrap">' + link +
          '<button type="button" class="copy-btn" data-copy="' + esc(text) + '" title="Copy">⎘</button></span>';
      }}
      function shortAddr(a) {{
        // Kept for compatibility — wallets always returned in full.
        if (!a) return '—';
        return String(a);
      }}
      function fmtInterval(s) {{
        if (s == null) return '—';
        s = Number(s);
        var nb = '\u00a0';
        if (s < 60) return s + 's';
        if (s < 3600) return Math.floor(s/60) + 'm' + nb + (s%60) + 's';
        if (s < 86400) return Math.floor(s/3600) + 'h' + nb + Math.floor((s%3600)/60) + 'm';
        return Math.floor(s/86400) + 'd';
      }}
      function fmtSize(n) {{
        n = Number(n);
        if (!isFinite(n) || n < 0) return '—';
        if (n < 1024) return Math.round(n) + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / (1024 * 1024)).toFixed(2) + ' MB';
      }}
      function fitLiveBlocksTwoCards() {{
        // Height is CSS-driven (max-height on .term-feed) to avoid layout jump on refresh.
        return;
      }}
      function renderFinds(blocks) {{
        var feed = document.getElementById('liveFinds');
        if (!feed || !blocks || !blocks.length) return;
        // Skip DOM rewrite when tip list unchanged — prevents Live Blocks jump.
        var nextKey = blocks.map(function (b) {{ return String(b.height) + ':' + String(b.hash || ''); }}).join('|');
        if (feed.dataset.key === nextKey) return;
        feed.dataset.key = nextKey;
        feed.innerHTML = blocks.map(function (b, i) {{
          var dt = b.interval_seconds;
          var timeS = (typeof dt === 'number') ? (dt.toFixed(2) + 's') : '—';
          var diff = b.difficulty_display || '—';
          if (b.bits) diff = diff + ' (' + b.bits + ')';
          return (
            '<div class="found" data-height="' + esc(b.height) + '">' +
            '<div class="title"><span class="row-ico">' + mintMark('MHC Mined') + '▸ MHC Mined</span></div>' +
            '<div>  <span class="k">height</span>     <span class="h"><a href="/block/' + esc(b.height) + '">#' + esc(b.height) + '</a></span></div>' +
            '<div>  <span class="k">hash</span>       <span class="hash"><a href="/block/' + esc(b.hash) + '">' + esc(b.hash) + '</a></span></div>' +
            '<div>  <span class="k">reward</span>     <span class="reward">' + esc(b.reward_mhc || '—') + ' MHC</span></div>' +
            '<div>  <span class="k">time</span>       <span class="val v-time">' + esc(timeS) + '</span></div>' +
            '<div>  <span class="k">size</span>       <span class="val v-size">' + esc(fmtSize(b.size_bytes)) + '</span></div>' +
            '<div>  <span class="k">difficulty</span> <span class="val v-diff">' + esc(diff) + '</span></div>' +
            '<div>  <span class="k">h/s</span>        <span class="val v-hps">' + esc(b.implied_hashrate || '—') + '</span></div>' +
            '<div>  <span class="k">utc</span>        <span class="val v-utc">' + esc(b.time_utc || '—') + '</span></div>' +
            '<div class="rule">──────────────────────────────────────────────────────────────</div>' +
            '</div>'
          );
        }}).join('');
      }}
      function fmtHps(hps) {{
        hps = Number(hps) || 0;
        if (hps <= 0) return null;
        if (hps >= 1e9) return (hps / 1e9).toFixed(2) + ' GH/s';
        if (hps >= 1e6) return (hps / 1e6).toFixed(2) + ' MH/s';
        if (hps >= 1e3) return (hps / 1e3).toFixed(1) + ' kH/s';
        return Math.round(hps).toLocaleString() + ' H/s';
      }}
      function renderPeers(peers, count) {{
        var box = document.getElementById('peersTable');
        var cnt = document.getElementById('peersCount');
        if (cnt) cnt.textContent = String(count != null ? count : (peers ? peers.length : 0)) + ' online';
        if (!box) return;
        if (!peers || !peers.length) {{
          box.innerHTML = '<p class="muted" id="peersEmpty">No peers connected to the seed.</p>';
          return;
        }}
        var sum = 0, miners = 0;
        var rows = peers.map(function (p) {{
          var dir = p.inbound ? 'in' : 'out';
          var height = (p.height == null) ? '—' : ('#' + p.height);
          var peerLbl = p.id || p.ip || '—';
          var hr = p.hashrate || (p.mining ? '…' : '—');
          var mine = p.mining ? 'yes' : 'no';
          if (p.mining) {{ miners += 1; sum += Number(p.hps) || 0; }}
          return '<tr>' +
            '<td class="ip" title="' + esc(peerLbl) + '">' + esc(peerLbl) + '</td>' +
            '<td><span class="dir ' + dir + '">' + dir + '</span></td>' +
            '<td>' + esc(height) + '</td>' +
            '<td>' + esc(hr) + '</td>' +
            '<td>' + mine + '</td>' +
            '<td>' + esc(p.agent || '—') + '</td>' +
            '<td>' + esc(p.state || '—') + '</td>' +
            '</tr>';
        }}).join('');
        var total = fmtHps(sum) || '—';
        box.innerHTML =
          '<div class="table-wrap peers-panel"><table>' +
          '<thead><tr><th>Peer</th><th>Dir</th><th>Height</th><th>H/s</th><th>Mining</th><th>Version</th><th>State</th></tr></thead>' +
          '<tbody>' + rows + '</tbody></table></div>' +
          '<p class="peers-total" id="peersTotal">Total live: ' + esc(total) +
          ' · ' + miners + ' miners</p>';
      }}
            function renderBlocks(blocks) {{
        var el = document.getElementById('latestBlocks');
        if (!el || !blocks) return;
        var rows = blocks.map(function(b) {{
          var miner = b.miner ? copyable(b.miner, '/address/' + encodeURIComponent(b.miner)) : '<span class="muted">—</span>';
          return '<tr>' +
            '<td class="num"><span class="row-ico">' + mintMark('MHC Mined') + '<a href="/block/' + esc(b.height) + '"><strong>' + esc(b.height) + '</strong></a></span></td>' +
            '<td class="hash-col">' + copyable(b.hash, '/block/' + encodeURIComponent(b.hash), true) + '</td>' +
            '<td class="num">' + esc(b.tx_count) + '</td>' +
            '<td class="num"><strong class="reward">' + esc(b.reward_mhc || '—') + ' MHC</strong></td>' +
            '<td><span class="pill minted">MHC Mined</span></td>' +
            '<td class="muted nowrap num">' + esc(fmtInterval(b.interval_seconds)) + '</td>' +
            '<td class="muted nowrap num">' + esc(b.age || '—') + '</td>' +
            '<td>' + miner + '</td>' +
            '<td class="muted num-conf">' + esc(b.confirmations) + '</td></tr>';
        }}).join('');
        el.innerHTML = '<div class="table-wrap"><table class="data-table"><tr><th class="num">Height</th><th class="hash-col">Hash</th><th class="num">Txs</th><th class="num">Reward</th><th>Type</th><th class="num">Time</th><th class="num">Age</th><th>Miner</th><th class="num-conf" title="Confirmations">Conf</th></tr>' + rows + '</table></div>';
      }}
      function blocksPagerHtml(page, totalPages) {{
        page = Number(page) || 1;
        totalPages = Number(totalPages) || 1;
        var html = '';
        if (page > 1) html += '<a href="#" data-blocks-page="' + (page-1) + '" id="blocksPrev">← newer</a>';
        else html += '<span class="muted">← newer</span>';
        html += '<span class="muted">page ' + page + ' / ' + totalPages + '</span>';
        if (page < totalPages) html += '<a href="#" data-blocks-page="' + (page+1) + '" id="blocksNext">older →</a>';
        else html += '<span class="muted">older →</span>';
        return html;
      }}
      function updateBlocksPager(page, totalBlocks, perPage) {{
        var pg = document.getElementById('latestBlocksPager');
        var el = document.getElementById('latestBlocks');
        if (!pg || !el) return;
        perPage = Number(perPage || el.dataset.perPage || 100);
        totalBlocks = Number(totalBlocks != null ? totalBlocks : el.dataset.totalBlocks || 0);
        var totalPages = Math.max(1, Math.ceil(totalBlocks / perPage) || 1);
        page = Math.max(1, Math.min(Number(page) || 1, totalPages));
        el.dataset.page = String(page);
        el.dataset.totalBlocks = String(totalBlocks);
        el.dataset.perPage = String(perPage);
        pg.innerHTML = blocksPagerHtml(page, totalPages);
      }}
      async function loadBlocksPage(page) {{
        var el = document.getElementById('latestBlocks');
        if (!el) return;
        page = Number(page) || 1;
        try {{
          var r = await fetch('/api/blocks?page=' + page + '&_=' + Date.now(), {{ cache: 'no-store' }});
          if (!r.ok) return;
          var d = await r.json();
          renderBlocks(d.blocks || []);
          updateBlocksPager(d.page || page, d.total_blocks, d.per_page || 100);
        }} catch (e) {{}}
      }}
      function renderTxs(txs, elId) {{
        var el = document.getElementById(elId || 'latestTxs');
        if (!el) return;
        if (!txs || !txs.length) {{
          if ((elId || 'latestTxs') === 'xferOnly') el.innerHTML = '<p class="muted">No transfers yet — only mined rewards so far.</p>';
          else el.innerHTML = '<p class="muted">No transactions.</p>';
          return;
        }}
        var rows = txs.map(function(t) {{
          var cb = !!t.coinbase;
          var pill = cb ? '<span class="pill minted">MHC Mined</span>' : '<span class="pill xfer">Transfer</span>';
          var fr = t.from === 'coinbase' ? '<span class="muted">coinbase</span>'
            : (t.from ? copyable(t.from, '/address/' + encodeURIComponent(t.from)) : '<span class="muted">—</span>');
          var to = t.to ? copyable(t.to, '/address/' + encodeURIComponent(t.to)) : '<span class="muted">—</span>';
          var mark = mintMark(cb ? 'MHC Mined' : 'Transfer');
          return '<tr>' +
            '<td class="hash-col"><span class="row-ico">' + mark + copyable(t.txid, '/tx/' + encodeURIComponent(t.txid), true) + '</span></td>' +
            '<td>' + pill + '</td>' +
            '<td><a href="/block/' + esc(t.height) + '">#' + esc(t.height) + '</a></td>' +
            '<td>' + fr + '</td><td>' + to + '</td>' +
            '<td><strong class="reward">' + esc(t.amount_mhc || t.output_value_mhc) + ' MHC</strong></td>' +
            '<td class="muted">' + esc(t.fee_mhc != null ? t.fee_mhc : '—') + '</td>' +
            '<td class="muted nowrap">' + esc(t.age || '—') + '</td>' +
            '<td class="muted num-conf">' + esc(t.confirmations) + '</td></tr>';
        }}).join('');
        el.innerHTML = '<div class="table-wrap"><table class="data-table"><tr><th class="hash-col">Txid</th><th>Type</th><th>Block</th><th>From</th><th>To</th><th>Amount</th><th>Fee</th><th>Age</th><th class="num-conf">Confirmations</th></tr>' + rows + '</table></div>';
      }}
      function txsPagerHtml(page, totalPages) {{
        page = Number(page) || 1;
        totalPages = Number(totalPages) || 1;
        var html = '';
        if (page > 1) html += '<a href="#" data-txs-page="' + (page-1) + '">← newer</a>';
        else html += '<span class="muted">← newer</span>';
        html += '<span class="muted">page ' + page + ' / ' + totalPages + '</span>';
        if (page < totalPages) html += '<a href="#" data-txs-page="' + (page+1) + '">older →</a>';
        else html += '<span class="muted">older →</span>';
        return html;
      }}
      function updateTxsPager(page, totalTxs, perPage) {{
        var pg = document.getElementById('latestTxsPager');
        var el = document.getElementById('latestTxs');
        if (!pg || !el) return;
        perPage = Number(perPage || el.dataset.perPage || 100);
        totalTxs = Number(totalTxs != null ? totalTxs : el.dataset.totalTxs || 0);
        var totalPages = Math.max(1, Math.ceil(totalTxs / perPage) || 1);
        page = Math.max(1, Math.min(Number(page) || 1, totalPages));
        el.dataset.page = String(page);
        el.dataset.totalTxs = String(totalTxs);
        el.dataset.perPage = String(perPage);
        pg.innerHTML = txsPagerHtml(page, totalPages);
      }}
      async function loadTxsPage(page) {{
        var el = document.getElementById('latestTxs');
        if (!el) return;
        page = Number(page) || 1;
        try {{
          var r = await fetch('/api/transactions?page=' + page + '&_=' + Date.now(), {{ cache: 'no-store' }});
          if (!r.ok) return;
          var d = await r.json();
          renderTxs(d.transactions || [], 'latestTxs');
          updateTxsPager(d.page || page, d.total_transactions, d.per_page || 100);
        }} catch (e) {{}}
      }}
      function setText(id, text) {{
        var el = document.getElementById(id);
        if (!el || text == null) return;
        if (el.textContent !== String(text)) el.textContent = String(text);
      }}
      function applyKpis(d) {{
        var tip = d.tip_height;
        var node = d.node || {{}};
        var tipChanged = tip != null && Number(tip) !== Number(lastTip);
        var hrEl = document.getElementById('kpiHashrate');
        var hrLbl = document.getElementById('kpiHashLbl');
        var hh = document.getElementById('kpiHashHint');
        if (node.reported_hashrate_hps > 0 && node.reported_hashrate) {{
          if (hrLbl && hrLbl.textContent !== 'Hashrate (live)') hrLbl.textContent = 'Hashrate (live)';
          if (hrEl && hrEl.textContent !== node.reported_hashrate) hrEl.textContent = node.reported_hashrate;
          if (hh) {{
            var parts = ['total ' + (node.reported_miners || 0) + ' miners'];
            if (d.network_hashrate) parts.push('implied ' + d.network_hashrate);
            if (d.network_hashrate_window) parts.push('obs ' + d.network_hashrate_window);
            if (d.network_hashrate_short) parts.push('recent ' + d.network_hashrate_short);
            var htxt = parts.join(' · ');
            if (hh.textContent !== htxt) hh.textContent = htxt;
          }}
        }} else {{
          if (hrLbl && hrLbl.textContent !== 'Hashrate (implied)') hrLbl.textContent = 'Hashrate (implied)';
          if (hrEl && d.network_hashrate && hrEl.textContent !== d.network_hashrate) hrEl.textContent = d.network_hashrate;
          if (hh) {{
            var parts2 = ['live —', 'implied @10m'];
            if (d.network_hashrate_window) parts2.push('obs ' + d.network_hashrate_window);
            if (d.network_hashrate_short) parts2.push('recent ' + d.network_hashrate_short);
            var htxt2 = parts2.join(' · ');
            if (hh.textContent !== htxt2) hh.textContent = htxt2;
          }}
        }}
        var ageEl = document.getElementById('kpiAge');
        if (ageEl && d.tip_age) {{
          var ageTxt = d.tip_age + ' ago';
          if (ageEl.textContent !== ageTxt) ageEl.textContent = ageTxt;
        }}
        var hEl = document.getElementById('kpiHeight');
        if (hEl && tip != null) {{
          var href = '/block/' + tip;
          var a = hEl.querySelector('a');
          if (!a || a.getAttribute('href') !== href || a.textContent !== String(tip)) {{
            hEl.innerHTML = '<a href="' + href + '">' + tip + '</a>';
          }}
        }}
        var tot = document.getElementById('kpiTotal');
        if (tot && d.total_blocks != null) {{
          var th = '/blocks';
          var ta = tot.querySelector('a');
          if (!ta || ta.textContent !== String(d.total_blocks)) {{
            tot.innerHTML = '<a href="' + th + '">' + d.total_blocks + '</a>';
          }}
        }}
        setText('kpiTxs', d.total_transactions);
        setText('kpiMint', d.minted_mhc);
        setText('kpiDiff', d.tip_difficulty_display);
        if (d.tip_bits) setText('kpiBits', 'bits ' + d.tip_bits);
        var eta = document.getElementById('kpiEta');
        if (eta) {{
          var et = d.next_block_overdue ? 'overdue' : (d.next_block_eta || '—');
          if (eta.textContent !== et) eta.textContent = et;
        }}
        var etaH = document.getElementById('kpiEtaHint');
        if (etaH && d.next_block_hint) {{
          if (etaH.textContent !== d.next_block_hint) etaH.textContent = d.next_block_hint;
          var cls = 'hint ' + (d.next_block_overdue ? 'err' : 'muted');
          if (etaH.className !== cls) etaH.className = cls;
        }}
        setText('kpiPeers', node.peer_count != null ? String(node.peer_count) : null);
        setText('kpiSync', node.sync_state || '—');
        // Peers / live finds: only rebuild DOM when tip advances (stops layout jump).
        if (tipChanged) {{
          renderPeers(node.peers || [], node.peer_count);
          if (d.live_finds) renderFinds(d.live_finds);
        }}
        var meta = document.getElementById('hdrMeta');
        if (meta && tip != null) {{
          var mtxt = 'Live mainnet · read-only · tip #' + tip;
          if (meta.textContent !== mtxt) meta.textContent = mtxt;
        }}
      }}
      function applyTables(d) {{
        if (d.recent) {{
          var bel = document.getElementById('latestBlocks');
          var cur = bel ? Number(bel.dataset.page || 1) : 1;
          if (cur <= 1) renderBlocks(d.recent);
          updateBlocksPager(cur <= 1 ? 1 : cur, d.total_blocks, 100);
        }}
        if (d.recent_txs) {{
          var tel = document.getElementById('latestTxs');
          var tcur = tel ? Number(tel.dataset.page || 1) : 1;
          if (tcur <= 1) renderTxs(d.recent_txs, 'latestTxs');
          updateTxsPager(tcur <= 1 ? 1 : tcur, d.total_transactions, 100);
        }}
        if (d.recent_transfers) renderTxs(d.recent_transfers, 'xferOnly');
      }}
      async function tick() {{
        try {{
          // Light poll — full /api/ only when tip advances (tables refresh).
          var r = await fetch('/api/tip?_=' + Date.now(), {{ cache: 'no-store' }});
          if (!r.ok) return;
          var d = await r.json();
          var tip = d.tip_height;
          applyKpis(d);
          if (tip != null && tip > lastTip && lastTip >= 0) {{
            document.title = '▸ BLOCK #' + tip + ' — MHCOIN Explorer';
            var term = document.querySelector('.term');
            if (term) {{
              term.style.outline = '2px solid #111418';
              setTimeout(function(){{ term.style.outline = 'none'; }}, 1800);
            }}
            try {{
              var fr = await fetch('/api/?_=' + Date.now(), {{ cache: 'no-store' }});
              if (fr.ok) applyTables(await fr.json());
            }} catch (e2) {{}}
          }}
          if (tip != null) lastTip = tip;
          var home = document.getElementById('explorerHome');
          if (home && tip != null) home.dataset.tip = String(tip);
        }} catch (e) {{}}
      }}
      document.addEventListener('click', function (e) {{
        var bp = e.target && e.target.closest && e.target.closest('[data-blocks-page]');
        if (bp) {{
          e.preventDefault();
          loadBlocksPage(bp.getAttribute('data-blocks-page'));
          return;
        }}
        var tp = e.target && e.target.closest && e.target.closest('[data-txs-page]');
        if (tp) {{
          e.preventDefault();
          loadTxsPage(tp.getAttribute('data-txs-page'));
        }}
      }});
      // Delay first poll — SSR already painted; avoids flash/jump on refresh.
      setInterval(tick, 3000);
      setTimeout(tick, 3000);
    }})();
    </script>
    """
    return _page("Home", body, tip=data.get("tip_height"), hero=True)



def _render_blocks(data: dict[str, Any]) -> bytes:
    page = int(data.get("page") or 1)
    total_pages = int(data.get("total_pages") or 1)
    body = f"""
    <div class="shell">
    <div class="card">
      <div class="card-head">
        <h1>All blocks</h1>
        <span class="muted">{_esc(data.get("total_blocks"))} total</span>
      </div>
      <p class="muted" style="margin-top:0">
        Heights <strong>{_esc(data.get("to_height"))}</strong> →
        <strong>{_esc(data.get("from_height"))}</strong>
        · genesis = #0
      </p>
      {_pager(page, total_pages)}
      {_blocks_table(data.get("blocks") or [])}
      {_pager(page, total_pages)}
    </div>
    </div>
    """
    return _page("Blocks", body, tip=data.get("tip_height"))


def _render_transactions(data: dict[str, Any]) -> bytes:
    page = int(data.get("page") or 1)
    total_pages = int(data.get("total_pages") or 1)
    body = f"""
    <div class="shell">
    <div class="card">
      <div class="card-head">
        <h1>All transactions</h1>
        <span class="muted">{_esc(data.get("total_transactions"))} total</span>
      </div>
      {_pager(page, total_pages, base="/transactions")}
      {_txs_table(data.get("transactions") or [], empty="No transactions.")}
      {_pager(page, total_pages, base="/transactions")}
    </div>
    </div>
    """
    return _page("Transactions", body, tip=data.get("tip_height"))


def _render_block(b: dict[str, Any]) -> bytes:
    h = b.get("height")
    prev_h = h - 1 if isinstance(h, int) and h > 0 else None
    next_h = h + 1 if isinstance(h, int) and b.get("next") else None
    nav = []
    if prev_h is not None:
        nav.append(f'<a href="/block/{prev_h}">← #{prev_h}</a>')
    if next_h is not None:
        nav.append(f'<a href="/block/{next_h}">#{next_h} →</a>')
    nav_s = " · ".join(nav) if nav else ""

    txs = []
    for i, tx in enumerate(b.get("transactions") or []):
        tid = tx.get("txid") or ""
        fee = tx.get("fee_mhc")
        fee_s = f'{_esc(fee)}' if fee is not None else "—"
        rate = tx.get("fee_per_byte")
        rate_s = f'{rate} sat/B' if rate is not None else "—"
        txs.append(
            "<tr>"
            f'<td class="num">{i}</td>'
            f'<td class="hash-col">{_copyable(tid, short=False, href="/tx/" + tid)}</td>'
            f"<td>{_type_pill(bool(tx.get('coinbase')))}</td>"
            f'<td class="num"><strong class="reward">{_esc(tx.get("amount_mhc") or tx.get("output_value_mhc"))}</strong></td>'
            f'<td class="num muted">{fee_s}</td>'
            f'<td class="num muted">{_esc(rate_s)}</td>'
            "</tr>"
        )
    if not txs:
        for i, tid in enumerate(b.get("txids") or []):
            pill = _type_pill(i == 0)
            txs.append(
                f'<tr><td class="num">{i}</td><td class="hash-col">{_copyable(tid, short=False, href="/tx/" + tid)}</td>'
                f"<td>{pill}</td>"
                f'<td></td><td></td><td></td></tr>'
            )

    conf = b.get("confirmations")
    badge = f'<span class="badge">{conf} confirmations</span>' if conf else ""
    tip = _inferred_tip(h, conf)
    miner = b.get("miner")

    body = f"""
    <div class="shell">
    <div class="card">
      <div class="card-head">
        <h1>Block #{_esc(h)} {badge}</h1>
        <span class="muted">{nav_s}</span>
      </div>
      <div class="kv">
        <div>Hash</div><div>{_copyable(b.get("hash"), short=False)}</div>
        <div>Previous</div><div>{_copyable(b.get("previous"), href="/block/" + str(b.get("previous")), short=False) if b.get("previous") else "—"}</div>
        <div>Next</div><div>{_copyable(b.get("next"), href="/block/" + str(b.get("next")), short=False) if b.get("next") else '<span class="muted">— (tip)</span>'}</div>
        <div>Merkle root</div><div>{_copyable(b.get("merkle_root"), short=False)}</div>
        <div>Time</div><div>{_esc(b.get("time_utc") or b.get("timestamp"))} <span class="muted">({_esc(b.get("age") or "—")} ago)</span></div>
        <div>Miner</div><div>{_copyable(miner, href="/address/" + str(miner), short=False) if miner else "—"}</div>
        <div>Reward</div><div><strong class="reward">{_esc(b.get("reward_mhc") or "—")} MHC</strong></div>
        <div>Difficulty</div><div>{_esc(b.get("difficulty_display") or "—")} <span class="muted">({_esc(b.get("bits") or "")})</span></div>
        <div>Target</div><div class="mono">{_esc(b.get("target_short") or b.get("target") or "—")}</div>
        <div>Nonce</div><div class="mono">{_esc(b.get("nonce"))}</div>
        <div>Size</div><div>{_esc(b.get("size_bytes"))} bytes</div>
        <div>Transactions</div><div>{_esc(b.get("tx_count"))}</div>
        <div>Interval</div><div>{_esc(_fmt_interval(b.get("interval_seconds")))}</div>
        <div>Confirmations</div><div>{_esc(conf)}</div>
      </div>
    </div>
    <div class="card">
      <h1>Transactions</h1>
      <div class="table-wrap"><table class="data-table">
        <tr><th class="num">#</th><th>Txid</th><th>Type</th><th class="num">Amount</th><th class="num">Fee</th><th class="num">Fee rate</th></tr>
        {"".join(txs)}
      </table></div>
    </div>
    </div>
    """
    return _page(f"Block {h}", body, tip=tip)



def _status_pill(spent: bool | None) -> str:
    if spent is True:
        return '<span class="pill-spent">spent</span>'
    if spent is False:
        return '<span class="pill-unspent">unspent</span>'
    return '<span class="muted">—</span>'


def _flow_bars(flow: dict[str, Any] | None) -> str:
    """Horizontal received / sent / fees / balance comparison."""
    if not flow:
        return ""
    recv = int(flow.get("received_sats") or 0)
    sent = int(flow.get("sent_sats") or 0)
    fees = int(flow.get("fees_sats") or 0)
    bal = int(flow.get("balance_sats") or 0)
    peak = max(recv, sent, fees, bal, 1)

    def row(cls: str, label: str, sats: int) -> str:
        pct = max(2.0, min(100.0, 100.0 * sats / peak)) if sats else 0.0
        return (
            f'<div class="flow-row {cls}"><div class="flow-lbl">{_esc(label)}</div>'
            f'<div class="flow-track"><div class="flow-fill" style="width:{pct:.1f}%"></div></div>'
            f'<div class="flow-amt">{_esc(format_mhc(sats))} MHC</div></div>'
        )

    return (
        '<div class="flow-bars">'
        + row("f-recv", "Received", recv)
        + row("f-sent", "Sent", sent)
        + row("f-fee", "Fees", fees)
        + row("f-bal", "Balance", bal)
        + "</div>"
    )


def _stat_val(value: Any, *, unit: str | None = None, css: str = "") -> str:
    """Compact value + optional muted unit suffix for dense stat tiles."""
    if value is None or value == "":
        inner = "—"
    else:
        inner = _esc(value)
        if unit:
            inner += f'<span class="unit">{_esc(unit)}</span>'
    classes = "val" + (f" {css}" if css else "")
    return f'<div class="{classes}">{inner}</div>'


def _render_tx(t: dict[str, Any]) -> bytes:
    coinbase = bool(t.get("coinbase"))
    focus = (t.get("focus_address") or "").strip()
    focus_l = focus.lower() if focus else ""

    def _is_focus(addr: str | None) -> bool:
        return bool(focus_l and addr and str(addr).lower() == focus_l)

    def _tx_href(txid: str | None) -> str | None:
        if not txid:
            return None
        href = f"/tx/{txid}"
        if focus:
            href += f"?addr={focus}"
        return href

    ins = []
    for i in t.get("inputs") or []:
        if i.get("coinbase"):
            ins.append("<li><span class='muted'>New coins (block reward)</span></li>")
            continue
        addr = i.get("address") or "?"
        val = i.get("value_mhc") or "?"
        prev = i.get("prev_txid")
        vout = i.get("prev_vout")
        src_h = i.get("source_height")
        src_meta = (
            f'<span class="muted">vout {_esc(vout)}'
            + (f' · block <a href="/block/{_esc(src_h)}">#{_esc(src_h)}</a>' if src_h is not None else "")
            + "</span>"
        )
        focus_cls = " focus-addr" if _is_focus(addr) else ""
        ins.append(
            f'<li class="io-row{focus_cls}"><div class="io-main">'
            + _copyable(prev, href=_tx_href(prev), short=False)
            + f"<div class='io-meta'>{src_meta} → "
            + _copyable(addr, href=f"/address/{addr}", short=False)
            + "</div></div>"
            + f'<div class="io-amt"><strong>{_esc(val)}</strong><span class="unit"> MHC</span></div></li>'
        )
    # Sender = first non-coinbase input address (for change labeling).
    from_addr = None
    for i in t.get("inputs") or []:
        if not i.get("coinbase") and i.get("address"):
            from_addr = i.get("address")
            break
    outs = []
    change_mhc = None
    for o in t.get("outputs") or []:
        addr = o.get("address") or "?"
        tag = ""
        if not coinbase and from_addr and addr == from_addr:
            tag = " <span class='muted'>(change back)</span>"
            change_mhc = o.get("value_mhc")
        elif not coinbase and from_addr and addr != from_addr:
            tag = " <span class='muted'>(payment)</span>"
        spent = o.get("spent")
        if spent is None and o.get("status") in ("spent", "unspent"):
            spent = o.get("status") == "spent"
        focus_cls = " focus-addr" if _is_focus(addr) else ""
        outs.append(
            f'<li class="io-row{focus_cls}"><div class="io-main">'
            + _copyable(addr, href=f"/address/{addr}", short=False)
            + f"{tag}"
            + f" {_status_pill(spent)}</div>"
            + f'<div class="io-amt"><strong class="reward">{_esc(o.get("value_mhc"))}</strong>'
            + '<span class="unit"> MHC</span></div></li>'
        )
    # Collapse long From lists (multi-input payments).
    show_n = 6
    if len(ins) > show_n + 1:
        hidden = len(ins) - show_n
        ins_html = (
            "".join(ins[:show_n])
            + f'<details class="io-more"><summary>Show {hidden} more inputs</summary>'
            + "".join(ins[show_n:])
            + "</details>"
        )
    else:
        ins_html = "".join(ins) or "<li class='muted'>—</li>"

    conf = t.get("confirmations")
    badge = f'<span class="badge">{conf} confirmations</span>' if conf else ""
    height = t.get("block_height")
    included = (
        f'<a href="/block/{height}">Included in block #{height}</a>'
        if height is not None
        else '<span class="muted">Unconfirmed</span>'
    )
    tip = _inferred_tip(height, conf)
    amt_lbl = "Reward" if coinbase else "Payment"
    amt = t.get("amount_mhc") or t.get("output_value_mhc")
    in_n = t.get("input_count") if t.get("input_count") is not None else len(t.get("inputs") or [])
    out_n = t.get("output_count") if t.get("output_count") is not None else len(t.get("outputs") or [])
    in_lbl = f'{_esc(in_n)} input{"s" if int(in_n or 0) != 1 else ""}'
    if t.get("input_value_mhc"):
        in_lbl += f' · {_esc(t.get("input_value_mhc"))} MHC'
    out_lbl = f'{_esc(out_n)} output{"s" if int(out_n or 0) != 1 else ""}'
    if t.get("output_value_mhc"):
        out_lbl += f' · {_esc(t.get("output_value_mhc"))} MHC'

    # Primary = money only (confirmations live in the card-head badge — no duplicate tile).
    primary = f"""
      <div class="stats stats-primary" id="txSummary">
        <div class="stat s-amt"><div class="lbl">{amt_lbl}</div>
          {_stat_val(amt, unit="MHC", css="reward")}</div>
"""
    if not coinbase:
        primary += f"""
        <div class="stat s-fee"><div class="lbl">Fee</div>
          {_stat_val(t.get("fee_mhc"), unit="MHC")}</div>
"""
    primary += "</div>"

    meta_tiles = []
    if not coinbase:
        meta_tiles.append(
            f'<div class="stat"><div class="lbl">Fee rate</div>{_stat_val(t.get("fee_rate") or "—")}</div>'
        )
        meta_tiles.append(
            f'<div class="stat"><div class="lbl">Inputs Σ</div>{_stat_val(t.get("input_value_mhc"), unit="MHC")}</div>'
        )
        meta_tiles.append(
            f'<div class="stat"><div class="lbl">Outputs Σ</div>{_stat_val(t.get("output_value_mhc"), unit="MHC")}</div>'
        )
        if change_mhc is not None:
            meta_tiles.append(
                f'<div class="stat"><div class="lbl">Change</div>{_stat_val(change_mhc, unit="MHC")}</div>'
            )
    meta_tiles.extend(
        [
            f'<div class="stat"><div class="lbl">Age</div>{_stat_val(t.get("age") or "—")}</div>',
            f'<div class="stat"><div class="lbl">Size</div>{_stat_val(t.get("size_bytes"), unit="B")}</div>',
            f'<div class="stat"><div class="lbl">vSize</div>{_stat_val(t.get("vsize") or t.get("size_bytes"), unit="vB")}</div>',
            f'<div class="stat"><div class="lbl">Weight</div>{_stat_val(t.get("weight"), unit="WU")}</div>',
            f'<div class="stat"><div class="lbl">In / Out</div>{_stat_val(f"{in_n} / {out_n}")}</div>',
        ]
    )
    if not coinbase:
        rbf = t.get("rbf")
        rbf_lbl = "yes" if rbf is True else ("no" if rbf is False else "—")
        meta_tiles.append(
            f'<div class="stat"><div class="lbl">RBF</div>{_stat_val(rbf_lbl)}</div>'
        )
        meta_tiles.append(
            f'<div class="stat"><div class="lbl">Witness</div>{_stat_val("no")}</div>'
        )
    meta = f'<div class="stats stats-compact">{"".join(meta_tiles)}</div>'

    # Compact sticky: only after scroll (see JS). Avoid repeating Payment/Fee/Age from tiles.
    sticky = f"""
    <div class="tx-sticky" id="txSticky" aria-hidden="true">
      <div class="tx-sticky-id">{_copyable(t.get("txid"), short=True)}</div>
      <div class="tx-sticky-meta">
        {_type_pill(coinbase)}
        <strong class="reward">{_esc(amt)}</strong><span class="unit"> MHC</span>
        {badge}
      </div>
    </div>
"""

    raw_hex = t.get("raw_hex") or ""
    decode_obj = {
        "txid": t.get("txid"),
        "version": t.get("version"),
        "locktime": t.get("locktime"),
        "size_bytes": t.get("size_bytes"),
        "coinbase": coinbase,
        "inputs": t.get("inputs"),
        "outputs": t.get("outputs"),
        "input_value_mhc": t.get("input_value_mhc"),
        "output_value_mhc": t.get("output_value_mhc"),
        "fee_mhc": t.get("fee_mhc"),
        "focus_address": focus or None,
    }
    decode_pre = _esc(json.dumps(decode_obj, indent=2, sort_keys=True, default=str))
    body = f"""
    <div class="shell">
    {sticky}
    <div class="card">
      <div class="card-head">
        <h1>{_type_pill(coinbase)} {badge}</h1>
        <span class="muted">{included}</span>
      </div>
      {primary}
      {meta}
      <p class="muted" style="margin:.75rem 0 0;font-size:.85rem">
        {"Block reward credited to miner." if coinbase else
         "Payment → recipient · change → sender · fee = inputs − outputs."}
      </p>
      <div class="kv" style="margin-top:1rem">
        <div>Txid</div><div>{_copyable(t.get("txid"), short=False)}</div>
        <div>Block</div><div>{('<a href="/block/'+_esc(height)+'">#'+_esc(height)+'</a> · '+_esc(t.get("time_utc") or "")) if height is not None else "—"}</div>
        <div>Block hash</div><div>{_copyable(t.get("block_hash"), href="/block/"+str(t.get("block_hash")), short=False) if t.get("block_hash") else "—"}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-head">
        <h1>From</h1>
        <span class="muted">{in_lbl}</span>
      </div>
      <ul class="plain">{ins_html}</ul>
    </div>
    <div class="card">
      <div class="card-head">
        <h1>To</h1>
        <span class="muted">{out_lbl}</span>
      </div>
      <ul class="plain">{"".join(outs) or "<li class='muted'>—</li>"}</ul>
    </div>
    <div class="card">
      <div class="card-head">
        <h1>Raw &amp; decoded</h1>
        <span class="muted">{_esc(t.get("size_bytes"))} bytes</span>
      </div>
      <div class="subtabs" role="tablist">
        <button type="button" class="subtab on" data-tx-tab="raw">Raw hex</button>
        <button type="button" class="subtab" data-tx-tab="decode">Decoded</button>
      </div>
      <pre class="hex-pre" id="txPanelRaw">{_esc(raw_hex or "—")}</pre>
      <pre class="hex-pre" id="txPanelDecode" hidden>{decode_pre}</pre>
    </div>
    </div>
    <script>
    (function(){{
      var tabs = document.querySelectorAll('[data-tx-tab]');
      var raw = document.getElementById('txPanelRaw');
      var dec = document.getElementById('txPanelDecode');
      tabs.forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          var mode = btn.getAttribute('data-tx-tab');
          tabs.forEach(function(b) {{ b.classList.toggle('on', b === btn); }});
          if (mode === 'decode') {{
            raw.hidden = true; dec.hidden = false;
          }} else {{
            raw.hidden = false; dec.hidden = true;
          }}
        }});
      }});
      var sticky = document.getElementById('txSticky');
      var summary = document.getElementById('txSummary');
      if (sticky && summary && 'IntersectionObserver' in window) {{
        var io = new IntersectionObserver(function(entries) {{
          var on = entries[0] && !entries[0].isIntersecting;
          sticky.classList.toggle('is-on', on);
          sticky.setAttribute('aria-hidden', on ? 'false' : 'true');
        }}, {{ rootMargin: '-4.2rem 0px 0px 0px', threshold: 0 }});
        io.observe(summary);
      }}
    }})();
    </script>
    """
    return _page("Transaction", body, tip=tip)


def _render_address(a: dict[str, Any]) -> bytes:
    addr = a.get("address")
    rows = []
    for o in a.get("outputs") or []:
        spent = o.get("spent")
        if spent is None and o.get("status") in ("spent", "unspent"):
            spent = o.get("status") == "spent"
        txid = o.get("txid") or ""
        tx_href = f"/tx/{txid}?addr={addr}" if addr and txid else (f"/tx/{txid}" if txid else None)
        rows.append(
            "<tr>"
            f'<td class="num"><span class="row-ico">{_mint_mark(title="MHC Mined" if o.get("coinbase") else "Transfer")}'
            f'<a href="/block/{o.get("height")}">{o.get("height")}</a></span></td>'
            f'<td class="hash-col">{_copyable(txid, short=False, href=tx_href)}</td>'
            f'<td class="num">{o.get("vout")}</td>'
            f'<td class="num"><strong class="reward">{_esc(o.get("value_mhc"))}</strong></td>'
            f'<td>{_status_pill(spent)}</td>'
            f'<td class="muted nowrap num">{_esc(o.get("age") or "")}</td>'
            f'<td class="num-conf">{_esc(o.get("confirmations"))}</td>'
            f"<td>{_type_pill(bool(o.get('coinbase')))}</td>"
            "</tr>"
        )
    tip = a.get("tip_height")
    qr_svg = D.qr_svg(str(addr)) if addr else ""
    series = a.get("balance_series_sats") or []
    # sparkline expects MHC-scale floats for nicer Y axis — use MHC units
    series_mhc = [s / 100_000_000 for s in series] if series else []
    spark = _sparkline(series_mhc, width=420, height=56) if series_mhc else ""
    flow_html = _flow_bars(a.get("flow"))
    body = f"""
    <div class="shell">
    <div class="card">
      <h1><span class="row-ico">{_mint_mark(title="Wallet")} Wallet</span></h1>
      <div class="qr-row">
        <div class="qr-box">{qr_svg}</div>
        <div class="kv" style="flex:1;min-width:220px">
        <div>Address</div><div>{_copyable(addr, short=False)}</div>
        <div>Balance</div><div><strong class="reward">{_esc(a.get("balance_mhc") or "0")} MHC</strong>
          <span class="muted">({_esc(a.get("utxo_count") or 0)} UTXO)</span></div>
        <div>{_flow_pill(received=True)}</div><div>{_esc(a.get("total_received_mhc") or "0")} MHC
          <span class="muted"> · mining + incoming (no change)</span></div>
        <div>{_flow_pill(received=False)}</div><div><strong>{_esc(a.get("total_sent_mhc") or "0")}</strong> MHC
          <span class="muted"> · payments only</span></div>
        <div>Network fees</div><div>{_esc(a.get("total_fees_mhc") or "0")} MHC</div>
        <div>Outputs</div><div>{_esc(a.get("received_count"))}</div>
        </div>
      </div>
      {flow_html}
      <div class="bal-spark">
        <div class="muted" style="font-size:.72rem;font-weight:650;text-transform:uppercase;letter-spacing:.04em;margin-bottom:.25rem">Balance over activity</div>
        {spark or '<span class="muted">No balance history yet.</span>'}
      </div>
      <p class="muted" style="margin:.75rem 0 0;font-size:.85rem">
        Balance ≈ Received − Sent − Fees.
      </p>
    </div>
    <div class="card">
      <div class="card-head">
        <h1>Received outputs</h1>
        <span class="muted">newest first · {_esc(a.get("per_page") or D.ADDRESS_PER_PAGE)} / page</span>
      </div>
      {_pager(int(a.get("page") or 1), int(a.get("total_pages") or 1), base=f"/address/{addr}", chain_links=False) if int(a.get("total_pages") or 1) > 1 else ""}
      <div class="table-wrap"><table class="data-table">
        <tr><th class="num">Height</th><th class="hash-col">Txid</th><th class="num">vout</th><th class="num">MHC</th>
            <th>Status</th><th class="num">Age</th><th class="num-conf">Confirmations</th><th>Type</th></tr>
        {"".join(rows) or '<tr><td colspan="8" class="muted">No outputs</td></tr>'}
      </table></div>
      {_pager(int(a.get("page") or 1), int(a.get("total_pages") or 1), base=f"/address/{addr}", chain_links=False) if int(a.get("total_pages") or 1) > 1 else ""}
    </div>
    </div>
    """
    return _page("Address", body, tip=tip)


def _render_richlist(data: dict[str, Any]) -> bytes:
    rows = []
    for r in data.get("addresses") or []:
        addr = r.get("address")
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(r.get("rank"))}</td>'
            f'<td>{_copyable(addr, href="/address/" + str(addr), short=False)}</td>'
            f'<td class="num"><span class="row-ico">{_mint_mark(title="Balance")}'
            f'<strong class="reward">{_esc(r.get("balance_mhc"))}</strong></span></td>'
            f'<td class="num">{_esc(r.get("utxo_count"))}</td>'
            f'<td class="num">{_esc(r.get("share_pct"))}%</td>'
            "</tr>"
        )
    body = f"""
    <div class="shell">
      <div class="card">
        <div class="card-head">
          <h1>Rich list</h1>
          <span class="muted">top {_esc(data.get("limit"))} by UTXO balance · {_esc(data.get("source"))}</span>
        </div>
        <p class="muted" style="margin:0 0 .75rem;font-size:.85rem">
          Circulating in UTXO set:
          <span class="row-ico">{_mint_mark(title="Supply")}
          <strong class="reward">{_esc(data.get("total_supply_mhc"))} MHC</strong></span>
          · {_esc(data.get("total_utxos"))} UTXOs
        </p>
        <div class="table-wrap"><table class="data-table">
          <tr><th class="num">#</th><th>Address</th><th class="num">Balance</th><th class="num">UTXOs</th><th class="num">Share</th></tr>
          {"".join(rows) or '<tr><td colspan="5" class="muted">No balances</td></tr>'}
        </table></div>
      </div>
    </div>
    """
    return _page("Rich list", body)


def _render_stats(data: dict[str, Any]) -> bytes:
    avg = data.get("avg_block_interval_seconds")
    avg_s = _fmt_interval(avg) if avg is not None else "—"
    halv = data.get("halving") or {}
    node = data.get("node") or {}
    miners = data.get("top_miners") or []
    miner_rows = []
    for m in miners:
        miner_rows.append(
            "<tr>"
            f'<td>{_copyable(m.get("address"), href="/address/" + str(m.get("address")))}</td>'
            f'<td class="num">{_esc(m.get("blocks"))}</td>'
            f'<td class="num">{_esc(m.get("share_pct"))}%</td>'
            f'<td class="num"><strong class="reward">{_esc(m.get("reward_mhc"))}</strong></td>'
            "</tr>"
        )
    peers_panel = _peers_panel(
        node.get("peers") or [],
        peer_count=node.get("peer_count"),
        total_hps=node.get("reported_hashrate_hps"),
        total_hashrate=node.get("reported_hashrate"),
    )
    body = f"""
    <div class="shell">
      <div class="kpi">
        <div class="stat k-height"><div class="lbl">Height</div><div class="val">{_esc(data.get("tip_height"))}</div></div>
        <div class="stat k-blocks"><div class="lbl">Blocks</div><div class="val">{_esc(data.get("total_blocks"))}</div></div>
        <div class="stat k-txs"><div class="lbl">Transactions</div><div class="val">{_esc(data.get("total_transactions"))}</div></div>
        <div class="stat k-hash"><div class="lbl">Transfers</div><div class="val">{_esc(data.get("transfer_transactions"))}</div></div>
        <div class="stat k-mint"><div class="lbl">Minted</div><div class="val">{_esc(data.get("minted_mhc"))}</div></div>
        <div class="stat k-diff"><div class="lbl">Difficulty</div><div class="val">{_esc(data.get("tip_difficulty_display"))}</div></div>
        <div class="stat k-eta"><div class="lbl">Avg interval</div><div class="val">{_esc(avg_s)}</div></div>
        <div class="stat k-peers"><div class="lbl">Peers</div><div class="val">{_esc(node.get("peer_count") if node.get("peer_count") is not None else "—")}</div></div>
      </div>
      <section class="card">
        <div class="card-head"><h1>Halving</h1><span class="muted">era {_esc(halv.get("era"))}</span></div>
        <div class="kv">
          <div>Current subsidy</div><div>{_esc(halv.get("current_subsidy_mhc"))} MHC</div>
          <div>Next halving</div><div>block #{_esc(halv.get("next_halving_height"))} · {_esc(halv.get("blocks_to_halving"))} blocks</div>
          <div>Next subsidy</div><div>{_esc(halv.get("next_subsidy_mhc"))} MHC</div>
          <div>Target</div><div class="mono">{_esc(data.get("tip_target_short") or data.get("tip_target") or "—")}</div>
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h1>Top miners</h1><span class="muted">last 100 blocks</span></div>
        <div class="table-wrap"><table class="data-table">
          <tr><th>Miner</th><th class="num">Blocks</th><th class="num">Share</th><th class="num">Rewards</th></tr>
          {"".join(miner_rows) or '<tr><td colspan="4" class="muted">No miners yet.</td></tr>'}
        </table></div>
      </section>
      {peers_panel}
    </div>
    """
    return _page("Network stats", body, tip=data.get("tip_height"))


def _render_charts(data: dict[str, Any]) -> bytes:
    charts = data.get("charts") or {}
    halv = data.get("halving") or {}
    avg = data.get("avg_block_interval_seconds")
    avg_s = _fmt_interval(avg) if avg is not None else "—"
    spark_iv = _sparkline(charts.get("intervals") or [], width=320, height=72)
    spark_hr = _sparkline(charts.get("hashrate_hps") or [], width=320, height=72, stroke="#16a34a")
    body = f"""
    <div class="shell">
      <div class="meta-row">
        <section class="card mini-stat">
          <div class="card-head"><h1>Block intervals</h1><span class="muted">last { _esc(charts.get("window") or 30) }</span></div>
          {spark_iv}
          <p class="muted" style="margin:.5rem 0 0">avg {_esc(avg_s)} · target {_esc(data.get("target_block_time_seconds") or 600)}s</p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Observed hashrate</h1><span class="muted">per block</span></div>
          {spark_hr}
          <p class="muted" style="margin:.5rem 0 0">
            recent {_esc(data.get("network_hashrate_short") or "—")} ·
            window {_esc(data.get("network_hashrate_window") or "—")}
          </p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Implied hashrate</h1><span class="muted">@ tip bits</span></div>
          <div class="val">{_esc(data.get("network_hashrate") or "—")}</div>
          <p class="muted" style="margin:.5rem 0 0">10m target spacing</p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Halving</h1><span class="muted">era {_esc(halv.get("era"))}</span></div>
          <div class="val">{_esc(halv.get("blocks_to_halving"))} blocks</div>
          <p class="muted" style="margin:.5rem 0 0">next #{_esc(halv.get("next_halving_height"))}</p>
        </section>
      </div>
      <p class="muted" style="font-size:.85rem"><a href="/api/charts">JSON</a> · heights in series: {len(charts.get("heights") or [])}</p>
    </div>
    """
    return _page("Charts", body, tip=data.get("tip_height"))


def _mempool_row_html(tx: dict[str, Any]) -> str:
    tid = tx.get("txid") or "?"
    fr = tx.get("from")
    to = tx.get("to")
    fr_html = _copyable(fr, href=f"/address/{fr}") if fr else '<span class="muted">—</span>'
    to_html = _copyable(to, href=f"/address/{to}") if to else '<span class="muted">—</span>'
    rbf = tx.get("rbf")
    rbf_html = (
        '<span class="pill xfer">RBF</span>'
        if rbf is True
        else '<span class="muted">—</span>'
    )
    return (
        "<tr>"
        f'<td class="hash-col">{_copyable(tid, href="/tx/" + str(tid), short=False)}</td>'
        f"<td>{fr_html}</td>"
        f"<td>{to_html}</td>"
        f'<td class="num">{_esc(tx.get("amount_mhc") or tx.get("output_value_mhc") or "—")}</td>'
        f'<td class="num">{_esc(tx.get("fee_mhc") or "—")}</td>'
        f'<td class="num">{_esc(tx.get("fee_rate") or "—")}</td>'
        f'<td class="num">{_esc(tx.get("vsize") or tx.get("size_bytes") or "—")}</td>'
        f"<td>{rbf_html}</td>"
        "</tr>"
    )


def _mempool_table_html(txs: list[dict[str, Any]]) -> str:
    rows = "".join(_mempool_row_html(tx) for tx in txs)
    empty_row = (
        '<tr><td colspan="8" class="muted">'
        "Mempool empty — no unconfirmed transactions on this node."
        "</td></tr>"
    )
    body_rows = rows or empty_row
    return (
        '<div class="table-wrap"><table class="data-table">'
        '<tr><th class="hash-col">Txid</th><th>From</th><th>To</th>'
        '<th class="num">Amount</th><th class="num">Fee</th><th class="num">Fee rate</th>'
        '<th class="num">vSize</th><th>RBF</th></tr>'
        f"{body_rows}"
        "</table></div>"
    )


def _render_mempool(data: dict[str, Any]) -> bytes:
    txs = data.get("transactions") or []
    init_key = "|".join(str(tx.get("txid") or "") for tx in txs)
    age = data.get("updated_age") or "—"
    body = f"""
    <div class="shell">
      <div class="card">
        <div class="card-head">
          <h1>Mempool</h1>
          <span class="muted" id="mempoolCount">{_esc(data.get("count") or 0)} unconfirmed · snapshot {_esc(age)} ago</span>
        </div>
        <p class="muted" style="margin:0 0 .75rem;font-size:.85rem">
          Live from node <span class="mono">mempool.json</span>
          · <a href="/api/mempool">JSON</a>
          · <span class="term-live" id="mempoolLive" style="font-size:.7rem">live</span>
        </p>
        <div id="mempoolWrap" data-key="{_esc(init_key)}">{_mempool_table_html(txs)}</div>
      </div>
    </div>
    <script>
    (function () {{
      // Mempool auto-refresh — polls /api/mempool, diffs by txid set so an
      // unchanged (e.g. still-empty) mempool never re-renders / layout-shifts.
      function esc(s) {{
        return String(s == null ? '' : s)
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
      }}
      function shortHash(h) {{
        h = String(h || '');
        if (h.length <= 21) return h;
        return h.slice(0, 10) + '\u2026' + h.slice(-10);
      }}
      function copyable(text, href) {{
        if (!text) return '<span class="muted">\u2014</span>';
        return '<span class="copy-wrap"><a class="mono" href="' + href + '" title="' + esc(text) + '">' +
          esc(shortHash(text)) + '</a><button type="button" class="copy-btn" data-copy="' + esc(text) +
          '" title="Copy">\u23d8</button></span>';
      }}
      function addrCell(a) {{
        if (!a) return '<span class="muted">\u2014</span>';
        return copyable(a, '/address/' + encodeURIComponent(a));
      }}
      function renderRows(txs) {{
        if (!txs || !txs.length) {{
          return '<tr><td colspan="8" class="muted">Mempool empty — no unconfirmed transactions on this node.</td></tr>';
        }}
        return txs.map(function (tx) {{
          var amt = tx.amount_mhc || tx.output_value_mhc || '\u2014';
          var rbf = tx.rbf === true ? '<span class="pill xfer">RBF</span>' : '<span class="muted">\u2014</span>';
          return '<tr>' +
            '<td class="hash-col">' + copyable(tx.txid, '/tx/' + encodeURIComponent(tx.txid || '')) + '</td>' +
            '<td>' + addrCell(tx.from) + '</td>' +
            '<td>' + addrCell(tx.to) + '</td>' +
            '<td class="num">' + esc(amt) + '</td>' +
            '<td class="num">' + esc(tx.fee_mhc || '\u2014') + '</td>' +
            '<td class="num">' + esc(tx.fee_rate || '\u2014') + '</td>' +
            '<td class="num">' + esc(tx.vsize || tx.size_bytes || '\u2014') + '</td>' +
            '<td>' + rbf + '</td>' +
            '</tr>';
        }}).join('');
      }}
      async function tick() {{
        try {{
          var r = await fetch('/api/mempool?_=' + Date.now(), {{ cache: 'no-store' }});
          if (!r.ok) return;
          var d = await r.json();
          var txs = d.transactions || [];
          var key = txs.map(function (t) {{ return t.txid || ''; }}).join('|');
          var wrap = document.getElementById('mempoolWrap');
          if (wrap && wrap.dataset.key !== key) {{
            wrap.dataset.key = key;
            wrap.innerHTML = '<div class="table-wrap"><table class="data-table">' +
              '<tr><th class="hash-col">Txid</th><th>From</th><th>To</th>' +
              '<th class="num">Amount</th><th class="num">Fee</th><th class="num">Fee rate</th>' +
              '<th class="num">vSize</th><th>RBF</th></tr>' +
              renderRows(txs) + '</table></div>';
          }}
          var cnt = document.getElementById('mempoolCount');
          if (cnt) {{
            var age = d.updated_age || '\u2014';
            var txt = (d.count || 0) + ' unconfirmed \u00b7 snapshot ' + age + ' ago';
            if (cnt.textContent !== txt) cnt.textContent = txt;
          }}
        }} catch (e) {{}}
      }}
      setInterval(tick, 4000);
      setTimeout(tick, 4000);
    }})();
    </script>
    """
    return _page("Mempool", body)


def _render_supply(data: dict[str, Any]) -> bytes:
    schedule = data.get("schedule") or []
    tip = int(data.get("tip_height") or 0)
    rows = []
    for row in schedule:
        era = row.get("era")
        from_h = row.get("from_height")
        to_h = row.get("to_height")
        reached = tip >= from_h
        passed = tip > to_h
        status = (
            '<span class="pill xfer">current</span>'
            if reached and not passed
            else ('<span class="badge">done</span>' if passed else '<span class="muted">upcoming</span>')
        )
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(era)}</td>'
            f'<td class="num">#{_esc(from_h)}</td>'
            f'<td class="num">#{_esc(to_h)}</td>'
            f'<td class="num"><strong class="reward">{_esc(row.get("subsidy_mhc"))} MHC</strong></td>'
            f"<td>{status}</td>"
            "</tr>"
        )
    body = f"""
    <div class="shell">
      <div class="kpi">
        <div class="stat k-height">
          <div class="lbl">Tip height</div>
          <div class="val"><a href="/block/{_esc(tip)}">#{_esc(tip)}</a></div>
        </div>
        <div class="stat k-mint">
          <div class="lbl">Current block reward</div>
          <div class="val">{_esc(data.get("current_subsidy_mhc"))} MHC</div>
        </div>
        <div class="stat k-supply">
          <div class="lbl">Minted (subsidy)</div>
          <div class="val">{_esc(data.get("minted_mhc"))} MHC</div>
          <div class="hint">{_esc(data.get("minted_pct"))}% of hard cap</div>
        </div>
        <div class="stat k-blocks">
          <div class="lbl">Hard cap</div>
          <div class="val">21,000,000 MHC</div>
          <div class="hint">{_esc(data.get("remaining_mhc"))} MHC still to mine</div>
        </div>
        <div class="stat k-halving">
          <div class="lbl">Next halving</div>
          <div class="val"><a href="/block/{_esc(data.get("next_halving_height"))}">#{_esc(data.get("next_halving_height"))}</a></div>
          <div class="hint">{_esc(data.get("blocks_to_halving"))} blocks to go</div>
        </div>
        <div class="stat k-eta">
          <div class="lbl">Est. halving ETA</div>
          <div class="val">{_esc(data.get("next_halving_eta"))}</div>
          <div class="hint">{_esc(data.get("eta_basis"))}</div>
        </div>
      </div>
      <section class="card">
        <div class="card-head">
          <h1>Subsidy schedule</h1>
          <span class="muted">era {_esc(data.get("era"))} of {_esc(len(schedule) - 1 if schedule else 0)} · {_esc(data.get("halving_interval"))} blocks/era</span>
        </div>
        <p class="muted" style="margin:0 0 .75rem;font-size:.85rem">
          Reward halves every {_esc(data.get("halving_interval"))} blocks (integer right-shift of the
          initial subsidy) until it rounds to zero — defined in
          <span class="mono">mhcoin.consensus.block_reward.get_block_subsidy</span>.
          · <a href="/api/supply">JSON</a>
        </p>
        <div class="table-wrap"><table class="data-table">
          <tr><th class="num">Era</th><th class="num">From height</th><th class="num">To height</th><th class="num">Reward</th><th>Status</th></tr>
          {"".join(rows) or '<tr><td colspan="5" class="muted">No schedule.</td></tr>'}
        </table></div>
      </section>
    </div>
    """
    return _page("Supply", body, tip=data.get("tip_height"))


def _render_orphans(data: dict[str, Any]) -> bytes:
    rows = []
    for b in data.get("blocks") or []:
        active_h = b.get("active_hash_at_height")
        active_html = (
            _copyable(active_h, short=False, href="/block/" + str(active_h))
            if active_h
            else '<span class="muted">—</span>'
        )
        tip_badge = (
            '<span class="pill xfer">fork tip</span>'
            if b.get("is_fork_tip")
            else '<span class="muted">superseded</span>'
        )
        rows.append(
            "<tr>"
            f'<td class="num">#{_esc(b.get("height"))}</td>'
            f'<td class="hash-col">{_copyable(b.get("hash"), short=False)}</td>'
            f'<td class="hash-col">{active_html}</td>'
            f'<td class="num">{_esc(b.get("difficulty_display"))}</td>'
            f'<td class="muted nowrap">{_esc(b.get("age") or "—")}</td>'
            f'<td class="muted nowrap">{_esc(b.get("time_utc") or "—")}</td>'
            f"<td>{tip_badge}</td>"
            "</tr>"
        )
    body = f"""
    <div class="shell">
      <div class="card">
        <div class="card-head">
          <h1>Orphans &amp; reorgs</h1>
          <span class="muted">{_esc(data.get("count") or 0)} side-chain block(s) on disk · {_esc(data.get("fork_tip_count") or 0)} fork tip(s)</span>
        </div>
        <p class="muted" style="margin:0 0 .75rem;font-size:.85rem">
          {_esc(data.get("gap_note") or "")}
          · <a href="/api/orphans">JSON</a>
        </p>
        <div class="table-wrap"><table class="data-table">
          <tr>
            <th class="num">Height</th><th class="hash-col">Side-chain hash</th><th class="hash-col">Active hash (won)</th>
            <th class="num">Difficulty</th><th>Age</th><th>Time (UTC)</th><th>Branch</th>
          </tr>
          {"".join(rows) or '<tr><td colspan="7" class="muted">No stored side-chain blocks — this node has not seen a reorg (yet).</td></tr>'}
        </table></div>
      </div>
    </div>
    """
    return _page("Orphans", body, tip=data.get("tip_height"))


def make_handler(app: ExplorerApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MHCOINExplorer/1"
        sys_version = ""

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _client(self) -> str:
            return (self.client_address or ("unknown", 0))[0]

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; "
                "base-uri 'self'; "
                "form-action 'self'",
            )

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            raw = json.dumps(obj, indent=2, sort_keys=True, default=str).encode()
            self._send(code, raw, "application/json; charset=utf-8")

        def _html(self, code: int, body: bytes) -> None:
            self._send(code, body, "text/html; charset=utf-8")

        def _error(self, code: int, message: str, *, want_json: bool) -> None:
            if want_json:
                self._json(code, {"error": message, "code": code})
            else:
                self._html(code, _page("Error", f'<p class="err">{_esc(message)}</p>'))

        def _static(self, name: str) -> bool:
            path = STATIC_FILES.get(name)
            if path is None or not path.is_file():
                return False
            data = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            if name.endswith(".svg"):
                ctype = "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self._security_headers()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return True

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path or "/")
            qs = parse_qs(parsed.query)
            want_json = path == "/api" or path.startswith("/api/")
            key = path
            if want_json:
                key = path[4:] or "/"
                if not key.startswith("/"):
                    key = "/" + key

            try:
                if key.startswith("/static/"):
                    name = key[len("/static/") :].strip("/")
                    if not self._static(name):
                        self._error(404, "Asset not found.", want_json=want_json)
                    return
                if key in (
                    "/favicon.ico",
                    "/apple-touch-icon.png",
                    "/apple-touch-icon-precomposed.png",
                ):
                    asset = "favicon.ico" if key == "/favicon.ico" else "logo.png"
                    if not self._static(asset):
                        self.send_response(404)
                        self.end_headers()
                    return

                if key in ("/health", "/health/"):
                    self._json(200, app.health())
                    return

                if key == "/robots.txt":
                    body = (
                        "User-agent: *\n"
                        "Allow: /\n"
                        "Disallow: /api/\n"
                        "Sitemap: /sitemap.xml\n"
                    ).encode()
                    self._send(200, body, "text/plain; charset=utf-8")
                    return

                if key == "/sitemap.xml":
                    tip = None
                    try:
                        tip = app.tip_status().get("tip_height")
                    except Exception:
                        tip = None
                    urls = [
                        "/",
                        "/blocks",
                        "/transactions",
                        "/richlist",
                        "/stats",
                        "/charts",
                        "/mempool",
                        "/supply",
                        "/orphans",
                        "/block/0",
                    ]
                    if isinstance(tip, int) and tip >= 0:
                        urls.append(f"/block/{tip}")
                    items = "".join(
                        f"<url><loc>{u}</loc><changefreq>hourly</changefreq></url>"
                        for u in urls
                    )
                    xml = (
                        '<?xml version="1.0" encoding="UTF-8"?>'
                        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f"{items}</urlset>"
                    ).encode()
                    self._send(200, xml, "application/xml; charset=utf-8")
                    return

                if key in ("/tip", "/tip/", "/status", "/status/"):
                    # Hot poll path — never rate-limit (cached server-side).
                    self._json(200, app.tip_status())
                    return

                if key in ("/", ""):
                    # HTML home is unrestricted; only full JSON dump is capped.
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.home()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_home(data))
                    return

                if key in ("/blocks", "/blocks/"):
                    page = 1
                    try:
                        page = int((qs.get("page") or ["1"])[0])
                    except ValueError:
                        page = 1
                    data = app.blocks(page=page)
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_blocks(data))
                    return

                if key in ("/transactions", "/transactions/", "/txs", "/txs/"):
                    page = 1
                    try:
                        page = int((qs.get("page") or ["1"])[0])
                    except ValueError:
                        page = 1
                    data = app.transactions(page=page)
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_transactions(data))
                    return

                if key.startswith("/block/"):
                    bkey = key[len("/block/") :].strip("/")
                    data = app.block(bkey)
                    if data is None:
                        self._error(404, "Block not found.", want_json=want_json)
                        return
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_block(data))
                    return

                if key.startswith("/tx/"):
                    txid = key[len("/tx/") :].strip("/").lower()
                    data = app.tx(txid)
                    if data is None:
                        self._error(404, "Transaction not found.", want_json=want_json)
                        return
                    focus = ((qs.get("addr") or qs.get("focus") or [""])[0] or "").strip()
                    if focus and validate_address(focus, hrp=app.hrp):
                        data = dict(data)
                        data["focus_address"] = focus
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_tx(data))
                    return

                if key in ("/richlist", "/richlist/"):
                    limit = 100
                    try:
                        limit = int((qs.get("limit") or ["100"])[0])
                    except ValueError:
                        limit = 100
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.richlist(limit=limit)
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_richlist(data))
                    return

                if key in ("/stats", "/stats/"):
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.stats()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_stats(data))
                    return

                if key in ("/charts", "/charts/"):
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.charts()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_charts(data))
                    return

                if key in ("/mempool", "/mempool/"):
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.mempool()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_mempool(data))
                    return

                if key in ("/supply", "/supply/", "/halving", "/halving/"):
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.supply()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_supply(data))
                    return

                if key in ("/orphans", "/orphans/", "/reorgs", "/reorgs/"):
                    if want_json and not app.allow_request(
                        self._client(), bucket="api-home", limit=_RATE_LIMIT_API_HOME
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=True,
                        )
                        return
                    data = app.orphans()
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_orphans(data))
                    return

                if key.startswith("/address/"):
                    if not app.allow_request(
                        self._client(), bucket="address", limit=_RATE_LIMIT_ADDRESS
                    ):
                        self._error(
                            429,
                            "Rate limit exceeded. Try again shortly.",
                            want_json=want_json,
                        )
                        return
                    addr = key[len("/address/") :].strip("/")
                    page = 1
                    per_page = D.ADDRESS_PER_PAGE
                    try:
                        page = int((qs.get("page") or ["1"])[0])
                    except ValueError:
                        page = 1
                    try:
                        per_page = int(
                            (qs.get("per_page") or qs.get("limit") or [str(D.ADDRESS_PER_PAGE)])[0]
                        )
                    except ValueError:
                        per_page = D.ADDRESS_PER_PAGE
                    data = app.address(addr, page=page, per_page=per_page)
                    if data is None:
                        self._error(404, "Invalid or unknown address.", want_json=want_json)
                        return
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_address(data))
                    return

                if key.startswith("/search"):
                    q = ((qs.get("q") or qs.get("query") or [""])[0] or "").strip()
                    if not q:
                        self._error(400, "Empty query.", want_json=want_json)
                        return
                    if q.isdigit():
                        self.send_response(302)
                        self.send_header("Location", f"/block/{int(q)}")
                        self._security_headers()
                        self.end_headers()
                        return
                    ql = q.lower()
                    if HEX64.match(ql):
                        if app.block(ql) is not None:
                            self.send_response(302)
                            self.send_header("Location", f"/block/{ql}")
                            self._security_headers()
                            self.end_headers()
                            return
                        if app.tx(ql) is not None:
                            self.send_response(302)
                            self.send_header("Location", f"/tx/{ql}")
                            self._security_headers()
                            self.end_headers()
                            return
                        self._error(404, "No block or tx with that hash.", want_json=want_json)
                        return
                    if ql.startswith(app.hrp + "1"):
                        if not validate_address(q, hrp=app.hrp):
                            self._error(400, "Invalid address checksum.", want_json=want_json)
                            return
                        self.send_response(302)
                        self.send_header("Location", f"/address/{q}")
                        self._security_headers()
                        self.end_headers()
                        return
                    self._error(400, "Unrecognized query.", want_json=want_json)
                    return

                self._error(404, "Not found.", want_json=want_json)
            except Exception:
                logger.exception("explorer request failed path=%s", path)
                try:
                    self._error(500, "Internal error.", want_json=want_json)
                except Exception:
                    pass

    return Handler


def serve(data_dir: Path, *, host: str, port: int, hrp: str = "mhc") -> None:
    app = ExplorerApp(data_dir, hrp=hrp)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    logger.info("MHCOIN explorer on http://%s:%s/ datadir=%s", host, port, data_dir)
    httpd.serve_forever()
