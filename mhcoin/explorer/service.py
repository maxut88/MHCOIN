"""MHCOIN LAN block explorer — stdlib HTTP (read-only)."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from mhcoin.blockchain.readonly_chain import ReadOnlyChain
from mhcoin.explorer import decode as D
from mhcoin.wallet.addresses import validate_address

logger = logging.getLogger("mhcoin.explorer")

HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
ASSETS_DIR = Path(__file__).resolve().parents[1] / "desktop" / "assets"
STATIC_FILES = {
    "logo.svg": ASSETS_DIR / "mhcoin-logo.svg",
    "logo.png": ASSETS_DIR / "mhcoin-256.png",
    "favicon.ico": ASSETS_DIR / "mhcoin.ico",
}

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


def _mint_mark(*, title: str = "Mined") -> str:
    """Tiny MHCOIN coin mark for mined / minted rows."""
    return (
        f'<span class="mint" title="{_esc(title)}">'
        f'<span class="mint-coin">{_LOGO_SVG_M}</span>'
        f"</span>"
    )


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
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#ffffff"/>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='12' y1='8' x2='52' y2='56' gradientUnits='userSpaceOnUse'%3E%3Cstop stop-color='%235dffc0'/%3E%3Cstop offset='.42' stop-color='%232dd4a0'/%3E%3Cstop offset='1' stop-color='%230f7a55'/%3E%3C/linearGradient%3E%3C/defs%3E%3Ccircle cx='32' cy='32' r='30' fill='url(%23g)'/%3E%3Cpath d='M14 46V18h7.2l9.8 17.6L40.8 18H48v28h-5.8V27.6L36.2 46h-4.1L21.8 27.6V46H14z' fill='%2306261a'/%3E%3C/svg%3E"/>
<link rel="alternate icon" href="/static/favicon.ico"/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@450;600&display=swap" rel="stylesheet"/>
<title>{_esc(title)} — MHCOIN Explorer</title>
<style>
  :root {{
    --bg: #f4f5f7; --panel: #ffffff; --text: #111418; --muted: #6b7280;
    --link: #111418; --line: #e5e7eb; --soft: #f9fafb;
    --sans: "DM Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; font-family: var(--sans); color: var(--text); line-height: 1.5;
    background: var(--bg); min-height: 100vh;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ color: #000; text-decoration: underline; }}
  header.top {{
    position: sticky; top: 0; z-index: 20;
    background: rgba(255,255,255,.94);
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
    border-color: #9ca3af; background: #fff; box-shadow: 0 0 0 3px rgba(17,20,24,.06);
  }}
  form.search button {{
    padding: .65rem 1.05rem; border-radius: 10px; border: 0;
    background: var(--text); color: #fff; font-weight: 700; cursor: pointer; font: inherit;
  }}
  form.search button:hover {{ background: #000; }}
  main {{ width: 100%; margin: 0; padding: 1rem 1.5rem 2.75rem; }}
  .shell {{ width: 100%; }}
  .kpi {{
    display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr));
    gap: .75rem; margin: 0 0 1rem;
  }}
  @media (max-width: 1100px) {{ .kpi {{ grid-template-columns: repeat(3, minmax(120px, 1fr)); }} }}
  @media (max-width: 640px) {{ .kpi {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }} }}
  .kpi .stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: .85rem .95rem; }}
  .kpi .lbl {{ color: var(--muted); font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .05em; }}
  .kpi .val {{ font-size: 1.2rem; font-weight: 780; margin-top: .2rem; letter-spacing: -.02em; }}
  .kpi .hint {{ color: var(--muted); font-size: .75rem; margin-top: .15rem; }}
  .blocks-row {{
    display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(300px, .95fr);
    gap: .75rem; align-items: start; margin: 0 0 1rem;
  }}
  @media (max-width: 1100px) {{ .blocks-row {{ grid-template-columns: 1fr; }} }}
  .blocks-side {{
    display: flex; flex-direction: column; gap: .65rem; min-width: 0;
  }}
  /* Height set by JS to exactly two full BLOCK FOUND cards (peers sits below). */
  .blocks-row .term {{
    margin: 0; overflow: auto;
    display: flex; flex-direction: column;
  }}
  .blocks-row .term .term-feed {{ flex: 1 1 auto; overflow: visible; }}
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
  .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 1rem 1.1rem; margin: 0 0 1rem; }}
  .card-head {{ display: flex; align-items: center; justify-content: space-between; gap: .75rem; margin-bottom: .75rem; }}
  .card-head h1 {{ margin: 0; font-size: 1.05rem; }}
  .card-head .more {{ font-size: .85rem; font-weight: 650; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .85rem; font-weight: 750; letter-spacing: -.02em; }}
  .table-wrap {{ width: 100%; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .78rem; }}
  th, td {{ text-align: left; padding: .55rem .45rem; border-bottom: 1px solid var(--line); vertical-align: middle; }}
  th {{ color: var(--muted); font-weight: 700; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; }}
  tbody tr:hover {{ background: var(--soft); }}

  .row-ico {{ display: inline-flex; align-items: center; gap: .45rem; }}
  .mint {{
    --mint: 18px; width: var(--mint); height: var(--mint);
    display: inline-grid; place-items: center; flex-shrink: 0; vertical-align: middle;
  }}
  .mint-coin {{
    width: 100%; height: 100%; border-radius: 50%;
    background: linear-gradient(145deg, #5dffc0 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow: 0 0 0 1px rgba(15,118,110,.25);
    display: grid; place-items: center;
    animation: throb 2.6s ease-in-out infinite;
  }}
  .mint-coin svg {{ width: 58%; height: 58%; display: block; }}
  .pill {{
    display: inline-flex; align-items: center; padding: .12rem .45rem; border-radius: 999px;
    font-size: .72rem; font-weight: 700; background: #111418; color: #fff; white-space: nowrap;
  }}
  .pill.minted {{ background: #0f766e; }}
  .pill.xfer {{ background: #1f2937; }}
  .alert {{
    border: 1px solid #fcd34d; background: #fffbeb; color: #92400e;
    border-radius: 12px; padding: .85rem 1rem; margin: 0 0 1rem; font-size: .92rem;
  }}
  .alert strong {{ color: #78350f; }}
  .term {{
    background: #ffffff; color: #111418; border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.1rem; margin: 0 0 1rem; font-family: var(--mono); font-size: .86rem;
  }}
  .term-head {{ display: flex; flex-wrap: wrap; gap: .75rem; align-items: baseline; justify-content: space-between; margin-bottom: .75rem; }}
  .term-head h1 {{ margin: 0; color: #111418; font-family: var(--sans); font-size: 1.05rem; }}
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
  .found {{ border-top: 1px solid #e5e7eb; padding-top: .65rem; }}
  .found:first-child {{ border-top: 0; padding-top: 0; }}
  .found .title {{ color: #16a34a; font-weight: 800; }}
  .found .k {{ color: #9ca3af; }}
  .found .hash, .found .hash a {{ color: #ca8a04; word-break: break-all; text-decoration: none; }}
  .found .hash a:hover {{ color: #a16207; text-decoration: underline; }}
  .found .reward {{ color: #16a34a; font-weight: 700; }}
  table .reward, .kv .reward, .stat .reward {{ color: #16a34a; font-weight: 700; }}
  .found .h, .found .h a {{ color: #111418; font-weight: 700; text-decoration: none; }}
  .found .h a:hover {{ text-decoration: underline; }}
  .found .val, .found .session {{ color: #111418; }}
  .found .cyan, .found .cyan a {{ color: #0891b2; text-decoration: none; }}
  .found .cyan a:hover {{ text-decoration: underline; }}
  .found a {{ color: inherit; }}
  .found .rule {{ color: #d1d5db; margin-top: .35rem; }}
  table a, table a.mono, .row-ico a {{ color: #111418; }}
  table a:hover, table a.mono:hover, .row-ico a:hover {{ color: #000; }}
  .kpi .val a {{ color: #111418; }}
  .kpi .val a:hover {{ color: #000; }}
  .mono {{ font-family: var(--mono); font-size: .72rem; word-break: break-all; }}
  .nowrap {{ white-space: nowrap; }}
  th.num, td.num {{ text-align: center; white-space: nowrap; }}
  th.num-conf, td.num-conf {{ text-align: center; width: 1%; white-space: nowrap; }}
  .muted {{ color: var(--muted); }}
  .err {{ color: #b42318; }}
  .ok {{ color: #0f766e; font-weight: 650; }}
  .kv {{ display: grid; grid-template-columns: 150px 1fr; gap: .4rem .85rem; font-size: .93rem; }}
  .kv div:nth-child(odd) {{ color: var(--muted); }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .75rem; }}
  .stat {{ background: var(--soft); border: 1px solid var(--line); border-radius: 12px; padding: .85rem .9rem; }}
  .stat .lbl {{ color: var(--muted); font-size: .72rem; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; }}
  .stat .val {{ font-size: 1.15rem; font-weight: 750; margin-top: .25rem; letter-spacing: -.02em; }}
  .pager {{ display: flex; flex-wrap: wrap; gap: .65rem; align-items: center; margin: .75rem 0 0; }}
  .pager a {{
    padding: .35rem .75rem; border-radius: 8px; border: 1px solid var(--line);
    background: var(--soft); font-weight: 650; font-size: .88rem; color: var(--text); text-decoration: none;
  }}
  .pager a:hover {{ background: #fff; border-color: #c5c9d0; text-decoration: none; }}
  .badge {{
    display: inline-block; padding: .18rem .5rem; border-radius: 999px; background: #111418; color: #fff;
    font-size: .75rem; font-weight: 700; vertical-align: middle;
  }}
  .badge.warn {{ background: #fef3c7; color: #92400e; }}
  ul.plain {{ list-style: none; padding: 0; margin: 0; }}
  ul.plain li {{ padding: .55rem 0; border-bottom: 1px solid var(--line); }}
  footer.site {{ width: 100%; margin: 0; padding: 0 1.5rem 2rem; color: var(--muted); font-size: .82rem; }}
  @media (max-width: 720px) {{
    .kv {{ grid-template-columns: 1fr; }}
    header.top {{ padding: .85rem 1rem; }}
    main {{ padding: 1rem; }}
    .logo {{ --logo-size: 44px; }}
  }}

  .copy-wrap {{ display: inline-flex; align-items: center; gap: .35rem; max-width: 100%; }}
  .copy-btn {{
    border: 1px solid var(--line); background: var(--soft); color: var(--muted);
    border-radius: 6px; padding: .05rem .35rem; cursor: pointer; font-size: .7rem; line-height: 1.2;
  }}
  .copy-btn:hover {{ color: var(--text); background: #fff; }}
  .copy-btn.ok {{ color: #16a34a; border-color: #86efac; }}
  .spark {{ display: block; width: 100%; max-width: 260px; height: 48px; }}
  .kpi {{ grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }}
  .meta-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .75rem; margin: 0 0 1rem; }}
  .mini-stat .val {{ font-size: 1.05rem; }}
  table .mono {{ font-size: .72rem; }}
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
          <a href="/blocks">All blocks</a>
          <a href="/transactions">All transactions</a>
          <a href="/block/0">Genesis</a>
          <a href="/block/1">Block #1</a>
        </nav>
      </div>
    </div>
    <form class="search" method="get" action="/search">
      <input name="q" placeholder="height · block / txid · mhc1…" autocomplete="off"/>
      <button type="submit">Search</button>
    </form>
  </div>
</header>
<main>
{body}
</main>
<footer class="site">MHCOIN · HASH256 PoW · target 600s · LAN explorer</footer>

<script>
(function(){{
  document.addEventListener('click', function(e){{
    var b = e.target.closest && e.target.closest('.copy-btn');
    if (!b) return;
    var t = b.getAttribute('data-copy') || '';
    if (!t) return;
    function done(){{
      b.classList.add('ok');
      b.textContent = '✓';
      setTimeout(function(){{ b.classList.remove('ok'); b.textContent = '⎘'; }}, 900);
    }}
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(t).then(done).catch(function(){{
        var a=document.createElement('textarea'); a.value=t; document.body.appendChild(a); a.select();
        try {{ document.execCommand('copy'); }} catch(e) {{}}
        document.body.removeChild(a); done();
      }});
    }}
  }});
}})();
</script>
</body>
</html>
"""
    return doc.encode("utf-8")



