#!/usr/bin/env python3
"""
MSX Dividend Desk — daily fetcher
---------------------------------
Runs in the same GitHub Actions job as msx_fetch.py. Standard library only.

Writes (all inside ./data, committed by the workflow):
  dividends_auto.csv   dividend history + declared upcoming dividends, per symbol
  div_news.json        dividend-related headlines from the last ~120 days, tagged by source reliability
  names.csv            ticker → company name (+ your own aliases, e.g. Arabic names). Edit freely.

Your hand-entered rows live in data/dividends_manual.csv and always win over auto data.

Sources
  1. stockanalysis.com dividend pages (structured: ex-date, amount, record, pay, declaration)
  2. Google News RSS searches (English + Arabic) — only used for the news feed and to flag
     fresh board proposals / AGM approvals. Each item is tagged:
       A = official (msx.om, fsa.gov.om, ONA)   B = established Omani/GCC financial press
       C = everything else (hidden by default in the page)
Both are unofficial public endpoints; if one changes shape the script logs it and carries on.
"""
import csv, json, os, re, sys, time, html, datetime as dt
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
TODAY = dt.date.today().isoformat()

TIER_A = ['msx.om', 'fsa.gov.om', 'omannews.gov.om', 'cma.gov.om', 'mcd.gov.om']
TIER_B = ['omanobserver.om', 'timesofoman.com', 'muscatdaily.com', 'zawya.com', 'argaam.com',
          'mubasher.info', 'alroya.om', 'atheer.om', 'omandaily.om', 'shabiba.com', 'alwatan.com',
          'omaninfo.om', 'reuters.com', 'bloomberg.com', 'thenationalnews.com', 'gulfnews.com',
          'arabnews.com', 'asharqbusiness.com', 'cnbcarabia.com', 'aleqt.com']

NEWS_QUERIES = [
    ('en', 'Muscat Stock Exchange dividend'),
    ('en', 'MSX cash dividend SAOG'),
    ('en', 'SAOG board recommends cash dividend'),
    ('en', 'SAOG AGM approves dividend'),
    ('en', 'Oman SAOG interim dividend'),
    ('ar', 'توزيعات أرباح نقدية بورصة مسقط'),
    ('ar', 'ش.م.ع.ع توزيع أرباح نقدية على المساهمين'),
    ('ar', 'الجمعية العامة توزيع أرباح بورصة مسقط'),
]


def log(*a):
    print('[dividends]', *a, flush=True)


def get(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*',
                                                       'Accept-Language': 'en-US,en;q=0.9'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:
            if i == tries - 1:
                log('fetch failed', url, '→', e)
                return None
            time.sleep(2 + 3 * i)


def read_symbols():
    p = os.path.join(ROOT, 'symbols.txt')
    out = []
    if os.path.exists(p):
        for line in open(p, encoding='utf-8'):
            s = line.split('#')[0].strip().upper()
            if s:
                out.append(s)
    return out


# ---------------------------------------------------------------- SvelteKit __data.json
def devalue(arr):
    """Unflatten SvelteKit's devalue payload (array of values with index references)."""
    memo = {}

    def h(i):
        if not isinstance(i, int):
            return i
        if i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, dict):
            o = {}
            memo[i] = o
            for k, idx in v.items():
                o[k] = h(idx)
            return o
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in ('Date', 'BigInt', 'RegExp', 'Object'):
                return v[1] if len(v) > 1 else None
            out = []
            memo[i] = out
            out.extend(h(x) for x in v)
            return out
        return v
    return h(0)


DATE_RX = re.compile(r'^\d{4}-\d{2}-\d{2}')
EX_KEYS = ['exDate', 'ex_date', 'exdate', 'dt', 'date', 'ex']
AMT_KEYS = ['amt', 'amount', 'dividend', 'cash', 'value', 'adjAmt']
REC_KEYS = ['record', 'recordDate', 'record_date', 'rd']
PAY_KEYS = ['pay', 'payDate', 'paymentDate', 'pay_date', 'pd']
DECL_KEYS = ['decl', 'declared', 'declarationDate', 'declaration_date', 'announced']


def pick(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, '', 'n/a', '-'):
            return d[k]
    return None


