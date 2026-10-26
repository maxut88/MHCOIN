"""MHCOIN pool stats UI — explorer-matched design (DM Sans / JetBrains Mono).

Full miner dashboard (immature / pending / paid, short·long H/s, workers,
payments, blocks) styled like the MHCOIN explorer (:8766).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mhcoin.pool.db import PoolDB

logger = logging.getLogger("mhcoin.pool.web")

ASSETS_DIR = Path(__file__).resolve().parents[1] / "desktop" / "assets"
POOL_STATIC = {
    "favicon.ico": ASSETS_DIR / "mhcoin.ico",
    "logo.svg": ASSETS_DIR / "mhcoin-logo.svg",
    "logo.png": ASSETS_DIR / "mhcoin-256.png",
}

_LOGO_SVG_M = (
    '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/>'
    "</svg>"
)


def _fmt_hps(hps: float) -> str:
    h = float(hps or 0)
    if h >= 1e12:
        return f"{h / 1e12:.2f} TH/s"
    if h >= 1e9:
        return f"{h / 1e9:.2f} GH/s"
    if h >= 1e6:
        return f"{h / 1e6:.2f} MH/s"
    if h >= 1e3:
        return f"{h / 1e3:.1f} kH/s"
    return f"{h:.0f} H/s"


def _fmt_mhc_sats(sats: float | int | None) -> str:
    n = float(sats or 0) / 1e8
    s = f"{n:.8f}".rstrip("0").rstrip(".")
    if "." not in s:
        s += ".0"
    return f"{s} MHC"


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _page_shell(
    *,
    pool_address: str,
    json_port: int,
    stratum_port: int,
    fee_percent: float,
    share_factor: int,
    explorer_url: str,
    payout_threshold_sats: int,
    mature_confirms: int,
    overview: dict | None = None,
    miner_boot: dict | None = None,
    public_host: str = "mine.mhcscan.com",
) -> bytes:
    """SPA: pool overview + /miner/<addr> dashboard with tabs."""
    addr = _esc(pool_address or "—")
    exp = _esc(explorer_url.rstrip("/")) if explorer_url else ""
    exp_href = exp or "https://explorer.mhcscan.com"
    host_pub = (public_host or "mine.mhcscan.com").strip() or "mine.mhcscan.com"
    host_pub_esc = _esc(host_pub)
    threshold_mhc = payout_threshold_sats / 1e8
    ov = overview if isinstance(overview, dict) else {}
    mb = miner_boot if isinstance(miner_boot, dict) else None
    # Miner SSR — avoid "—" flash on /miner/<addr> refresh (same idea as pool KPIs).
    view_home_cls = "hidden" if mb else ""
    view_miner_cls = "" if mb else "hidden"
    m_addr = str(mb.get("address") or "") if mb else ""
    m_title = _esc(m_addr) if m_addr else "Miner"
    m_page_title = (
        f"{_esc(m_addr[:10] + '…' + m_addr[-6:]) if len(m_addr) > 20 else _esc(m_addr)} — MHCOIN Pool"
        if m_addr
        else "MHCOIN Pool"
    )
    m_thr = int(mb.get("payout_threshold_sats") or payout_threshold_sats) if mb else payout_threshold_sats
    m_mat = int(mb.get("mature_confirms") or mature_confirms) if mb else mature_confirms
    m_pending = int(
        mb.get("pending_sats")
        if mb and mb.get("pending_sats") is not None
        else (mb.get("matured_sats") if mb else 0) or 0
    )
    m_imm = int(mb.get("immature_sats") or 0) if mb else 0
    m_paid = int(mb.get("paid_sats") or 0) if mb else 0
    m_h_short = float(mb.get("hashrate_short") or mb.get("hashrate") or 0) if mb else 0.0
    m_h_long = float(mb.get("hashrate_long") or 0) if mb else 0.0
    m_workers = list(mb.get("workers") or []) if mb else []
    m_won = int(mb.get("workers_online") or 0) if mb else 0
    m_immature = _esc(_fmt_mhc_sats(m_imm)) if mb else "—"
    m_pending_txt = _esc(_fmt_mhc_sats(m_pending)) if mb else "—"
    m_paid_txt = _esc(_fmt_mhc_sats(m_paid)) if mb else "—"
    m_hash_short = _esc(_fmt_hps(m_h_short)) if mb else "—"
    m_hash_long = _esc(_fmt_hps(m_h_long)) if mb else "—"
    m_workers_on = _esc(f"{m_won} / {len(m_workers)}") if mb else "—"
    m_meta = (
        _esc(
            f"threshold {m_thr / 1e8:g} MHC · mature {m_mat} confirms · {m_won} worker(s) online"
        )
        if mb
        else "—"
    )
    if mb:
        _ls = mb.get("last_share")
        if _ls:
            _age = max(0.0, time.time() - float(_ls))
            if _age < 60:
                m_last_share = _esc(f"last share {int(_age)}s ago")
            elif _age < 3600:
                m_last_share = _esc(f"last share {int(_age // 60)}m ago")
            elif _age < 86400:
                m_last_share = _esc(f"last share {int(_age // 3600)}h ago")
            else:
                m_last_share = _esc(f"last share {int(_age // 86400)}d ago")
        else:
            m_last_share = "last share never"
    else:
        m_last_share = "last share —"
    m_unpaid = m_imm + m_pending
    m_chart_hr_live = _esc(_fmt_hps(m_h_short)) if mb else "—"
    m_chart_pay_live = (
        _esc(
            f"earned {_fmt_mhc_sats(m_unpaid + m_paid)}"
            f" · paid {_fmt_mhc_sats(m_paid)}"
            f" · unpaid {_fmt_mhc_sats(m_unpaid)}"
            + (
                f" · {int(mb.get('payments_count') or 0)} tx"
                if int(mb.get("payments_count") or 0)
                else ""
            )
        )
        if mb
        else "—"
    )
    m_shares_txt = _esc(str(int(mb.get("shares") or 0))) if mb else "—"
    m_shares_win = (
        _esc(
            f"short {int(mb.get('shares_short') or 0)} · long {int(mb.get('shares_long') or 0)}"
        )
        if mb
        else "short — · long —"
    )
    m_blocks_found = (
        _esc(
            str(
                int(
                    mb.get("blocks_found")
                    if mb.get("blocks_found") is not None
                    else len(mb.get("blocks") or [])
                )
            )
        )
        if mb
        else "—"
    )
    m_pay_count = (
        _esc(
            str(
                int(
                    mb.get("payments_count")
                    if mb.get("payments_count") is not None
                    else len(mb.get("payments") or [])
                )
            )
        )
        if mb
        else "—"
    )
    m_prog = min(100.0, (100.0 * m_pending / m_thr)) if mb and m_thr > 0 else 0.0
    if mb:
        _left = max(0, m_thr - m_pending)
        m_left_txt = _esc(
            f"{_fmt_mhc_sats(_left)} left until auto-payout"
            if _left > 0
            else "Threshold reached — payout on next cycle"
        )
    else:
        m_left_txt = ""

    miner_boot_json = (
        json.dumps(mb, separators=(",", ":"), default=str).replace("<", "\\u003c")
        if mb
        else "null"
    )
    # SSR KPI values so a browser refresh does not flash "—" → numbers.
    k_hash = _esc(_fmt_hps(float(ov.get("pool_hashrate") or 0))) if ov else "—"
    k_workers = _esc(str(int(ov.get("workers_active") or 0))) if ov else "—"
    k_miners_hint = (
        f"miners total {_esc(str(int(ov.get('miners_total') or 0)))}" if ov else "miners total —"
    )
    k_shares = _esc(str(int(ov.get("shares_1h") or 0))) if ov else "—"
    k_round_shares = (
        f"this round {_esc(str(int(ov.get('shares_round') or 0)))}" if ov else "this round —"
    )
    k_round_id = ov.get("current_round") if ov else None
    k_round = _esc(f"#{k_round_id}") if k_round_id is not None else "—"
    k_round_href = f"/round/{int(k_round_id)}" if k_round_id is not None else "#"
    blocks_total = int(ov.get("blocks_total") or 0) if ov else 0
    k_blocks = _esc(str(blocks_total)) if ov else "—"
    chart_pool_live = k_hash
    chart_blocks_live = _esc(f"{blocks_total} blocks") if ov else "—"
    # Network panel SSR (same keys as /api/stats metrics attach).
    n_tip_raw = ov.get("mainnet_tip")
    n_tip = _esc(f"#{n_tip_raw}") if n_tip_raw is not None else "—"
    n_peers = (
        _esc(str(int(ov["peer_count"]))) if ov.get("peer_count") is not None else "—"
    )
    n_peers_hint = (
        f"target {_esc(str(int(ov['outbound_target'])))}"
        if ov.get("outbound_target") is not None
        else "connected / target"
    )
    n_addrs = (
        _esc(str(int(ov["known_addrs"]))) if ov.get("known_addrs") is not None else "—"
    )
    n_orphans = (
        _esc(str(int(ov["orphaned_rounds"])))
        if ov.get("orphaned_rounds") is not None
        else "—"
    )
    n_stale = (
        _esc(str(int(ov["stale_immature_rounds"])))
        if ov.get("stale_immature_rounds") is not None
        else "—"
    )
    peers_boot = ov.get("peers") or []
    if peers_boot:
        peers_html = "".join(
            f'<div class="peer-row">{"←" if p.get("inbound") else "→"} '
            f'{_esc(str(p.get("addr") or "?"))} · {_esc(str(p.get("state") or ""))}'
            f'{(" · #" + _esc(str(p.get("height")))) if p.get("height") is not None else ""}'
            f"</div>"
            for p in peers_boot[:16]
            if isinstance(p, dict)
        )
    else:
        peers_html = (
            "No handshaked peers yet — check MHCOIN_CONNECT / seeds."
            if ov.get("peer_count") is None
            else "No handshaked peers right now."
        )
    boot_json = json.dumps(ov, separators=(",", ":"), default=str).replace("<", "\\u003c")
    # Logo button doubles as day/night toggle (same pattern as explorer).
    logo = (
        '<button type="button" class="theme-toggle" id="themeBtn" '
        'aria-label="Toggle day / night" title="Toggle day / night" aria-pressed="false">'
        '<span class="theme-coin" aria-hidden="true">'
        '<span class="tc-ring"></span><span class="tc-ring2"></span>'
        f'<span class="tc-core">{_LOGO_SVG_M}</span>'
        "</span></button>"
    )
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#ffffff" id="metaThemeColor"/>
<script>
(function(){{
  try {{
    var saved = localStorage.getItem('mhcoin-theme');
    var dark = saved ? saved === 'dark' : (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (dark) document.documentElement.setAttribute('data-theme', 'dark');
  }} catch (e) {{}}
  // Mobile F5 with /#blocks jumps past the sticky header — start at top on every load.
  try {{
    if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
    var h = location.hash || '';
    if (/^#(blocks|miners|connect)$/i.test(h)) {{
      history.replaceState(null, '', location.pathname + location.search);
    }}
    window.scrollTo(0, 0);
  }} catch (e) {{}}
}})();
</script>
<meta name="description" content="MHCOIN mining pool — HASH256 · PROP · live hashrate and payouts."/>
<link rel="icon" href="/favicon.ico?v=3" sizes="any"/>
<link rel="icon" type="image/svg+xml" href="/static/logo.svg?v=3"/>
<link rel="apple-touch-icon" href="/static/logo.png?v=3"/>
<link rel="shortcut icon" href="/favicon.ico?v=3"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@450;600&display=optional" rel="stylesheet"/>
<title>{m_page_title}</title>
<style>
  :root {{
    --bg: #f4f5f7; --panel: #ffffff; --text: #111418; --muted: #6b7280;
    --link: #111418; --line: #e5e7eb; --soft: #f9fafb;
    --header-bg: rgba(255,255,255,.94); --hover-bg: #ffffff; --invert: #000000;
    --val: #374151; --mint: #0f7a55; --mint-soft: rgba(45,212,160,.14);
    --warn: #b45309; --warn-soft: rgba(245,158,11,.14);
    --bad: #b91c1c; --bad-soft: rgba(239,68,68,.12);
    --sans: "DM Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
  }}
  [data-theme="dark"] {{
    --bg: #0b0d10; --panel: #15181d; --text: #e7eaee; --muted: #9aa3af;
    --link: #e7eaee; --line: #262b33; --soft: #1b1f26;
    --header-bg: rgba(11,13,16,.92); --hover-bg: #1e222a; --invert: #ffffff;
    --val: #c7ccd4; --mint: #5dffc0; --mint-soft: rgba(45,212,160,.12);
    --warn: #fbbf24; --warn-soft: rgba(245,158,11,.16);
    --bad: #f87171; --bad-soft: rgba(239,68,68,.14);
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--bg); color-scheme: light; overflow-x: clip; }}
  [data-theme="dark"] html, html[data-theme="dark"] {{ color-scheme: dark; }}
  body {{
    margin: 0; font-family: var(--sans); color: var(--text); line-height: 1.5;
    background: var(--bg); min-height: 100vh; overflow-x: clip; max-width: 100%;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  header.top {{
    position: sticky; top: 0; z-index: 20;
    background: var(--header-bg);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--line);
    padding: .85rem 1.25rem .8rem;
  }}
  .header-inner {{
    max-width: 1100px; margin: 0 auto; width: 100%;
    display: flex; flex-wrap: wrap; gap: .75rem 1.25rem;
    align-items: center; justify-content: space-between; min-width: 0;
  }}
  .brand-row {{
    display: flex; align-items: center; gap: .85rem; min-width: 0; flex: 1 1 auto;
  }}
  .brand {{ min-width: 0; }}
  .brand h1 {{
    margin: 0; font-size: 1.15rem; font-weight: 800; letter-spacing: -.02em;
    line-height: 1.2;
  }}
  .brand h1 a {{ color: var(--text); text-decoration: none; }}
  .brand h1 a:hover {{ color: var(--mint); }}
  .brand .sub {{
    margin: .1rem 0 0; color: var(--muted); font-size: .78rem;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  header nav {{
    display: flex; flex-wrap: wrap; gap: .25rem .7rem; align-items: center;
    min-width: 0; max-width: 100%;
  }}
  header nav a {{
    font-size: .84rem; font-weight: 600; color: var(--muted);
    border-bottom: 2px solid transparent; padding-bottom: 2px;
    white-space: nowrap;
  }}
  header nav a:hover, header nav a.active {{
    color: var(--text); border-bottom-color: var(--text); text-decoration: none;
  }}
  /* Logo = day/night toggle (explorer-style PoW coin) */
  .theme-toggle {{
    flex: 0 0 auto; width: 2.75rem; height: 2.75rem; padding: 0; border-radius: 999px;
    border: 1px solid var(--line); background: transparent; cursor: pointer;
    display: grid; place-items: center; position: relative; overflow: visible;
    transition: transform .25s ease, border-color .2s ease, box-shadow .25s ease;
  }}
  .theme-toggle:hover {{ transform: scale(1.08); border-color: #2dd4a0; }}
  .theme-toggle:active {{ transform: scale(.94) rotate(-18deg); }}
  .theme-toggle:focus-visible {{ outline: 2px solid #2dd4a0; outline-offset: 2px; }}
  .theme-coin {{
    --tc-size: 1.95rem; width: var(--tc-size); height: var(--tc-size);
    position: relative; display: grid; place-items: center; border-radius: 50%;
  }}
  .theme-coin .tc-ring, .theme-coin .tc-ring2 {{
    position: absolute; inset: -3px; border-radius: 50%;
    border: 1.5px solid transparent; pointer-events: none;
  }}
  .theme-coin .tc-core {{
    width: 100%; height: 100%; border-radius: 50%;
    display: grid; place-items: center; position: relative; overflow: hidden; z-index: 1;
  }}
  .theme-coin .tc-core svg {{
    width: 58%; height: 58%; display: block; position: relative; z-index: 2;
    filter: drop-shadow(0 1px 0 rgba(0,40,25,.3));
  }}
  .theme-coin .tc-core::after {{
    content: ""; position: absolute; inset: -45% -25%; z-index: 1;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.55), transparent 70%);
  }}
  html:not([data-theme="dark"]) .theme-coin .tc-core {{
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.55), transparent 42%),
      linear-gradient(145deg, #7dffc8 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.4), 0 0 16px rgba(250,204,21,.35),
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
  [data-theme="dark"] .theme-coin .tc-core {{
    background:
      radial-gradient(circle at 30% 26%, rgba(165,243,252,.35), transparent 40%),
      linear-gradient(145deg, #1a9b72 0%, #0d5c44 48%, #06261a 100%);
    box-shadow:
      0 0 0 1px rgba(34,211,238,.35), 0 0 18px rgba(34,211,238,.28),
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
    box-shadow: 0 0 16px rgba(34,211,238,.4); border-color: #22d3ee;
  }}
  .theme-toggle.tc-found .theme-coin {{
    animation: tc-block-found .7s cubic-bezier(.2,.8,.2,1);
  }}
  @keyframes tc-mine-spin {{ to {{ transform: rotate(360deg); }} }}
  @keyframes tc-day-throb {{
    0%,100% {{ transform: scale(1); }} 50% {{ transform: scale(1.06); }}
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
    100% {{ transform: rotateY(360deg) scale(1); }}
  }}
  @keyframes throb {{ 0%,100% {{ transform: scale(1); }} 50% {{ transform: scale(1.04); }} }}
  @keyframes sheen {{
    0%,100% {{ transform: translateX(-30%) rotate(12deg); opacity: .35; }}
    50% {{ transform: translateX(30%) rotate(12deg); opacity: .7; }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .theme-coin .tc-ring, .theme-coin .tc-ring2, .theme-coin .tc-core,
    .theme-coin .tc-core::after, .theme-toggle.tc-found .theme-coin,
    .mint-coin, .mint-coin::after {{ animation: none !important; }}
  }}
  main {{
    max-width: 1100px; margin: 0 auto; padding: 1.25rem 1.25rem 3rem;
    width: 100%; min-width: 0;
  }}
  .hero {{ margin: .35rem 0 1.1rem; min-width: 0; }}
  .hero h2 {{
    margin: 0 0 .35rem; font-size: clamp(1.25rem, 4vw, 1.65rem);
    font-weight: 800; letter-spacing: -.03em;
  }}
  .hero p {{
    margin: 0; color: var(--muted); max-width: 52rem;
    font-size: .92rem; overflow-wrap: anywhere; word-break: break-word;
  }}
  .hero .mono {{ font-size: .78rem; }}
  /* KPI tiles — match explorer home (compact accent cards). */
  .kpi {{
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap: .5rem; margin: .75rem 0 1rem;
  }}
  @media (max-width: 1280px) {{
    .kpi {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
  }}
  @media (max-width: 720px) {{
    .kpi {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  }}
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
  .kpi .lbl, .kpi .label {{
    position: relative; z-index: 1;
    color: var(--muted); font-size: .54rem; font-weight: 720;
    text-transform: uppercase; letter-spacing: .06em; line-height: 1.15;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }}
  .kpi .val {{
    position: relative; z-index: 1;
    font-family: var(--mono); font-size: .92rem; font-weight: 700;
    margin-top: .12rem; letter-spacing: -.03em;
    line-height: 1.12; min-height: 0; color: var(--text);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere; word-break: break-word;
  }}
  .kpi .hint {{
    position: relative; z-index: 1;
    color: var(--muted); font-size: .6rem; margin-top: .2rem;
    line-height: 1.25; min-height: 0;
    border-top: 1px solid var(--line); padding-top: .2rem;
    overflow-wrap: anywhere;
  }}
  .kpi .stat.k-hash {{ --accent: #16a34a; --accent-soft: rgba(22,163,74,.10); }}
  .kpi .stat.k-miners {{ --accent: #0ea5e9; --accent-soft: rgba(14,165,233,.10); }}
  .kpi .stat.k-shares {{ --accent: #7c3aed; --accent-soft: rgba(124,58,237,.10); }}
  .kpi .stat.k-round {{ --accent: #d97706; --accent-soft: rgba(217,119,6,.10); }}
  .kpi .stat.k-fee {{ --accent: #0f766e; --accent-soft: rgba(15,118,110,.10); }}
  .kpi .stat.k-blocks {{ --accent: #475569; --accent-soft: rgba(71,85,105,.10); }}
  .kpi .stat.k-imm {{ --accent: #d97706; --accent-soft: rgba(217,119,6,.10); }}
  .kpi .stat.k-pend {{ --accent: #0ea5e9; --accent-soft: rgba(14,165,233,.10); }}
  .kpi .stat.k-paid {{ --accent: #16a34a; --accent-soft: rgba(22,163,74,.10); }}
  .panel {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.1rem; margin: 0 0 1rem;
    overflow: hidden; min-width: 0;
  }}
  .panel h3 {{ margin: 0 0 .75rem; font-size: 1rem; font-weight: 750; }}
  .panel .muted {{ color: var(--muted); font-size: .88rem; overflow-wrap: anywhere; }}
  .connect-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
    gap: .85rem; min-width: 0;
  }}
  .connect-card {{
    background: var(--soft); border: 1px solid var(--line); border-radius: 12px;
    padding: .85rem 1rem;
  }}
  .connect-card .t {{
    font-size: .72rem; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; color: var(--muted); margin-bottom: .35rem;
  }}
  code, .mono {{ font-family: var(--mono); font-size: .84rem; }}
  .cmd {{
    display: block; background: var(--bg); border: 1px solid var(--line);
    border-radius: 10px; padding: .7rem .85rem; margin-top: .45rem;
    white-space: pre-wrap; word-break: break-word; line-height: 1.45;
  }}
  .copy-row {{ display: flex; gap: .5rem; align-items: flex-start; margin-top: .45rem; }}
  .copy-row button {{
    flex-shrink: 0; border: 1px solid var(--line); background: var(--panel);
    color: var(--text); border-radius: 8px; padding: .45rem .65rem;
    font: inherit; font-size: .78rem; font-weight: 650; cursor: pointer;
  }}
  .copy-row button:hover {{ background: var(--hover-bg); }}
  .table-scroll {{
    width: 100%; max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch;
    margin: 0; border-radius: 10px;
  }}
  table {{ width: 100%; border-collapse: collapse; min-width: 0; }}
  /* Stable columns — avoid label/hash jump on 8s auto-refresh. */
  #miners table {{ table-layout: fixed; width: 100%; }}
  #miners th:nth-child(1), #miners td:nth-child(1) {{ width: 28%; }}
  #miners th:nth-child(2), #miners td:nth-child(2) {{ width: 18%; }}
  #miners th:nth-child(3), #miners td:nth-child(3) {{ width: 18%; }}
  #miners th:nth-child(4), #miners td:nth-child(4) {{ width: 36%; }}
  #miners td.w-addr,
  #miners td.w-name,
  #miners td.w-hr,
  #miners td.w-st {{
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 0;
  }}
  #miners td.w-st {{ white-space: nowrap; }}
  /* Full block hash on one line; scroll sideways instead of wrapping zeros. */
  #blocks .table-scroll {{
    overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch;
  }}
  #blocks table {{
    table-layout: auto; width: max-content; min-width: 100%;
    border-collapse: collapse;
  }}
  #blocks th, #blocks td {{ white-space: nowrap; vertical-align: middle; }}
  #blocks th:nth-child(1), #blocks td:nth-child(1) {{ width: 1%; padding-right: .6rem; }}
  #blocks th:nth-child(2), #blocks td:nth-child(2) {{ width: auto; }}
  #blocks th:nth-child(3), #blocks td:nth-child(3) {{ width: 1%; padding-left: .75rem; }}
  #blocks th:nth-child(4), #blocks td:nth-child(4) {{ width: 1%; padding-left: .75rem; text-align: right; }}
  #workersBody, #blocksBody {{ contain: layout style; }}
  #blocks td.b-h {{ font-weight: 700; }}
  #connect, #miners, #blocks {{ scroll-margin-top: 5.5rem; }}
  #blocks td.b-time-cell {{ white-space: nowrap; color: var(--muted); font-size: .82rem; }}
  #blocks td.b-hash,
  #blocks td.b-hash a {{
    font-family: var(--mono); font-size: .72rem; line-height: 1.25;
    letter-spacing: -0.02em;
    white-space: nowrap !important;
    word-break: keep-all !important;
    overflow-wrap: normal !important;
  }}
  #blocks td.b-reward,
  #blocks td.b-status {{ white-space: nowrap; }}
  .hash-full {{ display: inline; }}
  .hash-short {{ display: none; }}
  #miners .mint-coin, #miners .mint-coin::after,
  #blocks .mint-coin, #blocks .mint-coin::after {{
    animation: none !important;
  }}
  th, td {{
    text-align: left; padding: .5rem .35rem; border-bottom: 1px solid var(--line);
    font-size: .84rem; vertical-align: middle;
  }}
  th {{
    color: var(--muted); font-size: .68rem; font-weight: 700;
    letter-spacing: .05em; text-transform: uppercase; white-space: nowrap;
  }}
  td.mono {{ font-family: var(--mono); font-size: .78rem; }}
  td.hash {{
    font-family: var(--mono); font-size: .72rem; line-height: 1.25;
    white-space: nowrap; word-break: keep-all; overflow-wrap: normal;
  }}
  td.hash a {{
    color: var(--link); text-decoration: none;
    white-space: nowrap; word-break: keep-all;
  }}
  td.hash a:hover {{ text-decoration: underline; }}
  .reward-cell {{
    display: inline-flex; align-items: center; gap: .35rem;
    font-family: var(--mono); font-size: .78rem; font-weight: 650;
    white-space: nowrap; max-width: 100%;
  }}
  .reward-cell > span:last-child {{
    overflow: hidden; text-overflow: ellipsis;
  }}
  .reward-full {{ display: inline; }}
  .reward-short {{ display: none; }}
  .mint {{
    --mint: 16px; width: var(--mint); height: var(--mint);
    display: inline-flex; align-items: center; justify-content: center;
    flex: 0 0 var(--mint); position: relative; border-radius: 50%;
  }}
  .mint-coin {{
    width: 100%; height: 100%; border-radius: 50%; clip-path: circle(50%);
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.55), transparent 42%),
      linear-gradient(145deg, #7dffc8 0%, #2dd4a0 45%, #0f7a55 100%);
    box-shadow: 0 0 0 1px rgba(45,212,160,.45), 0 0 8px rgba(45,212,160,.2);
    display: grid; place-items: center; position: relative; overflow: hidden;
    animation: throb 2.6s ease-in-out infinite;
  }}
  .mint-coin::after {{
    content: ""; position: absolute; inset: -40% -20%; z-index: 0;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.45), transparent 70%);
    animation: sheen 3.2s ease-in-out infinite;
  }}
  .mint-coin svg {{ width: 58%; height: 58%; display: block; position: relative; z-index: 1; }}
  [data-theme="dark"] .mint-coin {{
    background:
      radial-gradient(circle at 30% 26%, rgba(165,243,252,.35), transparent 42%),
      linear-gradient(145deg, #1a9b72 0%, #0d5c44 48%, #06261a 100%);
    box-shadow: 0 0 0 1px rgba(34,211,238,.4), 0 0 10px rgba(34,211,238,.18);
  }}
  .pill {{
    display: inline-block; padding: .12rem .45rem; border-radius: 999px;
    font-size: .72rem; font-weight: 700; background: var(--mint-soft); color: var(--mint);
  }}
  .pill.off {{ background: var(--soft); color: var(--muted); }}
  .pill.sick {{ background: var(--warn-soft); color: var(--warn); }}
  .pill.dead {{ background: var(--bad-soft); color: var(--bad); }}
  .pill.immature {{ background: var(--warn-soft); color: var(--warn); }}
  .pill.matured {{ background: var(--mint-soft); color: var(--mint); }}
  .pill.orphaned, .pill.rejected {{ background: var(--bad-soft); color: var(--bad); }}
  .pill.stale {{ background: var(--warn-soft); color: var(--warn); }}
  .note-cell {{ font-size: .78rem; color: var(--muted); max-width: 22rem; line-height: 1.35; }}
  .note-cell.warn {{ color: var(--warn); }}
  .note-cell.bad {{ color: var(--bad); }}
  #flash .alert {{
    border: 1px solid var(--line); border-radius: 12px; padding: .65rem .85rem;
    margin: 0 0 .65rem; background: var(--panel); font-size: .88rem;
  }}
  #flash .alert.warn {{ border-color: #f59e0b55; background: var(--warn-soft); }}
  #flash .alert.crit {{ border-color: #ef444455; background: var(--bad-soft); }}
  #flash .alert.info {{ border-color: #38bdf855; background: var(--mint-soft); }}
  .net-panel .peer-row {{ font-size: .82rem; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  .lookup {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .5rem; }}
  .lookup input {{
    flex: 1; min-width: 200px; padding: .65rem .8rem; border-radius: 10px;
    border: 1px solid var(--line); background: var(--bg); color: var(--text);
    font: inherit;
  }}
  .lookup button, .btn {{
    padding: .65rem 1rem; border-radius: 10px; border: none;
    background: var(--text); color: var(--bg); font: inherit; font-weight: 700;
    cursor: pointer;
  }}
  .lookup button:hover, .btn:hover {{ opacity: .9; }}
  .btn.ghost {{
    background: transparent; color: var(--text); border: 1px solid var(--line);
  }}
  .btn-mint, .lookup button.btn-mint {{
    display: inline-flex; align-items: center; justify-content: center; gap: .4rem;
    padding: .7rem 1.15rem; border-radius: 10px; border: none;
    background: linear-gradient(145deg, #2dd4a0 0%, #0f7a55 100%);
    color: #06261a; font: inherit; font-weight: 800; font-size: .88rem;
    letter-spacing: -.01em; cursor: pointer;
    box-shadow: 0 1px 0 rgba(255,255,255,.25) inset, 0 6px 16px rgba(15,122,85,.22);
    transition: transform .15s ease, box-shadow .2s ease, filter .15s ease;
  }}
  .btn-mint:hover, .lookup button.btn-mint:hover {{
    opacity: 1; filter: brightness(1.05);
    box-shadow: 0 1px 0 rgba(255,255,255,.3) inset, 0 8px 20px rgba(15,122,85,.3);
    transform: translateY(-1px);
  }}
  .btn-mint:active {{ transform: translateY(0); }}
  .btn-outline, a.btn-outline {{
    display: inline-flex; align-items: center; justify-content: center; gap: .4rem;
    padding: .55rem .9rem; border-radius: 10px;
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    font: inherit; font-weight: 700; font-size: .82rem; text-decoration: none;
    cursor: pointer; transition: border-color .15s, background .15s, color .15s;
  }}
  .btn-outline:hover, a.btn-outline:hover {{
    border-color: #2dd4a0; color: var(--mint); background: var(--mint-soft);
    text-decoration: none;
  }}
  .docs-bar {{
    display: flex; flex-wrap: wrap; gap: .55rem; align-items: center;
    margin: 0 0 1rem;
  }}
  .docs-bar .lbl {{
    font-size: .78rem; font-weight: 650; color: var(--muted); margin-right: .15rem;
  }}
  .pager {{
    display: flex; flex-wrap: wrap; gap: .65rem; align-items: center;
    margin: .75rem 0 0; font-size: .84rem;
  }}
  .pager a {{
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    border-radius: 8px; padding: .35rem .65rem; font-weight: 650; text-decoration: none;
  }}
  .pager a:hover {{ background: var(--hover-bg); border-color: #9ca3af; text-decoration: none; }}
  .footer-row {{
    display: flex; flex-wrap: wrap; gap: .75rem 1.25rem;
    align-items: center; justify-content: space-between;
  }}
  .footer-links {{
    display: flex; flex-wrap: wrap; gap: .55rem .85rem; align-items: center;
  }}
  .footer-links a {{
    display: inline-flex; align-items: center; gap: .35rem;
    color: var(--muted); font-weight: 650; text-decoration: none;
  }}
  .footer-links a:hover {{ color: var(--mint); }}
  .gh-ico {{
    width: 1.1rem; height: 1.1rem; display: inline-block; vertical-align: -2px;
    fill: currentColor;
  }}
  #flash {{
    display: none; margin: .75rem 0; padding: .65rem .85rem; border-radius: 10px;
    background: var(--mint-soft); color: var(--text); font-size: .88rem;
  }}
  #flash.show {{ display: block; }}
  footer {{
    max-width: 1100px; margin: 0 auto; padding: 0 1.5rem 2rem;
    color: var(--muted); font-size: .8rem;
  }}
  .hidden {{ display: none !important; }}
  .tabs {{
    display: flex; flex-wrap: nowrap; gap: .25rem; margin: 0 0 1rem;
    border-bottom: 1px solid var(--line); padding-bottom: .45rem;
    overflow-x: auto; -webkit-overflow-scrolling: touch; max-width: 100%;
  }}
  .tabs button {{
    border: none; background: transparent; color: var(--muted);
    font: inherit; font-weight: 650; font-size: .86rem; padding: .4rem .7rem;
    border-radius: 8px; cursor: pointer; flex: 0 0 auto; white-space: nowrap;
  }}
  .tabs button:hover {{ background: var(--soft); color: var(--text); }}
  .tabs button.active {{
    background: var(--soft); color: var(--text);
    box-shadow: inset 0 -2px 0 var(--text);
  }}
  .progress {{
    height: 8px; border-radius: 999px; background: var(--soft);
    border: 1px solid var(--line); overflow: hidden; margin-top: .55rem;
  }}
  .progress > i {{
    display: block; height: 100%; width: 0%;
    background: linear-gradient(90deg, #2dd4a0, #0f7a55);
    transition: width .35s ease;
  }}
  /* Payout: classic bar + mini header-logo (day/night + speed by %) */
  .pay-threshold .pay-row {{
    display: flex; align-items: center; gap: .75rem; margin-top: .55rem;
  }}
  .pay-threshold .progress {{
    flex: 1 1 auto; min-width: 0; margin-top: 0;
  }}
  .pay-threshold .progress {{
    height: 10px;
  }}
  .pay-threshold .progress > i {{
    background: linear-gradient(90deg, #2dd4a0, #0f7a55);
  }}
  .pay-threshold .pay-spin {{
    --spin1: 2.8s; --spin2: 4.2s;
    --tc-size: 1.85rem;
    position: relative; flex: 0 0 auto;
    width: var(--tc-size); height: var(--tc-size);
    display: grid; place-items: center; border-radius: 50%;
  }}
  .pay-threshold .pay-spin .tc-ring,
  .pay-threshold .pay-spin .tc-ring2 {{
    position: absolute; inset: -4px; border-radius: 50%;
    border: 2.75px solid transparent; pointer-events: none;
  }}
  .pay-threshold .pay-spin .tc-core {{
    width: 100%; height: 100%; border-radius: 50%;
    display: grid; place-items: center; position: relative; overflow: hidden; z-index: 1;
  }}
  .pay-threshold .pay-spin .tc-core svg {{
    width: 58%; height: 58%; display: block; position: relative; z-index: 2;
    filter: drop-shadow(0 1px 0 rgba(0,40,25,.3));
  }}
  .pay-threshold .pay-spin .tc-core::after {{
    content: ""; position: absolute; inset: -45% -25%; z-index: 1;
    background: linear-gradient(115deg, transparent 30%, rgba(255,255,255,.55), transparent 70%);
  }}
  /* Day — match header logo */
  html:not([data-theme="dark"]) .pay-threshold .pay-spin .tc-core {{
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,.55), transparent 42%),
      linear-gradient(145deg, #7dffc8 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow:
      0 0 0 1px rgba(45,212,160,.4), 0 0 10px rgba(250,204,21,.3),
      0 0 8px rgba(45,212,160,.3),
      inset 0 -2px 3px rgba(0,40,25,.3), inset 0 2px 2px rgba(255,255,255,.4);
    animation: tc-day-throb 1.8s ease-in-out infinite;
  }}
  html:not([data-theme="dark"]) .pay-threshold .pay-spin .tc-core::after {{
    animation: tc-day-sheen 1.6s ease-in-out infinite;
  }}
  html:not([data-theme="dark"]) .pay-threshold .pay-spin .tc-ring {{
    border-top-color: #f59e0b; border-right-color: rgba(245,158,11,.55);
    border-bottom-color: rgba(245,158,11,.12);
    animation: tc-mine-spin var(--spin1) linear infinite;
  }}
  html:not([data-theme="dark"]) .pay-threshold .pay-spin .tc-ring2 {{
    inset: -1px; border-width: 2.5px;
    border-bottom-color: #10b981; border-left-color: rgba(16,185,129,.45);
    border-top-color: rgba(16,185,129,.12);
    animation: tc-mine-spin var(--spin2) linear infinite reverse;
  }}
  /* Night — match header logo */
  [data-theme="dark"] .pay-threshold .pay-spin .tc-core {{
    background:
      radial-gradient(circle at 30% 26%, rgba(165,243,252,.35), transparent 40%),
      linear-gradient(145deg, #1a9b72 0%, #0d5c44 48%, #06261a 100%);
    box-shadow:
      0 0 0 1px rgba(34,211,238,.35), 0 0 12px rgba(34,211,238,.28),
      0 0 6px rgba(45,212,160,.2),
      inset 0 -2px 4px rgba(0,0,0,.55), inset 0 2px 2px rgba(165,243,252,.15);
    animation: tc-night-pulse 2.8s ease-in-out infinite;
  }}
  [data-theme="dark"] .pay-threshold .pay-spin .tc-core::after {{
    background: linear-gradient(115deg, transparent 28%, rgba(165,243,252,.4), transparent 72%);
    animation: tc-night-sheen 3.4s ease-in-out infinite;
  }}
  [data-theme="dark"] .pay-threshold .pay-spin .tc-ring {{
    border-top-color: #22d3ee; border-right-color: rgba(34,211,238,.5);
    border-bottom-color: rgba(34,211,238,.12);
    animation: tc-mine-spin var(--spin1) linear infinite;
  }}
  [data-theme="dark"] .pay-threshold .pay-spin .tc-ring2 {{
    inset: -1px; border-width: 2.5px;
    border-bottom-color: #a78bfa; border-left-color: rgba(167,139,250,.45);
    border-top-color: rgba(167,139,250,.12);
    animation: tc-mine-spin var(--spin2) linear infinite reverse;
  }}
  .pay-threshold.celebrate .pay-spin {{
    --spin1: .4s; --spin2: .55s;
    animation: tc-block-found .75s cubic-bezier(.2,.8,.2,1) 1;
  }}
  .pay-threshold .pay-stats {{
    display: flex; flex-wrap: wrap; align-items: baseline; gap: .35rem .55rem;
    margin: .55rem 0 0; font-size: .88rem;
  }}
  .pay-threshold .pay-now {{
    font-family: var(--mono); font-weight: 800; font-size: 1.02rem;
    letter-spacing: -.02em; color: var(--text);
  }}
  .pay-threshold .pay-sep {{ color: var(--muted); font-weight: 600; }}
  .pay-threshold .pay-goal {{
    font-family: var(--mono); font-weight: 650; color: var(--muted);
  }}
  .pay-threshold .pay-pill {{
    font-family: var(--mono); font-size: .72rem; font-weight: 800;
    padding: .18rem .45rem; border-radius: 999px;
    background: var(--mint-soft); color: var(--mint);
    border: 1px solid color-mix(in srgb, var(--mint) 28%, var(--line));
  }}
  .pay-threshold.near .pay-pill {{
    background: var(--mint-soft); color: var(--mint);
    border-color: color-mix(in srgb, var(--mint) 40%, var(--line));
  }}
  .pay-threshold .pay-left {{
    font-size: .78rem; color: var(--muted); width: 100%; margin: .1rem 0 0;
  }}
  .pay-toast {{
    display: none; margin-top: .65rem; padding: .55rem .75rem; border-radius: 10px;
    font-size: .84rem; font-weight: 700;
    background: var(--mint-soft); border: 1px solid color-mix(in srgb, var(--mint) 35%, var(--line));
    color: var(--text);
  }}
  .pay-toast.show {{ display: flex; align-items: center; gap: .5rem; }}
  .chart-wrap .dot-pay-event {{
    fill: #0ea5e9; stroke: var(--panel); stroke-width: 2;
    filter: drop-shadow(0 0 6px rgba(14,165,233,.45));
  }}
  [data-theme="dark"] .chart-wrap .dot-pay-event {{ fill: #38bdf8; }}
  .chart-legend {{
    display: flex; flex-wrap: wrap; gap: .55rem 1rem; margin: .55rem 0 0;
    font-size: .72rem; color: var(--muted); font-weight: 650;
  }}
  .chart-legend span {{ display: inline-flex; align-items: center; gap: .35rem; }}
  .chart-legend i {{
    width: .65rem; height: .65rem; border-radius: 999px; display: inline-block;
  }}
  .chart-legend .lg-earn {{ background: #10b981; }}
  .chart-legend .lg-paid {{ background: #0ea5e9; }}
  .chart-legend .lg-unpaid {{ background: #d97706; }}
  [data-theme="dark"] .chart-legend .lg-unpaid {{ background: #fbbf24; }}
  .chart-legend .lg-event {{
    width: .45rem; height: .45rem; box-shadow: 0 0 0 2px #0ea5e9;
    background: var(--panel);
  }}
  @media (prefers-reduced-motion: reduce) {{
    .pay-threshold .pay-spin .tc-ring,
    .pay-threshold .pay-spin .tc-ring2,
    .pay-threshold .pay-spin .tc-core,
    .pay-threshold .pay-spin .tc-core::after,
    .pay-threshold.celebrate .pay-spin {{ animation: none !important; }}
  }}
  .panel {{ overflow: hidden; min-width: 0; }}
  .connect-grid {{ min-width: 0; }}
  .cmd {{ font-size: .78rem; }}
  footer {{
    overflow-wrap: anywhere; word-break: break-word;
  }}
  .miner-head {{
    display: flex; flex-wrap: wrap; gap: .65rem 1rem;
    align-items: flex-start; justify-content: space-between;
    margin-bottom: .75rem; min-width: 0;
  }}
  .miner-head > div:first-child {{ min-width: 0; flex: 1 1 14rem; }}
  .miner-head h2 {{
    margin: 0; font-size: clamp(.95rem, 3.5vw, 1.2rem); font-weight: 800;
    letter-spacing: -.02em; word-break: break-all; overflow-wrap: anywhere;
  }}
  .miner-meta {{
    color: var(--muted); font-size: .8rem; margin-top: .25rem;
    overflow-wrap: anywhere;
  }}
  .back {{ font-size: .86rem; font-weight: 650; color: var(--muted); }}
  .back:hover {{ color: var(--text); }}
  .round-link {{ color: inherit; text-decoration: none; }}
  .round-link:hover {{ color: var(--link); text-decoration: underline; }}
  #kRoundLink {{ font-variant-numeric: tabular-nums; }}
  .round-nav {{ display: flex; gap: .5rem; align-items: center; flex-wrap: wrap; }}
  .round-nav a.disabled {{ opacity: .4; pointer-events: none; }}
  #roundsListBody tr.is-current {{ background: var(--soft); }}
  #roundsListBody tr.is-current td:first-child {{ font-weight: 700; }}
  .charts {{
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: .85rem; margin: 0 0 1.15rem;
  }}
  .chart-card {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
    padding: .9rem 1rem 1rem; min-width: 0; overflow: hidden;
    box-shadow: 0 1px 0 color-mix(in srgb, var(--text) 4%, transparent);
  }}
  .chart-card .ch-head {{
    display: flex; align-items: baseline; justify-content: space-between;
    gap: .75rem; margin-bottom: .35rem;
  }}
  .chart-card h3 {{
    margin: 0; font-size: .95rem; font-weight: 750; letter-spacing: -.015em;
  }}
  .chart-card .ch-live {{
    font-family: var(--mono); font-size: .78rem; font-weight: 700; color: var(--mint);
    text-align: right; max-width: 55%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .chart-card .ch-sub {{ margin: 0 0 .65rem; color: var(--muted); font-size: .74rem; }}
  .chart-wrap {{
    position: relative; width: 100%; height: 200px;
    border-radius: 12px;
    background:
      radial-gradient(120% 80% at 10% 0%, color-mix(in srgb, var(--mint) 10%, transparent), transparent 55%),
      var(--soft);
    border: 1px solid var(--line);
    overflow: hidden;
    display: grid; place-items: center;
  }}
  .chart-wrap svg {{
    display: block; width: 100%; height: 100%;
    max-height: 200px;
  }}
  .chart-wrap .grid line {{ stroke: var(--line); stroke-width: 1; opacity: .85; }}
  .chart-wrap .axis-label {{
    fill: var(--muted); font-size: 10px; font-family: var(--mono);
    paint-order: stroke; stroke: var(--soft); stroke-width: 3px; stroke-linejoin: round;
  }}
  .chart-wrap .area {{ fill: url(#gradMint); opacity: .55; }}
  .chart-wrap .line {{
    fill: none; stroke: #10b981; stroke-width: 2.4;
    stroke-linecap: round; stroke-linejoin: round;
    filter: drop-shadow(0 0 8px rgba(16,185,129,.28));
  }}
  .chart-wrap .bar {{
    fill: #10b981; opacity: .88;
  }}
  .chart-wrap .bar:hover {{ opacity: 1; filter: brightness(1.08); }}
  .chart-wrap .bar-zero {{ fill: color-mix(in srgb, var(--mint) 18%, transparent); opacity: .55; }}
  .chart-wrap .bar-pay {{ fill: #0ea5e9; opacity: .9; }}
  .chart-wrap .bar-pay:hover {{ opacity: 1; filter: brightness(1.08); }}
  [data-theme="dark"] .chart-wrap .bar-pay {{ fill: #38bdf8; }}
  .chart-wrap .bar-unpaid {{ fill: #d97706; opacity: .88; }}
  .chart-wrap .bar-unpaid:hover {{ opacity: 1; filter: brightness(1.08); }}
  [data-theme="dark"] .chart-wrap .bar-unpaid {{ fill: #fbbf24; }}
  .chart-wrap .area-pay {{ fill: url(#gradSky); opacity: .55; }}
  .chart-wrap .line-pay {{
    fill: none; stroke: #0ea5e9; stroke-width: 2.2;
    stroke-linecap: round; stroke-linejoin: round;
    filter: drop-shadow(0 0 6px rgba(14,165,233,.28));
  }}
  .chart-wrap .dot {{
    fill: var(--panel); stroke: #10b981; stroke-width: 2.25;
  }}
  .chart-wrap .dot-pay {{ stroke: #0ea5e9; }}
  [data-theme="dark"] .chart-wrap .line {{ stroke: #34d399; }}
  [data-theme="dark"] .chart-wrap .bar {{ fill: #34d399; }}
  [data-theme="dark"] .chart-wrap .dot {{ stroke: #34d399; }}
  [data-theme="dark"] .chart-wrap .line-pay {{ stroke: #38bdf8; }}
  [data-theme="dark"] .chart-wrap .dot-pay {{ stroke: #38bdf8; }}
  .chart-empty {{
    position: absolute; inset: 0; display: grid; place-items: center;
    color: var(--muted); font-size: .82rem; pointer-events: none;
  }}
  .chart-empty.hidden {{ display: none; }}
  @media (max-width: 900px) {{
    .charts {{ grid-template-columns: minmax(0, 1fr); }}
    .header-inner {{ gap: .65rem; }}
  }}
  @media (max-width: 640px) {{
    header.top {{ padding: .7rem .85rem .65rem; }}
    main, footer {{ padding-left: .85rem; padding-right: .85rem; }}
    .theme-toggle {{ width: 2.5rem; height: 2.5rem; }}
    .theme-coin {{ --tc-size: 1.75rem; }}
    .brand h1 {{ font-size: 1.05rem; }}
    .brand .sub {{ font-size: .72rem; }}
    header nav {{
      width: 100%;
      gap: .2rem .55rem;
      padding-top: .15rem;
    }}
    header nav a {{ font-size: .78rem; }}
    .hero {{ margin: .2rem 0 .9rem; }}
    .kpi {{ gap: .45rem; margin: .55rem 0 .9rem; }}
    .kpi .stat {{ padding: .38rem .5rem .36rem .6rem; border-radius: 7px; }}
    .kpi .val {{ font-size: .86rem; }}
    .chart-wrap {{ height: 168px; }}
    .chart-card .ch-live {{ max-width: 48%; font-size: .68rem; }}
    .miner-head .lookup {{ width: 100%; }}
    .miner-head .lookup input {{ min-width: 0; width: 100%; }}
    .lookup {{ width: 100%; }}
    .lookup input {{ min-width: 0; flex: 1 1 auto; }}
    .copy-row {{ flex-direction: column; }}
    .copy-row button {{ align-self: flex-start; }}
    .panel {{ padding: .85rem .85rem; border-radius: 12px; }}
    .connect-card {{ padding: .75rem .8rem; }}
    .reward-full {{ display: none; }}
    .reward-short {{ display: inline; }}
    .hash-full {{ display: none; }}
    .hash-short {{ display: inline; }}
    /* Pool blocks: stacked cards — no column collision on narrow screens. */
    #blocks .table-scroll {{ overflow: visible; }}
    #blocks table,
    #blocks thead,
    #blocks tbody,
    #blocks tr,
    #blocks th,
    #blocks td {{
      display: block; width: 100% !important; max-width: none !important;
    }}
    #blocks thead {{ display: none; }}
    #blocks tbody {{ contain: none; }}
    #blocks tr {{
      position: relative;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: .75rem .85rem .7rem;
      padding-right: 5.5rem;
      margin: 0 0 .6rem;
    }}
    #blocks tr:last-child {{ margin-bottom: 0; }}
    #blocks td {{
      border: 0; padding: 0; text-align: left !important;
    }}
    #blocks td.b-h {{
      font-size: .95rem; line-height: 1.25; margin: 0 0 .28rem;
    }}
    #blocks td.b-time-cell {{
      font-size: .75rem; color: var(--muted); margin: 0 0 .28rem;
    }}
    /* Status pill on the height row (out of document flow). */
    #blocks td.b-status {{
      position: absolute; top: .62rem; right: .75rem;
      width: auto !important; height: 0; margin: 0; padding: 0 !important;
      overflow: visible; text-align: right !important;
    }}
    #blocks td.b-hash {{
      font-size: .78rem; line-height: 1.35; margin: 0 0 .3rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    #blocks td.b-hash a {{
      max-width: 100%; font-size: inherit;
    }}
    #blocks td.b-reward {{
      font-size: .8rem; color: var(--muted); margin: 0 0 .2rem;
    }}
    #blocks td.b-reward .reward-cell {{ font-size: .8rem; }}
    #blocks td.b-note {{
      font-size: .72rem; color: var(--muted); margin-top: .2rem;
      white-space: normal !important; line-height: 1.35;
    }}
    /* Live workers: stacked cards — worker name must not overlap hashrate. */
    #miners .table-scroll {{ overflow: visible; }}
    #miners table,
    #miners thead,
    #miners tbody,
    #miners tr,
    #miners th,
    #miners td {{
      display: block; width: 100% !important; max-width: none !important;
    }}
    #miners thead {{ display: none; }}
    #miners tbody {{ contain: none; }}
    #miners tr {{
      position: relative;
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: .75rem .85rem .7rem;
      padding-right: 6.25rem;
      margin: 0 0 .6rem;
      min-height: 3.4rem;
    }}
    #miners tr:last-child {{ margin-bottom: 0; }}
    #miners td {{
      border: 0; padding: 0; text-align: left !important;
      overflow: visible; text-overflow: unset; white-space: normal; max-width: none;
    }}
    #miners td.w-name {{
      font-size: .95rem; font-weight: 700; line-height: 1.25;
      margin: 0 0 .22rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      padding-right: .25rem;
    }}
    #miners td.w-addr {{
      font-size: .75rem; color: var(--muted); margin: 0 0 .28rem;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
    #miners td.w-addr a {{ color: var(--link); }}
    #miners td.w-hr {{
      position: absolute; top: .72rem; right: .75rem;
      width: auto !important; margin: 0; padding: 0 !important;
      font-size: .84rem; font-weight: 700; text-align: right !important;
      white-space: nowrap; overflow: visible;
    }}
    #miners td.w-st {{
      font-size: .75rem; color: var(--muted);
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }}
  }}
</style>
</head>
<body>
<svg width="0" height="0" style="position:absolute;overflow:hidden" aria-hidden="true">
  <defs>
    <linearGradient id="gradMint" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#2dd4a0" stop-opacity="0.35"/>
      <stop offset="100%" stop-color="#2dd4a0" stop-opacity="0.02"/>
    </linearGradient>
    <linearGradient id="gradSky" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#0ea5e9" stop-opacity="0.32"/>
      <stop offset="100%" stop-color="#0ea5e9" stop-opacity="0.02"/>
    </linearGradient>
  </defs>
</svg>

<header class="top">
  <div class="header-inner">
    <div class="brand-row">
      {logo}
      <div class="brand">
        <h1><a href="/" id="brandHome">MHCOIN Pool</a></h1>
        <p class="sub">HASH256 · PROP · live mainnet</p>
      </div>
    </div>
    <nav>
      <a href="{exp_href}/" rel="noopener">Explorer</a>
      <a href="/" id="navPool" class="active">Pool</a>
      <a href="/#connect" id="navConnect">Connect</a>
      <a href="/#miners" id="navMiners">Miners</a>
      <a href="/#blocks" id="navBlocks">Blocks</a>
      <a href="https://github.com/maxut88/MHCOIN/blob/main/docs/POOL.md" target="_blank" rel="noopener">Docs</a>
      <a href="https://github.com/maxut88/MHCOIN/releases" target="_blank" rel="noopener">Download</a>
    </nav>
  </div>
</header>

<main>
  <div id="flash"></div>

  <!-- ===== Pool home ===== -->
  <div id="viewHome" class="{view_home_cls}">
    <div class="hero">
      <h2>Professional HASH256 pool</h2>
      <p>Proportional (PROP) shares · fee {fee_percent:g}% · share factor ×{share_factor} ·
         payout threshold {threshold_mhc:g} MHC · mature after {mature_confirms} confirms.
         Coinbase / operator: <span class="mono">{addr}</span></p>
    </div>

    <div class="docs-bar" aria-label="Documentation and downloads">
      <span class="lbl">Get started</span>
      <a class="btn-outline" href="https://github.com/maxut88/MHCOIN/blob/main/docs/POOL.md" target="_blank" rel="noopener">Documentation</a>
      <a class="btn-outline" href="https://github.com/maxut88/MHCOIN/blob/main/docs/USER_QUICKSTART.md" target="_blank" rel="noopener">Quick start</a>
      <a class="btn-outline" href="https://github.com/maxut88/MHCOIN/releases" target="_blank" rel="noopener">Download</a>
      <a class="btn-outline" href="https://github.com/maxut88/MHCOIN" target="_blank" rel="noopener">
        <svg class="gh-ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8"/></svg>
        GitHub
      </a>
    </div>

    <section class="panel net-panel" id="networkPanel">
      <h3 style="margin:0 0 .35rem">Network</h3>
      <p class="muted" style="margin:0 0 .55rem">Pool-node P2P · tip sync · peer diversity</p>
      <div class="kpi" style="margin:0">
        <div class="stat"><div class="lbl">Tip</div><div class="val" id="nTip">{n_tip}</div><div class="hint" id="nTipHash">canonical</div></div>
        <div class="stat"><div class="lbl">Peers</div><div class="val" id="nPeers">{n_peers}</div><div class="hint" id="nPeersHint">{n_peers_hint}</div></div>
        <div class="stat"><div class="lbl">Known addrs</div><div class="val" id="nAddrs">{n_addrs}</div><div class="hint">AddrDB</div></div>
        <div class="stat"><div class="lbl">Voided pool rounds</div><div class="val" id="nOrphans">{n_orphans}</div><div class="hint" title="Lifetime-time count of pool ledger rounds marked orphaned after a confirmed non-canonical hash. Not the node's in-memory orphan pool and not explorer side-chain blocks.">ledger voids (≠ chain orphans)</div></div>
        <div class="stat"><div class="lbl">Stale immature</div><div class="val" id="nStale">{n_stale}</div><div class="hint">past maturity depth</div></div>
      </div>
      <div id="peersList" class="muted" style="margin-top:.65rem">{peers_html}</div>
    </section>

    <div class="kpi" id="kpi">
      <div class="stat k-hash"><div class="lbl">Pool hashrate</div><div class="val" id="kHash">{k_hash}</div><div class="hint">sum of workers (10m)</div></div>
      <div class="stat k-miners"><div class="lbl">Workers online</div><div class="val" id="kWorkers">{k_workers}</div><div class="hint" id="kMinersHint">{k_miners_hint}</div></div>
      <div class="stat k-shares"><div class="lbl">Shares (1h)</div><div class="val" id="kShares">{k_shares}</div><div class="hint" id="kRoundShares">{k_round_shares}</div></div>
      <div class="stat k-round"><div class="lbl">Current round</div><div class="val"><a class="round-link" href="{k_round_href}" id="kRoundLink"><span id="kRound">{k_round}</span></a></div><div class="hint">round id · open details</div></div>
      <div class="stat k-fee"><div class="lbl">Pool fee</div><div class="val">{fee_percent:g}%</div><div class="hint">PROP after fee</div></div>
      <div class="stat k-blocks"><div class="lbl">Blocks found</div><div class="val" id="kBlocks">{k_blocks}</div><div class="hint">by this pool</div></div>
    </div>

    <div class="charts" id="homeCharts">
      <div class="chart-card">
        <div class="ch-head">
          <h3>Pool hashrate</h3>
          <span class="ch-live" id="chartPoolLive">{chart_pool_live}</span>
        </div>
        <p class="ch-sub">Last 24h · 5‑min average · live</p>
        <div class="chart-wrap" id="chartPoolWrap">
          <div class="chart-empty hidden" id="chartPoolEmpty">Waiting for samples…</div>
          <svg id="chartPoolSvg" viewBox="0 0 640 200" preserveAspectRatio="xMidYMid meet" aria-hidden="true"></svg>
        </div>
      </div>
      <div class="chart-card">
        <div class="ch-head">
          <h3>Pool blocks</h3>
          <span class="ch-live" id="chartBlocksLive">{chart_blocks_live}</span>
        </div>
        <p class="ch-sub">Found per hour · last 24h</p>
        <div class="chart-wrap" id="chartBlocksWrap">
          <div class="chart-empty hidden" id="chartBlocksEmpty">No pool blocks yet</div>
          <svg id="chartBlocksSvg" viewBox="0 0 640 200" preserveAspectRatio="xMidYMid meet" aria-hidden="true"></svg>
        </div>
      </div>
    </div>

    <section class="panel" id="connect">
      <h3>How to connect</h3>
      <p class="muted">Terminal miner only (Desktop solo is separate). Same machine can solo OR pool — not both on one datadir.
         Public endpoint: <span class="mono">{host_pub_esc}</span> (LAN also works: 192.168.0.221).</p>
      <div class="connect-grid" style="margin-top:.85rem">
        <div class="connect-card">
          <div class="t">JSON (recommended)</div>
          <div class="mono">{host_pub_esc}:{json_port}</div>
          <div class="copy-row">
            <code class="cmd" id="cmdJson">mhcoin mining pool-start --url {host_pub_esc}:{json_port} --address mhc1YOUR_PAYOUT --worker rig1</code>
            <button type="button" data-copy="cmdJson">Copy</button>
          </div>
        </div>
        <div class="connect-card">
          <div class="t">Stratum</div>
          <div class="mono">stratum+tcp://{host_pub_esc}:{stratum_port}</div>
          <p class="muted" style="margin:.45rem 0 0">Stratum adapter for MHCOIN. Prefer JSON client for best compatibility.</p>
        </div>
        <div class="connect-card">
          <div class="t">Your stats</div>
          <p class="muted" style="margin:0 0 .45rem">Paste the payout address you mine with:</p>
          <div class="lookup">
            <input id="minerQ" placeholder="mhc1…" autocomplete="off" spellcheck="false"/>
            <button type="button" class="btn-mint" id="minerGo">Open dashboard →</button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="miners">
      <h3>Live workers</h3>
      <p class="muted">Active in the last 10 minutes · auto-refresh 8s</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Address</th><th>Worker</th><th>Hashrate</th><th>Status</th></tr></thead>
        <tbody id="workersBody"><tr><td colspan="4" class="muted">Loading…</td></tr></tbody>
      </table>
      </div>
    </section>

    <section class="panel" id="blocks">
      <div class="ch-head" style="display:flex;align-items:baseline;justify-content:space-between;gap:.75rem;flex-wrap:wrap;margin-bottom:.35rem">
        <h3 style="margin:0">Pool blocks</h3>
        <span class="muted" style="font-size:.82rem" id="blocksMeta">—</span>
      </div>
      <p class="muted">Found by this pool · time · hash vs canonical · reorg/side notes · 20 / page</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Round</th><th>Height</th><th>Time</th><th>Block hash</th><th>Reward</th><th>Status</th><th>Note</th></tr></thead>
        <tbody id="blocksBody"
               data-page="1"
               data-per-page="20"
               data-total="0"><tr><td colspan="7" class="muted">No pool blocks yet</td></tr></tbody>
      </table>
      </div>
      <div class="pager" id="blocksPager"></div>
    </section>
  </div>

  <!-- ===== Miner dashboard ===== -->
  <div id="viewMiner" class="{view_miner_cls}">
    <div class="miner-head">
      <div>
        <a class="back" href="/" id="minerBack">← Pool</a>
        <h2 id="minerTitle">{m_title}</h2>
        <div class="miner-meta" id="minerMeta">{m_meta}</div>
      </div>
      <div class="lookup" style="margin:0">
        <input id="minerQ2" placeholder="mhc1…" autocomplete="off" spellcheck="false" value="{_esc(m_addr)}"/>
        <button type="button" class="btn-mint" id="minerGo2">Go →</button>
      </div>
    </div>

    <div class="kpi" id="minerKpi">
      <div class="stat k-imm"><div class="lbl">Immature</div><div class="val" id="mImmature">{m_immature}</div><div class="hint" id="mImmatureHint">locked until {mature_confirms} confirms</div></div>
      <div class="stat k-pend"><div class="lbl">Pending</div><div class="val" id="mPending">{m_pending_txt}</div><div class="hint">ready for payout</div></div>
      <div class="stat k-paid"><div class="lbl">Paid</div><div class="val" id="mPaid">{m_paid_txt}</div><div class="hint">already sent</div></div>
      <div class="stat k-hash"><div class="lbl">Hashrate (short)</div><div class="val" id="mHashShort">{m_hash_short}</div><div class="hint" id="mHashShortHint">miner report</div></div>
      <div class="stat k-miners"><div class="lbl">Hashrate (long)</div><div class="val" id="mHashLong">{m_hash_long}</div><div class="hint" id="mHashLongHint">~3 h trend</div></div>
      <div class="stat k-blocks"><div class="lbl">Workers</div><div class="val" id="mWorkersOn">{m_workers_on}</div><div class="hint" id="mLastShare">{m_last_share}</div></div>
    </div>

    <div class="charts" id="minerCharts">
      <div class="chart-card">
        <div class="ch-head">
          <h3>Your hashrate</h3>
          <span class="ch-live" id="chartMinerLive">{m_chart_hr_live}</span>
        </div>
        <p class="ch-sub">Reported by workers · rises &amp; falls with load</p>
        <div class="chart-wrap">
          <div class="chart-empty hidden" id="chartMinerEmpty">Waiting for samples…</div>
          <svg id="chartMinerSvg" viewBox="0 0 600 160" preserveAspectRatio="none" aria-hidden="true"></svg>
        </div>
      </div>
      <div class="chart-card">
        <div class="ch-head">
          <h3>Earnings &amp; payouts</h3>
          <span class="ch-live" id="chartPayLive">{m_chart_pay_live}</span>
        </div>
        <p class="ch-sub">Per hour (last 24h) · earned · paid · unpaid balance</p>
        <div class="chart-wrap" id="chartPayWrap">
          <div class="chart-empty hidden" id="chartPayEmpty">Credits appear after pool blocks</div>
          <svg id="chartPaySvg" viewBox="0 0 640 200" preserveAspectRatio="xMidYMid meet" aria-hidden="true"></svg>
        </div>
        <div class="chart-legend" aria-hidden="true">
          <span><i class="lg-earn"></i> earned / h</span>
          <span><i class="lg-paid"></i> paid / h</span>
          <span><i class="lg-unpaid"></i> unpaid (end of hour)</span>
        </div>
      </div>
    </div>

    <section class="panel pay-threshold" id="payThreshold">
      <h3>Payout threshold</h3>
      <p class="muted" id="mThresholdHint">Auto-payout when pending ≥ {threshold_mhc:g} MHC</p>
      <div class="pay-row">
        <div class="progress" aria-hidden="true"><i id="mProgress" style="width:{m_prog:.1f}%"></i></div>
        <div class="pay-spin" id="mPaySpin" title="payout progress" aria-hidden="true">
          <span class="tc-ring"></span>
          <span class="tc-ring2"></span>
          <span class="tc-core">{_LOGO_SVG_M}</span>
        </div>
      </div>
      <div class="pay-stats" id="mProgressText">
        <span class="pay-now" id="mProgressNow">{_esc(_fmt_mhc_sats(m_pending)) if mb else "—"}</span>
        <span class="pay-sep">/</span>
        <span class="pay-goal" id="mProgressGoal">{_esc(_fmt_mhc_sats(m_thr)) if mb else "—"}</span>
        <span class="pay-pill" id="mProgressPct">{f"{m_prog:.1f}%" if mb else "0%"}</span>
        <span class="pay-left" id="mProgressLeft">{m_left_txt}</span>
      </div>
      <div class="pay-toast" id="mPayToast" role="status"></div>
    </section>

    <div class="tabs" id="minerTabs" role="tablist">
      <button type="button" class="active" data-tab="overview">Overview</button>
      <button type="button" data-tab="workers">Workers</button>
      <button type="button" data-tab="payments">Payments</button>
      <button type="button" data-tab="mblocks">Blocks</button>
    </div>

    <section class="panel" data-pane="overview">
      <h3>Overview</h3>
      <div class="connect-grid">
        <div class="connect-card">
          <div class="t">Shares</div>
          <div class="mono" id="mSharesTotal">{m_shares_txt}</div>
          <p class="muted" style="margin:.35rem 0 0" id="mSharesWin">{m_shares_win}</p>
        </div>
        <div class="connect-card">
          <div class="t">Blocks found</div>
          <div class="mono" id="mBlocksFound">{m_blocks_found}</div>
          <p class="muted" style="margin:.35rem 0 0">by this address</p>
        </div>
        <div class="connect-card">
          <div class="t">Payments</div>
          <div class="mono" id="mPayCount">{m_pay_count}</div>
          <p class="muted" style="margin:.35rem 0 0">recorded payouts</p>
        </div>
      </div>
      <div class="connect-card" style="margin-top:.85rem" id="mCreditBox">
        <div class="t">Last block credit</div>
        <p class="muted" style="margin:0" id="mCreditText">No credits yet — keep mining until the pool finds a block.</p>
      </div>
    </section>

    <section class="panel hidden" data-pane="workers">
      <h3>Workers</h3>
      <p class="muted">online &lt;10m · sick &lt;½ short window · dead otherwise · short≈30m / long≈3h from shares</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Worker</th><th>Short H/s</th><th>Long H/s</th><th>Reported</th><th>Status</th><th>Last share</th></tr></thead>
        <tbody id="mWorkersBody"><tr><td colspan="6" class="muted">—</td></tr></tbody>
      </table>
      </div>
    </section>

    <section class="panel hidden" data-pane="payments">
      <h3>Payments</h3>
      <p class="muted">Ledger <strong>payment</strong> = amount to your payout address.
         On-chain there may also be an HD <strong>change</strong> vout (Bitcoin-style, still pool-controlled).</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>When</th><th>Payment</th><th>Detail</th><th>Txid</th><th>Status</th></tr></thead>
        <tbody id="mPayBody"><tr><td colspan="5" class="muted">No payments yet</td></tr></tbody>
      </table>
      </div>
    </section>

    <section class="panel hidden" data-pane="mblocks">
      <h3>Your blocks</h3>
      <p class="muted">Accepted pool blocks for this address (network rejects are not listed)</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>When</th><th>Height</th><th>Block hash</th><th>Share diff</th><th>Status</th></tr></thead>
        <tbody id="mBlocksBody"><tr><td colspan="5" class="muted">No blocks yet</td></tr></tbody>
      </table>
      </div>
    </section>
  </div>

  <!-- ===== Round audit ===== -->
  <div id="viewRound" class="hidden">
    <div class="miner-head">
      <div>
        <a class="back" href="/" id="roundBack">← Pool</a>
        <h2 id="roundTitle">Round</h2>
        <div class="miner-meta" id="roundMeta">PROP shares · fee split · credits</div>
      </div>
      <div class="round-nav" id="roundNav">
        <a class="btn-outline" href="#" id="roundPrev">← older</a>
        <a class="btn-outline" href="#" id="roundNext">newer →</a>
      </div>
    </div>

    <div class="kpi" id="roundKpi">
      <div class="stat k-round"><div class="lbl">Round</div><div class="val" id="rId">—</div><div class="hint" id="rStatus">—</div></div>
      <div class="stat k-blocks"><div class="lbl">Height</div><div class="val" id="rHeight">—</div><div class="hint" id="rFound">—</div></div>
      <div class="stat k-hash"><div class="lbl">Reward</div><div class="val" id="rReward">—</div><div class="hint" id="rHashHint">block hash</div></div>
      <div class="stat k-fee"><div class="lbl">Pool fee</div><div class="val" id="rFee">—</div><div class="hint" id="rFeeHint">from reward − credits</div></div>
      <div class="stat k-pend"><div class="lbl">Distributable</div><div class="val" id="rDist">—</div><div class="hint">after fee</div></div>
      <div class="stat k-shares"><div class="lbl">Shares</div><div class="val" id="rShares">—</div><div class="hint" id="rMinersHint">miners</div></div>
    </div>

    <section class="panel" id="roundBlock">
      <h3>Block</h3>
      <p class="muted" style="margin:0" id="rBlockText">—</p>
    </section>

    <section class="panel" id="roundMiners">
      <h3>Miners this round</h3>
      <p class="muted">PROP % from share difficulty · credit = distributable × PROP</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Address</th><th>Shares</th><th>Difficulty</th><th>PROP %</th><th>Credit</th><th>Status</th></tr></thead>
        <tbody id="rMinersBody"><tr><td colspan="6" class="muted">Loading…</td></tr></tbody>
      </table>
      </div>
    </section>

    <section class="panel" id="roundsList">
      <div class="ch-head" style="display:flex;align-items:baseline;justify-content:space-between;gap:.75rem;flex-wrap:wrap;margin-bottom:.35rem">
        <h3 style="margin:0">All rounds</h3>
        <span class="muted" style="font-size:.82rem" id="roundsListMeta">—</span>
      </div>
      <p class="muted">Newest first · click a round to audit shares / PROP / fee</p>
      <div class="table-scroll">
      <table>
        <thead><tr><th>Round</th><th>Height</th><th>Time</th><th>Reward</th><th>Shares</th><th>Miners</th><th>Status</th></tr></thead>
        <tbody id="roundsListBody"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody>
      </table>
      </div>
      <div class="pager" id="roundsListPager"></div>
    </section>
  </div>
</main>

<footer>
  <div class="footer-row">
    <div>
      MHCOIN native pool · HASH256 PoW ·
      <a href="{exp_href}/" rel="noopener">Explorer</a> ·
      <span class="mono">/api/stats</span>
    </div>
    <div class="footer-links">
      <a href="https://github.com/maxut88/MHCOIN/blob/main/docs/POOL.md" target="_blank" rel="noopener">Docs</a>
      <a href="https://github.com/maxut88/MHCOIN/releases" target="_blank" rel="noopener">Download</a>
      <a href="https://github.com/maxut88/MHCOIN" target="_blank" rel="noopener" aria-label="MHCOIN on GitHub">
        <svg class="gh-ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8"/></svg>
        GitHub
      </a>
    </div>
  </div>
</footer>

<script>
(function(){{
  var EXP = {json.dumps(explorer_url.rstrip("/") if explorer_url else "")};
  var THRESHOLD = {int(payout_threshold_sats)};
  var MATURE = {int(mature_confirms)};
  var currentMiner = "";
  var currentRound = null;
  var minerTimer = null;
  var roundsListPage = 1;
  var ROUNDS_PER_PAGE = 20;

  function $(id){{ return document.getElementById(id); }}
  function setTxt(id, txt){{
    var el = $(id);
    if (el && el.textContent !== String(txt)) el.textContent = String(txt);
  }}
  function flash(msg){{
    var el = $("flash"); el.textContent = msg; el.classList.add("show");
    setTimeout(function(){{ el.classList.remove("show"); }}, 2200);
  }}
  function fmtHps(h){{
    h = Number(h)||0;
    if (h >= 1e12) return (h/1e12).toFixed(2)+" TH/s";
    if (h >= 1e9) return (h/1e9).toFixed(2)+" GH/s";
    if (h >= 1e6) return (h/1e6).toFixed(2)+" MH/s";
    if (h >= 1e3) return (h/1e3).toFixed(1)+" kH/s";
    return Math.round(h)+" H/s";
  }}
  /** Compact Y-axis labels so they fit the left gutter (no H/s suffix). */
  function fmtHpsAxis(h){{
    h = Number(h)||0;
    if (h <= 0) return "0";
    if (h >= 1e12) return (h/1e12).toFixed(h >= 1e13 ? 1 : 2)+"T";
    if (h >= 1e9) return (h/1e9).toFixed(h >= 1e10 ? 1 : 2)+"G";
    if (h >= 1e6) return (h/1e6).toFixed(h >= 1e7 ? 1 : 2)+"M";
    if (h >= 1e3) return (h/1e3).toFixed(h >= 1e4 ? 0 : 1)+"k";
    return String(Math.round(h));
  }}
  function fmtMhc(sats, digits){{
    var n = (Number(sats)||0)/1e8;
    var d = digits == null ? 4 : digits;
    if (Math.abs(n) > 0 && Math.abs(n) < 0.0001) d = 8;
    var s = n.toFixed(d);
    if (d >= 4) s = s.replace(/(\.\d*?[1-9])0+$/,"$1").replace(/\.0+$/,"");
    return s+" MHC";
  }}
  function fmtMhcFull(sats){{
    return ((Number(sats)||0)/1e8).toFixed(8)+" MHC";
  }}
  var MINT_SVG = '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M6 24V8h4.2l5.8 10.4L21.8 8H26v16h-3.4V13.2L17.2 24h-2.4L9.4 13.2V24H6z" fill="#06261a"/></svg>';
  function mintMark(title){{
    return '<span class="mint" title="'+(title||"MHC")+'"><span class="mint-coin">'+MINT_SVG+'</span></span>';
  }}
  function rewardCell(sats){{
    var full = fmtMhcFull(sats);
    var compact = fmtMhc(sats, 2);
    return '<span class="reward-cell" title="'+full+'">'+mintMark("MHC mined")+
      '<span class="reward-full">'+full+'</span>'+
      '<span class="reward-short">'+compact+'</span></span>';
  }}
  function shortHash(h){{
    h = String(h||"");
    if (h.length <= 20) return h;
    return h.slice(0, 10) + "…" + h.slice(-8);
  }}
  function hashCell(h, kind){{
    h = String(h||"");
    if (!h) return '—';
    var body = '<span class="hash-full">'+h+'</span>'+
               '<span class="hash-short">'+shortHash(h)+'</span>';
    var href = EXP
      ? EXP+'/'+(kind||'block')+'/'+encodeURIComponent(h)
      : '';
    return href
      ? '<a href="'+href+'" class="mono" title="'+h+'" rel="noopener">'+body+'</a>'
      : '<span class="mono" title="'+h+'">'+body+'</span>';
  }}
  function statusBlockPill(b){{
    var st = String((b && b.status) || b || "—");
    var cls = "off";
    if (b && b.stale_immature) {{ st = "stale"; cls = "stale"; }}
    else if (st === "immature") cls = "immature";
    else if (st === "matured" || st === "paid") cls = "matured";
    else if (st === "orphaned" || st === "rejected") cls = st;
    var title = (b && b.status_note) ? String(b.status_note) : st;
    return '<span class="pill '+cls+'" title="'+title.replace(/"/g,'&quot;')+'">'+st+'</span>';
  }}
  function noteCell(b){{
    var note = String((b && b.status_note) || "");
    if (!note) {{
      if (b && b.on_best_chain === false && b.canonical_hash)
        note = "side vs canonical "+String(b.canonical_hash).slice(0,16)+"…";
      else return '<span class="muted">—</span>';
    }}
    var cls = "note-cell";
    if (b && (b.status === "orphaned" || (b.status === "matured" && b.on_best_chain === false)))
      cls += " bad";
    else if (b && b.stale_immature) cls += " warn";
    var tip = b && b.canonical_hash
      ? ('canonical '+b.canonical_hash+(b.on_best_chain ? ' (match)' : ' (differs)'))
      : note;
    return '<span class="'+cls+'" title="'+tip.replace(/"/g,'&quot;')+'">'+note+'</span>';
  }}
  function renderAlerts(alerts){{
    var box = $("flash");
    if (!box) return;
    alerts = alerts || [];
    if (!alerts.length) {{ box.innerHTML = ""; return; }}
    box.innerHTML = alerts.map(function(a){{
      var sev = String(a.severity||"info");
      return '<div class="alert '+sev+'"><strong>'+String(a.code||"alert")+'</strong> · '+
        String(a.message||"")+'</div>';
    }}).join("");
  }}
  function renderNetwork(ov){{
    var tip = ov.mainnet_tip != null ? ov.mainnet_tip : "—";
    setTxt("nTip", tip === "—" ? "—" : "#"+tip);
    setTxt("nPeers", ov.peer_count != null ? String(ov.peer_count) : "—");
    var ot = ov.outbound_target != null ? ov.outbound_target : "—";
    setTxt("nPeersHint", "target "+ot);
    setTxt("nAddrs", ov.known_addrs != null ? String(ov.known_addrs) : "—");
    setTxt("nOrphans", ov.orphaned_rounds != null ? String(ov.orphaned_rounds) : "—");
    setTxt("nStale", ov.stale_immature_rounds != null ? String(ov.stale_immature_rounds) : "—");
    var pl = $("peersList");
    if (pl) {{
      var peers = ov.peers || [];
      if (!peers.length) pl.textContent = "No handshaked peers yet — check MHCOIN_CONNECT / seeds.";
      else pl.innerHTML = peers.map(function(p){{
        return '<div class="peer-row">'+(p.inbound?"←":"→")+' '+String(p.addr||"?")+
          ' · '+String(p.state||"")+(p.height!=null?" · #"+p.height:"")+'</div>';
      }}).join("");
    }}
  }}

  function shortAddr(a){{
    a = String(a||"");
    if (a.length < 18) return a;
    return a.slice(0,10)+"…"+a.slice(-6);
  }}

  /* —— Smooth SVG charts (lerp + cubic path) —— */
  function lerp(a, b, t){{ return a + (b - a) * t; }}
  function easeOut(t){{ return 1 - Math.pow(1 - t, 3); }}
  function catmullPath(pts){{
    if (!pts.length) return "";
    if (pts.length === 1) return "M "+pts[0][0]+" "+pts[0][1];
    var d = "M "+pts[0][0]+" "+pts[0][1];
    for (var i = 0; i < pts.length - 1; i++) {{
      var p0 = pts[Math.max(0, i - 1)];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[Math.min(pts.length - 1, i + 2)];
      var c1x = p1[0] + (p2[0] - p0[0]) / 6;
      var c1y = p1[1] + (p2[1] - p0[1]) / 6;
      var c2x = p2[0] - (p3[0] - p1[0]) / 6;
      var c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += " C "+c1x+" "+c1y+", "+c2x+" "+c2y+", "+p2[0]+" "+p2[1];
    }}
    return d;
  }}

  function SmoothChart(svgId, emptyId, opts){{
    this.svg = $(svgId);
    this.empty = emptyId ? $(emptyId) : null;
    this.opts = opts || {{}};
    this.w = 640; this.h = 200;
    // Left gutter must fit compact axis labels (e.g. "19.65M") inside viewBox
    // even when preserveAspectRatio=none stretches the SVG.
    this.pad = {{ t: 14, r: 12, b: 28, l: 58 }};
    this.display = [];
    this.display2 = [];
    this.display3 = [];
    this.xLabels = [];
    this.markers = [];
    this.yFmt = this.opts.yFmt || null;
    this.mode = this.opts.mode || "line"; // line | bars
    this.anim = null;
    this.lineClass = this.opts.lineClass || "line";
    this.areaClass = this.opts.areaClass || "area";
    this.dotClass = this.opts.dotClass || "dot";
    this.gradId = this.opts.gradId || "gradMint";
    this._ensureBase();
  }}
  SmoothChart.prototype._ensureBase = function(){{
    if (!this.svg || this.svg.dataset.ready) return;
    var ns = "http://www.w3.org/2000/svg";
    var gid = this.gradId === "gradSky" ? "gradSky" : "gradMint";
    var grid = document.createElementNS(ns, "g"); grid.setAttribute("class", "grid");
    grid.setAttribute("data-role", "grid");
    var axes = document.createElementNS(ns, "g"); axes.setAttribute("data-role", "axes");
    var bars = document.createElementNS(ns, "g"); bars.setAttribute("data-role", "bars");
    var marks = document.createElementNS(ns, "g"); marks.setAttribute("data-role", "marks");
    var area2 = document.createElementNS(ns, "path");
    area2.setAttribute("class", "area area-pay");
    area2.setAttribute("fill", "url(#gradSky)");
    var area = document.createElementNS(ns, "path");
    area.setAttribute("class", this.areaClass);
    area.setAttribute("fill", "url(#"+gid+")");
    var line2 = document.createElementNS(ns, "path");
    line2.setAttribute("class", "line line-pay");
    var line = document.createElementNS(ns, "path");
    line.setAttribute("class", this.lineClass);
    var dot2 = document.createElementNS(ns, "circle");
    dot2.setAttribute("class", "dot dot-pay");
    dot2.setAttribute("r", "4.5");
    dot2.setAttribute("cx", "-20");
    dot2.setAttribute("cy", "-20");
    var dot = document.createElementNS(ns, "circle");
    dot.setAttribute("class", this.dotClass);
    dot.setAttribute("r", "4.5");
    dot.setAttribute("cx", "-20");
    dot.setAttribute("cy", "-20");
    this.svg.appendChild(grid);
    this.svg.appendChild(axes);
    this.svg.appendChild(bars);
    this.svg.appendChild(area2);
    this.svg.appendChild(area);
    this.svg.appendChild(line2);
    this.svg.appendChild(line);
    this.svg.appendChild(marks);
    this.svg.appendChild(dot2);
    this.svg.appendChild(dot);
    this.grid = grid; this.axes = axes; this.bars = bars; this.marks = marks;
    this.area = area; this.line = line; this.dot = dot;
    this.area2 = area2; this.line2 = line2; this.dot2 = dot2;
    this.svg.dataset.ready = "1";
  }};
  SmoothChart.prototype.setSeries = function(values, values2, meta){{
    meta = meta || {{}};
    if (meta.mode) this.mode = meta.mode;
    if (meta.yFmt) this.yFmt = meta.yFmt;
    this.xLabels = meta.xLabels || [];
    this.markers = meta.markers || [];
    var vals = (values || []).map(function(v){{ return Math.max(0, Number(v)||0); }});
    var vals2 = (values2 || []).map(function(v){{ return Math.max(0, Number(v)||0); }});
    var vals3 = (meta.values3 || []).map(function(v){{ return Math.max(0, Number(v)||0); }});
    if (vals.length === 1) vals = [vals[0], vals[0]];
    if (vals2.length === 1) vals2 = [vals2[0], vals2[0]];
    if (vals3.length === 1) vals3 = [vals3[0], vals3[0]];
    if (!this.display.length) {{
      this.display = vals.slice();
      this.display2 = vals2.slice();
      this.display3 = vals3.slice();
      this._paint(this.display, this.display2);
      return;
    }}
    var n = Math.max(vals.length, vals2.length, vals3.length, this.display.length, 2);
    function resample(old, n){{
      if (!old || !old.length) {{
        var z = []; for (var i = 0; i < n; i++) z.push(0); return z;
      }}
      if (old.length === n) return old.slice();
      var out = [];
      for (var i = 0; i < n; i++) {{
        var u = n === 1 ? 0 : i / (n - 1);
        var j = u * (old.length - 1);
        var j0 = Math.floor(j), j1 = Math.min(old.length - 1, j0 + 1);
        var t = j - j0;
        out.push((old[j0]||0) * (1 - t) + (old[j1]||0) * t);
      }}
      return out;
    }}
    vals = resample(vals.length ? vals : [0], n);
    vals2 = resample(vals2.length ? vals2 : [0], n);
    vals3 = resample(vals3.length ? vals3 : [0], n);
    this._animate(
      resample(this.display, n), vals,
      resample(this.display2 || [0], n), vals2,
      resample(this.display3 || [0], n), vals3
    );
  }};
  SmoothChart.prototype._animate = function(from, to, from2, to2, from3, to3){{
    var self = this;
    if (this.anim) cancelAnimationFrame(this.anim);
    var t0 = performance.now();
    var dur = 480;
    from2 = from2 || []; to2 = to2 || [];
    from3 = from3 || []; to3 = to3 || [];
    function frame(now){{
      var t = Math.min(1, (now - t0) / dur);
      var e = 1 - Math.pow(1 - t, 3);
      var cur = [], cur2 = [], cur3 = [];
      for (var i = 0; i < to.length; i++) cur.push((from[i]||0) + ((to[i]||0) - (from[i]||0)) * e);
      for (var j = 0; j < to2.length; j++) cur2.push((from2[j]||0) + ((to2[j]||0) - (from2[j]||0)) * e);
      for (var k = 0; k < to3.length; k++) cur3.push((from3[k]||0) + ((to3[k]||0) - (from3[k]||0)) * e);
      self.display = cur;
      self.display2 = cur2;
      self.display3 = cur3;
      self._paint(cur, cur2);
      if (t < 1) self.anim = requestAnimationFrame(frame);
      else self.anim = null;
    }}
    this.anim = requestAnimationFrame(frame);
  }};
  SmoothChart.prototype._pts = function(vals, min, max, iw, ih){{
    var pts = [];
    for (var i = 0; i < vals.length; i++) {{
      var x = this.pad.l + (vals.length === 1 ? iw / 2 : iw * i / (vals.length - 1));
      var y = this.pad.t + ih * (1 - (vals[i] - min) / (max - min || 1));
      pts.push([x, y]);
    }}
    return pts;
  }};
  SmoothChart.prototype._drawAxes = function(min, max, iw, ih){{
    if (!this.axes || !this.grid) return;
    var ns = "http://www.w3.org/2000/svg";
    while (this.grid.firstChild) this.grid.removeChild(this.grid.firstChild);
    while (this.axes.firstChild) this.axes.removeChild(this.axes.firstChild);
    var yTicks = 4;
    for (var i = 0; i <= yTicks; i++) {{
      var u = i / yTicks;
      var y = this.pad.t + ih * (1 - u);
      var ln = document.createElementNS(ns, "line");
      ln.setAttribute("x1", this.pad.l); ln.setAttribute("x2", this.pad.l + iw);
      ln.setAttribute("y1", y); ln.setAttribute("y2", y);
      this.grid.appendChild(ln);
      var val = min + (max - min) * u;
      var label = this.yFmt ? this.yFmt(val) : (max >= 10 ? String(Math.round(val)) : val.toFixed(1));
      var tx = document.createElementNS(ns, "text");
      tx.setAttribute("class", "axis-label");
      tx.setAttribute("x", this.pad.l - 6);
      tx.setAttribute("y", y + 3);
      tx.setAttribute("text-anchor", "end");
      tx.textContent = label;
      this.axes.appendChild(tx);
    }}
    var labels = this.xLabels || [];
    if (labels.length) {{
      var idxs = [0, Math.floor((labels.length - 1) / 2), labels.length - 1];
      var seen = {{}};
      for (var k = 0; k < idxs.length; k++) {{
        var ix = idxs[k];
        if (seen[ix]) continue;
        seen[ix] = 1;
        var x = this.pad.l + (labels.length === 1 ? iw / 2 : iw * ix / (labels.length - 1));
        var tx2 = document.createElementNS(ns, "text");
        tx2.setAttribute("class", "axis-label");
        tx2.setAttribute("x", x);
        tx2.setAttribute("y", this.h - 8);
        tx2.setAttribute("text-anchor", k === 0 ? "start" : (k === idxs.length - 1 ? "end" : "middle"));
        tx2.textContent = labels[ix] || "";
        this.axes.appendChild(tx2);
      }}
    }}
  }};
  SmoothChart.prototype._paint = function(vals, vals2){{
    if (!this.svg) return;
    this._ensureBase();
    vals = vals || this.display || [];
    vals2 = vals2 || this.display2 || [];
    var vals3 = this.display3 || [];
    if (!vals.length && !vals2.length && !vals3.length) {{
      if (this.empty) this.empty.classList.remove("hidden");
      this.line.setAttribute("d", ""); this.area.setAttribute("d", "");
      this.line2.setAttribute("d", ""); this.area2.setAttribute("d", "");
      if (this.bars) while (this.bars.firstChild) this.bars.removeChild(this.bars.firstChild);
      if (this.marks) while (this.marks.firstChild) this.marks.removeChild(this.marks.firstChild);
      return;
    }}
    if (this.empty) this.empty.classList.add("hidden");
    var all = vals.concat(vals2).concat(vals3);
    var max = Math.max.apply(null, all.concat([1e-9]));
    // Nice headroom for line charts
    if (this.mode !== "bars") max = max * 1.08;
    var min = 0;
    var iw = this.w - this.pad.l - this.pad.r;
    var ih = this.h - this.pad.t - this.pad.b;
    this._drawAxes(min, max, iw, ih);
    var self = this;
    var ns = "http://www.w3.org/2000/svg";
    if (this.bars) while (this.bars.firstChild) this.bars.removeChild(this.bars.firstChild);
    if (this.mode === "bars") {{
      this.line.setAttribute("d",""); this.area.setAttribute("d","");
      this.line2.setAttribute("d",""); this.area2.setAttribute("d","");
      this.dot.setAttribute("cx","-20"); this.dot2.setAttribute("cx","-20");
      if (this.marks) while (this.marks.firstChild) this.marks.removeChild(this.marks.firstChild);
      var n = Math.max(vals.length, vals2.length, vals3.length, 1);
      var has2 = vals2 && vals2.length > 0;
      var has3 = vals3 && vals3.length > 0;
      var pair = 1 + (has2 ? 1 : 0) + (has3 ? 1 : 0);
      var slot = iw / n;
      var bw = Math.max(1.5, (slot * (pair > 1 ? 0.82 : 0.62)) / pair);
      var gap = pair > 1 ? Math.max(0.8, bw * 0.1) : 0;
      for (var i = 0; i < n; i++) {{
        var groupW = bw * pair + gap * (pair - 1);
        var groupX = this.pad.l + slot * i + (slot - groupW) / 2;
        function drawBar(v, x0, cls){{
          var vv = v || 0;
          var bh = ih * (vv - min) / (max - min || 1);
          var rect = document.createElementNS(ns, "rect");
          rect.setAttribute("class", vv > 0 ? cls : (cls + " bar-zero"));
          rect.setAttribute("x", x0);
          rect.setAttribute("y", vv > 0 ? (self.pad.t + ih - bh) : (self.pad.t + ih - 2));
          rect.setAttribute("width", bw);
          rect.setAttribute("height", vv > 0 ? Math.max(0, bh) : 2);
          rect.setAttribute("rx", "2");
          self.bars.appendChild(rect);
        }}
        var xi = 0;
        drawBar(vals[i] || 0, groupX + xi, "bar"); xi += bw + gap;
        if (has2) {{ drawBar(vals2[i] || 0, groupX + xi, "bar bar-pay"); xi += bw + gap; }}
        if (has3) drawBar(vals3[i] || 0, groupX + xi, "bar bar-unpaid");
      }}
      return;
    }}
    function stroke(pts, lineEl, areaEl, dotEl){{
      if (!pts.length) {{
        lineEl.setAttribute("d",""); areaEl.setAttribute("d","");
        dotEl.setAttribute("cx","-20"); return;
      }}
      var lineD = catmullPath(pts);
      var areaD = lineD + " L "+pts[pts.length-1][0]+" "+(self.h - self.pad.b)+
        " L "+pts[0][0]+" "+(self.h - self.pad.b)+" Z";
      lineEl.setAttribute("d", lineD);
      areaEl.setAttribute("d", areaD);
      var last = pts[pts.length - 1];
      dotEl.setAttribute("cx", last[0]);
      dotEl.setAttribute("cy", last[1]);
    }}
    stroke(vals2.length ? this._pts(vals2, min, max, iw, ih) : [], this.line2, this.area2, this.dot2);
    stroke(vals.length ? this._pts(vals, min, max, iw, ih) : [], this.line, this.area, this.dot);
    if (this.marks) while (this.marks.firstChild) this.marks.removeChild(this.marks.firstChild);
    var markSrc = vals2.length ? vals2 : vals;
    var markPts = markSrc.length ? this._pts(markSrc, min, max, iw, ih) : [];
    var marks = this.markers || [];
    for (var mi = 0; mi < marks.length; mi++) {{
      var midx = Number(marks[mi]);
      if (!(midx >= 0) || midx >= markPts.length) continue;
      var mp = markPts[midx];
      var mc = document.createElementNS(ns, "circle");
      mc.setAttribute("class", "dot-pay-event");
      mc.setAttribute("r", "5");
      mc.setAttribute("cx", mp[0]);
      mc.setAttribute("cy", mp[1]);
      this.marks.appendChild(mc);
    }}
  }};

  var chartPool = null, chartBlocks = null, chartMiner = null, chartPay = null;
  var homeBlocksCache = [];
  var homeBlocksTotal = 0;
  var BLOCKS_PER_PAGE = 20;
  function ensureCharts(){{
    if (!chartPool) chartPool = new SmoothChart("chartPoolSvg", "chartPoolEmpty", {{
      gradId: "gradMint",
      yFmt: fmtHpsAxis
    }});
    if (!chartBlocks) chartBlocks = new SmoothChart("chartBlocksSvg", "chartBlocksEmpty", {{
      gradId: "gradMint", mode: "bars",
      yFmt: function(v){{ return String(Math.round(v)); }}
    }});
    if (!chartMiner) chartMiner = new SmoothChart("chartMinerSvg", "chartMinerEmpty", {{
      gradId: "gradMint",
      yFmt: fmtHpsAxis
    }});
    if (!chartPay) chartPay = new SmoothChart("chartPaySvg", "chartPayEmpty", {{
      gradId: "gradMint",
      yFmt: function(v){{
        if (v >= 1000) return (v/1000).toFixed(v >= 10000 ? 0 : 1) + "k";
        if (v >= 100) return String(Math.round(v));
        if (v >= 10) return v.toFixed(1);
        return v.toFixed(2);
      }}
    }});
  }}

  var lastPaidSats = null;
  var payCelebrateTimer = null;
  function timeAxisLabel(ts){{
    try {{
      var d = new Date(Number(ts) * 1000);
      var hh = String(d.getHours()).padStart(2, "0");
      var mm = String(d.getMinutes()).padStart(2, "0");
      return hh + ":" + mm;
    }} catch (e) {{ return ""; }}
  }}
  function buildEarnPaySeries(earnHist, payHist, hours){{
    // Hourly MHC credited / paid + unpaid balance at end of each hour.
    hours = hours || 24;
    var now = Date.now() / 1000;
    var start = now - hours * 3600;
    var earn = [], paid = [], unpaid = [], labels = [];
    for (var i = 0; i < hours; i++) {{
      earn.push(0);
      paid.push(0);
      unpaid.push(0);
      labels.push(timeLabel(start + i * 3600, false));
    }}
    var erows = (earnHist || []).slice().sort(function(a,b){{ return (Number(a.t)||0) - (Number(b.t)||0); }});
    var prows = (payHist || []).slice().sort(function(a,b){{ return (Number(a.t)||0) - (Number(b.t)||0); }});
    // Prefer per-event amount_sats (same as payments). Fall back to cumulative deltas.
    var prevS = null;
    for (var j = 0; j < erows.length; j++) {{
      var t = Number(erows[j].t) || 0;
      var s = Number(erows[j].sats) || 0;
      var amtE = Number(erows[j].amount_sats);
      var delta = (!isNaN(amtE) && amtE > 0) ? amtE
        : (prevS == null ? 0 : Math.max(0, s - prevS));
      prevS = s;
      if (!delta || t < start || t > now) continue;
      var eidx = Math.min(hours - 1, Math.max(0, Math.floor((t - start) / 3600)));
      earn[eidx] += delta / 1e8;
    }}
    for (var k = 0; k < prows.length; k++) {{
      var pt = Number(prows[k].t) || 0;
      var amt = Number(prows[k].amount_sats) || 0;
      if (!amt || pt < start || pt > now) continue;
      var pidx = Math.min(hours - 1, Math.max(0, Math.floor((pt - start) / 3600)));
      paid[pidx] += amt / 1e8;
    }}
    // Unpaid = cumulative earned − cumulative paid at end of each hour.
    function cumAt(rows, tEnd, key){{
      var last = 0;
      for (var r = 0; r < rows.length; r++) {{
        if ((Number(rows[r].t) || 0) <= tEnd) last = Number(rows[r][key]) || 0;
        else break;
      }}
      return last;
    }}
    for (var h = 0; h < hours; h++) {{
      var tEnd = Math.min(now, start + (h + 1) * 3600);
      var eCum = cumAt(erows, tEnd, "sats");
      var pCum = cumAt(prows, tEnd, "sats");
      unpaid[h] = Math.max(0, (eCum - pCum) / 1e8);
    }}
    return {{ earn: earn, paid: paid, unpaid: unpaid, labels: labels, markers: [] }};
  }}
  function celebratePayout(amountSats){{
    var panel = $("payThreshold");
    var toast = $("mPayToast");
    if (!panel) return;
    panel.classList.remove("celebrate");
    void panel.offsetWidth;
    panel.classList.add("celebrate");
    if (toast) {{
      toast.innerHTML = mintMark("paid") + "<span>Payout sent · <strong>"+fmtMhc(amountSats)+"</strong></span>";
      toast.classList.add("show");
    }}
    if (payCelebrateTimer) clearTimeout(payCelebrateTimer);
    payCelebrateTimer = setTimeout(function(){{
      panel.classList.remove("celebrate");
      if (toast) toast.classList.remove("show");
    }}, 4200);
  }}
  function updatePayoutProgress(pending, thr, paidSats){{
    var panel = $("payThreshold");
    var fill = $("mProgress");
    var spin = $("mPaySpin");
    var prog = thr > 0 ? Math.min(1, pending / thr) : 0;
    var pct = prog * 100;
    if (fill) fill.style.width = pct.toFixed(1) + "%";
    if (spin) {{
      // Slow at 0% → fast at 100% (payout ready / sent)
      var spin1 = 3.0 - prog * 2.5;   // 3.0s → 0.5s
      var spin2 = 4.4 - prog * 3.6;   // 4.4s → 0.8s
      if (spin1 < 0.45) spin1 = 0.45;
      if (spin2 < 0.65) spin2 = 0.65;
      spin.style.setProperty("--spin1", spin1.toFixed(2) + "s");
      spin.style.setProperty("--spin2", spin2.toFixed(2) + "s");
    }}
    if (panel) panel.classList.toggle("near", prog >= 0.9);
    var nowEl = $("mProgressNow");
    var goalEl = $("mProgressGoal");
    var pctEl = $("mProgressPct");
    var leftEl = $("mProgressLeft");
    if (nowEl) nowEl.textContent = fmtMhc(pending);
    if (goalEl) goalEl.textContent = fmtMhc(thr);
    if (pctEl) pctEl.textContent = pct.toFixed(1) + "%";
    if (leftEl) {{
      var left = Math.max(0, thr - pending);
      leftEl.textContent = left > 0
        ? (fmtMhc(left) + " left until auto-payout")
        : "Threshold reached — payout on next cycle";
    }}
    if (lastPaidSats != null && Number(paidSats) > Number(lastPaidSats)) {{
      celebratePayout(Number(paidSats) - Number(lastPaidSats));
    }}
    lastPaidSats = Number(paidSats)||0;
  }}

  function timeLabel(ts, withMin){{
    try {{
      var d = new Date(Number(ts) * 1000);
      var hh = String(d.getHours()).padStart(2, "0");
      if (!withMin) return hh + ":00";
      var mm = String(d.getMinutes()).padStart(2, "0");
      return hh + ":" + mm;
    }} catch (e) {{ return ""; }}
  }}
  function bucketHashrate(hist, bucketSec){{
    bucketSec = bucketSec || 300;
    var rows = hist || [];
    if (!rows.length) return {{ values: [], labels: [] }};
    // Server may already send 5‑min averages; still re-bucket + fill gaps for a smooth axis.
    var map = {{}};
    var order = [];
    var tMin = Infinity, tMax = -Infinity;
    for (var i = 0; i < rows.length; i++) {{
      var t = Number(rows[i].t) || 0;
      var h = Number(rows[i].h) || 0;
      if (!t) continue;
      if (t < tMin) tMin = t;
      if (t > tMax) tMax = t;
      var b = Math.floor(t / bucketSec) * bucketSec;
      if (!map[b]) {{ map[b] = {{ sum: 0, n: 0 }}; order.push(b); }}
      map[b].sum += h; map[b].n += 1;
    }}
    if (!order.length) return {{ values: [], labels: [] }};
    order.sort(function(a,b){{ return a - b; }});
    // Prefer a continuous window ending at now (up to 24h / 288 buckets).
    var nowB = Math.floor((Date.now() / 1000) / bucketSec) * bucketSec;
    var startB = Math.floor(tMin / bucketSec) * bucketSec;
    var maxBuckets = 288;
    if ((nowB - startB) / bucketSec + 1 > maxBuckets) {{
      startB = nowB - (maxBuckets - 1) * bucketSec;
    }}
    var values = [], labels = [], last = 0;
    for (var key = startB; key <= nowB; key += bucketSec) {{
      if (map[key] && map[key].n) {{
        last = map[key].sum / map[key].n;
        values.push(last);
      }} else {{
        // Carry-forward so disconnects don't drop the line to zero.
        values.push(last);
      }}
      labels.push(timeLabel(key, true));
    }}
    return {{ values: values, labels: labels }};
  }}
  function blocksPerHour(blocks, hours){{
    hours = hours || 24;
    var now = Date.now() / 1000;
    var start = now - hours * 3600;
    var values = [];
    var labels = [];
    for (var i = 0; i < hours; i++) {{
      values.push(0);
      labels.push(timeLabel(start + i * 3600, false));
    }}
    var list = blocks || [];
    for (var j = 0; j < list.length; j++) {{
      var t = blockFoundTs(list[j]) || 0;
      if (t < start || t > now) continue;
      var idx = Math.min(hours - 1, Math.max(0, Math.floor((t - start) / 3600)));
      values[idx] += 1;
    }}
    return {{ values: values, labels: labels }};
  }}
  function seriesFromHistory(hist, key, fallback){{
    var arr = (hist || []).map(function(p){{ return Number(p[key])||0; }});
    if (!arr.length && fallback != null) arr = [fallback, fallback];
    if (arr.length > 90) arr = arr.slice(arr.length - 90);
    return arr;
  }}

  function age(ts){{
    if (!ts) return "never";
    var s = Math.max(0, (Date.now()/1000) - Number(ts||0));
    if (s < 60) return Math.floor(s)+"s ago";
    if (s < 3600) return Math.floor(s/60)+"m ago";
    if (s < 86400) return Math.floor(s/3600)+"h ago";
    return Math.floor(s/86400)+"d ago";
  }}
  function when(ts){{
    if (!ts) return "—";
    try {{ return new Date(Number(ts)*1000).toLocaleString(); }} catch(e){{ return age(ts); }}
  }}
  function setTheme(dark){{
    var root = document.documentElement;
    if (dark) root.setAttribute("data-theme","dark");
    else root.removeAttribute("data-theme");
    try {{ localStorage.setItem("mhcoin-theme", dark ? "dark" : "light"); }} catch(e){{}}
    var meta = $("metaThemeColor");
    if (meta) meta.setAttribute("content", dark ? "#0b0d10" : "#ffffff");
    var btn = $("themeBtn");
    if (btn) {{
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
      btn.title = dark ? "Switch to day" : "Switch to night";
    }}
  }}
  $("themeBtn").onclick = function(){{
    var next = document.documentElement.getAttribute("data-theme") !== "dark";
    setTheme(next);
    var btn = $("themeBtn");
    if (btn) {{
      btn.classList.remove("tc-found");
      void btn.offsetWidth;
      btn.classList.add("tc-found");
      setTimeout(function(){{ btn.classList.remove("tc-found"); }}, 750);
    }}
  }};
  setTheme(document.documentElement.getAttribute("data-theme") === "dark");

  document.querySelectorAll("[data-copy]").forEach(function(btn){{
    btn.onclick = function(){{
      var el = $(btn.getAttribute("data-copy"));
      var t = el ? el.textContent : "";
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(t).then(function(){{ flash("Copied"); }});
      }} else {{
        var ta = document.createElement("textarea");
        ta.value = t; document.body.appendChild(ta); ta.select();
        document.execCommand("copy"); document.body.removeChild(ta);
        flash("Copied");
      }}
    }};
  }});

  function setNav(active){{
    ["navPool","navConnect","navMiners","navBlocks"].forEach(function(id){{
      var a = $(id); if (a) a.classList.toggle("active", id === active);
    }});
  }}

  function showHome(){{
    currentMiner = "";
    currentRound = null;
    lastPaidSats = null;
    if (minerTimer) {{ clearInterval(minerTimer); minerTimer = null; }}
    $("viewHome").classList.remove("hidden");
    $("viewMiner").classList.add("hidden");
    $("viewRound").classList.add("hidden");
    setNav("navPool");
    document.title = "MHCOIN Pool";
  }}

  function showMinerView(addr, opts){{
    addr = (addr||"").trim();
    if (!addr) return;
    opts = opts || {{}};
    if (currentMiner !== addr) lastPaidSats = null;
    currentMiner = addr;
    currentRound = null;
    $("viewHome").classList.add("hidden");
    $("viewMiner").classList.remove("hidden");
    $("viewRound").classList.add("hidden");
    setNav("");
    $("minerQ").value = addr;
    $("minerQ2").value = addr;
    if (window.history && history.pushState) {{
      var path = "/miner/"+encodeURIComponent(addr);
      if (location.pathname !== path) history.pushState({{miner:addr}}, "", path);
    }}
    var boot = opts.boot || null;
    if (boot && boot.address && String(boot.address) !== addr) boot = null;
    loadMiner(addr, boot);
    if (minerTimer) clearInterval(minerTimer);
    minerTimer = setInterval(function(){{ if (currentMiner) loadMiner(currentMiner); }}, 8000);
  }}

  function showRoundView(rid, opts){{
    rid = Number(rid);
    if (!rid || !isFinite(rid)) return;
    opts = opts || {{}};
    currentMiner = "";
    currentRound = rid;
    lastPaidSats = null;
    if (minerTimer) {{ clearInterval(minerTimer); minerTimer = null; }}
    $("viewHome").classList.add("hidden");
    $("viewMiner").classList.add("hidden");
    $("viewRound").classList.remove("hidden");
    setNav("");
    if (window.history && history.pushState) {{
      var path = "/round/"+rid;
      if (location.pathname !== path) history.pushState({{round:rid}}, "", path);
    }}
    loadRound(rid);
  }}

  function blocksPagerHtml(page, totalPages){{
    page = Number(page)||1; totalPages = Number(totalPages)||1;
    var html = '';
    if (page > 1) html += '<a href="#" data-blocks-page="'+(page-1)+'">← newer</a>';
    else html += '<span class="muted">← newer</span>';
    html += '<span class="muted">page '+page+' / '+totalPages+'</span>';
    if (page < totalPages) html += '<a href="#" data-blocks-page="'+(page+1)+'">older →</a>';
    else html += '<span class="muted">older →</span>';
    return html;
  }}
  function blocksIdSig(slice){{
    return (slice||[]).map(function(b){{
      return String(b.height||'')+':' + String(b.block_hash||'');
    }}).join('|');
  }}
  function blockFoundTs(b){{
    // Prefer found_ts = block header timestamp (same as explorer Time).
    // Fallbacks: started_ts / ts / ended_ts for older rows before backfill.
    if (!b) return null;
    if (b.found_ts != null && b.found_ts !== '') return Number(b.found_ts);
    if (b.started_ts != null && b.started_ts !== '') return Number(b.started_ts);
    if (b.ts != null && b.ts !== '') return Number(b.ts);
    if (b.ended_ts != null && b.ended_ts !== '') return Number(b.ended_ts);
    return null;
  }}
  function blockTimeCell(b){{
    var ts = blockFoundTs(b);
    if (ts == null || !isFinite(ts)) return '<span class="muted">—</span>';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '<span class="muted">—</span>';
    var pad = function(n){{ return (n < 10 ? '0' : '') + n; }};
    // Local wall clock — same machine / LAN operators.
    var txt = d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+
      pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
    return '<span class="mono b-time" title="'+txt+'">'+txt+'</span>';
  }}
  function blockRowHtml(b){{
    var rid = b.round_id != null ? Number(b.round_id) : null;
    var roundCell = (rid && isFinite(rid))
      ? '<a class="round-link mono" href="/round/'+rid+'" data-round="'+rid+'">#'+rid+'</a>'
      : '<span class="muted">—</span>';
    return '<tr data-h="'+String(b.height||'')+'" data-hash="'+String(b.block_hash||'')+'" data-round="'+(rid||'')+'">'+
      '<td class="mono b-round">'+roundCell+'</td>'+
      '<td class="mono b-h">#'+(b.height||"—")+'</td>'+
      '<td class="b-time-cell">'+blockTimeCell(b)+'</td>'+
      '<td class="hash b-hash">'+hashCell(b.block_hash, "block")+'</td>'+
      '<td class="b-reward">'+rewardCell(b.reward_sats)+'</td>'+
      '<td class="b-status">'+statusBlockPill(b)+'</td>'+
      '<td class="b-note">'+noteCell(b)+'</td>'+
      '</tr>';
  }}
  function patchHomeBlocks(bb, slice){{
    for (var i = 0; i < slice.length; i++) {{
      var b = slice[i];
      var tr = bb.querySelector('tr[data-h="'+String(b.height||'')+'"]');
      if (!tr) continue;
      var st = tr.querySelector('.b-status');
      var want = statusBlockPill(b);
      if (st && st.innerHTML !== want) st.innerHTML = want;
      var nt = tr.querySelector('.b-note');
      var wantN = noteCell(b);
      if (nt && nt.innerHTML !== wantN) nt.innerHTML = wantN;
      var tm = tr.querySelector('.b-time-cell');
      var wantT = blockTimeCell(b);
      if (tm && tm.innerHTML !== wantT) tm.innerHTML = wantT;
      var rw = tr.querySelector('.b-reward');
      var wantR = rewardCell(b.reward_sats);
      if (rw && rw.dataset.sats !== String(b.reward_sats||0)) {{
        rw.innerHTML = wantR;
        rw.dataset.sats = String(b.reward_sats||0);
      }}
    }}
  }}
  function renderHomeBlocksPage(page){{
    var bb = $("blocksBody");
    var pg = $("blocksPager");
    var meta = $("blocksMeta");
    if (!bb) return;
    var per = Number(bb.dataset.perPage)||BLOCKS_PER_PAGE;
    var total = homeBlocksTotal || homeBlocksCache.length;
    var totalPages = Math.max(1, Math.ceil(total / per) || 1);
    page = Math.max(1, Math.min(Number(page)||1, totalPages));
    bb.dataset.page = String(page);
    bb.dataset.total = String(total);
    var metaTxt = total + " total · page " + page + " / " + totalPages;
    if (meta && meta.textContent !== metaTxt) meta.textContent = metaTxt;
    if (pg) {{
      var phtml = blocksPagerHtml(page, totalPages);
      if (pg.dataset.sig !== phtml) {{ pg.dataset.sig = phtml; pg.innerHTML = phtml; }}
    }}
    if (!homeBlocksCache.length) {{
      if (bb.dataset.sig !== '') {{
        bb.dataset.sig = '';
        bb.innerHTML = '<tr><td colspan="7" class="muted">No pool blocks yet</td></tr>';
      }}
      return;
    }}
    var start = (page - 1) * per;
    var slice = homeBlocksCache.slice(start, start + per);
    var idSig = blocksIdSig(slice);
    if (bb.dataset.sig === idSig && bb.querySelector('tr[data-h]')) {{
      patchHomeBlocks(bb, slice);
      return;
    }}
    bb.dataset.sig = idSig;
    bb.innerHTML = slice.map(blockRowHtml).join("");
  }}

  function workersIdSig(workers){{
    return (workers||[]).map(function(w){{
      return String(w.address||'')+'\t'+String(w.worker||'');
    }}).join('|');
  }}
  function workerRowHtml(w){{
    var online = (Date.now()/1000 - Number(w.last_seen||0)) < 600;
    return '<tr data-addr="'+String(w.address||'')+'" data-worker="'+String(w.worker||'')+'">'+
      '<td class="mono w-addr"><a href="/miner/'+encodeURIComponent(w.address)+'" data-addr="'+String(w.address||'')+'">'+shortAddr(w.address)+'</a></td>'+
      '<td class="mono w-name" title="'+String(w.worker||'')+'">'+(w.worker||"—")+'</td>'+
      '<td class="mono w-hr">'+fmtHps(w.hashrate)+'</td>'+
      '<td class="w-st"><span class="pill '+(online?'':'off')+'">'+(online?'online':'idle')+'</span> <span class="w-age">'+age(w.last_seen)+'</span></td>'+
      '</tr>';
  }}
  function patchWorkers(wb, workers){{
    for (var i = 0; i < workers.length; i++) {{
      var w = workers[i];
      var tr = wb.querySelector('tr[data-addr="'+String(w.address||'')+'"][data-worker="'+String(w.worker||'')+'"]');
      if (!tr) continue;
      var online = (Date.now()/1000 - Number(w.last_seen||0)) < 600;
      var hr = tr.querySelector('.w-hr');
      var hrTxt = fmtHps(w.hashrate);
      if (hr && hr.textContent !== hrTxt) hr.textContent = hrTxt;
      var pill = tr.querySelector('.pill');
      var ageEl = tr.querySelector('.w-age');
      var pillCls = online ? 'pill' : 'pill off';
      var pillTxt = online ? 'online' : 'idle';
      if (pill) {{
        if (pill.className !== pillCls) pill.className = pillCls;
        if (pill.textContent !== pillTxt) pill.textContent = pillTxt;
      }}
      var ageTxt = age(w.last_seen);
      if (ageEl && ageEl.textContent !== ageTxt) ageEl.textContent = ageTxt;
    }}
  }}
  function renderWorkers(workers){{
    var wb = $("workersBody");
    if (!wb) return;
    workers = workers || [];
    if (!workers.length) {{
      if (wb.dataset.sig !== '') {{
        wb.dataset.sig = '';
        wb.innerHTML = '<tr><td colspan="4" class="muted">No active workers — connect with pool-start</td></tr>';
      }}
      return;
    }}
    var sig = workersIdSig(workers);
    if (wb.dataset.sig === sig && wb.querySelector('tr[data-addr]')) {{
      patchWorkers(wb, workers);
      return;
    }}
    wb.dataset.sig = sig;
    wb.innerHTML = workers.map(workerRowHtml).join("");
  }}

  function renderStats(ov){{
    ensureCharts();
    setTxt("kHash", fmtHps(ov.pool_hashrate));
    setTxt("kWorkers", String(ov.workers_active||0));
    setTxt("kMinersHint", "miners total "+(ov.miners_total||0));
    setTxt("kShares", String(ov.shares_1h||0));
    setTxt("kRoundShares", "this round "+(ov.shares_round||0));
    setTxt("kRound", "#"+ (ov.current_round||"—"));
    var kLink = $("kRoundLink");
    if (kLink && ov.current_round != null) {{
      kLink.href = "/round/"+ov.current_round;
      kLink.setAttribute("data-round", String(ov.current_round));
    }}
    var blocks = ov.blocks || [];
    homeBlocksCache = blocks.slice();
    homeBlocksTotal = Number(ov.blocks_total != null ? ov.blocks_total : blocks.length) || 0;
    setTxt("kBlocks", String(homeBlocksTotal));
    var hrBucket = bucketHashrate(ov.hashrate_history, 300);
    if (!hrBucket.values.length && ov.pool_hashrate) {{
      hrBucket = {{ values: [ov.pool_hashrate, ov.pool_hashrate], labels: ["now", "now"] }};
    }}
    chartPool.setSeries(hrBucket.values, null, {{
      mode: "line",
      xLabels: hrBucket.labels,
      yFmt: fmtHpsAxis
    }});
    setTxt("chartPoolLive", fmtHps(ov.pool_hashrate));
    var bHour = blocksPerHour(blocks, 24);
    var hourSum = bHour.values.reduce(function(a,b){{ return a+b; }}, 0);
    chartBlocks.setSeries(bHour.values, null, {{
      mode: "bars",
      xLabels: bHour.labels,
      yFmt: function(v){{ return String(Math.round(v)); }}
    }});
    setTxt("chartBlocksLive", homeBlocksTotal+" blocks · "+hourSum+"/24h");
    var bbEl = $("blocksBody");
    var curPage = bbEl ? Number(bbEl.dataset.page || 1) : 1;
    renderHomeBlocksPage(curPage <= 1 ? 1 : curPage);
    renderWorkers(ov.workers || []);
    renderNetwork(ov);
    renderAlerts(ov.alerts || []);
  }}

  function statusPill(st){{
    st = String(st||"dead");
    var cls = st === "online" ? "" : (st === "sick" ? "sick" : "dead");
    var label = st === "online" ? "online" : (st === "sick" ? "sick" : "offline");
    return '<span class="pill '+cls+'">'+label+'</span>';
  }}

  function applyMinerStats(st, addr){{
    if (!st) return;
    var a = st.address || addr || "";
    $("minerTitle").textContent = a;
    document.title = shortAddr(a)+" — MHCOIN Pool";
    var thr = Number(st.payout_threshold_sats != null ? st.payout_threshold_sats : THRESHOLD);
    var mat = Number(st.mature_confirms != null ? st.mature_confirms : MATURE);
    $("minerMeta").textContent =
      "threshold "+(thr/1e8)+" MHC · mature "+mat+" confirms · "+
      (st.workers_online||0)+" worker(s) online";

    $("mImmature").textContent = fmtMhc(st.immature_sats);
    $("mPending").textContent = fmtMhc(st.pending_sats != null ? st.pending_sats : st.matured_sats);
    $("mPaid").textContent = fmtMhc(st.paid_sats);
    $("mHashShort").textContent = fmtHps(st.hashrate_short);
    $("mHashLong").textContent = fmtHps(st.hashrate_long);
    $("mHashShortHint").textContent = "from miner report";
    $("mHashLongHint").textContent = "trend vs share rate";
    $("mWorkersOn").textContent = String(st.workers_online||0)+" / "+((st.workers||[]).length);
    $("mLastShare").textContent = "last share "+age(st.last_share);
    $("mImmatureHint").textContent = "locked · "+mat+" confirms";

    ensureCharts();
    var mHr = seriesFromHistory(st.hashrate_history, "h", st.hashrate_short || st.hashrate);
    chartMiner.setSeries(mHr, null, {{
      mode: "line",
      yFmt: fmtHpsAxis
    }});
    $("chartMinerLive").textContent = fmtHps(st.hashrate_short || st.hashrate);
    var ep = buildEarnPaySeries(st.earnings_history, st.payments_history, 24);
    var earn24 = ep.earn.reduce(function(a,b){{ return a+b; }}, 0);
    var paid24 = ep.paid.reduce(function(a,b){{ return a+b; }}, 0);
    var unpaidNow = Number(st.immature_sats||0) + Number(st.pending_sats != null ? st.pending_sats : st.matured_sats||0);
    chartPay.setSeries(ep.earn, ep.paid, {{
      mode: "bars",
      xLabels: ep.labels,
      markers: [],
      values3: ep.unpaid,
      yFmt: function(v){{
        if (v >= 1000) return (v/1000).toFixed(v >= 10000 ? 0 : 1) + "k";
        if (v >= 100) return String(Math.round(v));
        if (v >= 10) return v.toFixed(1);
        return v < 1 && v > 0 ? v.toFixed(2) : String(Math.round(v));
      }}
    }});
    var payCount = Number(st.payments_count != null ? st.payments_count : (st.payments||[]).length)||0;
    $("chartPayLive").textContent = "earned "+fmtMhc((unpaidNow+Number(st.paid_sats||0)))+
      " · paid "+fmtMhc(st.paid_sats)+
      " · unpaid "+fmtMhc(unpaidNow)+
      (payCount ? (" · "+payCount+" tx") : "")+
      " · 24h +"+earn24.toFixed(1)+" / paid "+paid24.toFixed(1);

    var pending = Number(st.pending_sats != null ? st.pending_sats : st.matured_sats)||0;
    updatePayoutProgress(pending, thr, st.paid_sats||0);
    $("mThresholdHint").textContent = "Auto-payout when pending ≥ "+(thr/1e8)+" MHC";

    $("mSharesTotal").textContent = String(st.shares||0);
    $("mSharesWin").textContent = "short "+(st.shares_short||0)+" · long "+(st.shares_long||0);
    $("mBlocksFound").textContent = String(st.blocks_found != null ? st.blocks_found : (st.blocks||[]).length);
    $("mPayCount").textContent = String(st.payments_count != null ? st.payments_count : (st.payments||[]).length);

    var lc = st.last_credit;
    var creditEl = $("mCreditText");
    if (lc && lc.reward_sats) {{
      var reward = lc.reward_sats;
      var fee = lc.fee_sats != null ? lc.fee_sats : Math.round(reward*0.01);
      var dist = lc.distributable_sats != null ? lc.distributable_sats : (reward - fee);
      var pct = lc.share_percent != null ? lc.share_percent.toFixed(2) : "?";
      var feePct = reward > 0 ? (100*fee/reward).toFixed(2) : "?";
      var roundLink = lc.round_id
        ? ' <a class="round-link" href="/round/'+lc.round_id+'" data-round="'+lc.round_id+'">Round #'+lc.round_id+'</a> ·'
        : "";
      creditEl.innerHTML =
        roundLink+
        " Block <span class=\\"mono\\">#"+(lc.height||"—")+"</span> reward "+fmtMhcFull(reward)+
        ". Pool fee "+feePct+"% = "+fmtMhcFull(fee)+" → distributable "+fmtMhcFull(dist)+". "+
        "Your PROP share <strong>"+pct+"%</strong> = <strong>"+fmtMhcFull(lc.amount_sats)+"</strong> ("+(lc.status||"")+"). "+
        "Immature stays locked until the block has "+mat+" confirmations, then moves to Pending.";
    }} else {{
      creditEl.textContent = "No credits yet — keep mining until the pool finds a block.";
    }}

    var rows = st.workers || [];
    $("mWorkersBody").innerHTML = rows.length ? rows.map(function(w){{
      return '<tr>'+
        '<td class="mono">'+(w.worker||"—")+'</td>'+
        '<td class="mono">'+fmtHps(w.hashrate_short)+'</td>'+
        '<td class="mono">'+fmtHps(w.hashrate_long)+'</td>'+
        '<td class="mono">'+fmtHps(w.hashrate)+'</td>'+
        '<td>'+statusPill(w.status)+'</td>'+
        '<td>'+age(w.last_share || w.last_seen)+'</td>'+
        '</tr>';
    }}).join("") : '<tr><td colspan="6" class="muted">No workers recorded</td></tr>';

    var pays = st.payments || [];
    $("mPayBody").innerHTML = pays.length ? pays.map(function(p){{
      var detail = "payment to your address";
      if (p.change_mhc) detail += " · on-chain change "+p.change_mhc+" MHC";
      return '<tr>'+
        '<td>'+when(p.ts)+'</td>'+
        '<td>'+rewardCell(p.amount_sats)+'</td>'+
        '<td class="muted">'+detail+'</td>'+
        '<td class="hash">'+hashCell(p.txid, "tx")+'</td>'+
        '<td>'+statusBlockPill(p.status)+'</td>'+
        '</tr>';
    }}).join("") : '<tr><td colspan="5" class="muted">No payments yet</td></tr>';

    var mblocks = st.blocks || [];
    $("mBlocksBody").innerHTML = mblocks.length ? mblocks.map(function(b){{
      return '<tr>'+
        '<td>'+when(b.ts)+'</td>'+
        '<td class="mono">#'+(b.height||"—")+'</td>'+
        '<td class="hash">'+hashCell(b.block_hash, "block")+'</td>'+
        '<td class="mono">'+(b.difficulty||"—")+'</td>'+
        '<td>'+statusBlockPill(b.status)+'</td>'+
        '</tr>';
    }}).join("") : '<tr><td colspan="5" class="muted">No blocks yet</td></tr>';
  }}
  function loadMiner(addr, boot){{
    // Paint SSR/boot instantly (no "—" flash), then soft-refresh from API.
    if (boot) applyMinerStats(boot, addr);
    fetch("/api/miner/"+encodeURIComponent(addr)).then(function(r){{ return r.json(); }}).then(function(st){{
      if (currentMiner !== addr) return;
      applyMinerStats(st, addr);
    }}).catch(function(){{ if (!boot) flash("Miner lookup failed"); }});
  }}

  function fmtTsLocal(ts){{
    if (ts == null || ts === '') return "—";
    var d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return "—";
    var pad = function(n){{ return (n < 10 ? '0' : '') + n; }};
    return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+
      pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());
  }}
  function setRoundNavLink(el, rid, label){{
    if (!el) return;
    if (rid != null && isFinite(Number(rid))) {{
      el.href = "/round/"+rid;
      el.setAttribute("data-round", String(rid));
      el.classList.remove("disabled");
      el.setAttribute("aria-disabled", "false");
    }} else {{
      el.href = "#";
      el.removeAttribute("data-round");
      el.classList.add("disabled");
      el.setAttribute("aria-disabled", "true");
    }}
    if (label) el.textContent = label;
  }}
  function applyRound(st){{
    if (!st || st.error) {{
      $("roundTitle").textContent = "Round not found";
      $("roundMeta").textContent = st && st.error ? st.error : "Unknown round";
      $("rMinersBody").innerHTML = '<tr><td colspan="6" class="muted">No data</td></tr>';
      setRoundNavLink($("roundPrev"), null);
      setRoundNavLink($("roundNext"), null);
      return;
    }}
    var rid = st.id;
    $("roundTitle").textContent = "Round #"+rid;
    document.title = "Round #"+rid+" — MHCOIN Pool";
    var status = st.status || "—";
    setTxt("rId", "#"+rid);
    setTxt("rStatus", status);
    setTxt("rHeight", st.height != null ? "#"+st.height : "—");
    var found = st.found_ts != null ? fmtTsLocal(st.found_ts) : (st.ended_ts != null ? fmtTsLocal(st.ended_ts) : "open");
    setTxt("rFound", found);
    setTxt("rReward", st.reward_sats != null ? fmtMhc(st.reward_sats) : "—");
    var feeSats = st.fee_sats;
    var feePct = st.fee_percent;
    if (feeSats != null) {{
      setTxt("rFee", fmtMhc(feeSats));
      setTxt("rFeeHint", (feePct != null ? feePct.toFixed(2)+"%" : "—")+" of reward");
    }} else if (feePct != null) {{
      setTxt("rFee", feePct.toFixed(2)+"%");
      setTxt("rFeeHint", "configured · no credits yet");
    }} else {{
      setTxt("rFee", "—");
      setTxt("rFeeHint", "open round");
    }}
    setTxt("rDist", st.reward_sats != null ? fmtMhc(st.distributable_sats||0) : "—");
    setTxt("rShares", String(st.shares_total||0));
    setTxt("rMinersHint", (st.miners_count||0)+" miners · diff "+Number(st.difficulty_total||0).toFixed(0));
    var metaBits = [];
    if (st.started_ts) metaBits.push("started "+fmtTsLocal(st.started_ts));
    if (st.ended_ts) metaBits.push("ended "+fmtTsLocal(st.ended_ts));
    else metaBits.push("still open");
    $("roundMeta").textContent = metaBits.join(" · ");
    var bh = st.block_hash;
    if (bh) {{
      $("rBlockText").innerHTML = hashCell(bh, "block") +
        (st.height != null ? ' · height <span class="mono">#'+st.height+'</span>' : '');
      setTxt("rHashHint", "explorer link");
    }} else {{
      $("rBlockText").textContent = "No block yet — shares accumulate until the pool finds one.";
      setTxt("rHashHint", "waiting for block");
    }}
    setRoundNavLink($("roundPrev"), st.prev_id, "← older");
    setRoundNavLink($("roundNext"), st.next_id, "newer →");
    var miners = st.miners || [];
    $("rMinersBody").innerHTML = miners.length ? miners.map(function(m){{
      var pct = m.prop_percent != null ? Number(m.prop_percent).toFixed(4) : "—";
      var cred = (st.reward_sats != null) ? fmtMhcFull(m.credit_sats||0) : "—";
      var stPill = m.credit_status ? statusBlockPill(m.credit_status) : '<span class="muted">—</span>';
      return '<tr>'+
        '<td class="mono"><a href="/miner/'+encodeURIComponent(m.address)+'" data-addr="'+String(m.address||'')+'">'+shortAddr(m.address)+'</a></td>'+
        '<td class="mono">'+String(m.shares||0)+'</td>'+
        '<td class="mono">'+Number(m.difficulty||0).toFixed(0)+'</td>'+
        '<td class="mono">'+pct+'%</td>'+
        '<td>'+cred+'</td>'+
        '<td>'+stPill+'</td>'+
        '</tr>';
    }}).join("") : '<tr><td colspan="6" class="muted">No shares in this round yet</td></tr>';
    loadRoundsList(Number(st.list_page) || roundsListPage || 1);
  }}
  function roundsListPagerHtml(page, totalPages){{
    page = Number(page)||1; totalPages = Number(totalPages)||1;
    var html = '';
    if (page > 1) html += '<a href="#" data-rounds-page="'+(page-1)+'">← newer</a>';
    else html += '<span class="muted">← newer</span>';
    html += '<span class="muted">page '+page+' / '+totalPages+'</span>';
    if (page < totalPages) html += '<a href="#" data-rounds-page="'+(page+1)+'">older →</a>';
    else html += '<span class="muted">older →</span>';
    return html;
  }}
  function roundListTime(r){{
    if (!r) return "—";
    if (r.found_ts != null && r.found_ts !== '') return fmtTsLocal(r.found_ts);
    if (r.ended_ts != null && r.ended_ts !== '') return fmtTsLocal(r.ended_ts);
    if (r.started_ts != null && r.started_ts !== '') return fmtTsLocal(r.started_ts);
    return "—";
  }}
  function renderRoundsList(payload){{
    var bb = $("roundsListBody");
    var pg = $("roundsListPager");
    var meta = $("roundsListMeta");
    if (!bb) return;
    var page = Number(payload.page)||1;
    var totalPages = Number(payload.total_pages)||1;
    var total = Number(payload.total)||0;
    roundsListPage = page;
    if (meta) meta.textContent = total+" total · page "+page+" / "+totalPages;
    if (pg) pg.innerHTML = roundsListPagerHtml(page, totalPages);
    var rows = payload.rounds || [];
    if (!rows.length) {{
      bb.innerHTML = '<tr><td colspan="7" class="muted">No rounds yet</td></tr>';
      return;
    }}
    bb.innerHTML = rows.map(function(r){{
      var cur = currentRound != null && Number(r.id) === Number(currentRound);
      return '<tr class="'+(cur?'is-current':'')+'" data-round="'+r.id+'">'+
        '<td class="mono"><a class="round-link" href="/round/'+r.id+'" data-round="'+r.id+'">#'+r.id+'</a></td>'+
        '<td class="mono">'+(r.height != null ? '#'+r.height : '—')+'</td>'+
        '<td class="mono">'+roundListTime(r)+'</td>'+
        '<td>'+(r.reward_sats != null ? rewardCell(r.reward_sats) : '<span class="muted">—</span>')+'</td>'+
        '<td class="mono">'+String(r.shares||0)+'</td>'+
        '<td class="mono">'+String(r.miners||0)+'</td>'+
        '<td>'+statusBlockPill(r.status)+'</td>'+
        '</tr>';
    }}).join("");
  }}
  function loadRoundsList(page){{
    page = Math.max(1, Number(page)||1);
    roundsListPage = page;
    fetch("/api/rounds?page="+page+"&per_page="+ROUNDS_PER_PAGE).then(function(r){{ return r.json(); }}).then(function(payload){{
      if (!currentRound) return;
      renderRoundsList(payload);
    }}).catch(function(){{}});
  }}
  function loadRound(rid){{
    setTxt("rId", "#"+rid);
    $("roundTitle").textContent = "Round #"+rid;
    $("rMinersBody").innerHTML = '<tr><td colspan="6" class="muted">Loading…</td></tr>';
    fetch("/api/round/"+rid).then(function(r){{ return r.json(); }}).then(function(st){{
      if (currentRound !== rid) return;
      applyRound(st);
    }}).catch(function(){{ flash("Round lookup failed"); }});
  }}

  // Tabs
  $("minerTabs").querySelectorAll("button").forEach(function(btn){{
    btn.onclick = function(){{
      $("minerTabs").querySelectorAll("button").forEach(function(b){{ b.classList.remove("active"); }});
      btn.classList.add("active");
      var tab = btn.getAttribute("data-tab");
      document.querySelectorAll("[data-pane]").forEach(function(p){{
        p.classList.toggle("hidden", p.getAttribute("data-pane") !== tab);
      }});
    }};
  }});

  $("minerGo").onclick = function(){{ showMinerView($("minerQ").value); }};
  $("minerGo2").onclick = function(){{ showMinerView($("minerQ2").value); }};
  document.addEventListener("click", function(e){{
    var bp = e.target && e.target.closest && e.target.closest("[data-blocks-page]");
    if (bp) {{
      e.preventDefault();
      renderHomeBlocksPage(bp.getAttribute("data-blocks-page"));
      return;
    }}
    var rp = e.target && e.target.closest && e.target.closest("[data-rounds-page]");
    if (rp) {{
      e.preventDefault();
      loadRoundsList(rp.getAttribute("data-rounds-page"));
      return;
    }}
    var rl = e.target && e.target.closest && e.target.closest("a[data-round], a.round-link[href^='/round/']");
    if (rl) {{
      if (rl.classList.contains("disabled")) {{ e.preventDefault(); return; }}
      var rid = rl.getAttribute("data-round") || (rl.getAttribute("href")||"").replace(/^\\/round\\//,"");
      rid = Number(rid);
      if (rid && isFinite(rid)) {{
        e.preventDefault();
        showRoundView(rid);
        try {{ window.scrollTo(0, 0); }} catch (err) {{}}
        return;
      }}
    }}
    var wa = e.target && e.target.closest && e.target.closest("#workersBody a[data-addr], #rMinersBody a[data-addr]");
    if (wa) {{
      e.preventDefault();
      showMinerView(wa.getAttribute("data-addr"));
    }}
  }});
  $("minerQ").addEventListener("keydown", function(e){{ if (e.key==="Enter") showMinerView($("minerQ").value); }});
  $("minerQ2").addEventListener("keydown", function(e){{ if (e.key==="Enter") showMinerView($("minerQ2").value); }});
  $("minerBack").onclick = function(e){{
    e.preventDefault();
    if (window.history && history.pushState) history.pushState({{}}, "", "/");
    showHome();
  }};
  $("roundBack").onclick = function(e){{
    e.preventDefault();
    if (window.history && history.pushState) history.pushState({{}}, "", "/");
    showHome();
  }};

  function route(){{
    var path = location.pathname || "/";
    var m = path.match(/^\\/miner\\/(.+)$/);
    if (m) {{
      var bootAddr = decodeURIComponent(m[1]);
      var boot = window.__MINER_BOOT || null;
      if (boot && boot.address && String(boot.address) !== bootAddr) boot = null;
      showMinerView(bootAddr, {{ boot: boot }});
      // Consume boot once so later in-SPA navigations fetch fresh.
      window.__MINER_BOOT = null;
      return;
    }}
    var rm = path.match(/^\\/round\\/(\\d+)$/);
    if (rm) {{
      showRoundView(Number(rm[1]));
      return;
    }}
    var qs = new URLSearchParams(location.search || "");
    if (qs.get("miner")) {{
      var qAddr = qs.get("miner");
      var qBoot = window.__MINER_BOOT || null;
      if (qBoot && qBoot.address && String(qBoot.address) !== qAddr) qBoot = null;
      showMinerView(qAddr, {{ boot: qBoot }});
      window.__MINER_BOOT = null;
      return;
    }}
    showHome();
  }}
  function scrollToSection(id){{
    var el = document.getElementById(id);
    if (!el) return;
    // Keep sticky header visible (native #hash scroll hides it on mobile).
    var header = document.querySelector("header.top");
    var pad = header ? header.getBoundingClientRect().height + 8 : 72;
    var y = el.getBoundingClientRect().top + window.pageYOffset - pad;
    window.scrollTo({{ top: Math.max(0, y), behavior: "smooth" }});
    setNav(id === "blocks" ? "navBlocks" : (id === "miners" ? "navMiners" : (id === "connect" ? "navConnect" : "navPool")));
  }}
  document.querySelectorAll('nav a[href^="/#"]').forEach(function(a){{
    a.addEventListener("click", function(e){{
      var href = a.getAttribute("href") || "";
      var id = href.replace(/^\\/#/, "");
      if (!id || !document.getElementById(id)) return;
      e.preventDefault();
      showHome();
      // Soft URL without leaving a sticky #hash that breaks F5 on mobile.
      if (history.replaceState) history.replaceState(null, "", "/");
      setTimeout(function(){{ scrollToSection(id); }}, 30);
    }});
  }});
  window.addEventListener("popstate", route);

  function refreshHome(){{
    if (currentMiner || currentRound) return;
    fetch("/api/stats").then(function(r){{ return r.json(); }}).then(renderStats).catch(function(){{}});
  }}
  ensureCharts();
  route();
  // Always land at top after load/F5 (header visible on mobile).
  try {{ window.scrollTo(0, 0); }} catch (e) {{}}
  // SSR boot paints instantly; pull full /api/stats shortly after (not in parallel with parse).
  if (!currentMiner && !currentRound && window.__POOL_BOOT) renderStats(window.__POOL_BOOT);
  if (currentMiner) {{
    // Soft refresh miner after first paint (boot already applied).
    setTimeout(function(){{ if (currentMiner) loadMiner(currentMiner); }}, 400);
  }} else if (!currentRound) {{
    setTimeout(refreshHome, 400);
  }}
  setInterval(refreshHome, 8000);
}})();
</script>
</body>
</html>"""
    # Inject boot payload before the main SPA script (doc already has single braces).
    boot_tag = (
        f"<script>window.__POOL_BOOT={boot_json};"
        f"window.__MINER_BOOT={miner_boot_json};</script>\n"
    )
    marker = "</footer>\n\n<script>\n(function(){"
    if marker in doc:
        doc = doc.replace(marker, "</footer>\n\n" + boot_tag + "<script>\n(function(){", 1)
    else:
        doc = boot_tag + doc
    return doc.encode("utf-8")


