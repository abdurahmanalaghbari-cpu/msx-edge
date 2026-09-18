#!/usr/bin/env python3
"""
MSX Dividend Desk — page builder
Reads data/*.csv prices (investing.com + msx_fetch.py formats), data/dividends_auto.csv,
data/dividends_manual.csv, data/div_news.json, data/names.csv and writes site/dividends.html
with everything embedded (so the page works on your phone and offline).
"""
import csv, json, os, re, datetime as dt

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
SITE = os.path.join(ROOT, 'site')


def norm_date(s):
    s = (s or '').strip().strip('"')
    if re.match(r'^\d{4}-\d{2}-\d{2}', s):
        return s[:10]
    for f in ('%m/%d/%Y', '%b %d, %Y', '%B %d, %Y', '%d/%m/%Y', '%d-%m-%Y', '%Y/%m/%d', '%d %b %Y'):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def num(s):
    s = (s or '').strip().replace(',', '')
    if s in ('', '-', 'null', 'NaN'):
        return None
    mult = 1
    if s[-1:] in 'KMB':
        mult = {'K': 1e3, 'M': 1e6, 'B': 1e9}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def sym_of(fn):
    m = re.match(r'([A-Za-z0-9]+)', fn)
    return m.group(1).upper() if m else None


def read_prices(symbols):
    per = {}
    files = [f for f in os.listdir(DATA) if '.csv' in f.lower()]
    skip = {'dividends_auto.csv', 'dividends_manual.csv', 'names.csv'}
    # investing.com exports first, generic fetcher files last → fetcher wins on overlapping dates
    def order(f):
        try:
            head = open(os.path.join(DATA, f), encoding='utf-8-sig').readline().lower()
        except Exception:
            head = ''
        return (0 if 'price' in head and 'change' in head else 1, f)
    for fn in sorted((f for f in files if f.lower() not in skip), key=order):
        sym = sym_of(fn)
        if not sym or (symbols and sym not in symbols):
            continue
        try:
            rows = list(csv.reader(open(os.path.join(DATA, fn), encoding='utf-8-sig')))
        except Exception:
            continue
        if len(rows) < 2:
            continue
        h = [c.strip().lower().strip('"') for c in rows[0]]
        def col(*names):
            for n in names:
                if n in h:
                    return h.index(n)
            return None
        di, oi, ci, vi = col('date'), col('open'), col('close', 'price', 'adj close'), col('volume', 'vol.', 'vol')
        if di is None or ci is None:
            continue
        book = per.setdefault(sym, {})
        for r in rows[1:]:
            if len(r) <= max(di, ci):
                continue
            d, c = norm_date(r[di]), num(r[ci])
            if not d or not c:
                continue
            o = num(r[oi]) if oi is not None and oi < len(r) else None
            v = num(r[vi]) if vi is not None and vi < len(r) else None
            book[d] = (o or 0, c, v or 0)
    out = {}
    for sym, book in per.items():
        ds = sorted(book)
        out[sym] = {'d': ds, 'o': [round(book[d][0], 4) for d in ds], 'c': [round(book[d][1], 4) for d in ds],
                    'v': [int(book[d][2]) for d in ds]}
    return out


def read_events():
    ev = []
    ap = os.path.join(DATA, 'dividends_auto.csv')
    if os.path.exists(ap):
        for r in csv.DictReader(open(ap, encoding='utf-8-sig')):
            a = num(r.get('amount_omr'))
            if a is None:
                continue
            ev.append({'sym': r['symbol'].upper(), 'ex': norm_date(r['ex_date']), 'amt': a,
                       'bonus': num(r.get('bonus_ratio')) or 0, 'record': norm_date(r.get('record_date')),
                       'pay': norm_date(r.get('pay_date')), 'ann': norm_date(r.get('announce_date')),
                       'status': r.get('status') or '', 'src': r.get('source') or 'auto'})
    mp = os.path.join(DATA, 'dividends_manual.csv')
    manual = []
    if os.path.exists(mp):
        for r in csv.DictReader(open(mp, encoding='utf-8-sig')):
            sym = (r.get('symbol') or '').strip().upper()
            if not sym or sym.startswith('#'):
                continue
            par = num(r.get('par_baizas')) or 100
            amt = num(r.get('amount_omr'))
            if amt is None and num(r.get('amount_baizas')) is not None:
                amt = num(r.get('amount_baizas')) / 1000
            if amt is None and num(r.get('cash_pct')) is not None:
                amt = num(r.get('cash_pct')) / 100 * par / 1000
            manual.append({'sym': sym, 'ex': norm_date(r.get('ex_date')), 'record': norm_date(r.get('record_date')),
                           'amt': amt or 0, 'bonus': (num(r.get('bonus_pct')) or 0) / 100,
                           'ann': norm_date(r.get('announce_date')), 'status': (r.get('status') or 'manual').strip().lower(),
                           'src': 'manual', 'note': r.get('note') or ''})
    # manual rows replace auto rows for the same stock within ±10 days; status=cancel just deletes
    def near(a, b):
        da, db = a.get('ex') or a.get('record'), b.get('ex') or b.get('record')
        if not da or not db:
            return False
        return abs((dt.date.fromisoformat(da) - dt.date.fromisoformat(db)).days) <= 10
    for m in manual:
        ev = [e for e in ev if not (e['sym'] == m['sym'] and near(e, m))]
    ev += [m for m in manual if m['status'] != 'cancel']
    return [e for e in ev if e.get('ex') or e.get('record')]


def main():
    symbols = []
    sp = os.path.join(ROOT, 'symbols.txt')
    if os.path.exists(sp):
        symbols = [l.split('#')[0].strip().upper() for l in open(sp, encoding='utf-8') if l.split('#')[0].strip()]
    prices = read_prices(set(symbols))
    events = read_events()
    news = []
    npth = os.path.join(DATA, 'div_news.json')
    if os.path.exists(npth):
        try:
            news = json.load(open(npth, encoding='utf-8'))
        except ValueError:
            pass
    names = {}
    nm = os.path.join(DATA, 'names.csv')
    if os.path.exists(nm):
        for r in csv.DictReader(open(nm, encoding='utf-8-sig')):
            names[r['symbol'].upper()] = r.get('name') or ''
    payload = {'generated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%MZ'), 'prices': prices,
               'events': events, 'news': news, 'names': names}
    tpl = open(os.path.join(ROOT, 'dividends_app.html'), encoding='utf-8').read()
    eng = open(os.path.join(ROOT, 'div_engine.js'), encoding='utf-8').read()
    js = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
    out = tpl.replace('/*__ENGINE__*/', eng).replace('/*__PAYLOAD__*/null', js)
    os.makedirs(SITE, exist_ok=True)
    open(os.path.join(SITE, 'dividends.html'), 'w', encoding='utf-8').write(out)
    print(f'[div_build] {len(prices)} symbols, {len(events)} dividend events, {len(news)} news items → site/dividends.html '
          f'({len(out)//1024} KB)')


if __name__ == '__main__':
    main()
