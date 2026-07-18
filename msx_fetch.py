#!/usr/bin/env python3
"""
MSX EDGE companion fetcher
--------------------------
Pulls end-of-day OHLCV for every symbol in symbols.txt and writes/updates
one CSV per stock (generic format: Date,Open,High,Low,Close,Volume) into ./data.
MSX EDGE ingests that folder with one click via "Sync folder now".

Source: stockanalysis.com (data by S&P Global, updated daily after the close).
This uses the site's public pages/endpoints, which are unofficial — if the site
changes its layout the adapter may need a small fix. Be polite: this script
sleeps between symbols and is meant to run ONCE per day.

Usage:
  python msx_fetch.py              # daily update (recent bars, merges into CSVs)
  python msx_fetch.py --full       # first run: pull maximum history
  python msx_fetch.py --check OQGN # verbose test of a single symbol
  python msx_fetch.py --source tv  # alternative: TradingView via tvdatafeed
                                   #   (pip install tvdatafeed; uses MSX: prefix)

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
SLEEP_RANGE = (1.5, 3.0)   # polite delay between symbols, seconds

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
    for fn in (fetch_sa_api, fetch_sa_sveltekit, fetch_sa_html):
        bars, how = fn(ticker, full, verbose)
        if bars:
            return bars, how
    return [], None


# ------------------------------------------------- TradingView adapter
def fetch_tradingview(ticker, full, verbose):
    try:
        from tvDatafeed import TvDatafeed, Interval
    except ImportError:
        log("tvdatafeed not installed. Run: pip install tvdatafeed")
        sys.exit(1)
    tv = TvDatafeed()   # anonymous works for delayed data; or TvDatafeed(user, pw)
    n = 5000 if full else 60
    df = tv.get_hist(symbol=ticker, exchange="MSX",
                     interval=Interval.in_daily, n_bars=n)
    if df is None or df.empty:
        return [], None
    bars = []
    for idx, row in df.iterrows():
        bars.append({"d": idx.strftime("%Y-%m-%d"),
                     "o": float(row["open"]), "h": float(row["high"]),
                     "l": float(row["low"]),  "c": float(row["close"]),
                     "v": float(row.get("volume", 0) or 0)})
    return bars, "tradingview"


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
    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        return [ln.strip().upper() for ln in f
                if ln.strip() and not ln.strip().startswith("#")]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="pull maximum history")
    ap.add_argument("--check", metavar="TICKER", help="verbose test of one symbol")
    ap.add_argument("--source", choices=["sa", "tv"], default="sa",
                    help="sa = stockanalysis.com (default), tv = TradingView")
    args = ap.parse_args()

    fetch = fetch_stockanalysis if args.source == "sa" else fetch_tradingview
    symbols = [args.check.upper()] if args.check else load_symbols()
    verbose = bool(args.check)

    ok, failed, stale = 0, [], []
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
            ok += 1
        if i < len(symbols) - 1:
            time.sleep(random.uniform(*SLEEP_RANGE))

    log(f"Done: {ok} updated, {len(failed)} failed."
        + (f" Failed: {', '.join(failed)}" if failed else ""))
    if stale:
        log(f"Note: {len(stale)} symbols not yet showing today's session — "
            "the source updates after the close; re-run later or schedule for the evening.")
    if failed and not args.check:
        log(f"Debug a failure with: python msx_fetch.py --check {failed[0]}")

if __name__ == "__main__":
    main()