def start_web(
    db: PoolDB,
    host: str,
    port: int,
    *,
    pool_address: str = "",
    json_port: int = 3333,
    stratum_port: int = 3334,
    fee_percent: float = 1.0,
    share_factor: int = 1024,
    explorer_url: str = "https://explorer.mhcscan.com",
    payout_threshold_sats: int = 10_000_000_000,
    mature_confirms: int = 20,
    public_host: str = "mine.mhcscan.com",
    engine: Any | None = None,
) -> ThreadingHTTPServer:
    def _attach_live(overview: dict, *, enrich_blocks: bool = True) -> dict:
        """Merge chain/peer metrics (+ optional block notes)."""
        ov = dict(overview or {})
        if engine is None:
            return ov
        if enrich_blocks:
            try:
                ov["blocks"] = engine.enrich_blocks(ov.get("blocks") or [])
            except Exception:
                logger.debug("block enrich failed", exc_info=True)
        try:
            # Prefer cached metrics from maintenance_tick — avoid blocking page load.
            metrics = getattr(engine, "_last_metrics", None) or {}
            if not metrics:
                metrics = engine.refresh_metrics()
            ov["mainnet_tip"] = metrics.get("mainnet_tip")
            ov["peer_count"] = metrics.get("peer_count")
            ov["known_addrs"] = metrics.get("known_addrs")
            ov["outbound_target"] = metrics.get("outbound_target")
            ov["peers"] = metrics.get("peers") or []
            ov["orphaned_rounds"] = metrics.get("orphaned_rounds")
            ov["stale_immature_rounds"] = metrics.get("stale_immature_rounds")
            ov["alerts"] = list(getattr(engine, "_last_alerts", []) or [])
        except Exception:
            logger.debug("metrics attach failed", exc_info=True)
        return ov

    def build_html(miner_addr: str | None = None) -> bytes:
        try:
            overview = db.stats_overview()
        except Exception:  # noqa: BLE001 — never fail page render
            overview = {}
        # Fast SSR: network KPIs + first page of blocks only (full list via /api/stats).
        try:
            blocks = list(overview.get("blocks") or [])
            overview["blocks"] = blocks[:20]
            # Drop heavy series from HTML boot — charts fill on first /api/stats.
            overview.pop("hashrate_history", None)
        except Exception:
            pass
        overview = _attach_live(overview, enrich_blocks=True)
        miner_boot = None
        addr = (miner_addr or "").strip()
        if addr:
            try:
                miner_boot = db.miner_stats(
                    addr,
                    payout_threshold_sats=payout_threshold_sats,
                    mature_confirms=mature_confirms,
                )
            except Exception:
                logger.debug("miner SSR boot failed", exc_info=True)
                miner_boot = {"address": addr}
        return _page_shell(
            pool_address=pool_address,
            json_port=json_port,
            stratum_port=stratum_port,
            fee_percent=fee_percent,
            share_factor=share_factor,
            explorer_url=explorer_url,
            payout_threshold_sats=payout_threshold_sats,
            mature_confirms=mature_confirms,
            overview=overview,
            miner_boot=miner_boot,
            public_host=public_host,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - " + fmt, self.address_string(), *args)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
                self._asset("favicon.ico" if path == "/favicon.ico" else "logo.png")
                return
            if path.startswith("/static/"):
                name = path.split("/static/", 1)[1].strip("/")
                if name in ("logo.svg", "logo.png", "favicon.ico"):
                    self._asset(name)
                    return
                self.send_error(404)
                return
            if path == "/api/stats":
                try:
                    ov = db.stats_overview()
                except Exception:
                    ov = {}
                self._json(_attach_live(ov))
                return
            if path.startswith("/api/miner/"):
                addr = unquote(path.split("/api/miner/", 1)[1].strip("/"))
                self._json(
                    db.miner_stats(
                        addr,
                        payout_threshold_sats=payout_threshold_sats,
                        mature_confirms=mature_confirms,
                    )
                )
                return
            if path.startswith("/api/round/"):
                rid_s = unquote(path.split("/api/round/", 1)[1].strip("/"))
                try:
                    rid = int(rid_s)
                except ValueError:
                    self._json({"error": "invalid round id"})
                    return
                detail = db.round_detail(rid, fee_percent=fee_percent)
                if detail is None:
                    self._json({"error": "round not found", "id": rid})
                    return
                self._json(detail)
                return
            if path == "/api/rounds":
                qs = parse_qs(parsed.query or "")
                try:
                    page = int((qs.get("page") or ["1"])[0])
                except ValueError:
                    page = 1
                try:
                    per_page = int((qs.get("per_page") or ["20"])[0])
                except ValueError:
                    per_page = 20
                self._json(db.rounds_list(page=page, per_page=per_page))
                return
            # SPA for /, /miner/<addr>, /round/<id>, and ?miner= — rebuild each request with live SSR KPIs.
            miner_addr = None
            if path.startswith("/miner/"):
                miner_addr = unquote(path.split("/miner/", 1)[1].strip("/"))
            else:
                qs = parse_qs(parsed.query or "")
                if qs.get("miner"):
                    miner_addr = unquote(str(qs.get("miner")[0] or ""))
            # /round/<id> uses the same SPA shell; client route loads /api/round.
            self._html(build_html(miner_addr=miner_addr))

        def _asset(self, name: str) -> None:
            path = POOL_STATIC.get(name)
            if path is None or not path.is_file():
                self.send_error(404)
                return
            data = path.read_bytes()
            ctype = {
                "favicon.ico": "image/x-icon",
                "logo.svg": "image/svg+xml",
                "logo.png": "image/png",
            }.get(name, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj: dict) -> None:
            if "pool_hashrate" in obj and "pool_hashrate_display" not in obj:
                obj = dict(obj)
                obj["pool_hashrate_display"] = _fmt_hps(
                    float(obj.get("pool_hashrate") or 0)
                )
                if obj.get("pool_address") is None and pool_address:
                    obj["pool_address"] = pool_address
                obj["fee_percent"] = fee_percent
                obj["share_factor"] = share_factor
                obj["payout_threshold_sats"] = payout_threshold_sats
                obj["mature_confirms"] = mature_confirms
                obj["ports"] = {
                    "json": json_port,
                    "stratum": stratum_port,
                    "web": port,
                }
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def _html(self, data: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer((host, port), Handler)
    t = threading.Thread(target=httpd.serve_forever, name="mhcoin-pool-web", daemon=True)
    t.start()
    logger.info("Pool stats http://%s:%s", host, port)
    return httpd