def norm_date(v):
    if v is None:
        return None
    s = str(v).strip()
    if DATE_RX.match(s):
        return s[:10]
    for f in ('%b %d, %Y', '%B %d, %Y', '%m/%d/%Y', '%d/%m/%Y', '%d-%m-%Y', '%d %b %Y', '%d %B %Y'):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def find_div_rows(node, found):
    if isinstance(node, list) and node and all(isinstance(x, dict) for x in node[:5]):
        rows = []
        for d in node:
            ex = norm_date(pick(d, EX_KEYS))
            amt = pick(d, AMT_KEYS)
            try:
                amt = float(str(amt).replace(',', '').replace('OMR', '').strip()) if amt is not None else None
            except ValueError:
                amt = None
            if ex and amt is not None:
                rows.append({'ex': ex, 'amt': amt, 'record': norm_date(pick(d, REC_KEYS)),
                             'pay': norm_date(pick(d, PAY_KEYS)), 'ann': norm_date(pick(d, DECL_KEYS))})
        if len(rows) >= max(1, len(node) // 2):
            found.append(rows)
    if isinstance(node, dict):
        for v in node.values():
            find_div_rows(v, found)
    elif isinstance(node, list):
        for v in node:
            find_div_rows(v, found)


def from_data_json(sym):
    base = f'https://stockanalysis.com/quote/msm/{sym}/dividend/'
    for url in (base + '__data.json', base + '__data.json?x-sveltekit-invalidated=01'):
        txt = get(url)
        if not txt:
            continue
        try:
            j = json.loads(txt)
        except ValueError:
            continue
        found = []
        for node in j.get('nodes') or []:
            if isinstance(node, dict) and isinstance(node.get('data'), list):
                try:
                    find_div_rows(devalue(node['data']), found)
                except Exception as e:  # never let one symbol kill the run
                    log(sym, 'devalue error', e)
        if found:
            return max(found, key=len)
    return None


TR_RX = re.compile(r'<tr[^>]*>(.*?)</tr>', re.S | re.I)
TD_RX = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>', re.S | re.I)
TAG_RX = re.compile(r'<[^>]+>')


def from_html(sym):
    """Fallback: parse the visible dividend table. Also returns the company name from <title>."""
    txt = get(f'https://stockanalysis.com/quote/msm/{sym}/dividend/')
    if not txt:
        return None, None
    name = None
    m = re.search(r'<title>(.*?)</title>', txt, re.S | re.I)
    if m:
        t = html.unescape(m.group(1)).strip()
        name = re.split(r'\s*\((?:MSM|MSX|MUS)?[:\s]?' + sym + r'\)', t)[0].strip() or None
        if name and name.upper().startswith(sym):
            name = None
    rows = []
    header = None
    for tr in TR_RX.findall(txt):
        cells = [html.unescape(TAG_RX.sub('', c)).strip() for c in TD_RX.findall(tr)]
        if not cells:
            continue
        low = [c.lower() for c in cells]
        if any('ex-div' in c or 'ex div' in c for c in low):
            header = low
            continue
        if header and len(cells) == len(header):
            d = dict(zip(header, cells))
            ex = norm_date(next((d[k] for k in d if 'ex' in k), None))
            amt_s = next((d[k] for k in d if 'amount' in k or 'cash' in k), None)
            try:
                amt = float(re.sub(r'[^\d.]', '', amt_s)) if amt_s else None
            except ValueError:
                amt = None
            if ex and amt is not None:
                rows.append({'ex': ex, 'amt': amt,
                             'record': norm_date(next((d[k] for k in d if 'record' in k), None)),
                             'pay': norm_date(next((d[k] for k in d if 'pay' in k), None)),
                             'ann': norm_date(next((d[k] for k in d if 'decl' in k or 'announ' in k), None))})
    return rows or None, name


def last_close(sym):
    """Latest close from the repo's price files — used to sanity-check the dividend unit."""
    best = (None, None)
    for fn in os.listdir(DATA):
        if not fn.upper().startswith(sym) or not fn.lower().endswith('.csv') or not re.match(sym + r'(?![A-Z0-9])', fn.upper()):
            continue
        try:
            rows = list(csv.reader(open(os.path.join(DATA, fn), encoding='utf-8-sig')))
        except Exception:
            continue
        if len(rows) < 2:
            continue
        hdr = [h.strip().lower() for h in rows[0]]
        ci = hdr.index('close') if 'close' in hdr else (hdr.index('price') if 'price' in hdr else None)
        if ci is None:
            continue
        for r in rows[1:]:
            d = norm_date(r[0]) if r else None
            try:
                c = float(r[ci].replace(',', ''))
            except (ValueError, IndexError):
                continue
            if d and (best[0] is None or d > best[0]):
                best = (d, c)
    return best[1]


def fix_unit(amt, px):
    """stockanalysis reports OMR; guard against baiza-denominated values."""
    if px and amt > px * 2 and amt / 1000 < px:
        return round(amt / 1000, 6)
    return amt


# ---------------------------------------------------------------- news
def extract(title):
    t = title.lower()
    info = {}
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)\s*(?:cash\s+)?dividend', t) or \
        re.search(r'cash dividend[s]?\s*(?:of|at)\s*(\d+(?:\.\d+)?)\s*%', t)
    if m:
        info['cash_pct'] = float(m.group(1))            # % of par; MSX par is usually 100 baizas
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:baiza|bz)', t)
    if m:
        info['baizas'] = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*%\s*(?:stock|bonus)', t)
    if m:
        info['bonus_pct'] = float(m.group(1))
    m = re.search(r'(\d+(?:\.\d+)?)\s*[٪%]', title)
    if m and 'cash_pct' not in info and ('نقدية' in title or 'أرباح' in title):
        info['cash_pct'] = float(m.group(1))
    if re.search(r'recommend|propos|يوصي|توصية|مقترح', t):
        info['stage'] = 'proposed'
    elif re.search(r'approv|agm|general (assembly|meeting)|الجمعية|يوافق|وافق|اعتماد', t):
        info['stage'] = 'approved'
    elif re.search(r'\bpaid\b|payment|distribut|صرف', t):
        info['stage'] = 'payment'
    if re.search(r'interim|مرحلية', t):
        info['interim'] = True
    return info


