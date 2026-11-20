#!/usr/bin/env python3
"""Panel GEX: call wall, put wall, gamma flip y niveles gamma (0DTE / 7DTE).
Datos públicos: opciones de CBOE (retraso ~15 min) y velas de Yahoo Finance.
Uso:  python gex_panel.py   ->   abre http://localhost:8765
Solo usa la librería estándar de Python 3.9+."""
import json, math, os, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
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

def get(url, ttl, timeout=15):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
         "Accept": "application/json,text/plain,*/*", "Accept-Language": "en-US,en;q=0.9"}
    if "cboe.com" in url:
        h.update({"Referer": "https://www.cboe.com/", "Origin": "https://www.cboe.com"})
    req = urllib.request.Request(url, headers=h)
    data = json.load(urllib.request.urlopen(req, timeout=timeout))
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


# ---- Mercado: índices + mapa de calor (tamaños = capitalización aproximada en miles de millones de USD) ----
IDX = [("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ"), ("^DJI", "DOW"), ("IWM", "RUSSELL 2000 (ETF)")]
SECT = {
 "TECNOLOGÍA": [("NVDA",4500),("AAPL",4000),("MSFT",3800),("AVGO",1500),("ORCL",700),("PLTR",400),("AMD",350),("CSCO",280),("IBM",260),("CRM",250),("NOW",200),("INTU",190),("QCOM",170),("TXN",170),("ADBE",150),("AMAT",150),("MU",140),("PANW",130),("LRCX",130),("INTC",110)],
 "COMUNICACIÓN": [("GOOGL",3500),("META",1800),("NFLX",500),("TMUS",260),("DIS",200),("VZ",180),("T",170),("CMCSA",130)],
 "CONSUMO CÍCLICO": [("AMZN",2400),("TSLA",1200),("HD",380),("MCD",210),("BKNG",170),("LOW",150),("TJX",150),("SBUX",110),("NKE",100)],
 "CONSUMO DEFENSIVO": [("WMT",800),("COST",420),("PG",370),("KO",300),("PEP",200),("PM",250),("MO",100),("MDLZ",90),("CL",75)],
 "SALUD": [("LLY",900),("JNJ",450),("ABBV",380),("UNH",300),("MRK",250),("ABT",230),("TMO",200),("ISRG",190),("AMGN",160),("PFE",150),("DHR",150),("GILD",140)],
 "FINANCIERO": [("BRK-B",1000),("JPM",800),("V",650),("MA",520),("BAC",350),("WFC",270),("AXP",230),("GS",220),("MS",220),("C",190),("SCHW",170),("BLK",160),("SPGI",150)],
 "INDUSTRIAL": [("GE",300),("CAT",280),("RTX",240),("BA",170),("UNP",150),("HON",150),("DE",130),("LMT",110),("UPS",90)],
 "ENERGÍA": [("XOM",480),("CVX",280),("COP",120),("EOG",65),("SLB",60)],
 "SERVICIOS PÚBLICOS": [("NEE",150),("SO",100),("DUK",95)],
 "INMOBILIARIO": [("PLD",110),("AMT",100),("EQIX",80)],
 "MATERIALES": [("LIN",210),("SHW",90),("FCX",65)],
}

def chart(sym, interval, rng):
    from urllib.parse import quote
    r = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym)}?interval={interval}&range={rng}", 25, 8)["chart"]["result"][0]
    m = r["meta"]; price = m.get("regularMarketPrice"); prev = m.get("chartPreviousClose") or m.get("previousClose")
    return r, price, prev

def one_stock(item):
    sec, sym, cap = item
    try:
        _, price, prev = chart(sym, "1d", "1d")
        return dict(t=sym, s=sec, cap=cap, price=price, pct=(price - prev) / prev * 100)
    except Exception:
        return None

def one_index(item):
    sym, name = item
    try:
        r, price, prev = chart(sym, "15m", "1d")
        off = r["meta"].get("gmtoffset", 0); qd = r["indicators"]["quote"][0]
        c = [dict(time=t + off, open=qd["open"][i], high=qd["high"][i], low=qd["low"][i], close=qd["close"][i])
             for i, t in enumerate(r["timestamp"]) if qd["close"][i] is not None]
        return dict(sym=sym, name=name, price=price, prev=prev, chg=price - prev, pct=(price - prev) / prev * 100, candles=c)
    except Exception:
        return None

