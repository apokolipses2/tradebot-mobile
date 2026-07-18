"""
TradeBot MOBILE — the same signal engine as goldbot.py, served as a phone-friendly web app.

RUN IT (test on your phone, same WiFi):
  In PyCharm open this file and press ▶ Run. It prints a URL like
  http://192.168.1.23:8000  — open THAT on your phone's browser (same WiFi).

USE WITH PC OFF (anywhere):
  Deploy this one file to a free host (e.g. Render.com). Steps are in the chat.
  It reads the PORT from the environment, so it works on cloud hosts as-is.

Pure Python standard library — no packages to install. Self-contained (no tkinter),
so it also runs on a headless cloud server.
"""

import json
import os
import ssl
import socket
import time
import threading
import webbrowser
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ------------------------------ config ------------------------------
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={iv}&range={rng}"
SPOT_GOLD_URL = "https://api.gold-api.com/price/XAU"
# STOCKS (user trades stocks, not crypto). Easy to swap — just tell Claude which tickers you trade.
INSTRUMENTS = [("NVIDIA", "NVDA", None), ("Apple", "AAPL", None), ("Tesla", "TSLA", None),
               ("Amazon", "AMZN", None), ("Meta", "META", None), ("Microsoft", "MSFT", None)]
# longer timeframes only (15m/30m/1h) — less noise, more 'understandable' than 1m/5m scalping
TFS = [("15m", "5d", 1), ("30m", "1mo", 2), ("1h", "1mo", 3)]
PRIMARY_TF = "30m"
FAST, SLOW, RSI_N = 9, 21, 14
STALE_SEC = 1800
CACHE_TTL = 12          # seconds to cache each market's analysis (keeps it snappy + gentle on data source)

_SSL = ssl.create_default_context()
_cache = {}             # sym -> (timestamp, result)


# ------------------------------ data + indicators (same engine as desktop) ------------------------------
def fetch_chart(symbol, interval, rng):
    url = CHART_URL.format(sym=symbol.replace("^", "%5E"), iv=interval, rng=rng)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, context=_SSL, timeout=15) as r:
        j = json.load(r)
    res = j["chart"]["result"][0]
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    mtime = meta.get("regularMarketTime", 0) or 0
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    candles = []
    for i in range(len(ts)):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        candles.append({"o": o, "h": h, "l": l, "cl": c})
    return price, mtime, candles


def fetch_spot_gold():
    req = urllib.request.Request(SPOT_GOLD_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, context=_SSL, timeout=12) as r:
        return json.load(r).get("price")


def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1); e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    e = sum(vals[:n]) / n; out[n - 1] = e; k = 2 / (n + 1)
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k); out[i] = e
    return out


