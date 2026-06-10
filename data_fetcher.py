#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLOBAL SITUATION ROOM - local data backend
-------------------------------------------------
Fetches everything SERVER-SIDE (no browser CORS proxies), so the dashboard
always reflects the real current state. Writes data.json next to itself and
serves the folder over http://localhost:8800 so the page can do true live
refreshes.

Sources:
  - FinMind        : TAIEX index + Taiwan stock basket (real OHLC + trend)
  - Stooq          : Brent, WTI, S&P500, VIX, USD/TWD (server-side CSV)
  - USGS           : earthquakes geojson
  - Google News RSS: Iran/Hormuz, global wire, official-feed signals

Stdlib only - no pip install required. Python 3.8+.

Run:   python data_fetcher.py            (fetch loop + local web server)
       python data_fetcher.py --once     (single fetch, write data.json, exit)
"""
import json, sys, time, threading, datetime, urllib.request, urllib.parse, os, re
import concurrent.futures
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8800
REFRESH_MIN = 5          # how often the backend re-pulls all sources
FINMIND_TOKEN = ""       # optional: paste FinMind API token here for higher rate limits
UA = {"User-Agent": "Mozilla/5.0 (SituationRoom)"}

# ---- Taiwan basket: semis core + the oil/Hormuz transmission lens ----
TW_BASKET = [
    ("2330", "TSMC 台積電",        "semi"),
    ("2317", "Hon Hai 鴻海",        "semi"),
    ("2454", "MediaTek 聯發科",     "semi"),
    ("2603", "Evergreen 長榮",      "ship"),   # shipping - Hormuz/freight sensitive
    ("2618", "EVA Air 長榮航",      "air"),    # airline - jet-fuel sensitive
    ("1301", "Formosa 台塑",        "chem"),   # petrochem - crude sensitive
]

# ---- YouTube live channels (handles must match the frontend CHANNELS list) ----
YT_CHANNELS = [
    "aljazeeraenglish", "ABCNews", "dwnews", "FRANCE24English",
    "SkyNews", "markets", "CNN", "WhiteHouse", "DeptofDefense",
]

def _load_token():
    global FINMIND_TOKEN
    cfg = os.path.join(HERE, "config.txt")
    if os.path.exists(cfg):
        try:
            for line in open(cfg, encoding="utf-8"):
                line = line.strip()
                if line.lower().startswith("finmind_token") and "=" in line:
                    FINMIND_TOKEN = line.split("=", 1)[1].strip()
        except Exception:
            pass

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ============================================================
#  FinMind - Taiwan
# ============================================================
def finmind(dataset, data_id, days=20):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    params = {"dataset": dataset, "data_id": data_id, "start_date": start.isoformat()}
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    url = "https://api.finmindtrade.com/api/v4/data?" + urllib.parse.urlencode(params)
    j = json.loads(http_get(url, 25))
    return j.get("data", [])

def series_from_rows(rows):
    """Return (last_close, prev_close, chg_pct, spark[list of closes], date)."""
    closes = [r["close"] for r in rows if r.get("close") not in (None, 0)]
    if len(closes) < 2:
        return None
    last, prev = closes[-1], closes[-2]
    chg = (last - prev) / prev * 100 if prev else 0
    return last, prev, chg, closes[-12:], rows[-1].get("date", "")

def _roc_to_iso(d):
    """Convert TWSE ROC date '1150529' -> '2026-05-29'."""
    d = str(d).strip()
    if len(d) >= 7 and d.isdigit():
        return f"{int(d[:-4]) + 1911:04d}-{d[-4:-2]}-{d[-2:]}"
    return d

def fetch_taiex_twse():
    """Official TWSE 發行量加權股價指數 daily history (current month) — the freshest
    same-day index close, used to fix FinMind's lagging index date."""
    url = "https://openapi.twse.com.tw/v1/indicesReport/MI_5MINS_HIST"
    rows = json.loads(http_get(url, 20))
    closes, dates = [], []
    for r in rows:
        c = r.get("收盤指數") or r.get("ClosingIndex") or ""
        d = r.get("日期") or r.get("Date") or ""
        try:
            v = float(str(c).replace(",", ""))
        except Exception:
            continue
        if v <= 0:
            continue
        closes.append(v); dates.append(_roc_to_iso(d))
    if len(closes) < 2:
        return None
    last, prev = closes[-1], closes[-2]
    chg = (last - prev) / prev * 100 if prev else 0
    return {"close": round(last, 2), "chg_pct": round(chg, 2),
            "spark": [round(x, 2) for x in closes[-12:]], "date": dates[-1], "source": "TWSE"}