def market():
    items = [(sec, t, cap) for sec, l in SECT.items() for t, cap in l]
    with ThreadPoolExecutor(12) as ex:
        idx = list(ex.map(one_index, IDX)); stk = list(ex.map(one_stock, items))
    stk = [x for x in stk if x]
    return dict(idx=[x for x in idx if x], stk=stk, n=len(items), ok=len(stk), asof=datetime.now(timezone.utc).isoformat())

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ctype="application/json", code=200):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        if getattr(self, "CC", None): self.send_header("Cache-Control", self.CC)
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        sym = q.get("symbol", ["SPX"])[0].upper(); sym = sym if sym in SRC else "SPX"
        mode = "7d" if q.get("mode", ["0dte"])[0] == "7d" else "0dte"
        try:
            if u.path == "/":
                return self.send(PAGE, "text/html")
            if u.path == "/api/market":
                return self.send(json.dumps(market()))
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
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#10141a;--pn:#171d26;--ln:#263040;--tx:#d5dbe5;--mu:#7d8798;--up:#2fbf8a;--dn:#e5575f;--fl:#e8b64a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.4 system-ui,sans-serif;display:grid;grid-template-columns:1fr 300px;height:100vh}
#main{position:relative;display:flex;flex-direction:column;min-width:0}#bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--ln)}
#bar b{margin-right:auto;font-size:16px}button{background:var(--pn);color:var(--tx);border:1px solid var(--ln);padding:5px 12px;border-radius:5px;cursor:pointer}
button.on{border-color:var(--fl);color:var(--fl)}#chart{flex:1;min-height:0}
aside{background:var(--pn);border-left:1px solid var(--ln);padding:14px;overflow:auto}
#reg{font-size:22px;font-weight:700;margin:0 0 2px}.mu{color:var(--mu);font-size:12px}
table{width:100%;border-collapse:collapse;margin:10px 0}td{padding:5px 0;border-bottom:1px solid var(--ln)}td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}.fl{color:var(--fl)}#bars div{display:flex;height:9px;margin:3px 0;gap:1px;align-items:center}
#help{position:fixed;inset:0;background:#000b;display:none;z-index:10;align-items:center;justify-content:center;padding:16px}
#help.on{display:flex}#help .box{background:var(--pn);border:1px solid var(--ln);border-radius:8px;max-width:660px;max-height:88vh;overflow:auto;padding:18px 24px}
#help h2{margin:0 0 4px;font-size:18px}#help h3{margin:16px 0 4px;font-size:14px}#help p,#help li{font-size:13.5px;margin:4px 0}#help ul{padding-left:18px;margin:4px 0}
#help .x{float:right}#help .ok{border:1px solid var(--ln);border-radius:6px;padding:8px 12px;margin-top:12px;color:var(--mu)}
#wm{position:absolute;left:12px;bottom:34px;font-size:11px;color:var(--mu);opacity:.4;pointer-events:none;user-select:none;z-index:2}
#mk{border-bottom:1px solid var(--ln)}#ix{display:flex;gap:8px;padding:8px 14px;overflow-x:auto}
.cd{flex:1;min-width:170px;background:var(--pn);border:1px solid var(--ln);border-radius:8px;padding:6px 8px}
.cd .h{display:flex;justify-content:space-between;font-size:12px;font-weight:600}.cd .mc{height:96px;margin-top:4px}.cd .px{font-size:11px;color:var(--mu)}
#hm{position:relative;height:300px;margin:0 14px 8px;background:#0c1016;border-radius:6px;overflow:hidden;display:none}#hm.on{display:block}
#hm .t{position:absolute;box-sizing:border-box;overflow:hidden;color:#fff;line-height:1.15;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;border:1px solid #0c1016}
#hm .s{position:absolute;color:#aab3c2;font-size:10px;padding:1px 4px;pointer-events:none;z-index:2;white-space:nowrap}#mkn{font-size:11px;padding:0 14px 6px}
@media(max-width:800px){body{grid-template-columns:1fr;grid-template-rows:60vh auto;height:auto}}
</style></head><body>
<div id="main"><div id="bar"><b id="sym">SPX</b>
<button data-s="SPX" class="on">SPX</button><button data-s="SPY">SPY</button>
<span class="mu">Temporalidad</span><button data-t="1m" class="on">1m</button><button data-t="5m">5m</button><button data-t="15m">15m</button><button data-t="30m">30m</button><button data-t="1h">1h</button><button data-t="1D">D</button><span class="mu" style="margin-left:10px">Vencimiento</span><button data-m="0dte" class="on">0DTE</button><button data-m="7d">7D</button><button id="hmb" style="margin-left:auto">Mapa de calor</button><button id="hb">? Guía</button></div><div id="mk"><div id="ix"></div><div id="hm"></div><div id="mkn" class="mu"></div></div><div id="chart"></div><div id="wm">Desarrollado por Darian G.</div></div>
<aside><p id="reg">…</p><div class="mu" id="sub">Cargando datos</div>
<table id="tb"></table><div class="mu">GEX neto por strike (cerca del precio)</div><div id="bars"></div>
<p class="mu" id="foot"></p><p class="mu">Solo informativo. Estimación basada en datos públicos con retraso; no es consejo financiero.</p></aside>
<div id="help"><div class="box"><button class="x" id="hx">Cerrar</button><h2>Cómo leer el panel</h2>
<p class="mu">Guía rápida para interpretar los niveles de gamma de SPX y SPY.</p>
<h3>1. Régimen de gamma (el titular del panel)</h3><ul>
<li><b class="up">Gamma positiva</b>: el precio está por encima del gamma flip. Los dealers tienden a comprar cuando baja y vender cuando sube, así que los movimientos se amortiguan. Suele haber rangos y reversión a la media.</li>
<li><b class="dn">Gamma negativa</b>: el precio está por debajo del gamma flip. Los dealers tienden a vender cuando baja y comprar cuando sube, y los movimientos se amplifican. Suele haber más volatilidad y tendencias más fuertes.</li></ul>
<h3>2. Niveles en la gráfica</h3><ul>
<li><b class="up">Call wall</b> (línea verde): el strike por encima del precio con más gamma de calls. Suele actuar como techo o resistencia.</li>
<li><b class="dn">Put wall</b> (línea roja): el strike por debajo del precio con más gamma de puts. Suele actuar como piso o soporte.</li>
<li><b class="fl">Gamma flip</b> (línea ámbar): el precio donde el GEX neto cambia de signo y cambia el régimen. Si dice "—", no hay cruce dentro de ±7.5% del precio.</li>
<li><b style="color:#5aa9ff">OI call</b> (azul) y <b style="color:#c18cff">OI put</b> (morado): los 3 strikes con más open interest de cada lado. Son niveles donde hay mucha posición abierta y el precio suele reaccionar. Si una línea no se ve, está fuera del rango de la gráfica: aleja el zoom.</li></ul>
<h3>3. Panel de la derecha</h3><ul>
<li><b>GEX total</b>: gamma neta de los dealers, en miles de millones de dólares por cada 1% de movimiento. Positivo = estabilizador, negativo = amplificador.</li>
<li><b>Barras por strike</b>: verde a la derecha son calls, rojo a la izquierda son puts. Las barras largas marcan los strikes con más peso.</li></ul>
<h3>4. Botones</h3><ul>
<li><b>0DTE</b>: solo opciones que vencen hoy. Es lo que más mueve el precio dentro del día y cambia rápido.</li>
<li><b>7D</b>: todas las que vencen en los próximos 7 días. Muestra niveles más estables, útiles como contexto de la semana.</li>
<li><b>Temporalidad</b> (1m a D) solo cambia las velas; los niveles de gamma no cambian.</li></ul>
<h3>5. Ejemplo de lectura</h3>
<p>Precio entre el put wall y el call wall, con gamma positiva: se espera un rango entre los dos niveles. Precio rompiendo el put wall con gamma negativa: el movimiento puede acelerarse hacia abajo.</p>
<div class="ok"><b>Importante:</b> es una estimación. Asume que los dealers están largos en calls y cortos en puts, el open interest se actualiza una vez al día, y los datos de CBOE tienen unos 15 minutos de retraso. Fuera del horario de mercado, 0DTE muestra los contratos de hoy ya vencidos. Úsalo como contexto, no como señal de compra o venta. Solo informativo; no es consejo financiero.</div></div></div>
<script>
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js');
const hp=document.getElementById('help');document.getElementById('hb').onclick=()=>hp.classList.add('on');hp.onclick=e=>{if(e.target==hp||e.target.id=='hx')hp.classList.remove('on')};document.addEventListener('keydown',e=>{if(e.key==='Escape')hp.classList.remove('on')});
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