def rsi_val(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        if d >= 0: g += d
        else: l -= d
    g /= n; l /= n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def trend_dir(closes):
    ef, es = ema(closes, FAST), ema(closes, SLOW)
    if ef is None or es is None: return 0, "not enough data"
    return (1, f"EMA{FAST}>EMA{SLOW} uptrend") if ef > es else (-1, f"EMA{FAST}<EMA{SLOW} downtrend")


def macd_dir(closes):
    if len(closes) < 35: return 0, "not enough data"
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    line = [a - b for a, b in zip(e12, e26) if a is not None and b is not None]
    if len(line) < 9: return 0, "not enough data"
    sig = ema_series(line, 9); hist = line[-1] - sig[-1]
    return (1, "MACD above signal") if hist > 0 else (-1, "MACD below signal") if hist < 0 else (0, "MACD flat")


def rsi_dir(closes):
    rv = rsi_val(closes, RSI_N)
    if rv is None: return 0, "not enough data"
    if rv >= 70: return -1, f"RSI {rv:.0f} overbought"
    if rv <= 30: return 1, f"RSI {rv:.0f} oversold"
    return 0, f"RSI {rv:.0f} neutral"


def bollinger_dir(closes, n=20, k=2):
    if len(closes) < n: return 0, "not enough data"
    w = closes[-n:]; mid = sum(w) / n
    sd = (sum((x - mid) ** 2 for x in w) / n) ** 0.5
    up, lo = mid + k * sd, mid - k * sd
    if up == lo: return 0, "flat band"
    pb = (closes[-1] - lo) / (up - lo)
    if pb <= 0.1: return 1, "at lower band"
    if pb >= 0.9: return -1, "at upper band"
    return 0, "mid-band"


def candle_dir(candles):
    if len(candles) < 2: return 0, "not enough data"
    p, k = candles[-2], candles[-1]
    body = abs(k["cl"] - k["o"]); rng = (k["h"] - k["l"]) or 1e-9
    up = k["h"] - max(k["o"], k["cl"]); lo = min(k["o"], k["cl"]) - k["l"]
    pB, pS = p["cl"] > p["o"], p["cl"] < p["o"]; kB, kS = k["cl"] > k["o"], k["cl"] < k["o"]
    if pS and kB and k["cl"] >= p["o"] and k["o"] <= p["cl"]: return 1, "bullish engulfing"
    if pB and kS and k["o"] >= p["cl"] and k["cl"] <= p["o"]: return -1, "bearish engulfing"
    if body <= rng * 0.1: return 0, "doji"
    if lo >= body * 2 and up <= body * 0.6: return 1, "hammer"
    if up >= body * 2 and lo <= body * 0.6: return -1, "shooting star"
    return 0, "no clear pattern"


def atr(candles, n=14):
    if len(candles) < n + 1: return None
    trs = [max(candles[i]["h"] - candles[i]["l"], abs(candles[i]["h"] - candles[i - 1]["cl"]),
               abs(candles[i]["l"] - candles[i - 1]["cl"])) for i in range(1, len(candles))]
    return sum(trs[-n:]) / n


def compute_tf(candles):
    closes = [c["cl"] for c in candles]
    ind = {"trend": trend_dir(closes), "macd": macd_dir(closes), "rsi": rsi_dir(closes),
           "boll": bollinger_dir(closes), "candle": candle_dir(candles)}
    return {"ind": ind, "score": sum(v[0] for v in ind.values()) / 5.0}


def build_signal(name, sym, src):
    """Full result for one market: price, verdict, confidence, timeframes, indicators, trade plan."""
    per, price, mtime, prim = {}, None, 0, None
    for iv, rng, _w in TFS:
        p, mt, candles = fetch_chart(sym, iv, rng)
        per[iv] = compute_tf(candles)
        if iv == "1m": price, mtime = p, mt
        if iv == PRIMARY_TF: prim = candles
    overall = sum(per[iv]["score"] * w for iv, _r, w in TFS) / sum(w for _i, _r, w in TFS)
    verdict = "BUY" if overall >= 0.3 else "SELL" if overall <= -0.3 else "WAIT"
    conf = min(99, round(abs(overall) * 100))
    tf_align = {iv: ("BUY" if per[iv]["score"] > 0.2 else "SELL" if per[iv]["score"] < -0.2 else "WAIT")
                for iv, _r, _w in TFS}
    sign = 1 if verdict == "BUY" else -1 if verdict == "SELL" else 0
    tf_agree = sum(1 for iv, _r, _w in TFS if (per[iv]["score"] > 0) == (sign > 0) and per[iv]["score"] != 0) if sign else 0
    primary = per[PRIMARY_TF]["ind"]
    ind_agree = sum(1 for d, _r in primary.values() if (d > 0) == (sign > 0) and d != 0) if sign else 0

    # display price: gold uses true spot; others use Yahoo's
    if src == "spot-gold":
        try: price = fetch_spot_gold() or price
        except Exception: pass

    stale = bool(mtime) and (time.time() - mtime > STALE_SEC)
    a = atr(prim) if prim else None
    plan = None
    if verdict in ("BUY", "SELL") and a and price and not stale:
        risk = 1.5 * a
        if verdict == "BUY":
            plan = {"entry": price, "stop": price - risk, "target": price + 2 * risk}
        else:
            plan = {"entry": price, "stop": price + risk, "target": price - 2 * risk}

    return {"name": name, "symbol": sym, "price": price, "verdict": "CLOSED" if stale else verdict,
            "confidence": conf, "tf_align": tf_align, "tf_agree": tf_agree, "ind_agree": ind_agree,
            "n_tf": len(TFS), "primary": {k: {"dir": v[0], "why": v[1]} for k, v in primary.items()},
            "plan": plan, "stale": stale}


def cached_signal(name, sym, src):
    now = time.time()
    hit = _cache.get(sym)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    res = build_signal(name, sym, src)
    _cache[sym] = (now, res)
    return res


# ------------------------------ web server ------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/instruments":
            self._send(200, json.dumps([{"name": n, "symbol": s} for n, s, _ in INSTRUMENTS]))
        elif u.path == "/api/signal":
            q = parse_qs(u.query)
            sym = (q.get("symbol") or [INSTRUMENTS[0][1]])[0]
            inst = next((i for i in INSTRUMENTS if i[1] == sym), INSTRUMENTS[0])
            try:
                self._send(200, json.dumps(cached_signal(*inst)))
            except Exception as e:
                self._send(200, json.dumps({"error": str(e)}))
        else:
            self._send(404, "not found", "text/plain")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>TradeBot Mobile</title>
<style>
  :root{--bg:#080a10;--panel:#111621;--panel2:#19202e;--line:#222c3d;--txt:#eef2f9;--dim:#79879f;
        --gold:#f5c451;--buy:#31d17f;--sell:#fb5a52;--wait:#e2b03e;}
  *{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
  body{background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;padding:12px;max-width:520px;margin:0 auto}
  .brand{font-size:20px;font-weight:800}.brand b{color:var(--gold)}
  .sub{color:var(--dim);font-size:12px;margin-bottom:12px}
  .tabs{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px;margin-bottom:12px}
  .tab{flex:0 0 auto;padding:9px 14px;border-radius:11px;background:var(--panel2);color:var(--dim);font-weight:700;font-size:13px;border:none}
  .tab.on{background:var(--gold);color:#1a1305}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:12px}
  .price{font-size:34px;font-weight:800}.fresh{font-size:11px;color:var(--buy)}.fresh.closed{color:var(--wait)}
  .verdict{font-size:44px;font-weight:900;text-align:center;letter-spacing:1px}
  .conf{text-align:center;color:var(--dim);font-size:13px}
  .bar{height:10px;border-radius:6px;background:var(--panel2);overflow:hidden;margin:10px 0}
  .bar > i{display:block;height:100%;border-radius:6px;transition:width .5s ease}
  .tfrow{display:flex;justify-content:center;gap:14px;margin-top:6px}
  .tf{text-align:center}.tf .k{font-size:11px;color:var(--dim)}
  .pill{display:inline-block;min-width:52px;padding:3px 9px;border-radius:999px;font-size:11px;font-weight:800;margin-top:3px}
  .lbl{font-size:11px;color:var(--dim);letter-spacing:1px;font-weight:700;margin-bottom:8px}
  .row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--line)}
  .row:last-child{border-bottom:none}
  .plan .k{color:var(--dim);font-size:13px}.plan .v{font-weight:800;font-size:16px}
  .note{color:var(--dim);font-size:11px;font-style:italic;margin-top:8px}
  .rr{color:var(--gold);font-weight:800;margin-top:8px}
  .muted{color:var(--dim);text-align:center;padding:8px}
  .c-buy{color:var(--buy)}.c-sell{color:var(--sell)}.c-wait{color:var(--wait)}.c-dim{color:var(--dim)}
  .p-buy{background:#0f2a1c;color:var(--buy)}.p-sell{background:#2c1414;color:var(--sell)}.p-wait{background:#1a2130;color:var(--dim)}
</style></head><body>
  <div class="brand">Trade<b>Bot</b> <span style="font-size:12px;color:var(--dim)">mobile</span></div>
  <div class="sub">live signals · not a prediction · demo/paper only</div>
  <div class="tabs" id="tabs"></div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:flex-end">
      <div class="price" id="price">—</div>
      <div class="fresh" id="fresh"></div>
    </div>
    <div class="sub" id="pname" style="margin:0"></div>
  </div>

  <div class="card">
    <div class="lbl" style="text-align:center">SIGNAL</div>
    <div class="verdict c-wait" id="verdict">…</div>
    <div class="conf" id="conf">loading…</div>
    <div class="bar"><i id="bar" style="width:0"></i></div>
    <div class="tfrow" id="tfrow"></div>
  </div>

  <div class="card plan">
    <div class="lbl">TRADE PLAN · entry / stop / target</div>
    <div id="plan"></div>
    <div class="note">stop = 1.5× the 30m ATR (volatility-based) · target = 2× your risk</div>
  </div>

  <div class="card">
    <div class="lbl">WHY · 30m chart</div>
    <div id="why"></div>
  </div>

<script>
let sym=null, instruments=[];
const $=id=>document.getElementById(id);
const cls=v=>v==="BUY"?"buy":v==="SELL"?"sell":v==="WAIT"?"wait":"dim";
const fmt=n=>n==null?"—":Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});

async function boot(){
  instruments=await (await fetch("/api/instruments")).json();
  sym=instruments[0].symbol;
  $("tabs").innerHTML=instruments.map(i=>`<button class="tab" data-s="${i.symbol}">${i.name}</button>`).join("");
  document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{sym=t.dataset.s;paint();load();});
  paint(); load(); setInterval(load, 8000);
}
function paint(){ document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.s===sym)); }

