/* MSX Dividend Desk — backtest engine
 * Pure functions, no DOM. Runs in the browser (window.DivEngine) and in Node (require).
 *
 * Price bars per symbol: { d:[YYYY-MM-DD], o:[], c:[], v:[] }  (UNADJUSTED prices, OMR)
 * Dividend events:       { sym, ex, amt (OMR cash/share), bonus (ratio, 0.1 = 10%), ann, record, pay, status, src }
 *
 * Timing conventions (conservative, end-of-day):
 *   exIdx       = first trading session on/after the ex-date (first day WITHOUT the dividend)
 *   last cum day = exIdx-1 → you must hold at that close to get the dividend
 *   Entry "E before"  = buy at the CLOSE of session exIdx-E       (E=1 → buy at the close of the last cum day)
 *   Entry "k after announcement" = buy at the CLOSE of session annIdx+k (k≥1: no same-day look-ahead)
 *   Exits: b1..b5 = sell at close 1..5 sessions before ex (NO dividend — pure run-up)
 *          xo = sell at the ex-day OPEN (dividend kept) · xc = ex-day close
 *          a1..a20 = sell at close 1..20 sessions after ex
 *   Costs: commission per side + slippage per side (illiquid market → be generous)
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.DivEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const ENTRY_EX = [1, 2, 3, 5, 7, 10, 15, 20, 30, 45, 60];
  const ENTRY_ANN = [1, 2, 3, 5, 10];
  const EXITS = ['b5', 'b3', 'b2', 'b1', 'xo', 'xc', 'a1', 'a2', 'a3', 'a5', 'a10', 'a20'];

  const DEFAULTS = {
    commission: 0.15,   // % per side
    slippage: 0.25,     // % per side
    divTax: 0,          // % withheld from cash dividends
    minTrades: 4,       // min events for a combo to be eligible as champion
    coverage: 0.6,      // combo must be tradable on ≥ this share of the stock's events
    minTrain: 3,        // walk-forward: events needed before the first out-of-sample test
    requireVolume: true,// skip entries on zero-volume sessions; push exits to next traded session
    priceMode: 'auto',  // 'auto' | 'raw' | 'adjusted'
    settleDays: 2,      // T+2 → ex-date = record date − 1 session (used when only a record date is known)
    studyPre: 60,
    studyPost: 20
  };

  // ---------- small utils ----------
  function lowerBound(arr, x) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < x) lo = m + 1; else hi = m; } return lo; }
  function toDate(s) { const [y, m, d] = s.split('-').map(Number); return new Date(Date.UTC(y, m - 1, d)); }
  function fmt(dt) { return dt.toISOString().slice(0, 10); }
  function dayDiff(a, b) { return Math.round((toDate(b) - toDate(a)) / 864e5); }
  // MSX trades Sun–Thu (Fri/Sat weekend). Public holidays are not modelled.
  function isTradingDay(dt) { const w = dt.getUTCDay(); return w !== 5 && w !== 6; }
  function shiftTradingDays(s, k) {
    const dt = toDate(s); const step = k < 0 ? -1 : 1; let left = Math.abs(k);
    while (left > 0) { dt.setUTCDate(dt.getUTCDate() + step); if (isTradingDay(dt)) left--; }
    return fmt(dt);
  }
  function tradingDaysBetween(a, b) { // sessions strictly after a up to and including b
    if (a === b) return 0;
    if (b < a) return -tradingDaysBetween(b, a);
    const dt = toDate(a), end = toDate(b); let n = 0;
    while (dt < end) { dt.setUTCDate(dt.getUTCDate() + 1); if (isTradingDay(dt)) n++; }
    return n;
  }
  function mean(a) { return a.length ? a.reduce((s, x) => s + x, 0) / a.length : NaN; }
  function median(a) { if (!a.length) return NaN; const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
  function stdev(a) { if (a.length < 2) return NaN; const m = mean(a); return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1)); }

  function exitLabel(code) {
    if (code === 'xo') return 'Sell at ex-day open';
    if (code === 'xc') return 'Sell at ex-day close';
    const n = +code.slice(1);
    return code[0] === 'b' ? `Sell ${n} session${n > 1 ? 's' : ''} before ex (no dividend)` : `Sell ${n} session${n > 1 ? 's' : ''} after ex`;
  }
  function entryLabel(anchor, k) {
    if (anchor === 'ann') return `Buy ${k} session${k > 1 ? 's' : ''} after the announcement`;
    return k === 1 ? 'Buy at the close of the last day before ex' : `Buy ${k} sessions before ex`;
  }
  function comboKey(anchor, k, x) { return anchor + ':' + k + ':' + x; }
  function parseKey(key) { const [anchor, k, x] = key.split(':'); return { anchor, k: +k, x }; }
  function describe(key) { const p = parseKey(key); return entryLabel(p.anchor, p.k) + ' · ' + exitLabel(p.x); }

  function stats(rets, days) {
    const n = rets.length;
    if (!n) return { n: 0 };
    const m = mean(rets), sd = stdev(rets), se = n > 1 ? sd / Math.sqrt(n) : Infinity;
    const avgDays = days ? mean(days) : NaN;
    return { n, mean: m, median: median(rets), sd, se, lb: m - se, win: rets.filter(r => r > 0).length / n,
      worst: Math.min(...rets), best: Math.max(...rets), avgDays, perDay: avgDays ? m / avgDays : NaN };
  }

  // ---------- event preparation ----------
  function mergeEvents(events, P) {
    const map = new Map();
    for (let e of events) {
      if (!e || !e.sym) continue;
      let ex = e.ex;
      if (!ex && e.record) ex = shiftTradingDays(e.record, -(P.settleDays - 1)), e = Object.assign({}, e, { exEstimated: true });
      if (!ex) continue;
      const k = e.sym + '|' + ex;
      const cur = map.get(k) || { sym: e.sym, ex, amt: 0, bonus: 0, ann: null, record: null, pay: null, status: e.status || '', src: [], exEstimated: !!e.exEstimated };
      cur.amt += +e.amt || 0; cur.bonus += +e.bonus || 0;
      cur.ann = cur.ann || e.ann || null; cur.record = cur.record || e.record || null; cur.pay = cur.pay || e.pay || null;
      if (e.src && !cur.src.includes(e.src)) cur.src.push(e.src);
      if (e.status) cur.status = e.status;
      map.set(k, cur);
    }
    return [...map.values()].sort((a, b) => a.ex < b.ex ? -1 : 1);
  }

  function detectAdjusted(bars, evs) {
    const ratios = [];
    for (const e of evs) {
      if (!(e.amt > 0) || e._exIdx < 1) continue;
      const i = e._exIdx, prev = bars.c[i - 1], op = bars.o[i] > 0 ? bars.o[i] : bars.c[i];
      if (prev > 0) ratios.push((prev - op) / e.amt);
    }
    const med = median(ratios);
    return { n: ratios.length, medianDropVsDividend: med, looksAdjusted: ratios.length >= 3 && med < 0.25 };
  }

  // ---------- one trade ----------
  function simTrade(bars, e, anchor, k, x, P, useVol, addDiv) {
    const n = bars.c.length, exIdx = e._exIdx;
    let ei = anchor === 'ex' ? exIdx - k : (e._annIdx == null ? -1 : e._annIdx + k);
    if (ei < 0 || ei >= exIdx) return null; // must be holding before ex for this playbook
    let xi, fld = 'c';
    if (x === 'xo') { xi = exIdx; fld = 'o'; }
    else if (x === 'xc') xi = exIdx;
    else if (x[0] === 'b') xi = exIdx - +x.slice(1);
    else xi = exIdx + +x.slice(1);
    if (xi <= ei || xi >= n) return null;
    if (useVol) {
      if (!(bars.v[ei] > 0)) return null;
      let t = 0; while (!(bars.v[xi] > 0) && xi < n - 1 && t < 5) { xi++; fld = 'c'; t++; }
    }
    const comm = P.commission / 100, slip = P.slippage / 100;
    const rawEntry = bars.c[ei];
    let rawExit = fld === 'o' ? bars.o[xi] : bars.c[xi];
    if (!(rawExit > 0)) rawExit = bars.c[xi];
    if (!(rawEntry > 0) || !(rawExit > 0)) return null;
    const entryPx = rawEntry * (1 + slip), exitPx = rawExit * (1 - slip);
    const gotDiv = ei <= exIdx - 1 && xi >= exIdx;
    const cash = gotDiv && addDiv ? e.amt * (1 - P.divTax / 100) : 0;
    const bonus = gotDiv && addDiv ? (e.bonus || 0) : 0;
    const exitValue = exitPx * (1 + bonus) * (1 - comm);
    const ret = (exitValue + cash) / (entryPx * (1 + comm)) - 1;
    return { sym: e.sym, ex: e.ex, entryDate: bars.d[ei], exitDate: bars.d[xi], entryPx: rawEntry, exitPx: rawExit,
      days: xi - ei, gotDiv, div: cash, ret, divRet: cash / (entryPx * (1 + comm)), priceRet: ret - cash / (entryPx * (1 + comm)),
      yieldAtEntry: e.amt / rawEntry };
  }

  function buildCombos(hasAnn) {
    const out = [];
    for (const k of ENTRY_EX) for (const x of EXITS) out.push(comboKey('ex', k, x));
    if (hasAnn) for (const k of ENTRY_ANN) for (const x of EXITS) out.push(comboKey('ann', k, x));
    return out;
  }

  function pickBest(combos, getRets, minN, minCover, total) {
    let best = null;
    for (const key of combos) {
      const r = getRets(key);
      const need = Math.max(minN, Math.ceil(minCover * total));
      if (r.rets.length < need) continue;
      const s = stats(r.rets, r.days);
      if (!best || s.lb > best.s.lb || (s.lb === best.s.lb && s.mean > best.s.mean)) best = { key, s };
    }
    return best;
  }

  function baselineHold(bars, H) { // unconditional H-session price return for this stock
    const r = [];
    for (let i = 0; i + H < bars.c.length; i += Math.max(1, Math.floor(H / 2))) if (bars.c[i] > 0) r.push(bars.c[i + H] / bars.c[i] - 1);
    return mean(r);
  }

  function eventStudy(bars, evs, P, addDiv) {
    const L = P.studyPre + P.studyPost + 1, sumP = new Array(L).fill(0), sumT = new Array(L).fill(0); let cnt = 0;
    for (const e of evs) {
      const i0 = e._exIdx - P.studyPre, i1 = e._exIdx + P.studyPost;
      if (i0 < 0 || i1 >= bars.c.length) continue;
      const base = bars.c[i0]; if (!(base > 0)) continue;
      for (let t = 0; t < L; t++) {
        const c = bars.c[i0 + t], after = addDiv && i0 + t >= e._exIdx;
        sumP[t] += c / base - 1;
        sumT[t] += (after ? (c * (1 + (e.bonus || 0)) + e.amt) : c) / base - 1;
      }
      cnt++;
    }
    return { n: cnt, pre: P.studyPre, post: P.studyPost, price: sumP.map(x => cnt ? x / cnt : NaN), total: sumT.map(x => cnt ? x / cnt : NaN) };
  }

  // ---------- per stock ----------
  function runStock(sym, bars, allEvents, P) {
    P = Object.assign({}, DEFAULTS, P || {});
    const n = bars.c.length;
    const lastDate = bars.d[n - 1];
    const evs = [], upcoming = [];
    for (const e0 of allEvents) {
      if (e0.sym !== sym) continue;
      const e = Object.assign({}, e0);
      if (e.ex > lastDate) { upcoming.push(e); continue; }
      const exIdx = lowerBound(bars.d, e.ex);
      if (exIdx >= n || exIdx < 1 || dayDiff(e.ex, bars.d[exIdx]) > 7) continue; // price gap around the event
      e._exIdx = exIdx;
      if (e.ann) { const a = lowerBound(bars.d, e.ann); e._annIdx = a < exIdx ? a : null; }
      evs.push(e);
    }
    const volShare = bars.v.filter(v => v > 0).length / Math.max(1, n);
    const useVol = P.requireVolume && volShare > 0.5;
    const adj = detectAdjusted(bars, evs);
    const addDiv = P.priceMode === 'raw' ? true : P.priceMode === 'adjusted' ? false : !adj.looksAdjusted;
    const hasAnn = evs.filter(e => e._annIdx != null).length >= 3;
    const combos = buildCombos(hasAnn);

    // trade matrix: combo → array aligned with evs
    const M = {};
    for (const key of combos) { const p = parseKey(key); M[key] = evs.map(e => simTrade(bars, e, p.anchor, p.k, p.x, P, useVol, addDiv)); }
    const collect = (key, upto) => { const rets = [], days = []; const arr = M[key]; const lim = upto == null ? arr.length : upto; for (let i = 0; i < lim; i++) if (arr[i]) { rets.push(arr[i].ret); days.push(arr[i].days); } return { rets, days }; };

    const table = {};
    for (const key of combos) { const r = collect(key); table[key] = stats(r.rets, r.days); }
    const champ = pickBest(combos, k => collect(k), P.minTrades, P.coverage, evs.length);
    const champEx = pickBest(combos.filter(k => k.startsWith('ex:')), k => collect(k), P.minTrades, P.coverage, evs.length);

    // expanding-window walk-forward (event by event)
    const oos = [];
    for (let i = P.minTrain; i < evs.length; i++) {
      const b = pickBest(combos, k => collect(k, i), Math.min(P.minTrades, i), P.coverage, i);
      if (!b) continue;
      const t = M[b.key][i];
      if (t) oos.push(Object.assign({ key: b.key }, t));
    }
    const oosStats = stats(oos.map(t => t.ret), oos.map(t => t.days));
    const champTrades = champ ? M[champ.key].filter(Boolean) : [];
    const base = champ ? baselineHold(bars, Math.max(1, Math.round(champ.s.avgDays))) : NaN;

    return { sym, bars: { first: bars.d[0], last: lastDate, n }, events: evs.map(e => ({ ex: e.ex, amt: e.amt, bonus: e.bonus, ann: e.ann, record: e.record, status: e.status, exEstimated: e.exEstimated,
        dropVsDiv: (e._exIdx > 0 && e.amt > 0) ? ((bars.c[e._exIdx - 1] - (bars.o[e._exIdx] > 0 ? bars.o[e._exIdx] : bars.c[e._exIdx])) / e.amt) : null,
        yieldAtCum: e.amt / bars.c[e._exIdx - 1] })),
      upcoming, adj, addDiv, useVol, hasAnn, combos, table, champ, champEx, champTrades, oos, oosStats, baseline: base,
      study: eventStudy(bars, evs, P, addDiv), confidence: confidenceOf(evs.length, oosStats.n) , _M: M };
  }

  function confidenceOf(nEv, nOos) {
    if (nEv >= 10 && nOos >= 6) return 'usable';
    if (nEv >= 6) return 'thin';
    return 'anecdotal';
  }

  // ---------- whole market ----------
  function runAll(prices, events, P) {
    P = Object.assign({}, DEFAULTS, P || {});
    const merged = mergeEvents(events, P);
    const per = {};
    for (const sym of Object.keys(prices)) {
      const b = prices[sym]; if (!b || !b.c || b.c.length < 30) continue;
      per[sym] = runStock(sym, b, merged, P);
    }
    // pooled: ex-anchored combos only (announcement dates are patchy across stocks)
    const exCombos = buildCombos(false);
    const all = [];
    for (const sym in per) per[sym].events.forEach((e, i) => all.push({ sym, i, ex: e.ex, year: e.ex.slice(0, 4) }));
    all.sort((a, b) => a.ex < b.ex ? -1 : 1);
    const pooledCollect = (key, filt) => { const rets = [], days = []; for (const a of all) { if (filt && !filt(a)) continue; const t = per[a.sym]._M[key] && per[a.sym]._M[key][a.i]; if (t) { rets.push(t.ret); days.push(t.days); } } return { rets, days }; };
    const pooledTable = {};
    for (const key of exCombos) { const r = pooledCollect(key); pooledTable[key] = stats(r.rets, r.days); }
    const pooledChamp = pickBest(exCombos, k => pooledCollect(k), 10, 0.5, all.length);

    // walk-forward by calendar year: pick the rule on all prior years, trade the next year
    const years = [...new Set(all.map(a => a.year))].sort();
    const wfYears = [], pooledOOS = [];
    for (const y of years) {
      const trainN = all.filter(a => a.year < y).length;
      if (trainN < 15) continue;
      const b = pickBest(exCombos, k => pooledCollect(k, a => a.year < y), 10, 0.5, trainN);
      if (!b) continue;
      const r = pooledCollect(b.key, a => a.year === y);
      const trades = [];
      for (const a of all) if (a.year === y) { const t = per[a.sym]._M[b.key][a.i]; if (t) trades.push(t); }
      pooledOOS.push(...trades);
      wfYears.push({ year: y, key: b.key, stats: stats(r.rets, r.days) });
    }
    // pooled event study (event-weighted)
    const L = P.studyPre + P.studyPost + 1, sp = new Array(L).fill(0), st = new Array(L).fill(0); let cnt = 0;
    for (const sym in per) { const s = per[sym].study; if (!s.n) continue; for (let t = 0; t < L; t++) { sp[t] += s.price[t] * s.n; st[t] += s.total[t] * s.n; } cnt += s.n; }
    const pooledStudy = { n: cnt, pre: P.studyPre, post: P.studyPost, price: sp.map(x => x / cnt), total: st.map(x => x / cnt) };

    // per-stock performance of the market rule, and the recommended plan per stock
    for (const sym in per) {
      const r = per[sym];
      if (pooledChamp) { const tr = r._M[pooledChamp.key] ? r._M[pooledChamp.key].filter(Boolean) : []; r.pooledOnStock = stats(tr.map(t => t.ret), tr.map(t => t.days)); r.pooledTrades = tr; }
      const own = r.champ && r.oosStats.n >= 5 && r.oosStats.mean > 0 && r.champ.s.n >= 6;
      r.plan = own ? { key: r.champ.key, basis: 'stock-specific' } : (pooledChamp ? { key: pooledChamp.key, basis: 'market rule' } : null);
      // fallback for upcoming events that have no announcement date yet
      r.planEx = own && r.champEx ? { key: r.champEx.key, basis: 'stock-specific' } : (pooledChamp ? { key: pooledChamp.key, basis: 'market rule' } : null);
    }
    for (const sym in per) {
      const r = per[sym];
    }
    return { P, per, merged, pooledTable, pooledChamp, wfYears, pooledOOSStats: stats(pooledOOS.map(t => t.ret), pooledOOS.map(t => t.days)), pooledOOS, pooledStudy, nEvents: all.length };
  }

  // ---------- forward calendar ----------
  function planUpcoming(result, todayStr) {
    const rows = [];
    for (const e of result.merged) {
      const r = result.per[e.sym];
      const last = r ? r.bars.last : null;
      if (last && e.ex <= last && e.ex < todayStr) continue; // already in price history → past
      if (!last && e.ex < todayStr) continue;
      const lastBuy = shiftTradingDays(e.ex, -1);
      let pl = r && r.plan ? r.plan : (result.pooledChamp ? { key: result.pooledChamp.key, basis: 'market rule' } : null);
      if (pl && parseKey(pl.key).anchor === 'ann' && !e.ann) pl = r.planEx;
      const plan = pl ? parseKey(pl.key) : null;
      let entryDate = null;
      if (plan) entryDate = plan.anchor === 'ex' ? shiftTradingDays(e.ex, -plan.k) : shiftTradingDays(e.ann, plan.k);
      const status = todayStr > lastBuy ? 'past' : !entryDate ? 'noplan' : (todayStr < entryDate ? 'wait' : 'window');
      rows.push({ sym: e.sym, ex: e.ex, exEstimated: e.exEstimated, lastBuy, record: e.record, ann: e.ann, amt: e.amt, bonus: e.bonus, status: e.status, src: e.src,
        planKey: pl ? pl.key : null, basis: pl ? pl.basis : null, entryDate,
        sessionsToLastBuy: tradingDaysBetween(todayStr, lastBuy), state: status });
    }
    return rows.sort((a, b) => a.lastBuy < b.lastBuy ? -1 : 1);
  }

  function seasonality(result) { // typical ex-month per stock from history
    const out = {};
    for (const sym in result.per) {
      const ev = result.per[sym].events; if (!ev.length) continue;
      const months = {}; ev.forEach(e => { const m = +e.ex.slice(5, 7); months[m] = (months[m] || 0) + 1; });
      const lastYears = [...new Set(ev.map(e => e.ex.slice(0, 4)))].slice(-3);
      out[sym] = { months, lastYears, lastEx: ev[ev.length - 1].ex, lastAmt: ev[ev.length - 1].amt };
    }
    return out;
  }

  return { DEFAULTS, ENTRY_EX, ENTRY_ANN, EXITS, runAll, runStock, planUpcoming, seasonality, mergeEvents, describe, parseKey, entryLabel, exitLabel,
    shiftTradingDays, tradingDaysBetween, stats };
});
