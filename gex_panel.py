#!/usr/bin/env python3
"""Panel GEX: call wall, put wall, gamma flip y niveles gamma (0DTE / 7DTE).
Datos públicos: opciones de CBOE (retraso ~15 min) y velas de Yahoo Finance.
Uso:  python gex_panel.py   ->   abre http://localhost:8765
Solo usa la librería estándar de Python 3.9+."""
import json, math, os, re, time, urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def ny_now():
    """Hora de Nueva York sin depender de la base de zonas horarias de Windows."""
    u = datetime.now(timezone.utc); y = u.year
    d = datetime(y, 3, 8); d += timedelta(days=(6 - d.weekday()) % 7)
    e = datetime(y, 11, 1); e += timedelta(days=(6 - e.weekday()) % 7)
    n = u.replace(tzinfo=None)
    off = -4 if d.replace(hour=7) <= n < e.replace(hour=6) else -5
    return u.astimezone(timezone(timedelta(hours=off)))
SRC = {"SPX": ("_SPX", "%5ESPX"), "SPY": ("SPY", "SPY")}
OCC = re.compile(r"^[A-Z]+(\d{6})([CP])(\d{8})$")
_cache = {}
TF = {"1m": ("1m", "5d"), "5m": ("5m", "5d"), "15m": ("15m", "1mo"), "30m": ("30m", "1mo"), "1h": ("60m", "3mo"), "1D": ("1d", "1y")}

def get(url, ttl):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=15))
    _cache[url] = (time.time(), data)
    return data

def ncdf_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)

def bs_gamma(S, K, T, iv):
    if iv <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return ncdf_pdf(d1) / (S * iv * math.sqrt(T))

def parse(raw, mode):
    """Devuelve (spot, lista de opciones filtradas por modo)."""
    d = raw["data"]
    spot = float(d["current_price"])
    now = ny_now()
    today = now.date()
    last = today + timedelta(days=0 if mode == "0dte" else 7)
    opts = []
    for o in d["options"]:
        m = OCC.match(o["option"])
        if not m:
            continue
        exp = datetime.strptime(m.group(1), "%y%m%d").date()
        if exp < today or exp > last or (mode == "0dte" and exp != today):
            continue
        close = datetime(exp.year, exp.month, exp.day, 16, tzinfo=now.tzinfo)
        T = max((close - now).total_seconds(), 300) / (365 * 24 * 3600)
        opts.append(dict(K=int(m.group(3)) / 1000, call=m.group(2) == "C",
                         oi=float(o.get("open_interest") or 0), iv=float(o.get("iv") or 0),
                         gamma=float(o.get("gamma") or 0), T=T))
    return spot, opts

def compute(spot, opts):
    sign = lambda o: 1 if o["call"] else -1
    by = {}
    for o in opts:
        g = o["gamma"] * o["oi"] * 100 * spot * spot * 0.01 * sign(o)
        k = by.setdefault(o["K"], [0.0, 0.0])
        k[0 if o["call"] else 1] += g
    strikes = sorted(by)
    calls_above = [(k, by[k][0]) for k in strikes if k >= spot and by[k][0] > 0]
    puts_below = [(k, by[k][1]) for k in strikes if k <= spot and by[k][1] < 0]
    call_wall = max(calls_above, key=lambda x: x[1])[0] if calls_above else None
    put_wall = min(puts_below, key=lambda x: x[1])[0] if puts_below else None
    # Gamma flip: GEX neto recalculado con Black-Scholes en una malla de precios
    def net(S):
        return sum(bs_gamma(S, o["K"], o["T"], o["iv"]) * o["oi"] * 100 * S * S * 0.01 * sign(o)
                   for o in opts if o["iv"] > 0)
    grid = [spot * (1 + i / 400) for i in range(-30, 31)]
    vals = [net(s) for s in grid]
    flip = None
    for i in range(1, len(grid)):
        if vals[i - 1] * vals[i] < 0:
            a, b = vals[i - 1], vals[i]
            flip = grid[i - 1] + (grid[i] - grid[i - 1]) * (-a / (b - a))
            if grid[i] >= spot:
                break
    total = net(spot)
    oi = {}
    for o in opts:
        if abs(o["K"] - spot) <= spot * 0.05:
            oi.setdefault(o["K"], [0.0, 0.0])[0 if o["call"] else 1] += o["oi"]
    top = lambda i: [dict(strike=k, oi=v[i]) for k, v in sorted(oi.items(), key=lambda kv: -kv[1][i])[:3] if v[i] > 0]
    near = [k for k in strikes if abs(k - spot) <= spot * 0.03]
    net_k = sorted(((k, by[k][0] + by[k][1]) for k in near), key=lambda x: -abs(x[1]))[:6]
    return dict(oi_calls=top(0), oi_puts=top(1), spot=spot, call_wall=call_wall, put_wall=put_wall, flip=flip,
                total_gex=total, regime="positive" if (flip is None and total > 0) or (flip and spot > flip) else "negative",
                levels=[dict(strike=k, gex=g) for k, g in net_k],
                bars=[dict(strike=k, call=by[k][0], put=by[k][1]) for k in near])

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        sym = q.get("symbol", ["SPX"])[0].upper(); sym = sym if sym in SRC else "SPX"
        mode = "7d" if q.get("mode", ["0dte"])[0] == "7d" else "0dte"
        try:
            if u.path == "/":
                return self.send(PAGE, "text/html")
            if u.path == "/manifest.json":
                return self.send(json.dumps(MANIFEST), "application/manifest+json")
            if u.path == "/sw.js":
                return self.send("self.addEventListener('fetch',e=>e.respondWith(fetch(e.request)));", "application/javascript")
            if u.path == "/icon.svg":
                return self.send(ICON, "image/svg+xml")
            if u.path == "/api/levels":
                raw = get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{SRC[sym][0]}.json", 10)
                spot, opts = parse(raw, mode)
                out = compute(spot, opts); out["asof"] = raw.get("timestamp"); out["n"] = len(opts)
                return self.send(json.dumps(out))
            if u.path == "/api/candles":
                tf = q.get("tf", ["1m"])[0]; tf = tf if tf in TF else "1m"
                iv, rg = TF[tf]
                r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{SRC[sym][1]}?interval={iv}&range={rg}", 10)["chart"]["result"][0]
                off = r["meta"].get("gmtoffset", 0); qd = r["indicators"]["quote"][0]; seen = {}
                for i, t in enumerate(r["timestamp"]):
                    if qd["close"][i] is None: continue
                    tt = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d") if iv == "1d" else t + off
                    seen[tt] = dict(time=tt, open=qd["open"][i], high=qd["high"][i], low=qd["low"][i], close=qd["close"][i])
                return self.send(json.dumps(list(seen.values())))
            self.send("{}", code=404)
        except Exception as e:
            self.send(json.dumps(dict(error=str(e))), code=502)