async function load(){
  try{
    const d=await (await fetch("/api/signal?symbol="+encodeURIComponent(sym))).json();
    if(d.error){ $("conf").textContent="fetch error: "+d.error; return; }
    $("price").textContent="$"+fmt(d.price);
    $("pname").textContent=d.name;
    $("fresh").textContent=d.stale?"● market closed":"● live";
    $("fresh").className="fresh"+(d.stale?" closed":"");
    const v=d.verdict, c=cls(v);
    $("verdict").textContent=v; $("verdict").className="verdict c-"+c;
    if(d.stale){ $("conf").textContent="market closed — signals paused"; }
    else{ $("conf").textContent=`confidence ${d.confidence}%  ·  ${d.tf_agree}/${d.n_tf} timeframes, ${d.ind_agree}/5 signals`; }
    const col=getComputedStyle(document.documentElement).getPropertyValue("--"+(c==="dim"?"panel2":c));
    $("bar").style.width=(d.stale?0:d.confidence)+"%"; $("bar").style.background=col;
    $("tfrow").innerHTML=Object.entries(d.tf_align).map(([k,val])=>
      `<div class="tf"><div class="k">${k}</div><div class="pill p-${cls(val)}">${d.stale?"—":val}</div></div>`).join("");
    // plan
    if(d.plan){
      $("plan").innerHTML=`
        <div class="row"><span class="k">Entry</span><span class="v">$${fmt(d.plan.entry)}</span></div>
        <div class="row"><span class="k">Stop-loss</span><span class="v c-sell">$${fmt(d.plan.stop)} (${(d.plan.stop-d.plan.entry>=0?"+":"")}${fmt(d.plan.stop-d.plan.entry)})</span></div>
        <div class="row"><span class="k">Take-profit</span><span class="v c-buy">$${fmt(d.plan.target)} (${(d.plan.target-d.plan.entry>=0?"+":"")}${fmt(d.plan.target-d.plan.entry)})</span></div>
        <div class="rr">Risk : Reward = 1 : 2</div>`;
    } else {
      $("plan").innerHTML=`<div class="muted">no clean setup — wait for an A+ signal (pros wait)</div>`;
    }
    $("why").innerHTML=Object.entries(d.primary).map(([k,o])=>{
      const lab={trend:"Trend",macd:"MACD",rsi:"RSI",boll:"Bollinger",candle:"Candles"}[k];
      const vv=o.dir>0?"BUY":o.dir<0?"SELL":"NEUTRAL";
      return `<div class="row"><span><b>${lab}</b> <span style="color:var(--dim);font-size:12px">${o.why}</span></span><span class="pill p-${cls(vv)}">${vv}</span></div>`;
    }).join("");
  }catch(e){ $("conf").textContent="connection lost — retrying…"; }
}
boot();
</script>
</body></html>"""


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    local = "PORT" not in os.environ          # cloud hosts set PORT; locally we auto-open the browser
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    pc_url = f"http://localhost:{port}"
    print("=" * 60)
    print("  TradeBot Mobile is RUNNING.")
    print(f"  On THIS PC (opening now):  {pc_url}")
    print(f"  On your PHONE (same WiFi):  http://{_lan_ip()}:{port}")
    print("  If the phone can't connect, allow Python through Windows Firewall.")
    print("  Stop it with the red Stop button.")
    print("=" * 60)
    if local:
        threading.Timer(1.2, lambda: webbrowser.open(pc_url)).start()   # auto-open on your PC
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