def tier(domain):
    d = (domain or '').lower()
    if any(d.endswith(x) for x in TIER_A):
        return 'A'
    if any(d.endswith(x) for x in TIER_B):
        return 'B'
    return 'C'


def fetch_news():
    items = {}
    for lang, q in NEWS_QUERIES:
        params = {'q': q + ' when:120d', 'hl': 'en-OM' if lang == 'en' else 'ar',
                  'gl': 'OM', 'ceid': 'OM:en' if lang == 'en' else 'OM:ar'}
        xml = get('https://news.google.com/rss/search?' + urllib.parse.urlencode(params))
        if not xml:
            continue
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            log('rss parse error', q, e)
            continue
        for it in root.iter('item'):
            title = (it.findtext('title') or '').strip()
            src_el = it.find('source')
            src_name = src_el.text.strip() if src_el is not None and src_el.text else ''
            src_url = src_el.get('url') if src_el is not None else ''
            if src_name and title.endswith(' - ' + src_name):
                title = title[: -len(src_name) - 3]
            domain = urllib.parse.urlparse(src_url).netloc.replace('www.', '')
            pub = it.findtext('pubDate') or ''
            try:
                pub = dt.datetime.strptime(pub[:25].strip(), '%a, %d %b %Y %H:%M:%S').date().isoformat()
            except ValueError:
                pub = None
            key = re.sub(r'\W+', '', title.lower())[:120]
            if key in items:
                continue
            items[key] = {'title': title, 'link': it.findtext('link'), 'date': pub, 'source': src_name,
                          'domain': domain, 'tier': tier(domain), 'lang': lang, **extract(title)}
        time.sleep(1.5)
    return list(items.values())


def load_names():
    p = os.path.join(DATA, 'names.csv')
    names = {}
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding='utf-8-sig')):
            s = (r.get('symbol') or '').strip().upper()
            if s:
                names[s] = {'name': (r.get('name') or '').strip(), 'aliases': (r.get('aliases') or '').strip()}
    return names


