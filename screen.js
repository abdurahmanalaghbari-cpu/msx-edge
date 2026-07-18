#!/usr/bin/env node
/* screen.js — runs every saved champion against the latest data and generates
   the static site: site/index.html (mobile tape), site/workspace.json (auto-
   loaded by the full tool at site/tool.html). Run by GitHub Actions daily. */
const fs = require('fs');
const path = require('path');
const E = require('./engine.js');

const DATA_DIR = path.join(__dirname, 'data');
const SITE_DIR = path.join(__dirname, 'site');
const CHAMPS_FILE = path.join(__dirname, 'champions.json');

/* ---------- load ---------- */
const champions = fs.existsSync(CHAMPS_FILE)
  ? JSON.parse(fs.readFileSync(CHAMPS_FILE, 'utf8'))
  : {};
if (!Object.keys(champions).length)
  console.log('No champions.json yet — publishing setup page + data-loaded tool.');
const symbols = {};
if (fs.existsSync(DATA_DIR)) {
  for (const f of fs.readdirSync(DATA_DIR)) {
    if (!/\.csv$/i.test(f)) continue;
    try {
      const { symbol, bars } = E.parseCSV(fs.readFileSync(path.join(DATA_DIR, f), 'utf8'), f);
      symbols[symbol] = { bars };
    } catch (e) { console.error(f + ': ' + e.message); }
  }
}
console.log(Object.keys(symbols).length + ' symbols loaded, ' +
            Object.keys(champions).length + ' champions.');

/* ---------- screen (mirrors screenSymbol in the tool) ---------- */
function screenSymbol(sym) {
  const ch = champions[sym], rec = symbols[sym];
  if (!ch || !rec || rec.bars.length < 30) return null;
  const bars = rec.bars, n = bars.length, L = n - 1;
  const def = E.STRATS[ch.strategy];
  const sig = def.fn(bars, ch.params);
  const last = bars[L], prev = bars[L - 1];
  const fmt3 = x => (+x).toFixed(3);
  if (def.kind === 'bracket') {
    const bt = E.backtestBracket(bars, sig.entries, ch.params, ch.costs || { comm: 0.3, slip: 0.1 });
    let signal = 'NONE', detail = '';
    if (bt.open) {
      if (bt.open.timeDue || bt.open.hold >= ch.params.hold) {
        signal = 'EXIT'; detail = `Time stop reached (${bt.open.hold} bars) — sell at next open`;
      } else {
        signal = 'HOLD';
        detail = `In position ${bt.open.hold} bars · TP <b>${fmt3(bt.open.tpPx)}</b> / SL <b>${fmt3(bt.open.slPx)}</b> working · P&L <b class="${bt.open.r >= 0 ? 'up' : 'down'}">${(bt.open.r * 100).toFixed(1)}%</b>`;
      }
    } else if (sig.entries[L]) {
      signal = 'BUY';
      detail = `Buy at next open · TP +${ch.params.tp}% / SL −${ch.params.sl}% (≈ ${fmt3(last.c * (1 + ch.params.tp / 100))} / ${fmt3(last.c * (1 - ch.params.sl / 100))}) · time stop ${ch.params.hold} bars`;
    } else if (sig.trigger != null && sig.trigger > 0) {
      const dist = (sig.trigger - last.c) / last.c * 100;
      if (dist > 0 && dist <= 1.5) {
        signal = 'NEAR'; detail = `Breakout arms above <b>${fmt3(sig.trigger)}</b> (${dist.toFixed(1)}% away)`;
      }
    }
    return { sym, signal, detail, last, prev, ch, bars };
  }
  const bt = E.backtest(bars, sig.pos, ch.costs || { comm: 0.3, slip: 0.1 });
  let signal = 'NONE', detail = '';
  if (sig.pos[L] === 1 && (L === 0 || sig.pos[L - 1] === 0)) {
    signal = 'BUY'; detail = `Signal on ${last.d} close <b>${last.c.toFixed(3)}</b> — buy at next open`;
  } else if (sig.pos[L] === 0 && sig.pos[L - 1] === 1) {
    signal = 'EXIT'; detail = `Exit fired on ${last.d} — sell at next open`;
  } else if (sig.pos[L] === 1) {
    signal = 'HOLD';
    const o = bt.open;
    detail = o ? `In position ${o.hold} bars · open P&L <b class="${o.r >= 0 ? 'up' : 'down'}">${(o.r * 100).toFixed(1)}%</b>` : 'In position';
  } else if (sig.trigger != null && sig.trigger > 0) {
    const dist = (sig.trigger - last.c) / last.c * 100;
    const armed = ch.strategy !== 'lvrb' || sig.squeezed;
    if (armed && dist > 0 && dist <= 1.5) {
      signal = 'NEAR'; detail = `Breakout arms above <b>${sig.trigger.toFixed(3)}</b> (${dist.toFixed(1)}% away)`;
    }
  }
  return { sym, signal, detail, last, prev, ch, bars };
}

