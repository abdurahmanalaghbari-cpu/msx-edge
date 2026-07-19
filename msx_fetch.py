#!/usr/bin/env python3
"""
MSX EDGE companion fetcher
--------------------------
Pulls end-of-day OHLCV for every symbol in symbols.txt and writes/updates
one CSV per stock (generic format: Date,Open,High,Low,Close,Volume) into ./data.
MSX EDGE ingests that folder with one click via "Sync folder now".

Source: stockanalysis.com (S&P Global-sourced MSX data) via its documented
internal endpoints — the SvelteKit __data.json layer that powers the history
table (full OHLCV), with progressively simpler fallbacks. TradingView has no
MSX coverage; the tv adapter remains only for other exchanges (--source tv).

Usage:
  python msx_fetch.py              # daily update (recent bars, merges into CSVs)
  python msx_fetch.py --full       # first run: pull maximum history
  python msx_fetch.py --check OQGN # verbose test of a single symbol
  python msx_fetch.py --source sa  # force the stockanalysis.com fallback

Optional: set TV_USERNAME / TV_PASSWORD environment variables (GitHub repo
Settings → Secrets) to log in to TradingView; anonymous access also works.

symbols.txt: one ticker per line. If a TradingView ticker differs from the
default name, map it with `NAME=TVTICKER` (data saves as NAME.csv).

Schedule daily on Windows (2:30 PM Muscat, after the MSX close):
  schtasks /create /tn "MSX fetch" /tr "python C:\\path\\to\\msx_fetch.py" /sc daily /st 14:30

symbols.txt format — one ticker per line, exactly as it appears in the
stockanalysis.com URL, e.g. https://stockanalysis.com/quote/msm/OQGN/ -> OQGN
Lines starting with # are ignored.
"""

import argparse, csv, json, os, re, sys, time, random
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
SYMBOLS_FILE = os.path.join(BASE_DIR, "symbols.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
SLEEP_RANGE = (0.8, 1.6)   # polite delay between symbols, seconds

MONTHS = {m: i+1 for i, m in enumerate(
    ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"])}


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def http_get(url, timeout=30):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://stockanalysis.com/",
    })
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def to_float(x):
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "n/a", "N/A", "null"):
        return None
    m = re.match(r"^(-?[\d.]+)\s*([KMB])?$", s, re.I)
    if not m:
        try:
            return float(s)
        except ValueError:
            return None
    v = float(m.group(1))
    if m.group(2):
        v *= {"K": 1e3, "M": 1e6, "B": 1e9}[m.group(2).upper()]
    return v