// ---- Mercado: índices + mapa de calor ----
let MK=null,cards={};const pc=p=>p>=0?'#2fbf8a':'#e5575f';
const hcol=d3.scaleLinear().domain([-3,0,3]).range(['#c9444b','#2c313c','#2a9d6b']).clamp(true);
function drawIdx(idx){const box=document.getElementById('ix');
 idx.forEach(d=>{let c=cards[d.sym];
  if(!c){const el=document.createElement('div');el.className='cd';el.innerHTML='<div class="h"><span></span><span class="v"></span></div><div class="px"></div><div class="mc"></div>';box.appendChild(el);
   const chh=LightweightCharts.createChart(el.querySelector('.mc'),{autoSize:true,layout:{background:{color:'transparent'},textColor:'#7d8798'},grid:{vertLines:{visible:false},horzLines:{visible:false}},rightPriceScale:{visible:false},leftPriceScale:{visible:false},timeScale:{visible:false,borderVisible:false},handleScroll:false,handleScale:false,crosshair:{vertLine:{visible:false},horzLine:{visible:false}}});
   const sr=chh.addCandlestickSeries({upColor:'#2fbf8a',downColor:'#e5575f',borderVisible:false,wickUpColor:'#2fbf8a',wickDownColor:'#e5575f',lastValueVisible:false,priceLineVisible:false});
   c=cards[d.sym]={el,sr,chh,pl:null};}
  c.el.querySelector('span').textContent=d.name;const v=c.el.querySelector('.v');v.textContent=(d.pct>=0?'+':'')+d.pct.toFixed(2)+'%';v.style.color=pc(d.pct);
  c.el.querySelector('.px').textContent=f(d.price,2)+'  ('+(d.chg>=0?'+':'')+f(d.chg,2)+')';
  c.sr.setData(d.candles);if(c.pl)c.sr.removePriceLine(c.pl);c.pl=c.sr.createPriceLine({price:d.prev,color:'#e5575f',lineWidth:1,lineStyle:2,axisLabelVisible:false});c.chh.timeScale().fitContent();});}