def yahoo_series(sym):
    """Daily series from Yahoo Finance for an index or stock. Returns
    dict(close, chg_pct, spark, date) from the latest (live) bar, or None.
    ^TWII is the TAIEX price index (發行量加權股價指數) — matches TradingView/brokers."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(sym) + "?range=2mo&interval=1d")
    j = json.loads(http_get(url, 15))
    r = j["chart"]["result"][0]
    ts = r.get("timestamp", []) or []
    cl = r["indicators"]["quote"][0]["close"]
    pairs = [(t, c) for t, c in zip(ts, cl) if c is not None]
    if len(pairs) < 2:
        return None
    closes = [c for _, c in pairs]
    last, prev = closes[-1], closes[-2]
    chg = (last - prev) / prev * 100 if prev else 0
    tpe = datetime.timezone(datetime.timedelta(hours=8))
    d = datetime.datetime.fromtimestamp(pairs[-1][0], tz=datetime.timezone.utc).astimezone(tpe).strftime("%Y-%m-%d")
    return {"close": round(last, 2), "chg_pct": round(chg, 2),
            "spark": [round(c, 2) for c in closes[-12:]], "date": d,
            "last": last, "prev": prev}

def fetch_taiwan():
    out = {"taiex": None, "stocks": [], "error": None}
    # --- TAIEX index: Yahoo ^TWII (live price index, matches the live chart) -> TWSE -> FinMind ---
    try:
        s = yahoo_series("^TWII")
        if s:
            s["source"] = "Yahoo"
            out["taiex"] = s
    except Exception as e:
        out["error"] = ("twii:" + str(e))[:110]
    if not out["taiex"]:
        try:
            twse = fetch_taiex_twse()
            if twse:
                out["taiex"] = twse
        except Exception:
            pass
    if not out["taiex"]:
        try:
            rows = finmind("TaiwanStockPrice", "TAIEX")
            r = series_from_rows(rows)
            if r:
                last, prev, chg, spark, date = r
                out["taiex"] = {"close": round(last, 2), "chg_pct": round(chg, 2),
                                "spark": [round(x, 2) for x in spark], "date": date, "source": "FinMind"}
        except Exception:
            pass
    # --- basket stocks: Yahoo (id.TW) live -> FinMind fallback ---
    for sid, name, tag in TW_BASKET:
        st, src = None, None
        try:
            st = yahoo_series(sid + ".TW")
            if st:
                src = "Yahoo"
        except Exception:
            st = None
        if not st:
            try:
                rows = finmind("TaiwanStockPrice", sid)
                r = series_from_rows(rows)
                if r:
                    last, prev, chg, spark, date = r
                    st = {"close": round(last, 2), "chg_pct": round(chg, 2),
                          "spark": [round(x, 2) for x in spark], "date": date}
                    src = "FinMind"
            except Exception:
                st = None
        if st:
            st.update({"id": sid, "name": name, "tag": tag, "source": src})
            out["stocks"].append(st)
    return out

# ============================================================
#  Stooq - oil / US indices / FX  (server-side, no CORS)
# ============================================================
def stooq_quote(sym):
    # live last-quote first
    try:
        csv = http_get(f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv", 15)
        rows = csv.strip().splitlines()
        if len(rows) >= 2:
            c = rows[1].split(",")
            close, open_ = float(c[6]), float(c[3])
            if close == close:  # not NaN
                return {"close": close, "chg": ((close-open_)/open_*100) if open_ else 0, "live": True}
    except Exception:
        pass
    # fallback: daily history (works when market closed)
    csv = http_get(f"https://stooq.com/q/d/l/?s={sym}&i=d", 15)
    rows = csv.strip().splitlines()
    if len(rows) < 3:
        raise ValueError("N/D")
    last, prev = rows[-1].split(","), rows[-2].split(",")
    close, pclose = float(last[4]), float(prev[4])
    closes = []
    for ln in rows[-12:]:
        try: closes.append(float(ln.split(",")[4]))
        except Exception: pass
    return {"close": close, "chg": ((close-pclose)/pclose*100) if pclose else 0,
            "live": False, "spark": closes}

def yahoo_quote(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
    j = json.loads(http_get(url, 15))
    r = j["chart"]["result"][0]
    cl = [c for c in r["indicators"]["quote"][0]["close"] if c is not None]
    if len(cl) < 2:
        raise ValueError("N/D")
    return {"close": cl[-1], "chg": (cl[-1]-cl[-2])/cl[-2]*100, "spark": cl[-12:]}

def fetch_markets():
    want = [("BRENT", "BZ=F", ["cb.f"], "$"), ("WTI", "CL=F", ["cl.f"], "$"),
            ("S&P 500", "^GSPC", ["^spx"], ""), ("VIX", "^VIX", [], ""),
            ("USD/TWD", "TWD=X", ["usdtwd"], "")]
    out, sig = [], {}
    for n, ysym, ssyms, pre in want:
        q = None
        try:
            q = yahoo_quote(ysym)
        except Exception:
            for s in ssyms:
                try: q = stooq_quote(s); break
                except Exception: continue
        try:
            if q is None: raise ValueError("N/D")
            out.append({"n": n, "p": pre + f"{q['close']:.2f}",
                        "c": ("+" if q["chg"] >= 0 else "") + f"{q['chg']:.2f}%",
                        "d": "up" if q["chg"] > 0 else "dn" if q["chg"] < 0 else "fl",
                        "spark": [round(x, 2) for x in q.get("spark", [])]})
            if n == "BRENT": sig["brent"] = round(q["close"], 2); sig["brent_chg"] = round(q["chg"], 2)
            if n == "WTI": sig["wti_chg"] = round(q["chg"], 2)
            if n == "VIX": sig["vix"] = round(q["close"], 2)
        except Exception:
            out.append({"n": n, "p": "-", "c": "n/a", "d": "fl", "spark": []})
    return out, sig

# ============================================================
#  USGS quakes
# ============================================================
def fetch_quakes(min_mag=4.5):
    feed = "4.5_day" if min_mag >= 4.5 else "2.5_day"
    out = []
    try:
        j = json.loads(http_get(f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/{feed}.geojson", 20))
        now = time.time() * 1000
        for f in j.get("features", []):
            p = f.get("properties", {})
            if p.get("mag") is None or p["mag"] < min_mag:
                continue
            out.append({"mag": round(p["mag"], 1), "place": p.get("place", "unknown"),
                        "mins": int((now - p["time"]) / 60000), "tsunami": p.get("tsunami", 0),
                        "url": p.get("url", "https://earthquake.usgs.gov/earthquakes/map/"), "id": f.get("id", "")})
        out.sort(key=lambda q: (q["tsunami"], q["mag"]), reverse=True)
    except Exception:
        pass
    return out[:6]

# ============================================================
#  Google News RSS  (server-side)
# ============================================================
def gnews(query, days=2, n=5):
    q = urllib.parse.quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    items = []
    try:
        xml = http_get(url, 18)
        root = ET.fromstring(xml)
        for it in root.iter("item"):
            def g(t):
                e = it.find(t); return e.text if e is not None and e.text else ""
            title, src = g("title"), ""
            srcEl = it.find("source")
            if srcEl is not None and srcEl.text:
                src = srcEl.text
            elif " - " in title:
                parts = title.split(" - "); src = parts.pop(); title = " - ".join(parts)
            items.append({"title": title, "src": src, "date": g("pubDate"), "link": g("link")})
            if len(items) >= n:
                break
    except Exception:
        pass
    return items

def fetch_news():
    hz = gnews("Iran OR Hormuz OR tanker OR Strait ceasefire OR strike", 2, 5)
    wire = gnews('White House OR Trump OR Iran OR "stock market" OR Federal Reserve OR breaking', 1, 6)
    inc = gnews("White House shooting OR lockdown OR Secret Service OR evacuated", 1, 1)
    wh = gnews("White House briefing OR Trump announcement", 2, 1)
    dod = gnews("Pentagon OR Hegseth OR Department of War", 2, 1)
    return {"hz": hz, "wire": wire,
            "gov": {"incident": inc[0] if inc else None,
                    "wh": wh[0] if wh else None, "dod": dod[0] if dod else None}}

# ============================================================
#  YouTube live-status (server-side, no API key / no CORS)
# ============================================================
def yt_status(handle):
    """Return 'live' | 'soon' | 'off' | 'unknown' for a channel's /live page.
    A channel that is actually streaming embeds an HLS manifest URL; an upcoming
    stream carries an 'isUpcoming' flag; otherwise it's offline."""
    try:
        html = http_get(f"https://www.youtube.com/@{handle}/live", 8)
    except Exception:
        return "unknown"
    if "hlsManifestUrl" in html:
        return "live"
    if '"isUpcoming":true' in html:
        return "soon"
    return "off"