class ExplorerApp:
    def __init__(self, data_dir: Path, *, hrp: str = "mhc"):
        self.data_dir = Path(data_dir)
        self.hrp = hrp

    def chain(self) -> ReadOnlyChain:
        c = ReadOnlyChain(self.data_dir)
        c.refresh_tip()
        return c

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
            import json
            import time as _time

            raw = json.loads(path.read_text(encoding="utf-8"))
            out["peer_count"] = int(raw.get("peer_count") or 0)
            out["height"] = raw.get("height")
            out["listen"] = raw.get("listen")
            sync = raw.get("sync") or {}
            out["sync_state"] = sync.get("state")
            out["updated"] = int(path.stat().st_mtime)
            out["updated_age"] = D.format_age(int(path.stat().st_mtime), now=int(_time.time()))
            peers_out: list[dict[str, Any]] = []
            for p in raw.get("peers") or []:
                addr = str(p.get("addr") or "")
                host, _, port = addr.rpartition(":")
                if not host:
                    host, port = addr, ""
                # Strip IPv6 brackets if present
                if host.startswith("[") and host.endswith("]"):
                    host = host[1:-1]
                mining = bool(p.get("mining"))
                hps = int(p.get("hps") or 0) if mining else 0
                peers_out.append(
                    {
                        "addr": addr,
                        "ip": host,
                        "port": port,
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
            # Live total = sum of rows shown (same source as peers table)
            reported_hps = sum(int(p.get("hps") or 0) for p in peers_out if p.get("mining"))
            reported_n = sum(1 for p in peers_out if p.get("mining"))
            out["reported_hashrate_hps"] = int(reported_hps)
            out["reported_hashrate"] = D.format_hps(float(reported_hps)) if reported_hps > 0 else None
            out["reported_miners"] = int(reported_n)
            # Stable order: inbound first, then IP
            peers_out.sort(key=lambda x: (0 if x["inbound"] else 1, x.get("ip") or ""))
            out["peers"] = peers_out
            miner = raw.get("miner") or {}
            if miner.get("hashrate_hps"):
                out["local_miner_hps"] = miner.get("hashrate_hps")
                out["local_miner_hashrate"] = D.format_hps(float(miner["hashrate_hps"]))
        except Exception:
            pass
        return out

    def home(self) -> dict[str, Any]:
        c = self.chain()
        try:
            data = D.chain_stats(c, hrp=self.hrp)
            data["node"] = self.node_status()
            data["mempool"] = D.load_mempool(self.data_dir, hrp=self.hrp)
            return data
        finally:
            c.close()

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
            return D.find_tx(c, txid.lower(), hrp=self.hrp)
        finally:
            c.close()

    def address(self, addr: str) -> dict[str, Any] | None:
        if not validate_address(addr, hrp=self.hrp):
            return None
        c = self.chain()
        try:
            return D.address_history(c, addr, hrp=self.hrp, limit=5000)
        finally:
            c.close()


def _pager(page: int, total_pages: int, base: str = "/blocks") -> str:
    prev_l = (
        f'<a href="{base}?page={page - 1}">← newer</a>' if page > 1 else '<span class="muted">← newer</span>'
    )
    next_l = (
        f'<a href="{base}?page={page + 1}">older →</a>'
        if page < total_pages
        else '<span class="muted">older →</span>'
    )
    return (
        f'<div class="pager">{prev_l}'
        f'<span class="muted">page {page} / {total_pages}</span>'
        f"{next_l}"
        f'<a href="{base}?page={total_pages}">genesis page</a>'
        f'<a href="/block/0">block #0</a></div>'
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


def _sparkline(values: list, *, width: int = 220, height: int = 48, stroke: str = "#111418") -> str:
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
            f'<td class="num"><span class="row-ico">{_mint_mark(title="Block mined")}'
            f'<a href="/block/{h}"><strong>{h}</strong></a></span></td>'
            f'<td>{_copyable(b.get("hash"), href="/block/" + str(b.get("hash")))}</td>'
            f'<td class="num">{b.get("tx_count")}</td>'
            f'<td class="num"><strong class="reward">{_esc(b.get("reward_mhc") or "—")}</strong> <span class="muted">MHC</span></td>'
            f'<td class="muted nowrap num">{_esc(_fmt_interval(b.get("interval_seconds")))}</td>'
            f'<td class="muted nowrap num">{_esc(b.get("age") or "—")}</td>'
            f"<td>{miner_html}</td>"
            f'<td class="muted num-conf">{_esc(b.get("confirmations"))}</td>'
            "</tr>"
        )
    return f"""
    <div class="table-wrap"><table>
      <tr>
        <th class="num">Height</th><th>Hash</th><th class="num">Txs</th><th class="num">Reward</th>
        <th class="num">Time</th><th class="num">Age</th><th>Miner</th><th class="num-conf">Confirmations</th>
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
        mark = _mint_mark(title="Mined reward" if cb else "Transfer")
        pill = (
            '<span class="pill minted">Mined</span>'
            if cb
            else '<span class="pill xfer">Transfer</span>'
        )
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
            f'<td><span class="row-ico">{mark}'
            f'{_copyable(t.get("txid"), href="/tx/" + str(t.get("txid")))}</span></td>'
            f"<td>{pill}</td>"
            f'<td><a href="/block/{t.get("height")}">#{t.get("height")}</a></td>'
            f"<td>{fr_html}</td>"
            f"<td>{to_html}</td>"
            f'<td><strong class="reward">{_esc(t.get("amount_mhc") or t.get("output_value_mhc"))}</strong> <span class="muted">MHC</span></td>'
            f'<td class="muted">{_esc(fee if fee is not None else "—")}</td>'
            f'<td class="muted nowrap">{_esc(t.get("age") or "—")}</td>'
            f'<td class="muted num-conf">{_esc(t.get("confirmations"))}</td>'
            "</tr>"
        )
    return f"""
    <div class="table-wrap"><table>
      <tr>
        <th>Txid</th><th>Type</th><th>Block</th><th>From wallet</th><th>To wallet</th>
        <th>Amount</th><th>Fee</th><th>Age</th><th class="num-conf">Confirmations</th>
      </tr>
      {"".join(rows)}
    </table></div>
    """


def _found_terminal(blocks: list[dict[str, Any]]) -> str:
    if not blocks:
        return '<p class="muted">Waiting for the next block…</p>'
    cards = []
    rewards: list[float] = []
    for b in blocks:
        try:
            rewards.append(float(b.get("reward_mhc") or 0))
        except (TypeError, ValueError):
            rewards.append(0.0)
    n = len(blocks)
    for i, b in enumerate(blocks):
        h = b.get("height")
        dt = b.get("interval_seconds")
        time_s = f"{dt:.2f}s" if isinstance(dt, (int, float)) else "—"
        session_n = n - i
        session_rew = sum(rewards[i:])
        session_txt = f"{session_n} blocks · {session_rew:.8f} MHC"
        bh = b.get("hash") or ""
        cards.append(
            "<div class='found' data-height='"
            + _esc(h)
            + "'>"
            "<div class='title'>▸ BLOCK FOUND</div>"
            "<div>  <span class='k'>height</span>   <span class='h'><a href='/block/"
            + _esc(h)
            + "'>#"
            + _esc(h)
            + "</a></span></div>"
            "<div>  <span class='k'>hash</span>     <span class='hash'><a href='/block/"
            + _esc(bh)
            + "'>"
            + _esc(bh)
            + "</a></span></div>"
            "<div>  <span class='k'>reward</span>   <span class='reward'>"
            + _esc(b.get("reward_mhc") or "—")
            + " MHC</span></div>"
            "<div>  <span class='k'>time</span>     <span class='val'>"
            + _esc(time_s)
            + "</span></div>"
            "<div>  <span class='k'>session</span>  <span class='session'>"
            + _esc(session_txt)
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
        ip = p.get("ip") or "—"
        port = p.get("port") or ""
        ip_full = f"{ip}:{port}" if port else str(ip)
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
            f'<td class="ip" title="{_esc(p.get("addr") or ip_full)}">{_esc(ip_full)}</td>'
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
        <th>IP</th><th>Dir</th><th>Height</th><th>H/s</th><th>Mining</th><th>Version</th><th>State</th>
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
    charts = data.get("charts") or {}
    spark_iv = _sparkline(charts.get("intervals") or [])
    spark_hr = _sparkline(charts.get("hashrate_hps") or [], stroke="#16a34a")
    eta_hint = data.get("next_block_hint") or "—"
    overdue = bool(data.get("next_block_overdue"))
    eta_cls = "err" if overdue else "muted"
    peers_panel = _peers_panel(
        node.get("peers") or [],
        peer_count=peers,
        total_hps=reported_hps,
        total_hashrate=reported_hr,
    )
    miners = data.get("top_miners") or []
    miner_rows = []
    for m in miners[:8]:
        miner_rows.append(
            "<tr>"
            f'<td>{_copyable(m.get("address"), href="/address/" + str(m.get("address")))}</td>'
            f'<td class="num">{_esc(m.get("blocks"))}</td>'
            f'<td class="num">{_esc(m.get("share_pct"))}%</td>'
            f'<td class="num"><strong class="reward">{_esc(m.get("reward_mhc"))}</strong></td>'
            "</tr>"
        )
    miners_html = (
        '<div class="table-wrap"><table><tr><th>Miner</th><th class="num">Blocks</th>'
        '<th class="num">Share</th><th class="num">Rewards</th></tr>'
        + "".join(miner_rows)
        + "</table></div>"
        if miner_rows
        else '<p class="muted">No miners yet.</p>'
    )
    mp = data.get("mempool") or {}
    mp_rows = []
    for tx in mp.get("transactions") or []:
        tid = tx.get("txid") or "?"
        mp_rows.append(
            "<tr>"
            f"<td>{_copyable(tid, href='/tx/' + str(tid))}</td>"
            f'<td class="num">{_esc(tx.get("size_bytes") or "—")}</td>'
            f'<td class="num">{_esc(tx.get("amount_mhc") or tx.get("output_value_mhc") or tx.get("fee_mhc") or "—")}</td>'
            "</tr>"
        )
    mempool_html = (
        '<div class="table-wrap"><table><tr><th>Txid</th><th class="num">Size</th><th class="num">Amount</th></tr>'
        + "".join(mp_rows)
        + "</table></div>"
        if mp_rows
        else '<p class="muted">Mempool empty — no unconfirmed transactions.</p>'
    )
    body = f"""
    <div class="shell" id="explorerHome" data-tip="{_esc(data.get("tip_height"))}">
      <div class="kpi">
        <div class="stat">
          <div class="lbl">Block height</div>
          <div class="val" id="kpiHeight"><a href="/block/{data.get("tip_height")}">{data.get("tip_height")}</a></div>
          <div class="hint" id="kpiAge">{_esc(data.get("tip_age") or "—")} ago</div>
        </div>
        <div class="stat">
          <div class="lbl" id="kpiHashLbl">{_esc(hr_lbl)}</div>
          <div class="val" id="kpiHashrate">{_esc(hr)}</div>
          <div class="hint" id="kpiHashHint">{_esc(hr_hint)}</div>
        </div>
        <div class="stat">
          <div class="lbl">Difficulty</div>
          <div class="val" id="kpiDiff">{_esc(data.get("tip_difficulty_display") or "—")}</div>
          <div class="hint" id="kpiBits">bits {_esc(data.get("tip_bits") or "—")}</div>
        </div>
        <div class="stat">
          <div class="lbl">Next block</div>
          <div class="val" id="kpiEta">{_esc(data.get("next_block_eta") if not overdue else "overdue")}</div>
          <div class="hint {eta_cls}" id="kpiEtaHint">{_esc(eta_hint)}</div>
        </div>
        <div class="stat">
          <div class="lbl">Peers</div>
          <div class="val" id="kpiPeers">{_esc(peers_s)}</div>
          <div class="hint" id="kpiSync">{_esc(node.get("sync_state") or "—")}</div>
        </div>
        <div class="stat">
          <div class="lbl">Total blocks</div>
          <div class="val" id="kpiTotal"><a href="/blocks">{data.get("total_blocks")}</a></div>
        </div>
        <div class="stat">
          <div class="lbl">Transactions</div>
          <div class="val" id="kpiTxs">{data.get("total_transactions")}</div>
          <div class="hint" id="kpiXfer">{data.get("transfer_transactions")} transfers</div>
        </div>
        <div class="stat">
          <div class="lbl">Minted supply</div>
          <div class="val" id="kpiMint">{_esc(data.get("minted_mhc"))}</div>
          <div class="hint">subsidy {_esc(halv.get("current_subsidy_mhc") or "50")} MHC</div>
        </div>
      </div>

      <div class="blocks-row">
        <section class="card">
          <div class="card-head">
            <h1>Latest blocks</h1>
            <a class="more" href="/blocks">View all →</a>
          </div>
          <div id="latestBlocks"
               data-page="1"
               data-per-page="100"
               data-total-blocks="{_esc(data.get("total_blocks") or 0)}">{_blocks_table(data.get("recent") or [])}</div>
          <div class="pager" id="latestBlocksPager"></div>
        </section>
        <div class="blocks-side">
          <section class="term">
            <div class="term-head">
              <h1>Live Blocks</h1>
            </div>
            {_found_terminal(data.get("live_finds") or [])}
          </section>
          {peers_panel}
        </div>
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
             data-total-txs="{_esc(data.get("total_transactions") or 0)}">{_txs_table(data.get("recent_txs") or [])}</div>
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

      <div class="meta-row">
        <section class="card mini-stat">
          <div class="card-head"><h1>Block intervals</h1><span class="muted">last 30</span></div>
          {spark_iv}
          <p class="muted" style="margin:.5rem 0 0">avg {_esc(avg_s)} · target 10m</p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Observed hashrate</h1><span class="muted">per block</span></div>
          {spark_hr}
          <p class="muted" style="margin:.5rem 0 0">recent {_esc(hr_short or "—")} · window {_esc(hr_win or "—")}</p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Halving</h1><span class="muted">era {_esc(halv.get("era"))}</span></div>
          <div class="val">{_esc(halv.get("blocks_to_halving"))} blocks</div>
          <p class="muted" style="margin:.5rem 0 0">
            next #{_esc(halv.get("next_halving_height"))} · subsidy
            {_esc(halv.get("current_subsidy_mhc"))} → {_esc(halv.get("next_subsidy_mhc"))} MHC
          </p>
        </section>
        <section class="card mini-stat">
          <div class="card-head"><h1>Target</h1><span class="muted">{_esc(data.get("tip_bits") or "")}</span></div>
          <div class="mono" style="font-size:.75rem;word-break:break-all">{_esc(data.get("tip_target_short") or "—")}</div>
          <p class="muted" style="margin:.5rem 0 0">difficulty {_esc(data.get("tip_difficulty_display") or "—")}× genesis</p>
        </section>
      </div>

      <section class="card">
        <div class="card-head">
          <h1>Top miners</h1>
          <span class="muted" style="font-size:.85rem">last 100 blocks</span>
        </div>
        {miners_html}
      </section>

      <section class="card">
        <div class="card-head">
          <h1>Mempool</h1>
          <span class="muted" style="font-size:.85rem">{_esc(mp.get("count") or 0)} unconfirmed</span>
        </div>
        {mempool_html}
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
        return '<span class="mint" title="' + esc(title || '') + '"><span class="mint-coin">' +
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
      function copyable(text, href) {{
        if (!text) return '<span class="muted">—</span>';
        // Full wallet addresses everywhere (Miner / From / To); hashes may stay short.
        var shown = isWalletAddr(text) ? String(text) : shortHash(text);
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
      function fitLiveBlocksTwoCards() {{
        var term = document.querySelector('.blocks-row .term');
        var feed = document.getElementById('liveFinds');
        if (!term || !feed) return;
        var head = term.querySelector('.term-head');
        var cards = feed.querySelectorAll('.found');
        if (!cards.length) {{
          term.style.height = '';
          term.style.minHeight = '';
          term.style.maxHeight = '';
          return;
        }}
        var st = window.getComputedStyle(term);
        var h = parseFloat(st.paddingTop) + parseFloat(st.paddingBottom);
        if (head) h += head.offsetHeight;
        var headSt = head ? window.getComputedStyle(head) : null;
        if (headSt) h += parseFloat(headSt.marginBottom) || 0;
        var n = Math.min(2, cards.length);
        for (var i = 0; i < n; i++) h += cards[i].offsetHeight;
        h = Math.ceil(h);
        term.style.height = h + 'px';
        term.style.minHeight = h + 'px';
        term.style.maxHeight = h + 'px';
      }}
      function renderFinds(blocks) {{
        var feed = document.getElementById('liveFinds');
        if (!feed || !blocks || !blocks.length) return;
        var rewards = blocks.map(function (b) {{
          var n = Number(b.reward_mhc);
          return isFinite(n) ? n : 0;
        }});
        var n = blocks.length;
        feed.innerHTML = blocks.map(function (b, i) {{
          var dt = b.interval_seconds;
          var timeS = (typeof dt === 'number') ? (dt.toFixed(2) + 's') : '—';
          var sessionN = n - i;
          var sessionRew = 0;
          for (var j = i; j < n; j++) sessionRew += rewards[j];
          var sessionTxt = sessionN + ' blocks · ' + sessionRew.toFixed(8) + ' MHC';
          return (
            '<div class="found" data-height="' + esc(b.height) + '">' +
            '<div class="title">▸ BLOCK FOUND</div>' +
            '<div>  <span class="k">height</span>   <span class="h"><a href="/block/' + esc(b.height) + '">#' + esc(b.height) + '</a></span></div>' +
            '<div>  <span class="k">hash</span>     <span class="hash"><a href="/block/' + esc(b.hash) + '">' + esc(b.hash) + '</a></span></div>' +
            '<div>  <span class="k">reward</span>   <span class="reward">' + esc(b.reward_mhc || '—') + ' MHC</span></div>' +
            '<div>  <span class="k">time</span>     <span class="val">' + esc(timeS) + '</span></div>' +
            '<div>  <span class="k">session</span>  <span class="session">' + esc(sessionTxt) + '</span></div>' +
            '<div class="rule">──────────────────────────────────────────────────────────────</div>' +
            '</div>'
          );
        }}).join('');
        // After layout: viewport = exactly two full cards; rest scroll inside.
        requestAnimationFrame(function () {{
          fitLiveBlocksTwoCards();
          requestAnimationFrame(fitLiveBlocksTwoCards);
        }});
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
          var ipFull = p.ip || '—';
          if (p.port) ipFull += ':' + p.port;
          var hr = p.hashrate || (p.mining ? '…' : '—');
          var mine = p.mining ? 'yes' : 'no';
          if (p.mining) {{ miners += 1; sum += Number(p.hps) || 0; }}
          return '<tr>' +
            '<td class="ip" title="' + esc(p.addr || ipFull) + '">' + esc(ipFull) + '</td>' +
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
          '<thead><tr><th>IP</th><th>Dir</th><th>Height</th><th>H/s</th><th>Mining</th><th>Version</th><th>State</th></tr></thead>' +
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
            '<td class="num"><span class="row-ico">' + mintMark('Block mined') + '<a href="/block/' + esc(b.height) + '"><strong>' + esc(b.height) + '</strong></a></span></td>' +
            '<td>' + copyable(b.hash, '/block/' + encodeURIComponent(b.hash)) + '</td>' +
            '<td class="num">' + esc(b.tx_count) + '</td>' +
            '<td class="num"><strong class="reward">' + esc(b.reward_mhc || '—') + '</strong> <span class="muted">MHC</span></td>' +
            '<td class="muted nowrap num">' + esc(fmtInterval(b.interval_seconds)) + '</td>' +
            '<td class="muted nowrap num">' + esc(b.age || '—') + '</td>' +
            '<td>' + miner + '</td>' +
            '<td class="muted num-conf">' + esc(b.confirmations) + '</td></tr>';
        }}).join('');
        el.innerHTML = '<div class="table-wrap"><table><tr><th class="num">Height</th><th>Hash</th><th class="num">Txs</th><th class="num">Reward</th><th class="num">Time</th><th class="num">Age</th><th>Miner</th><th class="num-conf">Confirmations</th></tr>' + rows + '</table></div>';
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
          var pill = cb ? '<span class="pill minted">Mined</span>' : '<span class="pill xfer">Transfer</span>';
          var fr = t.from === 'coinbase' ? '<span class="muted">coinbase</span>'
            : (t.from ? copyable(t.from, '/address/' + encodeURIComponent(t.from)) : '<span class="muted">—</span>');
          var to = t.to ? copyable(t.to, '/address/' + encodeURIComponent(t.to)) : '<span class="muted">—</span>';
          var mark = mintMark(cb ? 'Mined reward' : 'Transfer');
          return '<tr>' +
            '<td><span class="row-ico">' + mark + copyable(t.txid, '/tx/' + encodeURIComponent(t.txid)) + '</span></td>' +
            '<td>' + pill + '</td>' +
            '<td><a href="/block/' + esc(t.height) + '">#' + esc(t.height) + '</a></td>' +
            '<td>' + fr + '</td><td>' + to + '</td>' +
            '<td><strong class="reward">' + esc(t.amount_mhc || t.output_value_mhc) + '</strong> <span class="muted">MHC</span></td>' +
            '<td class="muted">' + esc(t.fee_mhc != null ? t.fee_mhc : '—') + '</td>' +
            '<td class="muted nowrap">' + esc(t.age || '—') + '</td>' +
            '<td class="muted num-conf">' + esc(t.confirmations) + '</td></tr>';
        }}).join('');
        el.innerHTML = '<div class="table-wrap"><table><tr><th>Txid</th><th>Type</th><th>Block</th><th>From</th><th>To</th><th>Amount</th><th>Fee</th><th>Age</th><th class="num-conf">Confirmations</th></tr>' + rows + '</table></div>';
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
async function tick() {{
        try {{
          var r = await fetch('/api/?_=' + Date.now(), {{ cache: 'no-store' }});
          if (!r.ok) return;
          var d = await r.json();
          var tip = d.tip_height;
          var node = d.node || {{}};
          var hrEl = document.getElementById('kpiHashrate');
          var hrLbl = document.getElementById('kpiHashLbl');
          var hh = document.getElementById('kpiHashHint');
          if (node.reported_hashrate_hps > 0 && node.reported_hashrate) {{
            if (hrLbl) hrLbl.textContent = 'Hashrate (live)';
            if (hrEl) hrEl.textContent = node.reported_hashrate;
            if (hh) {{
              var parts = ['total ' + (node.reported_miners || 0) + ' miners'];
              if (d.network_hashrate) parts.push('implied ' + d.network_hashrate);
              if (d.network_hashrate_window) parts.push('obs ' + d.network_hashrate_window);
              if (d.network_hashrate_short) parts.push('recent ' + d.network_hashrate_short);
              hh.textContent = parts.join(' · ');
            }}
          }} else {{
            if (hrLbl) hrLbl.textContent = 'Hashrate (implied)';
            if (hrEl && d.network_hashrate) hrEl.textContent = d.network_hashrate;
            if (hh) {{
              var parts2 = ['live —', 'implied @10m'];
              if (d.network_hashrate_window) parts2.push('obs ' + d.network_hashrate_window);
              if (d.network_hashrate_short) parts2.push('recent ' + d.network_hashrate_short);
              hh.textContent = parts2.join(' · ');
            }}
          }}
          var ageEl = document.getElementById('kpiAge');
          if (ageEl && d.tip_age) ageEl.textContent = d.tip_age + ' ago';
          var hEl = document.getElementById('kpiHeight');
          if (hEl && tip != null) hEl.innerHTML = '<a href="/block/' + tip + '">' + tip + '</a>';
          var tot = document.getElementById('kpiTotal');
          if (tot && d.total_blocks != null) tot.innerHTML = '<a href="/blocks">' + d.total_blocks + '</a>';
          var txs = document.getElementById('kpiTxs');
          if (txs && d.total_transactions != null) txs.textContent = d.total_transactions;
          var mint = document.getElementById('kpiMint');
          if (mint && d.minted_mhc) mint.textContent = d.minted_mhc;
          var diff = document.getElementById('kpiDiff');
          if (diff && d.tip_difficulty_display) diff.textContent = d.tip_difficulty_display;
          var bits = document.getElementById('kpiBits');
          if (bits && d.tip_bits) bits.textContent = 'bits ' + d.tip_bits;
          var eta = document.getElementById('kpiEta');
          if (eta) eta.textContent = d.next_block_overdue ? 'overdue' : (d.next_block_eta || '—');
          var etaH = document.getElementById('kpiEtaHint');
          if (etaH && d.next_block_hint) {{
            etaH.textContent = d.next_block_hint;
            etaH.className = 'hint ' + (d.next_block_overdue ? 'err' : 'muted');
          }}
          var peers = document.getElementById('kpiPeers');
          if (peers && node.peer_count != null) peers.textContent = String(node.peer_count);
          var sync = document.getElementById('kpiSync');
          if (sync) sync.textContent = node.sync_state || '—';
          renderPeers(node.peers || [], node.peer_count);
          var meta = document.getElementById('hdrMeta');
          if (meta && tip != null) meta.textContent = 'Live mainnet · read-only · tip #' + tip;
          if (d.live_finds) renderFinds(d.live_finds);
          else fitLiveBlocksTwoCards();
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
          if (tip != null && tip > lastTip && lastTip >= 0) {{
            document.title = '▸ BLOCK #' + tip + ' — MHCOIN Explorer';
            var term = document.querySelector('.term');
            if (term) {{
              term.style.outline = '2px solid #111418';
              setTimeout(function(){{ term.style.outline = 'none'; }}, 1800);
            }}
          }}
          if (tip != null) lastTip = tip;
          var home = document.getElementById('explorerHome');
          if (home && tip != null) home.dataset.tip = String(tip);
        }} catch (e) {{}}
      }}
      setInterval(tick, 3000);
      tick();
      window.addEventListener('resize', function () {{
        requestAnimationFrame(fitLiveBlocksTwoCards);
      }});
      requestAnimationFrame(fitLiveBlocksTwoCards);
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
        label = "Mined" if tx.get("coinbase") else "Transfer"
        fee = tx.get("fee_mhc")
        fee_s = f'{_esc(fee)}' if fee is not None else "—"
        rate = tx.get("fee_per_byte")
        rate_s = f'{rate} sat/B' if rate is not None else "—"
        txs.append(
            "<tr>"
            f'<td class="num">{i}</td>'
            f"<td>{_copyable(tid, href='/tx/' + tid)}</td>"
            f'<td class="muted">{label}</td>'
            f'<td class="num"><strong class="reward">{_esc(tx.get("amount_mhc") or tx.get("output_value_mhc"))}</strong></td>'
            f'<td class="num muted">{fee_s}</td>'
            f'<td class="num muted">{_esc(rate_s)}</td>'
            "</tr>"
        )
    if not txs:
        for i, tid in enumerate(b.get("txids") or []):
            txs.append(
                f'<tr><td class="num">{i}</td><td>{_copyable(tid, href="/tx/" + tid)}</td>'
                f'<td class="muted">{"Mined" if i == 0 else ""}</td>'
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
        <div>Reward</div><div><strong class="reward">{_esc(b.get("reward_mhc") or "—")}</strong> MHC</div>
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
      <div class="table-wrap"><table>
        <tr><th class="num">#</th><th>Txid</th><th>Type</th><th class="num">Amount</th><th class="num">Fee</th><th class="num">Fee rate</th></tr>
        {"".join(txs)}
      </table></div>
    </div>
    </div>
    """
    return _page(f"Block {h}", body, tip=tip)


def _render_tx(t: dict[str, Any]) -> bytes:
    coinbase = bool(t.get("coinbase"))
    ins = []
    for i in t.get("inputs") or []:
        if i.get("coinbase"):
            ins.append("<li><span class='muted'>New coins (block reward)</span></li>")
        else:
            addr = i.get("address") or "?"
            val = i.get("value_mhc") or "?"
            prev = i.get("prev_txid")
            ins.append(
                "<li>"
                + _copyable(prev, href=f"/tx/{prev}")
                + f":{i.get('prev_vout')} → "
                + _copyable(addr, href=f"/address/{addr}", short=False)
                + f" <strong>{_esc(val)} MHC</strong></li>"
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
        outs.append(
            "<li>"
            + _copyable(addr, href=f"/address/{addr}", short=False)
            + f" <strong class='reward'>{_esc(o.get('value_mhc'))} MHC</strong>{tag}</li>"
        )
    conf = t.get("confirmations")
    kind = "Mined reward" if coinbase else "Transfer"
    badge = f'<span class="badge">{conf} confirmations</span>' if conf else ""
    height = t.get("block_height")
    included = (
        f'<a href="/block/{height}">Included in block #{height}</a>'
        if height is not None
        else '<span class="muted">Unconfirmed</span>'
    )
    fee_row = ""
    if not coinbase:
        fee_row = f"""
        <div class="stat"><div class="lbl">Network fee</div>
          <div class="val">{_esc(t.get("fee_mhc") or "—")} MHC</div></div>
        <div class="stat"><div class="lbl">Fee rate</div>
          <div class="val">{_esc(t.get("fee_rate") or "—")}</div></div>
"""
        if change_mhc is not None:
            fee_row += f"""
        <div class="stat"><div class="lbl">Change back</div>
          <div class="val">{_esc(change_mhc)} MHC</div></div>
"""
    tip = _inferred_tip(height, conf)
    amt_lbl = "Reward" if coinbase else "Sent to recipient"
    body = f"""
    <div class="shell">
    <div class="card">
      <div class="card-head">
        <h1>{kind} {badge}</h1>
        <span class="muted">{included}</span>
      </div>
      <div class="stats">
        <div class="stat"><div class="lbl">{amt_lbl}</div>
          <div class="val"><strong class="reward">{_esc(t.get("amount_mhc") or t.get("output_value_mhc"))}</strong> MHC</div></div>
        {fee_row}
        <div class="stat"><div class="lbl">Confirmations</div>
          <div class="val">{_esc(conf if conf is not None else "—")}</div></div>
        <div class="stat"><div class="lbl">Age</div>
          <div class="val">{_esc(t.get("age") or "—")}</div></div>
        <div class="stat"><div class="lbl">Size</div>
          <div class="val">{_esc(t.get("size_bytes"))} bytes</div></div>
      </div>
      <p class="muted" style="margin:.75rem 0 0;font-size:.85rem">
        {"Block reward credited to miner." if coinbase else
         "You sent the payment amount to the recipient. Change returns to your wallet. Only payment + network fee leave your balance."}
      </p>
      <div class="kv" style="margin-top:1rem">
        <div>Txid</div><div>{_copyable(t.get("txid"), short=False)}</div>
        <div>Block</div><div>{('<a href="/block/'+_esc(height)+'">#'+_esc(height)+'</a> · '+_esc(t.get("time_utc") or "")) if height is not None else "—"}</div>
        <div>Block hash</div><div>{_copyable(t.get("block_hash"), href="/block/"+str(t.get("block_hash")), short=False) if t.get("block_hash") else "—"}</div>
      </div>
    </div>
    <div class="card">
      <h1>From</h1>
      <ul class="plain">{"".join(ins) or "<li class='muted'>—</li>"}</ul>
    </div>
    <div class="card">
      <h1>To</h1>
      <ul class="plain">{"".join(outs) or "<li class='muted'>—</li>"}</ul>
    </div>
    </div>
    """
    return _page("Transaction", body, tip=tip)


def _render_address(a: dict[str, Any]) -> bytes:
    rows = []
    for o in a.get("outputs") or []:
        rows.append(
            "<tr>"
            f'<td class="num"><span class="row-ico">{_mint_mark(title="mined" if o.get("coinbase") else "transfer")}'
            f'<a href="/block/{o.get("height")}">{o.get("height")}</a></span></td>'
            f'<td>{_copyable(o.get("txid"), href="/tx/" + str(o.get("txid")))}</td>'
            f'<td class="num">{o.get("vout")}</td>'
            f'<td class="num"><strong class="reward">{_esc(o.get("value_mhc"))}</strong></td>'
            f'<td class="muted nowrap num">{_esc(o.get("age") or "")}</td>'
            f'<td class="num-conf">{_esc(o.get("confirmations"))}</td>'
            f'<td class="muted">{"mined" if o.get("coinbase") else "transfer"}</td>'
            "</tr>"
        )
    addr = a.get("address")
    tip = a.get("tip_height")
    body = f"""
    <div class="shell">
    <div class="card">
      <h1><span class="row-ico">{_mint_mark(title="Wallet")} Wallet</span></h1>
      <div class="kv">
        <div>Address</div><div>{_copyable(addr, short=False)}</div>
        <div>Balance</div><div><strong class="reward">{_esc(a.get("balance_mhc") or "0")}</strong> MHC
          <span class="muted">({_esc(a.get("utxo_count") or 0)} UTXO)</span></div>
        <div>Received</div><div>{_esc(a.get("total_received_mhc") or "0")} MHC
          <span class="muted"> · mining + incoming (no change)</span></div>
        <div>Sent to others</div><div><strong>{_esc(a.get("total_sent_mhc") or "0")}</strong> MHC
          <span class="muted"> · payments only</span></div>
        <div>Network fees</div><div>{_esc(a.get("total_fees_mhc") or "0")} MHC</div>
        <div>Outputs</div><div>{_esc(a.get("received_count"))}{_esc(" (truncated)" if a.get("truncated") else "")}</div>
      </div>
      <p class="muted" style="margin:.75rem 0 0;font-size:.85rem">
        Balance ≈ Received − Sent − Fees.
        A send spends a larger UTXO, pays the recipient, and returns change to you — change is not “Sent”.
      </p>
    </div>
    <div class="card">
      <div class="card-head">
        <h1>Received outputs</h1>
        <span class="muted">newest first</span>
      </div>
      <div class="table-wrap"><table>
        <tr><th class="num">Height</th><th>Txid</th><th class="num">vout</th><th class="num">MHC</th>
            <th class="num">Age</th><th class="num-conf">Confirmations</th><th>Type</th></tr>
        {"".join(rows) or '<tr><td colspan="7" class="muted">No outputs</td></tr>'}
      </table></div>
    </div>
    </div>
    """
    return _page("Address", body, tip=tip)



def make_handler(app: ExplorerApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            raw = json.dumps(obj, indent=2, sort_keys=True, default=str).encode()
            self._send(code, raw, "application/json; charset=utf-8")

        def _html(self, code: int, body: bytes) -> None:
            self._send(code, body, "text/html; charset=utf-8")

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
            self.end_headers()
            self.wfile.write(data)
            return True

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
                        self._html(404, _page("Not found", '<p class="err">Asset not found.</p>'))
                    return
                if key == "/favicon.ico":
                    if not self._static("favicon.ico"):
                        self.send_response(404)
                        self.end_headers()
                    return

                if key in ("/", ""):
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
                        self._html(404, _page("Not found", '<p class="err">Block not found.</p>'))
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
                        self._html(404, _page("Not found", '<p class="err">Transaction not found.</p>'))
                        return
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_tx(data))
                    return

                if key.startswith("/address/"):
                    addr = key[len("/address/") :].strip("/")
                    data = app.address(addr)
                    if data is None:
                        self._html(404, _page("Not found", '<p class="err">Invalid or unknown address.</p>'))
                        return
                    if want_json:
                        self._json(200, data)
                    else:
                        self._html(200, _render_address(data))
                    return

                if key.startswith("/search"):
                    q = ((qs.get("q") or qs.get("query") or [""])[0] or "").strip()
                    if not q:
                        self._html(400, _page("Search", '<p class="err">Empty query.</p>'))
                        return
                    # height
                    if q.isdigit():
                        self.send_response(302)
                        self.send_header("Location", f"/block/{int(q)}")
                        self.end_headers()
                        return
                    ql = q.lower()
                    if HEX64.match(ql):
                        # try block then tx
                        if app.block(ql) is not None:
                            self.send_response(302)
                            self.send_header("Location", f"/block/{ql}")
                            self.end_headers()
                            return
                        if app.tx(ql) is not None:
                            self.send_response(302)
                            self.send_header("Location", f"/tx/{ql}")
                            self.end_headers()
                            return
                        self._html(404, _page("Not found", '<p class="err">No block or tx with that hash.</p>'))
                        return
                    if ql.startswith(app.hrp + "1"):
                        self.send_response(302)
                        self.send_header("Location", f"/address/{q}")
                        self.end_headers()
                        return
                    self._html(400, _page("Search", '<p class="err">Unrecognized query.</p>'))
                    return

                self._html(404, _page("Not found", '<p class="err">Not found.</p>'))
            except Exception:
                logger.exception("explorer request failed path=%s", path)
                try:
                    self._html(500, _page("Error", '<p class="err">Internal error.</p>'))
                except Exception:
                    pass

    return Handler



def serve(data_dir: Path, *, host: str, port: int, hrp: str = "mhc") -> None:
    app = ExplorerApp(data_dir, hrp=hrp)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    logger.info("MHCOIN explorer on http://%s:%s/ datadir=%s", host, port, data_dir)
    httpd.serve_forever()