const order = { BUY: 0, EXIT: 1, NEAR: 2, HOLD: 3, NONE: 4 };
const rows = Object.keys(champions).sort().map(screenSymbol).filter(Boolean)
  .sort((a, b) => order[a.signal] - order[b.signal] || a.sym.localeCompare(b.sym));

const latest = rows.reduce((m, r) => r.last.d > m ? r.last.d : m, '');
const active = rows.filter(r => r.signal !== 'NONE').length;
const staleDays = latest ? Math.floor((Date.now() - new Date(latest + 'T12:00:00Z')) / 864e5) : 99;

/* ---------- render helpers ---------- */
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function spark(bars) {
  const N = Math.min(bars.length, 40);
  const cs = bars.slice(-N).map(b => b.c);
  const mn = Math.min(...cs), mx = Math.max(...cs), rg = (mx - mn) || 1;
  const pts = cs.map((c, i) => `${(i / (N - 1) * 96 + 2).toFixed(1)},${(30 - (c - mn) / rg * 26).toFixed(1)}`).join(' ');
  const col = cs[N - 1] >= cs[0] ? '#3BA776' : '#D2545E';
  return `<svg viewBox="0 0 100 32" class="spark"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5"/></svg>`;
}
const pct = (x, d = 1) => x == null || isNaN(x) ? '—' : (x >= 0 ? '+' : '') + x.toFixed(d) + '%';
const pf = x => x === Infinity || x === null || x === undefined ? (x === Infinity ? '∞' : '—') : (+x).toFixed(2);
const genAt = new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC';

const cards = Object.keys(champions).length === 0
  ? `<div class="quiet">Data is loaded — now teach it your edges.<br><br>
     <span>Open the full tool below, run the Optimizer, save champions,<br>
     export champions.json from the Strategies tab, and upload it to the repo.<br>
     The next run turns this page into your daily tape.</span></div>`
  : rows.filter(r => r.signal !== 'NONE').map(r => card(r)).join('') ||
  `<div class="quiet">Quiet tape — no setups for the next session.<br><span>${rows.length} stocks scanned</span></div>`;
const quietCards = rows.filter(r => r.signal === 'NONE').map(r => card(r)).join('');

function card(r) {
  const chg = r.prev ? (r.last.c / r.prev.c - 1) * 100 : null;
  const oos = r.ch.stats && r.ch.stats.oos;
  return `<div class="card">
  <div class="c1"><div class="sym">${esc(r.sym)}</div>
    <div class="px">${r.last.c.toFixed(3)} <span class="${chg >= 0 ? 'up' : 'down'}">${pct(chg)}</span></div></div>
  <div class="chip ${r.signal}">${r.signal === 'NONE' ? '—' : r.signal}</div>
  ${spark(r.bars)}
  <div class="detail">${r.detail || 'No setup'}</div>
  <div class="strat">${esc(E.STRATS[r.ch.strategy].label)} · ${esc(E.STRATS[r.ch.strategy].fmt(r.ch.params))}${
    oos ? ` &nbsp;·&nbsp; OOS ${pct(oos.netPct)} · PF ${pf(oos.pf)} · ${oos.trades} tr` : ''}</div>
</div>`;
}