def save_names(names):
    with open(os.path.join(DATA, 'names.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['symbol', 'name', 'aliases'])
        for s in sorted(names):
            w.writerow([s, names[s]['name'], names[s]['aliases']])


STOP = {'company', 'co', 'saog', 'saoc', 'the', 'and', 'of', 'oman', 'omani', 'group', 'holding', 'holdings',
        'international', 'national', 'services', 'limited', 'ltd', 'bank', 'insurance'}


def match_symbols(title, names):
    t = ' ' + re.sub(r'[^\w\u0600-\u06FF]+', ' ', title.lower()) + ' '
    hits = []
    for sym, n in names.items():
        cands = [sym.lower()] + [a.strip().lower() for a in n['aliases'].split('|') if a.strip()]
        words = [w for w in re.sub(r'[^\w]+', ' ', n['name'].lower()).split() if w not in STOP]
        if len(words) >= 2:
            cands.append(' '.join(words[:2]))
        elif len(words) == 1 and len(words[0]) >= 5:
            cands.append(words[0])
        if any((' ' + c + ' ') in t for c in cands if c):
            hits.append(sym)
    return hits


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA, exist_ok=True)
    syms = read_symbols()
    if not syms:
        log('symbols.txt is empty — nothing to do')
        return
    names = load_names()
    auto_path = os.path.join(DATA, 'dividends_auto.csv')
    old = {}
    if os.path.exists(auto_path):
        for r in csv.DictReader(open(auto_path, encoding='utf-8-sig')):
            old[(r['symbol'], r['ex_date'])] = r

    fresh, ok, fail = {}, 0, []
    for sym in syms:
        rows = from_data_json(sym)
        name = None
        if not rows or sym not in names or not names[sym]['name']:
            hrows, name = from_html(sym)
            rows = rows or hrows
        if name:
            names.setdefault(sym, {'name': '', 'aliases': ''})
            if not names[sym]['name']:
                names[sym]['name'] = name
        if not rows:
            fail.append(sym)
            continue
        ok += 1
        px = last_close(sym)
        for r in rows:
            amt = fix_unit(r['amt'], px)
            fresh[(sym, r['ex'])] = {'symbol': sym, 'ex_date': r['ex'], 'amount_omr': f'{amt:.6f}'.rstrip('0').rstrip('.'),
                                     'record_date': r.get('record') or '', 'pay_date': r.get('pay') or '',
                                     'announce_date': r.get('ann') or '', 'bonus_ratio': '', 'status': 'declared',
                                     'source': 'stockanalysis', 'fetched': TODAY}
        time.sleep(1.2)

    merged = dict(old)
    for k, v in fresh.items():
        if k in merged:
            v['fetched'] = merged[k].get('fetched') or TODAY   # keep first-seen date
        merged[k] = v
    with open(auto_path, 'w', newline='', encoding='utf-8') as f:
        cols = ['symbol', 'ex_date', 'amount_omr', 'record_date', 'pay_date', 'announce_date', 'bonus_ratio', 'status', 'source', 'fetched']
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for k in sorted(merged):
            w.writerow({c: merged[k].get(c, '') for c in cols})
    for s in syms:
        names.setdefault(s, {'name': '', 'aliases': ''})
    save_names(names)
    log(f'dividend history: {ok}/{len(syms)} symbols, {len(merged)} events total')
    if fail:
        log('no dividend data for:', ' '.join(fail), '(fine for non-payers; add rows to dividends_manual.csv if wrong)')

    news_path = os.path.join(DATA, 'div_news.json')
    prev = []
    if os.path.exists(news_path):
        try:
            prev = json.load(open(news_path, encoding='utf-8'))
        except ValueError:
            prev = []
    got = fetch_news()
    seen = {re.sub(r'\W+', '', n['title'].lower())[:120] for n in got}
    allnews = got + [n for n in prev if re.sub(r'\W+', '', n['title'].lower())[:120] not in seen]
    cutoff = (dt.date.today() - dt.timedelta(days=200)).isoformat()
    allnews = [n for n in allnews if (n.get('date') or TODAY) >= cutoff]
    for n in allnews:
        n['symbols'] = match_symbols(n['title'], names)
        n.setdefault('first_seen', TODAY)
    allnews.sort(key=lambda n: n.get('date') or '', reverse=True)
    json.dump(allnews[:400], open(news_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    log(f'news: {len(got)} fetched today, {len(allnews[:400])} kept, '
        f'{sum(1 for n in allnews if n["symbols"])} matched to a ticker')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:   # never break the daily price pipeline
        log('ERROR (pipeline continues):', repr(e))
        sys.exit(0)
