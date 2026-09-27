"""Read-only dashboard (sections 48-51) on 127.0.0.1 only.

Reach it from the Mac with an SSH tunnel:
    ssh -N -L 8787:127.0.0.1:8787 droplet-eddie   then open http://localhost:8787

It has no controls and never displays credentials - only whether a
production key is CONFIGURED. PAPER, SHADOW and LIVE P&L are shown in
separate panels and never summed.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lvt.db import Database
from lvt.reports import full_report

log = logging.getLogger("lvt.dashboard")


def _json(v):
    return json.loads(v) if isinstance(v, str) and v[:1] in "{[" else v


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = str(cfg.path(cfg.app.state_db))
        self._local = threading.local()

    @property
    def db(self) -> Database:
        d = getattr(self._local, "db", None)
        if d is None:
            d = self._local.db = Database(self.path, readonly=True)
        return d

    def status(self):
        rows = {r["k"]: {"ts": r["ts"], **(_json(r["v"]) or {})} for r in self.db.query("SELECT * FROM status")}
        return {"engine": rows.get("engine", {}), "reconcile": rows.get("reconcile", {}), "now": time.time()}

    def scanner(self):
        rows = [_json(r["v"]) for r in self.db.query("SELECT v FROM scanner_state")]
        return sorted(rows, key=lambda r: (-(r.get("movement_score") or 0), -(r.get("volume") or 0)))

    def positions(self):
        return self.db.query("SELECT id, env, ticker, side, qty, avg_entry, last_bid, unrealized_pnl_usd, target, stop, "
                             "trail_stop, opened_ts, strategy, strategy_version, signal_score, status, sport "
                             "FROM positions WHERE status IN ('PENDING','OPEN','CLOSING') ORDER BY opened_ts DESC")

    def trades(self):
        return self.db.query("SELECT env, ticker, side, strategy, entry_qty, avg_entry, avg_exit, realized_pnl_usd, "
                             "entry_fees_usd + exit_fees_usd AS fees, slippage_cost_usd, exit_reason, opened_ts, closed_ts "
                             "FROM positions WHERE status='CLOSED' ORDER BY closed_ts DESC LIMIT 50")

    def performance(self):
        eq = self.cfg.risk.paper_starting_equity
        return {env: full_report(self.db, env, eq) for env in ("PAPER", "SHADOW", "LIVE")}

    def signals(self):
        return self.db.query("SELECT ts, ticker, strategy, side, action, decision, reject_reason, score, ask, target, "
                             "stop, expected_net_cents, state FROM signals ORDER BY ts DESC LIMIT 60")

    def alerts(self):
        return self.db.query("SELECT ts, level, code, message FROM alerts ORDER BY ts DESC LIMIT 40")

    def health(self):
        return self.db.query("SELECT * FROM health ORDER BY ts DESC LIMIT 1")

    def readiness(self):
        from lvt.live_gate import readiness
        return [i.__dict__ for i in readiness(self.cfg, self.db)]


def make_handler(api: Api):
    routes = {"/api/status": api.status, "/api/scanner": api.scanner, "/api/positions": api.positions,
              "/api/trades": api.trades, "/api/performance": api.performance, "/api/signals": api.signals,
              "/api/alerts": api.alerts, "/api/health": api.health, "/api/readiness": api.readiness}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/index.html"):
                    body, ctype = PAGE.encode(), "text/html; charset=utf-8"
                elif path in routes:
                    body, ctype = json.dumps(routes[path](), default=str).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
            except Exception as exc:                          # noqa: BLE001
                body, ctype = json.dumps({"error": str(exc)}).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    return H


def serve(cfg, stop: threading.Event) -> None:
    host, port = cfg.dashboard.host, int(cfg.dashboard.port)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.error("dashboard host %s refused: it only binds to localhost", host)
        return
    try:
        srv = ThreadingHTTPServer((host, port), make_handler(Api(cfg)))
    except OSError as exc:
        log.error("dashboard could not bind %s:%s: %s", host, port, exc)
        return
    srv.timeout = 1.0
    log.info("dashboard on http://%s:%d", host, port)
    while not stop.is_set():
        srv.handle_request()
    srv.server_close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Volatility Trader</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--mut:#6b7385;--line:#e3e6ec;--good:#0a7d4f;--bad:#c0392b;--warn:#b7791f;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--card:#1a1f28;--ink:#e6e9ef;--mut:#8e97a8;--line:#2a313d;--good:#3ecf8e;--bad:#ff6b6b;--warn:#f2b84b;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 -apple-system,system-ui,Segoe UI,sans-serif}
header{padding:14px 16px;border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
h1{font-size:16px;margin:0}h2{font-size:13px;margin:0 0 8px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
main{padding:12px 16px;display:grid;gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;overflow-x:auto}
.kv{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.kv div{border:1px solid var(--line);border-radius:6px;padding:6px 8px}.kv b{display:block;font-size:11px;color:var(--mut);font-weight:500}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--mut);font-weight:500;font-size:11px}td:first-child,th:first-child{text-align:left}
.pos{color:var(--good)}.neg{color:var(--bad)}.warn{color:var(--warn)}.pill{padding:1px 6px;border-radius:9px;border:1px solid var(--line);font-size:11px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}.mut{color:var(--mut)}
</style></head><body>
<header><h1>Kalshi Live Volatility Trader</h1><span id="mode" class="pill"></span><span id="upd" class="mut"></span></header>
<main>
<section class="card"><h2>System status</h2><div id="status" class="kv"></div><div id="fc" class="warn" style="margin-top:8px"></div></section>
<section class="card"><h2>Performance (environments are never combined)</h2><div id="perf" class="grid2"></div></section>
<section class="card"><h2>Active positions</h2><div id="positions"></div></section>
<section class="card"><h2>Live market scanner (hot tier)</h2><div id="scanner"></div></section>
<div class="grid2">
<section class="card"><h2>Recent signals</h2><div id="signals"></div></section>
<section class="card"><h2>Recent closed trades</h2><div id="trades"></div></section>
</div>
<div class="grid2">
<section class="card"><h2>Alerts</h2><div id="alerts"></div></section>
<section class="card"><h2>Production readiness</h2><div id="ready"></div></section>
</div>
</main>
<script>
const $=id=>document.getElementById(id);
const f=(v,d=1)=>v==null||v===''?'–':(typeof v==='number'?v.toFixed(d):v);
const money=v=>v==null?'–':`<span class="${v>0?'pos':v<0?'neg':''}">${v<0?'-':''}$${Math.abs(v).toFixed(2)}</span>`;
const ago=t=>t?Math.round(Date.now()/1000-t)+'s':'–';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function table(rows,cols){if(!rows||!rows.length)return '<p class="mut">none</p>';
 return '<table><tr>'+cols.map(c=>`<th>${c[0]}</th>`).join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map(c=>`<td>${c[1](r)}</td>`).join('')+'</tr>').join('')+'</table>'}
async function j(u){const r=await fetch(u);return r.json()}
async function tick(){
 try{
 const st=await j('/api/status'),e=st.engine||{};
 $('mode').textContent=(e.mode||'?')+' MODE';$('upd').textContent='status '+ago(e.ts)+' ago';
 const kv=[['Mode',e.mode],['Market data',e.data],['Execution',e.execution],['Kalshi',e.kalshi],['Production key',e.production_key],
  ['Production orders',e.production_orders],['Equity ('+(e.mode||'')+')',e.equity!=null?'$'+e.equity:'–'],['Daily P&L',e.daily_pnl!=null?'$'+e.daily_pnl:'–'],
  ['Universe / probe / hot',`${e.universe}/${e.probe}/${e.hot}`],['Open positions',e.open_positions],['Working orders',e.working_orders],
  ['Req/s · 429s',`${e.req_per_s} · ${e.http_429}`],['Clock skew',e.clock_skew_s+'s'],['Cycle',e.cycle_ms+'ms'],['Kill switch',e.kill_switch?'ON':'off'],
  ['Reconcile',(st.reconcile||{}).summary||'–']];
 $('status').innerHTML=kv.map(([k,v])=>`<div><b>${k}</b>${esc(v)}</div>`).join('');
 const fc=e.fail_closed||{};$('fc').textContent=Object.keys(fc).length?'FAIL-CLOSED (no new orders): '+Object.entries(fc).map(([k,v])=>k+': '+v).join(' | '):(e.entries_halted||'');
 const pf=await j('/api/performance');
 $('perf').innerHTML=Object.values(pf).map(p=>{const o=p.overall;return `<div><b>${p.label}</b><br>`+(o.trades?`trades ${o.trades} · win ${(o.win_rate*100).toFixed(0)}% · expectancy ${money(o.expectancy_usd)}<br>net ${money(o.net_pnl_usd)} · gross ${money(o.gross_pnl_usd)} · fees $${o.fees_usd} · slippage $${o.slippage_usd} · max DD $${o.max_drawdown_usd}`:'<span class="mut">no closed trades</span>')+`<br><span class="mut">${esc(p.verdict)}</span></div>`}).join('');
 $('positions').innerHTML=table(await j('/api/positions'),[['Market',r=>esc(r.ticker)],['Env',r=>r.env],['Side',r=>r.side.toUpperCase()],['Qty',r=>r.qty],
  ['Entry',r=>f(r.avg_entry)],['Current bid',r=>f(r.last_bid)],['Unrealized',r=>money(r.unrealized_pnl_usd)],['Target',r=>f(r.target)],['Stop',r=>f(r.stop)],
  ['Trail',r=>f(r.trail_stop)],['Hold',r=>ago(r.opened_ts)],['Strategy',r=>r.strategy],['Score',r=>f(r.signal_score,0)],['Status',r=>r.status]]);
 $('scanner').innerHTML=table(await j('/api/scanner'),[['Sport',r=>r.sport],['Event',r=>esc(r.event)],['Market',r=>esc(r.sub_title||r.ticker)],['Bid',r=>f(r.bid)],['Ask',r=>f(r.ask)],
  ['Spread',r=>f(r.spread)],['Vol (15m)',r=>f(r.volume,0)],['Liq $',r=>f(r.liquidity,0)],['Volat.',r=>f(r.volatility,2)],['High',r=>f(r.recent_high)],['Low',r=>f(r.recent_low)],
  ['Veloc.',r=>f(r.velocity,2)],['Accel.',r=>f(r.acceleration,2)],['State',r=>r.state],['Move',r=>f(r.movement_score,0)],['Dip',r=>f(r.dip_score,0)],
  ['Mom.',r=>f(r.momentum_score,0)],['Position',r=>r.position||''],['Action',r=>r.action||(r.eligible?'':'<span class="mut">'+esc((r.elig_reasons||[])[0]||'')+'</span>')]]);
 $('signals').innerHTML=table(await j('/api/signals'),[['Age',r=>ago(r.ts)],['Market',r=>esc(r.ticker)],['Strategy',r=>r.strategy],['Action',r=>r.action],
  ['Decision',r=>r.decision+(r.reject_reason?' <span class="mut">'+esc(r.reject_reason).slice(0,40)+'</span>':'')],['Score',r=>f(r.score,0)],['Ask',r=>f(r.ask)],['Tgt',r=>f(r.target)],['Stop',r=>f(r.stop)],['Net c',r=>f(r.expected_net_cents)]]);
 $('trades').innerHTML=table(await j('/api/trades'),[['Market',r=>esc(r.ticker)],['Env',r=>r.env],['Strat',r=>r.strategy],['Qty',r=>r.entry_qty],['In',r=>f(r.avg_entry)],['Out',r=>f(r.avg_exit)],
  ['Net',r=>money(r.realized_pnl_usd)],['Fees',r=>f(r.fees,2)],['Exit',r=>esc(r.exit_reason)]]);
 $('alerts').innerHTML=table(await j('/api/alerts'),[['Age',r=>ago(r.ts)],['Level',r=>r.level],['Code',r=>r.code],['Message',r=>esc(r.message).slice(0,90)]]);
 $('ready').innerHTML=table(await j('/api/readiness'),[['Item',r=>r.name],['',r=>r.passed?'<span class="pos">PASS</span>':'<span class="neg">FAIL</span>'],['Detail',r=>esc(r.detail)]]);
 }catch(err){$('upd').textContent='dashboard error: '+err}
}
tick();setInterval(tick,3000);
</script></body></html>"""