def norm_date(x):
    """Accept '2026-07-15', 'Jul 15, 2026', epoch seconds/ms -> 'YYYY-MM-DD'."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        ts = float(x)
        if ts > 1e12:        # ms epoch
            ts /= 1000.0
        try:
            return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return None
    s = str(x).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\w{3})\w*\s+(\d{1,2}),\s*(\d{4})$", s)
    if m and m.group(1).lower()[:3] in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)   # assume M/D/Y
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None


# ------------------------------------------------- payload interpretation
KEYMAP = {
    "date": "d", "t": "d", "time": "d", "datetime": "d",
    "open": "o", "o": "o",
    "high": "h", "h": "h",
    "low": "l", "l": "l",
    "close": "c", "c": "c", "adjclose": "c", "adj_close": "c",
    "volume": "v", "v": "v", "vol": "v",
}

def rows_from_records(records):
    """List of dicts with flexible keys -> list of bar dicts."""
    out = []
    for r in records:
        if not isinstance(r, dict):
            return []
        bar = {}
        for k, v in r.items():
            kk = KEYMAP.get(str(k).lower())
            if kk == "d":
                bar["d"] = norm_date(v)
            elif kk:
                bar[kk] = to_float(v)
        if bar.get("d") and all(bar.get(k) is not None for k in ("o", "h", "l", "c")):
            bar.setdefault("v", 0)
            bar["v"] = bar["v"] or 0
            out.append(bar)
    return out

def rows_from_columns(obj):
    """Column-oriented payload {'t':[...], 'o':[...], ...} -> bars."""
    cols = {}
    for k, v in obj.items():
        kk = KEYMAP.get(str(k).lower())
        if kk and isinstance(v, list):
            cols[kk] = v
    if "d" not in cols or "c" not in cols:
        return []
    n = min(len(v) for v in cols.values())
    out = []
    for i in range(n):
        d = norm_date(cols["d"][i])
        o = to_float(cols.get("o", cols["c"])[i])
        h = to_float(cols.get("h", cols["c"])[i])
        l = to_float(cols.get("l", cols["c"])[i])
        c = to_float(cols["c"][i])
        v = to_float(cols.get("v", [0]*n)[i]) or 0
        if d and None not in (o, h, l, c):
            out.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out

def bars_from_any_json(obj, depth=0):
    """Walk any JSON shape and return the largest plausible bar list found."""
    if depth > 6 or obj is None:
        return []
    best = []
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            best = rows_from_records(obj)
        if not best:
            for item in obj[:50]:
                cand = bars_from_any_json(item, depth+1)
                if len(cand) > len(best):
                    best = cand
    elif isinstance(obj, dict):
        cand = rows_from_columns(obj)
        if len(cand) > len(best):
            best = cand
        for v in obj.values():
            cand = bars_from_any_json(v, depth+1)
            if len(cand) > len(best):
                best = cand
    return best


# ---------------------------------------------- SvelteKit __data.json decoder
def devalue_resolve(arr):
    """stockanalysis.com is SvelteKit: __data.json 'data' arrays are devalue-
    encoded (objects hold integer references into the same flat array).
    Reconstruct the real nested structure."""
    if not isinstance(arr, list) or not arr:
        return None
    def res(ref, depth, seen):
        if depth > 24:
            return None
        if isinstance(ref, int):
            if ref < 0 or ref >= len(arr) or ref in seen:
                return None
            node = arr[ref]
            seen = seen | {ref}
            if isinstance(node, dict):
                return {k: res(v, depth + 1, seen) for k, v in node.items()}
            if isinstance(node, list):
                if len(node) == 2 and node[0] in ("Date", "BigInt") and isinstance(node[1], (str, int, float)):
                    return node[1]
                return [res(x, depth + 1, seen) for x in node]
            return node
        return ref
    return res(0, 0, frozenset())

def fetch_sa_datajson(ticker, full, verbose):
    """OHLCV from the history page's SvelteKit data endpoint. Tries several
    range parameters and keeps whichever variant returns the most bars."""
    t = ticker.upper()
    base = f"https://stockanalysis.com/quote/msm/{t}/history/__data.json"
    variants = [
        base + "?range=max", base + "?range=MAX",
        base + "?range=max&x-sveltekit-invalidated=001",
        base + "?range=5Y", base + "?range=1Y",
        base + "?p=max", base + "?period=max",
        base,
    ] if full else [
        f"https://stockanalysis.com/quote/msm/{t}/history/__data.json",
    ]
    best = []
    for url in variants:
        try:
            if verbose:
                log(f"  trying __data.json: {url}")
            payload = json.loads(http_get(url))
            candidates = []
            for node in (payload.get("nodes") or []):
                if isinstance(node, dict) and isinstance(node.get("data"), list):
                    resolved = devalue_resolve(node["data"])
                    bars = bars_from_any_json(resolved)
                    if bars:
                        candidates.append(bars)
            if candidates:
                got = max(candidates, key=len)
                if verbose:
                    log(f"    -> {len(got)} bars")
                if len(got) > len(best):
                    best = got
                if not full and best:
                    break
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError, ValueError) as e:
            if verbose:
                log(f"    failed: {e}")
    return (best, "__data.json") if best else ([], None)

def fetch_sa_chart(ticker, full, verbose):
    """Documented /api/symbol endpoint. type=chart returns [epoch_ms, close]
    pairs — full multi-year history but CLOSE ONLY, so it is used strictly as
    a diagnostic/probe source, never for building tradeable OHLC candles."""
    for t in (f"msm-{ticker.upper()}", f"msm-{ticker.lower()}"):
        url = f"https://stockanalysis.com/api/symbol/s/{t}/history?type=chart"
        try:
            if verbose:
                log(f"  trying chart api: {url}")
            payload = json.loads(http_get(url))
            data = payload.get("data")
            if isinstance(data, list) and data and isinstance(data[0], list):
                if verbose:
                    log(f"    -> {len(data)} close-only points "
                        f"({norm_date(data[0][0])} .. {norm_date(data[-1][0])})")
                return data, "chart-close-only"
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError, ValueError) as e:
            if verbose:
                log(f"    failed: {e}")
    return [], None

# ---------------------------------------------- MarketWatch / WSJ adapter
def _parse_dl_csv(text):
    """Parse the MarketWatch/WSJ historical-prices CSV download format."""
    import csv as _csv, io
    rows = list(_csv.reader(io.StringIO(text), skipinitialspace=True))
    if len(rows) < 2:
        return []
    head = [h.strip().lower() for h in rows[0]]
    def col(name):
        for i, h in enumerate(head):
            if h.startswith(name):
                return i
        return -1
    iD, iO, iH, iL, iC = col("date"), col("open"), col("high"), col("low"), col("close")
    iV = col("volume")
    if min(iD, iO, iH, iL, iC) < 0:
        return []
    out = []
    for r in rows[1:]:
        if len(r) <= max(iD, iO, iH, iL, iC):
            continue
        d = norm_date(r[iD])
        o, h, l, c = (to_float(r[i]) for i in (iO, iH, iL, iC))
        v = to_float(r[iV]) if iV >= 0 else 0
        if d and None not in (o, h, l, c) and o > 0:
            out.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": v or 0})
    return out

def fetch_mw(ticker, full, verbose):
    """MarketWatch CSV download endpoint (country code om). Chunked by year
    on full runs because the endpoint caps rows per request."""
    from datetime import date, timedelta
    t = ticker.lower()
    end = date.today()
    start_year = end.year - 11 if full else None
    spans = []
    if full:
        y = start_year
        while y <= end.year:
            spans.append((date(y, 1, 1), min(date(y, 12, 31), end)))
            y += 1
    else:
        spans.append((end - timedelta(days=45), end))
    allbars = {}
    got_any = False
    for a, b in spans:
        url = (f"https://www.marketwatch.com/investing/stock/{t}/downloaddatapartial"
               f"?startdate={a.strftime('%m/%d/%Y')}%2000:00:00"
               f"&enddate={b.strftime('%m/%d/%Y')}%2023:59:59"
               f"&daterange=d30&frequency=p1d&csvdownload=true"
               f"&downloadpartial=false&newdates=false&countrycode=om")
        try:
            if verbose:
                log(f"  trying marketwatch {a.year}: {url[:96]}...")
            bars = _parse_dl_csv(http_get(url))
            if bars:
                got_any = True
                for bb in bars:
                    allbars[bb["d"]] = bb
                if verbose:
                    log(f"    -> {len(bars)} bars")
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            if verbose:
                log(f"    failed: {e}")
        time.sleep(0.4)
    if not got_any:
        return [], None
    merged = [allbars[d] for d in sorted(allbars)]
    return merged, "marketwatch"

# ------------------------------------------------- stockanalysis adapters
def fetch_sa_api(ticker, full, verbose):
    rng = "max" if full else "6M"
    for url in (
        f"https://stockanalysis.com/api/symbol/s/msm-{ticker}/history?range={rng}&period=Daily",
        f"https://stockanalysis.com/api/symbol/s/msm-{ticker}/history?range={'10Y' if full else '6M'}",
    ):
        try:
            if verbose:
                log(f"  trying API: {url}")
            payload = json.loads(http_get(url))
            bars = bars_from_any_json(payload)
            if bars:
                return bars, "api"
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            if verbose:
                log(f"  api failed: {e}")
    return [], None

def fetch_sa_sveltekit(ticker, full, verbose):
    url = f"https://stockanalysis.com/quote/msm/{ticker}/history/__data.json"
    if full:
        url += "?range=max"
    try:
        if verbose:
            log(f"  trying __data.json: {url}")
        payload = json.loads(http_get(url))
        bars = bars_from_any_json(payload)
        if bars:
            return bars, "__data.json"
    except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
        if verbose:
            log(f"  __data.json failed: {e}")
    return [], None

def fetch_sa_html(ticker, full, verbose):
    """Last resort: parse the server-rendered history table (recent months only)."""
    url = f"https://stockanalysis.com/quote/msm/{ticker}/history/"
    try:
        if verbose:
            log(f"  trying HTML table: {url}")
        html = http_get(url)
    except (HTTPError, URLError, TimeoutError) as e:
        if verbose:
            log(f"  html failed: {e}")
        return [], None
    bars = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 6:
            continue
        d = norm_date(cells[0])
        if not d:
            continue
        o, h, l, c = (to_float(cells[i]) for i in range(1, 5))
        v = to_float(cells[-1]) or 0
        if None not in (o, h, l, c):
            bars.append({"d": d, "o": o, "h": h, "l": l, "c": c, "v": v})
    return bars, ("html" if bars else None)

def fetch_stockanalysis(ticker, full, verbose):
    # Full runs: chase deep history first, keep the richest result.
    # Daily runs: the light __data.json call is enough (recent bars merge in).
    order = ((fetch_mw, fetch_sa_datajson, fetch_sa_api, fetch_sa_sveltekit, fetch_sa_html)
             if full else
             (fetch_sa_datajson, fetch_mw, fetch_sa_api, fetch_sa_sveltekit, fetch_sa_html))
    best, best_how = [], None
    for fn in order:
        bars, how = fn(ticker, full, verbose)
        if len(bars) > len(best):
            best, best_how = bars, how
        if best and not full:
            break
        if len(best) > 400:          # deep history secured; stop probing
            break
    return (best, best_how) if best else ([], None)


# ------------------------------------------------- TradingView adapter
TV_MAP = {}          # NAME -> TradingView ticker, from `NAME=TVTICKER` lines
TV_EXCHANGES = ["MSX", "MSM"]   # candidate TradingView exchange codes for Muscat
_TV = None
_TV_EXCH = None      # resolved exchange code, cached after first success

def _tv_session():
    global _TV
    if _TV is not None:
        return _TV
    try:
        try:
            from tvDatafeed import TvDatafeed, Interval
        except ImportError:
            log("Installing tvdatafeed library (one-time)...")
            import subprocess
            for src in ("git+https://github.com/rongardF/tvdatafeed.git", "tvdatafeed"):
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", src],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    log(f"  pip install {src} failed: {r.stderr.strip()[-200:]}")
                try:
                    from tvDatafeed import TvDatafeed, Interval  # noqa: F401
                    break
                except ImportError:
                    continue
            from tvDatafeed import TvDatafeed, Interval
        user = os.environ.get("TV_USERNAME")
        pw = os.environ.get("TV_PASSWORD")
        if user and pw:
            tv = TvDatafeed(user, pw)
            log("TradingView session: logged in as " + user)
        else:
            tv = TvDatafeed()
            log("TradingView session: anonymous (delayed data). If fetches fail, add "
                "TV_USERNAME / TV_PASSWORD secrets in repo Settings for a logged-in session.")
        _TV = (tv, Interval)
    except Exception as e:
        log(f"TradingView session FAILED: {type(e).__name__}: {e}")
        _TV = (None, None)
    return _TV

def _tv_try(tv, Interval, sym, exch, n):
    try:
        df = tv.get_hist(symbol=sym, exchange=exch,
                         interval=Interval.in_daily, n_bars=n)
        if df is not None and not getattr(df, "empty", True):
            return df, None
        return None, None
    except Exception as e:
        return None, e

def fetch_tradingview(ticker, full, verbose):
    global _TV_EXCH
    tv, Interval = _tv_session()
    if tv is None:
        return [], None
    sym = TV_MAP.get(ticker, ticker)
    n = 5000 if full else 30
    df, last_err = None, None

    # 1) direct attempts on candidate exchange codes (resolved one first)
    exchanges = [_TV_EXCH] if _TV_EXCH else TV_EXCHANGES
    for exch in exchanges:
        for attempt in range(2):
            df, err = _tv_try(tv, Interval, sym, exch, n)
            if df is not None:
                if _TV_EXCH is None:
                    _TV_EXCH = exch
                    log(f"TradingView exchange code resolved: {exch}")
                break
            if err is not None:
                last_err = err
            time.sleep(1 + attempt)
        if df is not None:
            break

    # 2) last resort: ask TradingView's symbol search where this ticker lives
    if df is None and hasattr(tv, "search_symbol"):
        try:
            results = tv.search_symbol(sym, "") or []
            for r in results:
                rex = str(r.get("exchange", "")).upper()
                if "oman" in str(r).lower() or rex in ("MSX", "MSM"):
                    sym2 = str(r.get("symbol", sym)).upper()
                    df, err = _tv_try(tv, Interval, sym2, rex, n)
                    if df is not None:
                        log(f"{ticker}: resolved via search -> {rex}:{sym2}")
                        _TV_EXCH = _TV_EXCH or rex
                        if sym2 != sym:
                            TV_MAP[ticker] = sym2
                        break
        except Exception:
            pass

    if df is None:
        why = f"{type(last_err).__name__}: {last_err}" if last_err else "empty response"
        log(f"{ticker}: TradingView returned nothing ({why})")
        return [], None
    bars = []
    for idx, row in df.iterrows():
        try:
            bars.append({"d": idx.strftime("%Y-%m-%d"),
                         "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]),  "c": float(row["close"]),
                         "v": float(row.get("volume", 0) or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return (bars, "tradingview") if bars else ([], None)

def fetch_tv_then_sa(ticker, full, verbose):
    bars, how = fetch_tradingview(ticker, full, verbose)
    if bars:
        return bars, how
    log(f"{ticker}: WARNING — using stockanalysis fallback (short history, rounded prices)")
    return fetch_stockanalysis(ticker, full, verbose)


# ------------------------------------------------- CSV read/merge/write
def csv_path(ticker):
    return os.path.join(DATA_DIR, f"{ticker}.csv")

def read_existing(ticker):
    path = csv_path(ticker)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = norm_date(row.get("Date"))
            if d:
                out[d] = row
    return out

def write_merged(ticker, bars):
    existing = read_existing(ticker)
    added = 0
    for b in bars:
        key = b["d"]
        row = {"Date": key, "Open": f"{b['o']:.6g}", "High": f"{b['h']:.6g}",
               "Low": f"{b['l']:.6g}", "Close": f"{b['c']:.6g}",
               "Volume": f"{int(b['v'])}"}
        if key not in existing:
            added += 1
        existing[key] = row
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(csv_path(ticker), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Date","Open","High","Low","Close","Volume"])
        w.writeheader()
        for d in sorted(existing):
            w.writerow(existing[d])
    return added, len(existing)


# ------------------------------------------------- main
def load_symbols():
    if not os.path.exists(SYMBOLS_FILE):
        with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
            f.write("# One MSX ticker per line, exactly as it appears in the\n"
                    "# stockanalysis.com URL: stockanalysis.com/quote/msm/<TICKER>/\n"
                    "# (Find your stock there, copy the ticker from the address bar.)\n"
                    "OQGN\nOQEP\nOQBI\nASYAD\nOMRF\n")
        log(f"Created {SYMBOLS_FILE} with a starter list — edit it, then re-run.")
    out = []
    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" in ln:
                name, tvsym = ln.split("=", 1)
                name = name.strip().upper()
                TV_MAP[name] = tvsym.strip().upper()
                out.append(name)
            else:
                out.append(ln.upper())
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="pull maximum history")
    ap.add_argument("--check", metavar="TICKER", help="verbose test of one symbol")
    ap.add_argument("--source", choices=["sa", "tv"], default="sa",
                    help="sa = stockanalysis.com (default; TradingView has no MSX coverage)")
    ap.add_argument("--probe", metavar="TICKER",
                    help="test every adapter for one symbol and report row counts")
    args = ap.parse_args()

    if args.probe:
        t = args.probe.upper()
        log(f"PROBE {t} — testing every adapter:")
        for fn in (fetch_mw, fetch_sa_datajson, fetch_sa_api, fetch_sa_sveltekit, fetch_sa_html, fetch_sa_chart):
            try:
                bars, how = fn(t, True, True)
                if bars and how != "chart-close-only":
                    b0, b1 = bars[0], bars[-1]
                    log(f"  {fn.__name__}: {len(bars)} OHLCV bars "
                        f"({b0['d']} {b0['o']}/{b0['h']}/{b0['l']}/{b0['c']} .. {b1['d']} c={b1['c']})")
                elif bars:
                    log(f"  {fn.__name__}: {len(bars)} close-only points (diagnostic)")
                else:
                    log(f"  {fn.__name__}: nothing")
            except Exception as e:
                log(f"  {fn.__name__}: ERROR {type(e).__name__}: {e}")
        return

    fetch = fetch_stockanalysis if args.source == "sa" else fetch_tv_then_sa
    symbols = [args.check.upper()] if args.check else load_symbols()
    verbose = bool(args.check)

    ok, failed, stale, degraded = 0, [], [], []
    today = datetime.now().strftime("%Y-%m-%d")
    for i, t in enumerate(symbols):
        try:
            bars, how = fetch(t, args.full or args.check, verbose)
        except Exception as e:                      # keep the batch alive
            bars, how = [], None
            if verbose:
                log(f"  unexpected error: {e}")
        if not bars:
            failed.append(t)
            log(f"{t}: FAILED — no data from any adapter")
        else:
            bars.sort(key=lambda b: b["d"])
            added, total = write_merged(t, bars)
            last = bars[-1]["d"]
            fresh = "" if last >= today else f"  (latest bar {last} — source may lag)"
            if fresh:
                stale.append(t)
            log(f"{t}: +{added} new bars via {how}, {total} total, last {last}{fresh}")
            if how == "html":
                degraded.append(t)
            ok += 1
        if i < len(symbols) - 1:
            time.sleep(random.uniform(*SLEEP_RANGE))

    log(f"Done: {ok} updated, {len(failed)} failed."
        + (f" Failed: {', '.join(failed)}" if failed else ""))
    if degraded:
        log(f"*** {len(degraded)} symbols used the DEGRADED fallback source (rounded, ~50 bars): "
            f"{', '.join(degraded[:10])}{'…' if len(degraded) > 10 else ''}")
        log("*** Fix: run `python msx_fetch.py --probe <TICKER>` (or a workflow run) and "
            "send the log output to diagnose which endpoint changed.")
    if stale:
        log(f"Note: {len(stale)} symbols not yet showing today's session — "
            "the source updates after the close; re-run later or schedule for the evening.")
    if failed and not args.check:
        log(f"Debug a failure with: python msx_fetch.py --check {failed[0]}")

if __name__ == "__main__":
    main()