function drawHeat(){const hm=document.getElementById('hm');if(!MK||!hm.classList.contains('on'))return;
 const W=hm.clientWidth,H=hm.clientHeight,by={};MK.stk.forEach(x=>(by[x.s]=by[x.s]||[]).push(x));
 const root=d3.hierarchy({children:Object.entries(by).map(([k,v])=>({name:k,children:v}))}).sum(d=>d.cap||0).sort((a,b)=>b.value-a.value);
 d3.treemap().size([W,H]).paddingInner(1).paddingTop(d=>d.depth===1?13:0)(root);let h='';
 root.children.forEach(n=>{h+=`<div class="s" style="left:${n.x0}px;top:${n.y0}px">${n.data.name}</div>`;
  n.children.forEach(l=>{const d=l.data,w=l.x1-l.x0,hh=l.y1-l.y0,fs=Math.max(9,Math.min(22,Math.sqrt(w*hh)/5));
   const t=w>=30&&hh>=20?`<b style="font-size:${fs}px">${d.t}</b>`+(w>=46&&hh>=34?`<span style="font-size:${Math.max(9,fs*.7)}px">${d.pct>=0?'+':''}${d.pct.toFixed(2)}%</span>`:''):'';
   h+=`<div class="t" title="${d.t}  ${d.pct>=0?'+':''}${d.pct.toFixed(2)}%  $${f(d.price,2)}" style="left:${l.x0}px;top:${l.y0}px;width:${w}px;height:${hh}px;background:${hcol(d.pct)}">${t}</div>`;});});
 hm.innerHTML=h;}
async function loadMarket(){if(document.hidden)return;try{const r=await fetch('/api/market?v=1');const d=await r.json();if(d.error)throw new Error(d.error);MK=d;drawIdx(d.idx);drawHeat();
 document.getElementById('mkn').textContent=`Mercado actualizado ${new Date(d.asof).toLocaleTimeString()} · ${d.ok}/${d.n} acciones · tamaños aproximados por capitalización`;}
 catch(e){document.getElementById('mkn').textContent='Mercado: '+e.message}}
document.getElementById('hmb').onclick=function(){const hm=document.getElementById('hm');hm.classList.toggle('on');this.classList.toggle('on',hm.classList.contains('on'));drawHeat()};
if(innerHeight>=900){document.getElementById('hm').classList.add('on');document.getElementById('hmb').classList.add('on')}
new ResizeObserver(()=>drawHeat()).observe(document.getElementById('hm'));
loadMarket();setInterval(loadMarket,30000);
</script></body></html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"   # en la nube escucha a todos
    print(f"Panel GEX en http://localhost:{port}  (Ctrl+C para salir)")
    ThreadingHTTPServer((host, port), H).serve_forever()


# ---- Vercel: una sola función /api/data?kind=levels|candles|market ----
class handler(H):
    CC = "public, s-maxage=15, stale-while-revalidate=30"
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); kind = q.get("kind", ["levels"])[0]
        if kind == "market": self.CC = "public, s-maxage=30, stale-while-revalidate=60"
        self.path = "/api/" + kind + "?" + u.query
        super().do_GET()