MANIFEST = {"name": "Panel GEX", "short_name": "GEX", "start_url": "/", "display": "standalone",
            "background_color": "#10141a", "theme_color": "#10141a",
            "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}]}
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#10141a"/>'
        '<path d="M96 352h320" stroke="#e8b64a" stroke-width="16" stroke-dasharray="40 24"/>'
        '<path d="M120 300l70-90 60 50 80-120 62 70" fill="none" stroke="#2fbf8a" stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M96 160h320" stroke="#e5575f" stroke-width="12"/></svg>')

PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Panel GEX</title><link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg"><meta name="theme-color" content="#10141a">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#10141a;--pn:#171d26;--ln:#263040;--tx:#d5dbe5;--mu:#7d8798;--up:#2fbf8a;--dn:#e5575f;--fl:#e8b64a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,sans-serif;display:grid;grid-template-columns:1fr 300px;height:100vh}
#main{display:flex;flex-direction:column;min-width:0}#bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--ln)}
#bar b{margin-right:auto;font-size:16px}button{background:var(--pn);color:var(--tx);border:1px solid var(--ln);padding:5px 12px;border-radius:5px;cursor:pointer}
button.on{border-color:var(--fl);color:var(--fl)}#chart{flex:1;min-height:0}
aside{background:var(--pn);border-left:1px solid var(--ln);padding:14px;overflow:auto}
#reg{font-size:22px;font-weight:700;margin:0 0 2px}.mu{color:var(--mu);font-size:12px}
table{width:100%;border-collapse:collapse;margin:10px 0}td{padding:5px 0;border-bottom:1px solid var(--ln)}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}.fl{color:var(--fl)}#bars div{display:flex;height:9px;margin:3px 0;gap:1px;align-items:center}
@media(max-width:800px){body{grid-template-columns:1fr;grid-template-rows:60vh auto;height:auto}}
</style></head><body>
<div id="main"><div id="bar"><b id="sym">SPX</b>
<button data-s="SPX" class="on">SPX</button><button data-s="SPY">SPY</button>
<span class="mu">Temporalidad</span><button data-t="1m" class="on">1m</button><button data-t="5m">5m</button><button data-t="15m">15m</button><button data-t="30m">30m</button><button data-t="1h">1h</button><button data-t="1D">D</button><span class="mu" style="margin-left:10px">Vencimiento</span><button data-m="0dte" class="on">0DTE</button><button data-m="7d">7D</button></div><div id="chart"></div></div>
<aside><p id="reg">…</p><div class="mu" id="sub">Cargando datos</div>
<table id="tb"></table><div class="mu">GEX neto por strike (cerca del precio)</div><div id="bars"></div>
<p class="mu" id="foot"></p><p class="mu">Solo informativo. Estimación basada en datos públicos con retraso; no es consejo financiero.</p></aside>
<script>
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');
let S="SPX",M="0dte",T="1m",lines=[];
const ch=LightweightCharts.createChart(document.getElementById('chart'),{layout:{background:{color:'#10141a'},textColor:'#7d8798'},
grid:{vertLines:{color:'#1b222d'},horzLines:{color:'#1b222d'}},timeScale:{timeVisible:true,secondsVisible:false},rightPriceScale:{borderColor:'#263040'}});
const cs=ch.addCandlestickSeries({upColor:'#2fbf8a',downColor:'#e5575f',borderVisible:false,wickUpColor:'#2fbf8a',wickDownColor:'#e5575f'});
new ResizeObserver(()=>ch.applyOptions({autoSize:true})).observe(document.getElementById('chart'));
const f=(n,d=0)=>n==null?'—':n.toLocaleString('en-US',{maximumFractionDigits:d});
const fm=g=>(g/1e9>=0?'+':'')+(g/1e9).toFixed(2)+'B';
function setLines(l){lines.forEach(x=>cs.removePriceLine(x));lines=[];
 const add=(p,c,t,w=2,s=0)=>p&&lines.push(cs.createPriceLine({price:p,color:c,lineWidth:w,lineStyle:s,title:t}));
 l.oi_calls.forEach((v,i)=>add(v.strike,'#5aa9ff','OI call '+(i+1)+' · '+f(v.oi),i?1:2,3));
 l.oi_puts.forEach((v,i)=>add(v.strike,'#c18cff','OI put '+(i+1)+' · '+f(v.oi),i?1:2,3));
 add(l.call_wall,'#2fbf8a','Call wall');add(l.put_wall,'#e5575f','Put wall');add(l.flip,'#e8b64a','Gamma flip',2,1);}