def fetch_channels():
    out = {h: "unknown" for h in YT_CHANNELS}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(YT_CHANNELS)) as ex:
            futs = {ex.submit(yt_status, h): h for h in YT_CHANNELS}
            for f in futs:
                try:
                    out[futs[f]] = f.result(timeout=12)
                except Exception:
                    out[futs[f]] = "unknown"
    except Exception:
        pass
    return out

# ============================================================
#  CPI lie-detector : US 10Y yield + DXY (smart-money reaction)
# ============================================================
# US CPI release calendar (BLS) — all 08:30 AM ET, stored as UTC ISO.
# 08:30 EDT = 12:30Z (Mar–Oct); 08:30 EST = 13:30Z (Nov–Feb).
CPI_SCHEDULE = [
    ("May 2026",  "2026-06-10T12:30:00+00:00"),
    ("Jun 2026",  "2026-07-14T12:30:00+00:00"),
    ("Jul 2026",  "2026-08-12T12:30:00+00:00"),
    ("Aug 2026",  "2026-09-11T12:30:00+00:00"),
    ("Sep 2026",  "2026-10-14T12:30:00+00:00"),
    ("Oct 2026",  "2026-11-10T13:30:00+00:00"),
    ("Nov 2026",  "2026-12-10T13:30:00+00:00"),
]