const staleBanner = staleDays > 4
  ? `<div class="stale">⚠ Latest bar is ${latest} (${staleDays} days old) — data source may be lagging. Signals below are based on the last available session.</div>` : '';

const html = `<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0D1014">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>MSX EDGE — Tomorrow's Tape</title>
<style>
:root{--bg:#0D1014;--panel:#151A21;--panel2:#1B222B;--line:#242D38;--ink:#E9E4D6;--mut:#8B93A3;
--brass:#C9A227;--up:#3BA776;--down:#D2545E;--blue:#5B8DBE;
--mono:ui-monospace,"SF Mono",Consolas,monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font-family:-apple-system,"Segoe UI",Roboto,sans-serif;padding:16px 14px 60px;max-width:560px;margin:0 auto}
h1{font-family:var(--mono);font-size:16px;letter-spacing:.24em;color:var(--brass);font-weight:700;margin-bottom:2px}
.sub{font-family:var(--mono);font-size:11px;color:var(--mut);margin-bottom:14px}
.stale{background:#D2545E22;border:1px solid #D2545E88;border-radius:8px;padding:10px 12px;font-size:12.5px;margin-bottom:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px;
display:grid;grid-template-columns:1fr 76px 100px;gap:6px 10px;align-items:center}
.sym{font-family:var(--mono);font-weight:700;font-size:16px;letter-spacing:.06em}
.px{font-family:var(--mono);font-size:12px;color:var(--mut)}
.chip{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.1em;padding:6px 0;border-radius:5px;text-align:center;border:1px solid var(--line);color:var(--mut)}
.chip.BUY{border-color:var(--up);color:var(--up);background:#3BA77622}
.chip.EXIT{border-color:var(--down);color:var(--down);background:#D2545E22}
.chip.HOLD{border-color:var(--blue);color:var(--blue);background:#5B8DBE22}
.chip.NEAR{border-color:var(--brass);color:var(--brass);background:#C9A22722}
.spark{width:100px;height:32px}
.detail{grid-column:1/-1;font-size:13px;color:var(--mut)}
.detail b{color:var(--ink);font-family:var(--mono)}
.strat{grid-column:1/-1;font-family:var(--mono);font-size:10.5px;color:var(--mut);border-top:1px solid var(--line);padding-top:7px}
.up{color:var(--up)}.down{color:var(--down)}
.quiet{text-align:center;color:var(--mut);padding:40px 0;font-size:15px}
.quiet span{font-size:12px;font-family:var(--mono)}
details{margin-top:16px}summary{color:var(--mut);font-size:12.5px;cursor:pointer;padding:6px 0}
.foot{margin-top:22px;text-align:center;font-size:11px;color:#5C6572;font-family:var(--mono);line-height:2}
.foot a{color:var(--brass);text-decoration:none;border:1px solid #C9A22755;border-radius:6px;padding:6px 14px;display:inline-block;margin-top:6px}
</style></head><body>
<h1>TOMORROW'S TAPE</h1>
<div class="sub">MSX·EDGE · data through ${latest || '—'} · ${active} active of ${rows.length} tracked · built ${genAt}</div>
${staleBanner}
${cards}
${quietCards ? `<details><summary>${rows.length - active} stocks with no setup</summary>${quietCards}</details>` : ''}
<div class="foot">signals fill at next session's open · long-only · simulation, not a promise<br>
<a href="tool.html">Open full MSX EDGE tool →</a></div>
</body></html>`;

/* ---------- write site ---------- */
fs.mkdirSync(SITE_DIR, { recursive: true });
fs.writeFileSync(path.join(SITE_DIR, 'index.html'), html);
fs.writeFileSync(path.join(SITE_DIR, 'workspace.json'),
  JSON.stringify({ symbols, champions, generated: genAt }));
if (fs.existsSync(path.join(__dirname, 'msx_edge.html')))
  fs.copyFileSync(path.join(__dirname, 'msx_edge.html'), path.join(SITE_DIR, 'tool.html'));
fs.writeFileSync(path.join(SITE_DIR, '.nojekyll'), '');
console.log(`site/index.html written — ${active} active signal(s), data through ${latest}.`);
