"""MHCOIN pool stats UI — explorer-matched design (DM Sans / JetBrains Mono).

Inspired by classic pool dashboards (hashrate · miners · blocks · connect),
styled like the MHCOIN explorer (:8766).
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from mhcoin.pool.db import PoolDB

logger = logging.getLogger("mhcoin.pool.web")


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
) -> bytes:
    """Single-page app: KPIs, connect how-to, workers, blocks, miner lookup."""
    addr = _esc(pool_address or "—")
    exp = _esc(explorer_url.rstrip("/")) if explorer_url else ""
    exp_link = (
        f'<a href="{exp}/" rel="noopener">Explorer</a>'
        if exp
        else '<a href="http://192.168.0.221:8766/">Explorer</a>'
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
}})();
</script>
<meta name="description" content="MHCOIN mining pool — HASH256 · PROP · live hashrate and payouts."/>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700;800&family=JetBrains+Mono:wght@450;600&display=optional" rel="stylesheet"/>
<title>MHCOIN Pool</title>
<style>
  :root {{
    --bg: #f4f5f7; --panel: #ffffff; --text: #111418; --muted: #6b7280;
    --link: #111418; --line: #e5e7eb; --soft: #f9fafb;
    --header-bg: rgba(255,255,255,.94); --hover-bg: #ffffff; --invert: #000000;
    --val: #374151; --mint: #0f7a55; --mint-soft: rgba(45,212,160,.14);
    --sans: "DM Sans", system-ui, sans-serif;
    --mono: "JetBrains Mono", ui-monospace, Menlo, Consolas, monospace;
  }}
  [data-theme="dark"] {{
    --bg: #0b0d10; --panel: #15181d; --text: #e7eaee; --muted: #9aa3af;
    --link: #e7eaee; --line: #262b33; --soft: #1b1f26;
    --header-bg: rgba(11,13,16,.92); --hover-bg: #1e222a; --invert: #ffffff;
    --val: #c7ccd4; --mint: #5dffc0; --mint-soft: rgba(45,212,160,.12);
  }}
  * {{ box-sizing: border-box; }}
  html {{ background: var(--bg); color-scheme: light; overflow-x: clip; }}
  [data-theme="dark"] html, html[data-theme="dark"] {{ color-scheme: dark; }}
  body {{
    margin: 0; font-family: var(--sans); color: var(--text); line-height: 1.5;
    background: var(--bg); min-height: 100vh;
  }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  header.top {{
    position: sticky; top: 0; z-index: 20;
    background: var(--header-bg);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--line);
    padding: 1rem 1.5rem .9rem;
  }}
  .header-inner {{
    max-width: 1100px; margin: 0 auto;
    display: flex; flex-wrap: wrap; gap: 1rem 1.5rem;
    align-items: center; justify-content: space-between;
  }}
  .brand-row {{ display: flex; align-items: center; gap: .85rem; min-width: 0; }}
  .logo {{
    width: 44px; height: 44px; position: relative;
    display: grid; place-items: center; flex-shrink: 0;
  }}
  .logo-ring {{
    position: absolute; inset: 0; border-radius: 50%;
    border: 1.5px solid transparent; border-top-color: #2dd4a0;
    border-right-color: rgba(45,212,160,.28);
    animation: spin 2.8s linear infinite;
  }}
  .logo-coin {{
    width: 74%; height: 74%; border-radius: 50%;
    background: linear-gradient(145deg, #5dffc0 0%, #2dd4a0 42%, #0f7a55 100%);
    box-shadow: 0 0 0 1px rgba(45,212,160,.35), 0 0 12px rgba(45,212,160,.2);
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .brand h1 {{
    margin: 0; font-size: 1.15rem; font-weight: 800; letter-spacing: -.02em;
  }}
  .brand .sub {{ margin: .1rem 0 0; color: var(--muted); font-size: .82rem; }}
  header nav {{ display: flex; flex-wrap: wrap; gap: .35rem .95rem; }}
  header nav a {{
    font-size: .88rem; font-weight: 600; color: var(--muted);
    border-bottom: 2px solid transparent; padding-bottom: 2px;
  }}
  header nav a:hover, header nav a.active {{
    color: var(--text); border-bottom-color: var(--text); text-decoration: none;
  }}
  .theme-toggle {{
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
    border-radius: 999px; padding: .35rem .7rem; font: inherit; font-weight: 600;
    font-size: .8rem; cursor: pointer;
  }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 1.25rem 1.5rem 3rem; }}
  .hero {{
    margin: .5rem 0 1.25rem;
  }}
  .hero h2 {{
    margin: 0 0 .35rem; font-size: 1.65rem; font-weight: 800; letter-spacing: -.03em;
  }}
  .hero p {{ margin: 0; color: var(--muted); max-width: 52rem; }}
  .kpi {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: .75rem; margin: 1rem 0 1.35rem;
  }}
  .kpi .stat {{
    --accent: #64748b; --accent-soft: rgba(100,116,139,.08);
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: .9rem 1rem; position: relative; overflow: hidden;
    transition: border-color .15s ease, box-shadow .15s ease;
  }}
  .kpi .stat::before {{
    content: ""; position: absolute; inset: 0 auto 0 0; width: 3px;
    background: var(--accent);
  }}
  .kpi .stat:hover {{
    border-color: color-mix(in srgb, var(--accent) 35%, var(--line));
    box-shadow: 0 8px 24px rgba(0,0,0,.04);
  }}
  .kpi .label {{
    font-size: .72rem; font-weight: 700; letter-spacing: .06em;
    text-transform: uppercase; color: var(--muted); margin-bottom: .35rem;
  }}
  .kpi .val {{
    font-family: var(--mono); font-size: 1.15rem; font-weight: 700;
    color: var(--text); word-break: break-all;
  }}
  .kpi .hint {{ margin-top: .25rem; font-size: .78rem; color: var(--muted); }}
  .k-hash {{ --accent: #16a34a; --accent-soft: rgba(22,163,74,.10); }}
  .k-miners {{ --accent: #0ea5e9; --accent-soft: rgba(14,165,233,.10); }}
  .k-shares {{ --accent: #7c3aed; --accent-soft: rgba(124,58,237,.10); }}
  .k-round {{ --accent: #d97706; --accent-soft: rgba(217,119,6,.10); }}
  .k-fee {{ --accent: #0f766e; --accent-soft: rgba(15,118,110,.10); }}
  .k-blocks {{ --accent: #475569; --accent-soft: rgba(71,85,105,.10); }}
  .panel {{
    background: var(--panel); border: 1px solid var(--line); border-radius: 14px;
    padding: 1rem 1.1rem; margin: 0 0 1rem;
  }}
  .panel h3 {{
    margin: 0 0 .75rem; font-size: 1rem; font-weight: 750;
  }}
  .panel .muted {{ color: var(--muted); font-size: .88rem; }}
  .connect-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: .85rem;
  }}
  .connect-card {{
    background: var(--soft); border: 1px solid var(--line); border-radius: 12px;
    padding: .85rem 1rem;
  }}
  .connect-card .t {{
    font-size: .72rem; font-weight: 700; letter-spacing: .05em;
    text-transform: uppercase; color: var(--muted); margin-bottom: .35rem;
  }}
  code, .mono {{
    font-family: var(--mono); font-size: .84rem;
  }}
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
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{
    text-align: left; padding: .55rem .4rem; border-bottom: 1px solid var(--line);
    font-size: .88rem; vertical-align: top;
  }}
  th {{
    color: var(--muted); font-size: .72rem; font-weight: 700;
    letter-spacing: .05em; text-transform: uppercase;
  }}
  td.mono {{ font-family: var(--mono); font-size: .8rem; }}
  .pill {{
    display: inline-block; padding: .12rem .45rem; border-radius: 999px;
    font-size: .72rem; font-weight: 700; background: var(--mint-soft); color: var(--mint);
  }}
  .pill.off {{ background: var(--soft); color: var(--muted); }}
  .lookup {{
    display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .5rem;
  }}
  .lookup input {{
    flex: 1; min-width: 200px; padding: .65rem .8rem; border-radius: 10px;
    border: 1px solid var(--line); background: var(--bg); color: var(--text);
    font: inherit;
  }}
  .lookup button {{
    padding: .65rem 1rem; border-radius: 10px; border: none;
    background: var(--text); color: var(--bg); font: inherit; font-weight: 700;
    cursor: pointer;
  }}
  .lookup button:hover {{ opacity: .9; }}
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
  @media (max-width: 640px) {{
    header.top, main, footer {{ padding-left: 1rem; padding-right: 1rem; }}
    .hero h2 {{ font-size: 1.35rem; }}
  }}
</style>
</head>
<body>
<header class="top">
  <div class="header-inner">
    <div class="brand-row">
      <div class="logo" aria-hidden="true"><span class="logo-ring"></span><span class="logo-coin"></span></div>
      <div class="brand">
        <h1>MHCOIN Pool</h1>
        <p class="sub">HASH256 · PROP · live mainnet</p>
      </div>
    </div>
    <nav>
      {exp_link}
      <a href="/" class="active">Pool</a>
      <a href="#connect">Connect</a>
      <a href="#miners">Miners</a>
      <a href="#blocks">Blocks</a>
    </nav>
    <button type="button" class="theme-toggle" id="themeBtn" title="Toggle theme">Theme</button>
  </div>
</header>

<main>
  <div class="hero">
    <h2>Professional HASH256 pool</h2>
    <p>Proportional (PROP) shares · fee {fee_percent:g}% · share factor ×{share_factor}.
       Coinbase / operator: <span class="mono">{addr}</span></p>
  </div>
  <div id="flash"></div>

  <div class="kpi" id="kpi">
    <div class="stat k-hash"><div class="label">Pool hashrate</div><div class="val" id="kHash">—</div><div class="hint">sum of workers (10m)</div></div>
    <div class="stat k-miners"><div class="label">Workers online</div><div class="val" id="kWorkers">—</div><div class="hint" id="kMinersHint">miners total —</div></div>
    <div class="stat k-shares"><div class="label">Shares (1h)</div><div class="val" id="kShares">—</div><div class="hint" id="kRoundShares">this round —</div></div>
    <div class="stat k-round"><div class="label">Current round</div><div class="val" id="kRound">—</div><div class="hint">resets on pool block</div></div>
    <div class="stat k-fee"><div class="label">Pool fee</div><div class="val">{fee_percent:g}%</div><div class="hint">PROP after fee</div></div>
    <div class="stat k-blocks"><div class="label">Blocks found</div><div class="val" id="kBlocks">—</div><div class="hint">by this pool</div></div>
  </div>

  <section class="panel" id="connect">
    <h3>How to connect</h3>
    <p class="muted">Terminal miner only (Desktop solo is separate). Same machine can solo OR pool — not both on one datadir.</p>
    <div class="connect-grid" style="margin-top:.85rem">
      <div class="connect-card">
        <div class="t">JSON (recommended)</div>
        <div class="mono">HOST:{json_port}</div>
        <div class="copy-row">
          <code class="cmd" id="cmdJson">mhcoin mining pool-start --url 192.168.0.221:{json_port} --address mhc1YOUR_PAYOUT --worker rig1</code>
          <button type="button" data-copy="cmdJson">Copy</button>
        </div>
      </div>
      <div class="connect-card">
        <div class="t">Stratum</div>
        <div class="mono">stratum+tcp://HOST:{stratum_port}</div>
        <p class="muted" style="margin:.45rem 0 0">Bitcoin-like subset. Prefer JSON client for MHCOIN.</p>
      </div>
      <div class="connect-card">
        <div class="t">Your stats</div>
        <p class="muted" style="margin:0 0 .45rem">Paste the payout address you mine with:</p>
        <div class="lookup">
          <input id="minerQ" placeholder="mhc1…" autocomplete="off" spellcheck="false"/>
          <button type="button" id="minerGo">Lookup</button>
        </div>
      </div>
    </div>
  </section>

  <section class="panel hidden" id="minerPanel">
    <h3>Miner <span class="mono" id="minerAddr"></span></h3>
    <div class="kpi" style="margin:0 0 .5rem">
      <div class="stat"><div class="label">Matured</div><div class="val" id="mMature">—</div></div>
      <div class="stat"><div class="label">Immature</div><div class="val" id="mImmature">—</div></div>
      <div class="stat"><div class="label">Paid</div><div class="val" id="mPaid">—</div></div>
      <div class="stat"><div class="label">Shares</div><div class="val" id="mShares">—</div></div>
    </div>
    <table>
      <thead><tr><th>Worker</th><th>Hashrate</th><th>Last seen</th></tr></thead>
      <tbody id="minerWorkers"></tbody>
    </table>
  </section>

  <section class="panel" id="miners">
    <h3>Live workers</h3>
    <p class="muted">Active in the last 10 minutes · auto-refresh 8s</p>
    <table>
      <thead><tr><th>Address</th><th>Worker</th><th>Hashrate</th><th>Status</th></tr></thead>
      <tbody id="workersBody"><tr><td colspan="4" class="muted">Loading…</td></tr></tbody>
    </table>
  </section>

  <section class="panel" id="blocks">
    <h3>Pool blocks</h3>
    <table>
      <thead><tr><th>Height</th><th>Hash</th><th>Reward</th><th>Status</th></tr></thead>
      <tbody id="blocksBody"><tr><td colspan="4" class="muted">No pool blocks yet</td></tr></tbody>
    </table>
  </section>
</main>

<footer>
  MHCOIN native pool · not compatible with Ethereum / ProgPoW pools ·
  {exp_link} · stats JSON <span class="mono">/api/stats</span>
</footer>

<script>
(function(){{
  function $(id){{ return document.getElementById(id); }}
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
  function fmtMhc(sats){{
    var n = Number(sats)||0;
    return (n/1e8).toFixed(8)+" MHC";
  }}
  function shortAddr(a){{
    a = String(a||"");
    if (a.length < 18) return a;
    return a.slice(0,10)+"…"+a.slice(-6);
  }}
  function age(ts){{
    var s = Math.max(0, (Date.now()/1000) - Number(ts||0));
    if (s < 60) return Math.floor(s)+"s ago";
    if (s < 3600) return Math.floor(s/60)+"m ago";
    return Math.floor(s/3600)+"h ago";
  }}
  function setTheme(dark){{
    var root = document.documentElement;
    if (dark) root.setAttribute("data-theme","dark");
    else root.removeAttribute("data-theme");
    try {{ localStorage.setItem("mhcoin-theme", dark ? "dark" : "light"); }} catch(e){{}}
    var meta = $("metaThemeColor");
    if (meta) meta.setAttribute("content", dark ? "#0b0d10" : "#ffffff");
    $("themeBtn").textContent = dark ? "Light" : "Dark";
  }}
  $("themeBtn").onclick = function(){{
    setTheme(document.documentElement.getAttribute("data-theme") !== "dark");
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

  function renderStats(ov){{
    $("kHash").textContent = fmtHps(ov.pool_hashrate);
    $("kWorkers").textContent = String(ov.workers_active||0);
    $("kMinersHint").textContent = "miners total "+(ov.miners_total||0);
    $("kShares").textContent = String(ov.shares_1h||0);
    $("kRoundShares").textContent = "this round "+(ov.shares_round||0);
    $("kRound").textContent = "#"+ (ov.current_round||"—");
    var blocks = ov.blocks || [];
    $("kBlocks").textContent = String(blocks.length);
    var wb = $("workersBody");
    var workers = ov.workers || [];
    if (!workers.length) {{
      wb.innerHTML = '<tr><td colspan="4" class="muted">No active workers — connect with pool-start</td></tr>';
    }} else {{
      wb.innerHTML = workers.map(function(w){{
        var online = (Date.now()/1000 - Number(w.last_seen||0)) < 600;
        return '<tr>'+
          '<td class="mono"><a href="#miner" data-addr="'+w.address+'">'+shortAddr(w.address)+'</a></td>'+
          '<td class="mono">'+(w.worker||"—")+'</td>'+
          '<td class="mono">'+fmtHps(w.hashrate)+'</td>'+
          '<td><span class="pill '+(online?'':'off')+'">'+(online?'online':'idle')+'</span> '+age(w.last_seen)+'</td>'+
          '</tr>';
      }}).join("");
      wb.querySelectorAll("a[data-addr]").forEach(function(a){{
        a.onclick = function(e){{ e.preventDefault(); showMiner(a.getAttribute("data-addr")); }};
      }});
    }}
    var bb = $("blocksBody");
    if (!blocks.length) {{
      bb.innerHTML = '<tr><td colspan="4" class="muted">No pool blocks yet</td></tr>';
    }} else {{
      var expBase = {json.dumps(explorer_url.rstrip("/") if explorer_url else "")};
      bb.innerHTML = blocks.map(function(b){{
        var h = (b.block_hash||"");
        var link;
        if (expBase) {{
          link = '<a href="'+expBase+'/block/'+encodeURIComponent(h)+'" class="mono" rel="noopener">'+h.slice(0,18)+'…</a>';
        }} else {{
          link = '<span class="mono">'+h.slice(0,18)+'…</span>';
        }}
        return '<tr><td class="mono">#'+(b.height||"—")+'</td><td>'+link+'</td>'+
          '<td class="mono">'+fmtMhc(b.reward_sats)+'</td><td>'+(b.status||"—")+'</td></tr>';
      }}).join("");
    }}
  }}

  function showMiner(addr){{
    addr = (addr||"").trim();
    if (!addr) return;
    $("minerQ").value = addr;
    fetch("/api/miner/"+encodeURIComponent(addr)).then(function(r){{ return r.json(); }}).then(function(st){{
      $("minerPanel").classList.remove("hidden");
      $("minerAddr").textContent = st.address || addr;
      $("mMature").textContent = fmtMhc(st.matured_sats);
      $("mImmature").textContent = fmtMhc(st.immature_sats);
      $("mPaid").textContent = fmtMhc(st.paid_sats);
      $("mShares").textContent = String(st.shares||0);
      var rows = (st.workers||[]);
      $("minerWorkers").innerHTML = rows.length ? rows.map(function(w){{
        return '<tr><td class="mono">'+(w.worker||"—")+'</td><td class="mono">'+fmtHps(w.hashrate)+'</td><td>'+age(w.last_seen)+'</td></tr>';
      }}).join("") : '<tr><td colspan="3" class="muted">No workers recorded</td></tr>';
      $("minerPanel").scrollIntoView({{behavior:"smooth", block:"start"}});
    }}).catch(function(e){{ flash("Lookup failed"); }});
  }}
  $("minerGo").onclick = function(){{ showMiner($("minerQ").value); }};
  $("minerQ").addEventListener("keydown", function(e){{ if (e.key==="Enter") showMiner($("minerQ").value); }});

  function refresh(){{
    fetch("/api/stats").then(function(r){{ return r.json(); }}).then(renderStats).catch(function(){{}});
  }}
  refresh();
  setInterval(refresh, 8000);
}})();
</script>
</body>
</html>"""
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
    explorer_url: str = "http://192.168.0.221:8766",
) -> ThreadingHTTPServer:
    home_html = _page_shell(
        pool_address=pool_address,
        json_port=json_port,
        stratum_port=stratum_port,
        fee_percent=fee_percent,
        share_factor=share_factor,
        explorer_url=explorer_url,
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
            if path == "/api/stats":
                self._json(db.stats_overview())
                return
            if path.startswith("/api/miner/"):
                addr = unquote(path.split("/api/miner/", 1)[1].strip("/"))
                self._json(db.miner_stats(addr))
                return
            if path.startswith("/miner/"):
                # SPA handles miner via hash/query; keep redirect for old links.
                addr = unquote(path.split("/miner/", 1)[1].strip("/"))
                self.send_response(302)
                self.send_header("Location", "/?miner=" + addr)
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            # Always serve SPA home (miner lookup is client-side).
            _ = qs
            self._html(home_html)

        def _json(self, obj: dict) -> None:
            # Enrich overview with display helpers for external clients.
            if "pool_hashrate" in obj and "pool_hashrate_display" not in obj:
                obj = dict(obj)
                obj["pool_hashrate_display"] = _fmt_hps(float(obj.get("pool_hashrate") or 0))
                if obj.get("pool_address") is None and pool_address:
                    obj["pool_address"] = pool_address
                obj["fee_percent"] = fee_percent
                obj["share_factor"] = share_factor
                obj["ports"] = {"json": json_port, "stratum": stratum_port, "web": port}
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