def _yld_norm(v):
    """Yahoo ^TNX is sometimes quoted x10 (43.0) and sometimes as percent (4.30)."""
    return v / 10.0 if (v and v > 20) else v

def fetch_cpi_watch():
    cpi = {"us10y": None, "dxy": None, "next": None}
    # US 10Y Treasury yield (^TNX) — the pricing anchor
    try:
        y = yahoo_series("^TNX")
        if y:
            last, prev = _yld_norm(y["last"]), _yld_norm(y["prev"])
            cpi["us10y"] = {"value": round(last, 2),
                            "chg_bps": round((last - prev) * 100),
                            "spark": [round(_yld_norm(x), 2) for x in y["spark"]],
                            "date": y["date"]}
    except Exception:
        pass
    # US Dollar Index (DXY) — global liquidity gauge
    for sym in ("DX-Y.NYB", "DX=F"):
        try:
            d = yahoo_series(sym)
            if d:
                chg = (d["last"] - d["prev"]) / d["prev"] * 100 if d["prev"] else 0
                cpi["dxy"] = {"value": round(d["last"], 2), "chg_pct": round(chg, 2),
                              "spark": d["spark"], "date": d["date"]}
                break
        except Exception:
            continue
    # next scheduled CPI release (keep showing through the release hour)
    now = datetime.datetime.now(datetime.timezone.utc)
    tpe = datetime.timezone(datetime.timedelta(hours=8))
    for label, iso in CPI_SCHEDULE:
        dt = datetime.datetime.fromisoformat(iso)
        if dt >= now - datetime.timedelta(hours=1):
            dt2 = dt.astimezone(tpe)
            cpi["next"] = {"label": label, "iso": iso,
                           "tpe": f"{dt2.month}/{dt2.day} {dt2.hour:02d}:{dt2.minute:02d}"}
            break
    return cpi

