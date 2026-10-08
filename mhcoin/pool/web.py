"""Minimal pool stats HTTP UI."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from mhcoin.pool.db import PoolDB
from mhcoin.wallet.send import format_mhc

logger = logging.getLogger("mhcoin.pool.web")


def _html_page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
body{{font-family:ui-sans-serif,system-ui,sans-serif;margin:0;background:#0b1220;color:#e7eefc}}
a{{color:#7dd3fc}}
.wrap{{max-width:960px;margin:0 auto;padding:24px}}
h1{{font-size:1.4rem;margin:0 0 8px}}
.card{{background:#121a2b;border:1px solid #243049;border-radius:12px;padding:16px;margin:12px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.kpi{{font-size:1.3rem;font-weight:700}}
.muted{{color:#93a4c3;font-size:.9rem}}
table{{width:100%;border-collapse:collapse}}
td,th{{padding:8px;border-bottom:1px solid #243049;text-align:left;font-size:.92rem}}
code{{background:#0b1220;padding:2px 6px;border-radius:6px}}
input{{width:100%;padding:10px;border-radius:8px;border:1px solid #243049;background:#0b1220;color:#e7eefc}}
</style></head><body><div class="wrap">{body}</div></body></html>"""
    return doc.encode("utf-8")


def start_web(db: PoolDB, host: str, port: int, *, pool_address: str = "") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            logger.debug("%s - " + fmt, self.address_string(), *args)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path == "/api/stats":
                self._json(db.stats_overview())
                return
            if path.startswith("/api/miner/"):
                addr = path.split("/api/miner/", 1)[1].strip("/")
                self._json(db.miner_stats(addr))
                return
            if path.startswith("/miner/"):
                addr = path.split("/miner/", 1)[1].strip("/")
                st = db.miner_stats(addr)
                body = f"""
                <h1>Miner</h1>
                <p class="muted"><code>{addr}</code></p>
                <div class="grid">
                  <div class="card"><div class="muted">Matured</div><div class="kpi">{format_mhc(st['matured_sats'])} MHC</div></div>
                  <div class="card"><div class="muted">Immature</div><div class="kpi">{format_mhc(st['immature_sats'])} MHC</div></div>
                  <div class="card"><div class="muted">Paid</div><div class="kpi">{format_mhc(st['paid_sats'])} MHC</div></div>
                  <div class="card"><div class="muted">Shares</div><div class="kpi">{st['shares']}</div></div>
                </div>
                <p><a href="/">← Pool home</a></p>
                """
                self._html("MHCOIN Pool Miner", body)
                return
            ov = db.stats_overview()
            rows = ""
            for b in ov.get("blocks") or []:
                rows += (
                    f"<tr><td>{b.get('height')}</td><td><code>{(b.get('block_hash') or '')[:18]}…</code></td>"
                    f"<td>{format_mhc(int(b.get('reward_sats') or 0))} MHC</td><td>{b.get('status')}</td></tr>"
                )
            body = f"""
            <h1>MHCOIN Pool</h1>
            <p class="muted">HASH256 · PROP · payout address <code>{pool_address or '—'}</code></p>
            <div class="grid">
              <div class="card"><div class="muted">Pool hashrate</div><div class="kpi">{ov['pool_hashrate']/1000:.1f} kH/s</div></div>
              <div class="card"><div class="muted">Workers (10m)</div><div class="kpi">{ov['workers_active']}</div></div>
              <div class="card"><div class="muted">Shares (1h)</div><div class="kpi">{ov['shares_1h']}</div></div>
              <div class="card"><div class="muted">Round</div><div class="kpi">#{ov['current_round']}</div></div>
            </div>
            <div class="card">
              <h2 style="margin-top:0;font-size:1.05rem">Lookup miner</h2>
              <form method="get" action="/" onsubmit="location.href='/miner/'+encodeURIComponent(this.q.value);return false;">
                <input name="q" placeholder="mhc1…" autocomplete="off"/>
              </form>
              <p class="muted">CLI: <code>mhcoin mining pool-start --url HOST:3333 --address mhc1…</code></p>
            </div>
            <div class="card">
              <h2 style="margin-top:0;font-size:1.05rem">Recent blocks</h2>
              <table><thead><tr><th>Height</th><th>Hash</th><th>Reward</th><th>Status</th></tr></thead>
              <tbody>{rows or '<tr><td colspan="4" class="muted">No pool blocks yet</td></tr>'}</tbody></table>
            </div>
            """
            self._html("MHCOIN Pool", body)

        def _json(self, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _html(self, title: str, body: str) -> None:
            data = _html_page(title, body)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer((host, port), Handler)
    t = threading.Thread(target=httpd.serve_forever, name="mhcoin-pool-web", daemon=True)
    t.start()
    logger.info("Pool stats http://%s:%s", host, port)
    return httpd