async function loadCandles(fit){try{const c=await (await fetch(`/api/candles?symbol=${S}&tf=${T}`)).json();if(!c.error){ch.applyOptions({timeScale:{timeVisible:T!=='1D'}});cs.setData(c);if(fit)ch.timeScale().fitContent()}}catch(e){}}
async function loadLevels(){
 const r=await fetch(`/api/levels?symbol=${S}&mode=${M}`);const l=await r.json();
 if(l.error){document.getElementById('sub').textContent='Error: '+l.error;return}
 const pos=l.regime==='positive';const reg=document.getElementById('reg');
 reg.textContent=pos?'Gamma positiva':'Gamma negativa';reg.className=pos?'up':'dn';
 document.getElementById('sub').textContent=pos?'Movimientos amortiguados, tendencia a revertir':'Movimientos se amplifican, mayor volatilidad';
 document.getElementById('tb').innerHTML=`<tr><td>Precio</td><td>${f(l.spot,2)}</td></tr>
 <tr><td class="up">Call wall</td><td>${f(l.call_wall)}</td></tr><tr><td class="dn">Put wall</td><td>${f(l.put_wall)}</td></tr>
 <tr><td class="fl">Gamma flip</td><td>${f(l.flip)}</td></tr><tr><td>GEX total</td><td>${fm(l.total_gex)}</td></tr>
 <tr><td colspan=2 class="mu" style="padding-top:10px">Mayor open interest</td></tr>`+
 l.oi_calls.map((v,i)=>`<tr><td style="color:#5aa9ff">Call ${v.strike}</td><td>${f(v.oi)}</td></tr>`).join('')+
 l.oi_puts.map((v,i)=>`<tr><td style="color:#c18cff">Put ${v.strike}</td><td>${f(v.oi)}</td></tr>`).join('');
 const mx=Math.max(...l.bars.map(b=>Math.max(b.call,-b.put)),1);
 document.getElementById('bars').innerHTML=l.bars.filter((_,i)=>i%2==0).map(b=>`<div title="${b.strike}"><span class="mu" style="width:44px;font-size:11px">${b.strike}</span>
 <i style="width:${b.put/-mx*45}%;background:#e5575f;height:7px"></i><i style="width:${b.call/mx*45}%;background:#2fbf8a;height:7px"></i></div>`).join('');
 document.getElementById('foot').textContent=`${l.n} contratos · datos CBOE con ~15 min de retraso · ${l.asof||''}`;setLines(l)}
document.querySelectorAll('[data-s]').forEach(b=>b.onclick=()=>{S=b.dataset.s;document.querySelectorAll('[data-s]').forEach(x=>x.classList.toggle('on',x==b));document.getElementById('sym').textContent=S;loadCandles(true);loadLevels()});
document.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>{T=b.dataset.t;document.querySelectorAll('[data-t]').forEach(x=>x.classList.toggle('on',x==b));loadCandles(true)});
document.querySelectorAll('[data-m]').forEach(b=>b.onclick=()=>{M=b.dataset.m;document.querySelectorAll('[data-m]').forEach(x=>x.classList.toggle('on',x==b));loadLevels()});
loadCandles(true);loadLevels();setInterval(loadCandles,15000);setInterval(loadLevels,30000);
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"   # en la nube escucha a todos
    print(f"Panel GEX en http://localhost:{port}  (Ctrl+C para salir)")
    ThreadingHTTPServer((host, port), H).serve_forever()