# ============================================================
#  Assemble
# ============================================================
def hours_since(date_str):
    if not date_str:
        return 999
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            t = datetime.datetime.strptime(date_str, fmt)
            return (datetime.datetime.utcnow() - t).total_seconds() / 3600
        except Exception:
            continue
    return 999

def build():
    tw = fetch_taiwan()
    markets, sig = fetch_markets()
    quakes = fetch_quakes()
    news = fetch_news()
    channels = fetch_channels()
    cpi = fetch_cpi_watch()

    top_q = quakes[0] if quakes else None
    sig["quake"] = top_q["mag"] if top_q else None
    sig["tsunami"] = bool(top_q["tsunami"]) if top_q else False
    g = news["gov"]
    sig["security"] = bool(g["incident"] and hours_since(g["incident"]["date"]) < 8)

    # S&P 500 move (the selloff being judged by the CPI lie-detector)
    def _pct(s):
        try:
            return float(str(s).replace("%", "").replace("+", ""))
        except Exception:
            return None
    spx = next((m for m in markets if m.get("n") == "S&P 500"), None)
    cpi["spx_chg"] = _pct(spx["c"]) if spx else None
    cpi["vix"] = sig.get("vix")

    now = datetime.datetime.now(datetime.timezone.utc)
    tpe = now.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
    return {
        "generated_iso": now.isoformat(),
        "generated_tpe": tpe.strftime("%Y-%m-%d %H:%M") + " TPE",
        "generated_epoch": int(time.time()*1000),
        "taiwan": tw,
        "markets": markets,
        "signals": sig,
        "quakes": quakes,
        "news": news,
        "channels": channels,
        "cpi": cpi,
        "refresh_min": REFRESH_MIN,
    }

def write_data():
    try:
        data = build()
        path = os.path.join(HERE, "data.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        with open(os.path.join(HERE, "data.js"), "w", encoding="utf-8") as jf:
            jf.write("window.GSR_DATA=")
            json.dump(data, jf, ensure_ascii=False)
            jf.write(";")
        tw = data["taiwan"]["taiex"]
        chans = data.get("channels", {})
        live = sum(1 for v in chans.values() if v == "live")
        close = tw["close"] if tw else "n/a"
        src = tw.get("source", "?") if tw else "-"
        tdate = tw.get("date", "") if tw else ""
        nstk = len(data["taiwan"]["stocks"]); nq = len(data["quakes"]); nhz = len(data["news"]["hz"]); nch = len(chans)
        print(f"[{time.strftime('%H:%M:%S')}] data.json written  TAIEX={close} ({src} {tdate})  "
              f"stocks={nstk}  quakes={nq}  hz_news={nhz}  live_ch={live}/{nch}")
        return True
    except Exception as e:
        print("[ERROR] write_data:", e)
        return False

def loop():
    while True:
        time.sleep(REFRESH_MIN * 60)
        write_data()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=HERE, **k)
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()
    def log_message(self, *a):
        pass  # quiet

def lan_ip():
    """Best-effort local network IP so phones/tablets on the same Wi-Fi can connect."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent; just picks the outbound interface
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

def main():
    _load_token()
    once = "--once" in sys.argv
    print("Global Situation Room backend - fetching initial data...")
    write_data()
    if once:
        return
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)   # listen on all interfaces (LAN access)
    url = f"http://localhost:{PORT}/global-situation-room.html"
    ipad_url = f"http://{lan_ip()}:{PORT}/global-situation-room.html"
    print(f"\n  SITUATION ROOM LIVE  ->  {url}")
    print(f"  On iPad / phone (same Wi-Fi), open Safari to:")
    print(f"      {ipad_url}")
    print(f"  (First run: if Windows asks, allow Python through the firewall on Private networks.)")
    print(f"  Backend refreshing every {REFRESH_MIN} min. Leave this window open. Ctrl+C to stop.\n")
    try:
        import webbrowser; webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")

if __name__ == "__main__":
    main()
# end of file
