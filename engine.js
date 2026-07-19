/* engine.js — extracted verbatim from msx_edge.html so the pipeline's math
   is identical to the desktop tool. Regenerate if the tool's engine changes. */
/*
   CSV PARSING (investing.com + generic OHLC)
   ============================================================ */
function splitCSVLine(line){
  const out=[]; let cur='', q=false;
  for(let i=0;i<line.length;i++){
    const ch=line[i];
    if(q){ if(ch==='"'){ if(line[i+1]==='"'){cur+='"';i++;} else q=false; } else cur+=ch; }
    else{ if(ch==='"') q=true; else if(ch===','){ out.push(cur); cur=''; } else cur+=ch; }
  }
  out.push(cur); return out;
}
function parseNum(s){
  if(s==null) return null;
  s = String(s).trim().replace(/,/g,'');
  if(s==='' || s==='-' || s==='n/a' || s==='N/A') return null;
  const m = s.match(/^(-?[\d.]+)\s*([KMB])?$/i);
  if(!m) { const f = parseFloat(s); return isNaN(f) ? null : f; }
  let v = parseFloat(m[1]);
  if(m[2]){ const mult = {K:1e3, M:1e6, B:1e9}[m[2].toUpperCase()]; v *= mult; }
  return isNaN(v) ? null : v;
}
function parseDateToken(s, hint){
  s = String(s).trim().replace(/"/g,'');
  let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);              // ISO
  if(m) return m[1]+'-'+m[2]+'-'+m[3];
  m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);           // x/y/YYYY
  if(m){
    let a=+m[1], b=+m[2], y=m[3];
    let mo, dy;
    if(hint==='mdy'){ mo=a; dy=b; }
    else if(hint==='dmy'){ dy=a; mo=b; }
    else { if(a>12){ dy=a; mo=b; } else { mo=a; dy=b; } }   // default MDY (investing.com)
    return y+'-'+String(mo).padStart(2,'0')+'-'+String(dy).padStart(2,'0');
  }
  m = s.match(/^(\w{3})\s+(\d{1,2}),\s+(\d{4})$/);          // "Jul 15, 2026"
  if(m){
    const mo = {jan:1,feb:2,mar:3,apr:4,may:5,jun:6,jul:7,aug:8,sep:9,oct:10,nov:11,dec:12}[m[1].toLowerCase()];
    if(mo) return m[3]+'-'+String(mo).padStart(2,'0')+'-'+String(+m[2]).padStart(2,'0');
  }
  return null;
}
function parseCSV(text, filename){
  const lines = text.split(/\r?\n/).filter(l=>l.trim().length);
  if(lines.length < 3) throw new Error('file too short');
  const head = splitCSVLine(lines[0]).map(h=>h.trim().replace(/"/g,'').toLowerCase());
  const idx = name => head.findIndex(h => h===name || h.startsWith(name));
  const iDate = idx('date');
  if(iDate < 0) throw new Error('no Date column');
  const isInvesting = idx('price') >= 0 && (idx('change') >= 0 || idx('vol') >= 0);
  let iO,iH,iL,iC,iV;
  if(isInvesting){ iC=idx('price'); iO=idx('open'); iH=idx('high'); iL=idx('low'); iV=idx('vol'); }
  else{
    iO=idx('open'); iH=idx('high'); iL=idx('low');
    iC=idx('close'); if(iC<0) iC=idx('adj close'); if(iC<0) iC=idx('price');
    iV=idx('volume'); if(iV<0) iV=idx('vol');
  }
  if(iO<0||iH<0||iL<0||iC<0) throw new Error('missing OHLC columns');
  // date-format hint: investing.com exports MDY
  const hint = isInvesting ? 'mdy' : null;
  const bars = [];
  for(let i=1;i<lines.length;i++){
    const p = splitCSVLine(lines[i]);
    if(p.length < head.length-1) continue;
    const d = parseDateToken(p[iDate], hint);
    const o = parseNum(p[iO]), h = parseNum(p[iH]), l = parseNum(p[iL]), c = parseNum(p[iC]);
    const v = iV>=0 ? (parseNum(p[iV]) ?? 0) : 0;
    if(!d || o==null || h==null || l==null || c==null) continue;
    if(o<=0 || h<=0 || l<=0 || c<=0) continue;
    bars.push({d,o,h,l,c,v});
  }
  if(bars.length < 2) throw new Error('no valid rows parsed');
  bars.sort((a,b)=> a.d < b.d ? -1 : a.d > b.d ? 1 : 0);
  // dedupe by date (keep last occurrence)
  const seen = new Map();
  for(const b of bars) seen.set(b.d, b);
  const clean = [...seen.values()].sort((a,b)=> a.d < b.d ? -1 : 1);
  return { symbol: symbolFromFilename(filename), bars: clean };
}
function symbolFromFilename(fn){
  let s = fn.replace(/(\.(csv|txt))+$/i,'');            // handles .csv and .csv.csv
  s = s.replace(/[_\-–—]+/g,' ');                       // underscores/dashes -> spaces
  s = s.replace(/stock price history/ig,'').replace(/historical data/ig,'').replace(/price history/ig,'');
  s = s.replace(/\bcsv\b/ig,'').replace(/\(\d+\)/g,'');
  s = s.replace(/[^\w\s]/g,' ').replace(/\s+/g,' ').trim();  // stray dots/punctuation
  return (s || 'UNKNOWN').toUpperCase();
}
function mergeBars(existing, incoming){
  const map = new Map(existing.map(b=>[b.d,b]));
  let added = 0, updated = 0;
  for(const b of incoming){
    if(map.has(b.d)){ map.set(b.d,b); updated++; } else { map.set(b.d,b); added++; }
  }
  const merged = [...map.values()].sort((a,b)=> a.d < b.d ? -1 : 1);
  return { merged, added, updated: updated };
}
/*
   ENGINE — indicators, strategies, backtester  /*ENGINE_START*/
/* ============================================================ */
function smaArr(vals, n){
  const out = new Array(vals.length).fill(null);
  let sum = 0;
  for(let i=0;i<vals.length;i++){
    sum += vals[i];
    if(i>=n) sum -= vals[i-n];
    if(i>=n-1) out[i] = sum/n;
  }
  return out;
}
function atrArr(bars, n){
  const out = new Array(bars.length).fill(null);
  let prevATR = null;
  for(let i=0;i<bars.length;i++){
    const b = bars[i];
    const tr = i===0 ? (b.h-b.l)
      : Math.max(b.h-b.l, Math.abs(b.h-bars[i-1].c), Math.abs(b.l-bars[i-1].c));
    if(i===0){ prevATR = tr; }
    else if(i < n){ prevATR = (prevATR*i + tr)/(i+1); }       // simple avg during warmup
    else { prevATR = (prevATR*(n-1) + tr)/n; }                 // Wilder
    if(i >= n-1) out[i] = prevATR;
  }
  return out;
}
function supertrendArr(bars, period, mult){
  const n = bars.length;
  const atr = atrArr(bars, period);
  const trend = new Array(n).fill(0);   // +1 up, -1 down, 0 warmup
  const line  = new Array(n).fill(null);
  let fu = null, fl = null, tr = -1;    // final upper/lower band, trend
  for(let i=0;i<n;i++){
    if(atr[i]==null) continue;
    const hl2 = (bars[i].h + bars[i].l)/2;
    const bu = hl2 + mult*atr[i];
    const bl = hl2 - mult*atr[i];
    if(fu===null){ fu=bu; fl=bl; tr=-1; }
    else{
      fu = (bu < fu || bars[i-1].c > fu) ? bu : fu;
      fl = (bl > fl || bars[i-1].c < fl) ? bl : fl;
      if(tr === -1 && bars[i].c > fu) tr = 1;
      else if(tr === 1 && bars[i].c < fl) tr = -1;
    }
    trend[i] = tr;
    line[i]  = tr === 1 ? fl : fu;
  }
  return { trend, line };
}
function donchianPrev(bars, n){
  // highest high / lowest low of the PREVIOUS n bars (excludes current bar)
  const len = bars.length;
  const hi = new Array(len).fill(null), lo = new Array(len).fill(null);
  for(let i=n;i<len;i++){
    let h=-Infinity, l=Infinity;
    for(let j=i-n;j<i;j++){ if(bars[j].h>h)h=bars[j].h; if(bars[j].l<l)l=bars[j].l; }
    hi[i]=h; lo[i]=l;
  }
  return { hi, lo };
}

/* ---- strategy signal generators: return {pos[], lines:{}, trigger} ----
   pos[i] = desired position (0/1) as of the CLOSE of bar i.               */
function sigSupertrend(bars, p){
  const { trend, line } = supertrendArr(bars, p.period, p.mult);
  const pos = trend.map(t => t===1 ? 1 : 0);
  return { pos, lines: { 'ST': line } };
}
function sigSMACross(bars, p){
  const closes = bars.map(b=>b.c);
  const f = smaArr(closes, p.fast), s = smaArr(closes, p.slow);
  const pos = closes.map((_,i)=> (f[i]!=null && s[i]!=null && f[i] > s[i]) ? 1 : 0);
  return { pos, lines: { ['SMA'+p.fast]: f, ['SMA'+p.slow]: s } };
}
function sigDonchian(bars, p){
  const { hi } = donchianPrev(bars, p.entryN);
  const { lo } = donchianPrev(bars, p.exitN);
  const n = bars.length, pos = new Array(n).fill(0);
  let inP = false;
  for(let i=0;i<n;i++){
    if(!inP){ if(hi[i]!=null && bars[i].c > hi[i]) inP = true; }
    else    { if(lo[i]!=null && bars[i].c < lo[i]) inP = false; }
    pos[i] = inP ? 1 : 0;
  }
  // trigger level for screener "NEAR" detection (breakout level as of last bar)
  return { pos, lines: { ['DC'+p.entryN+'H']: hi, ['DC'+p.exitN+'L']: lo }, trigger: hi[n-1] };
}
function sigLVRB(bars, p){
  // Low-Volatility Range Breakout: N-bar range in its lowest pct-percentile
  // of the trailing 60 readings arms the squeeze; a close above the prior
  // N-bar high enters; exit = close below max(chandelier trail, range low).
  const n = bars.length, WIN = 60;
  const { hi, lo } = donchianPrev(bars, p.rangeN);
  const atr = atrArr(bars, 14);
  const rr = new Array(n).fill(null);
  for(let i=0;i<n;i++) if(hi[i]!=null) rr[i] = hi[i]-lo[i];
  const squeeze = new Array(n).fill(false);
  for(let i=0;i<n;i++){
    if(rr[i]==null) continue;
    let cnt=0, tot=0;
    for(let j=Math.max(0,i-WIN+1); j<=i; j++){
      if(rr[j]==null) continue;
      tot++; if(rr[j] <= rr[i]) cnt++;
    }
    if(tot >= 20) squeeze[i] = (cnt/tot)*100 <= p.pct;
  }
  const pos = new Array(n).fill(0);
  let inP=false, stop=null, hiClose=null;
  let trigger = null;
  for(let i=1;i<n;i++){
    if(!inP){
      if(squeeze[i-1] && hi[i]!=null && bars[i].c > hi[i]){
        inP = true; stop = lo[i]; hiClose = bars[i].c;
      }
    } else {
      hiClose = Math.max(hiClose, bars[i].c);
      const trail = atr[i]!=null ? hiClose - p.trailMult*atr[i] : null;
      const eff = trail!=null ? Math.max(stop, trail) : stop;
      if(bars[i].c < eff) inP = false;
    }
    pos[i] = inP ? 1 : 0;
  }
  if(squeeze[n-1] && hi[n-1]!=null) trigger = hi[n-1];
  return { pos, lines: { ['R'+p.rangeN+'H']: hi, ['R'+p.rangeN+'L']: lo }, trigger, squeezed: squeeze[n-1] };
}

function rsiArr(closes, n){
  const out = new Array(closes.length).fill(null);
  let ag = 0, al = 0;
  for(let i=1;i<closes.length;i++){
    const ch = closes[i]-closes[i-1];
    const g = Math.max(ch,0), l = Math.max(-ch,0);
    if(i <= n){ ag += g; al += l;
      if(i === n){ ag/=n; al/=n; out[i] = al===0 ? 100 : 100-100/(1+ag/al); }
    } else {
      ag = (ag*(n-1)+g)/n; al = (al*(n-1)+l)/n;
      out[i] = al===0 ? 100 : 100-100/(1+ag/al);
    }
  }
  return out;
}
/* ---- bracket strategies: fire entry events; exits are fixed TP / SL / time stop ---- */
function sigRSIDip(bars, p){
  const closes = bars.map(b=>b.c);
  const rsi = rsiArr(closes, p.rsiN);
  const tr = p.trend ? smaArr(closes, p.trend) : null;
  const entries = closes.map((c,i)=> rsi[i]!=null && rsi[i] < p.thr &&
    (!tr || (tr[i]!=null && c > tr[i])));
  return { entries, lines: tr ? { ['SMA'+p.trend]: tr } : {} };
}
function sigStreak(bars, p){
  const closes = bars.map(b=>b.c);
  const tr = p.trend ? smaArr(closes, p.trend) : null;
  const entries = new Array(bars.length).fill(false);
  let run = 0;
  for(let i=1;i<bars.length;i++){
    run = closes[i] < closes[i-1] ? run+1 : 0;
    entries[i] = run >= p.n && (!tr || (tr[i]!=null && closes[i] > tr[i]));
  }
  return { entries, lines: tr ? { ['SMA'+p.trend]: tr } : {} };
}
function sigFastBreak(bars, p){
  const { hi } = donchianPrev(bars, p.n);
  const entries = bars.map((b,i)=> hi[i]!=null && b.c > hi[i]);
  return { entries, lines: { ['DC'+p.n+'H']: hi }, trigger: hi[bars.length-1] };
}
/* Bracket backtester: entry at next open; TP/SL are working intrabar orders.
   Conservative conventions: if a bar touches both levels the STOP fills first;
   gaps through a level fill at the open. Time stop exits at the next open. */
function backtestBracket(bars, entries, p, costs){
  const n = bars.length;
  const cs = (costs.comm + costs.slip)/100;
  const trades = [];
  const equity = new Array(n).fill(1);
  let eq = 1, inP = false, entryPx = 0, tpPx = 0, slPx = 0, ei = -1, hold = 0, timeDue = false;
  for(let i=0;i<n;i++){
    const b = bars[i], prevC = i>0 ? bars[i-1].c : b.o;
    if(inP){
      hold++;
      let xp = null, reason = null;
      if(timeDue){ xp = b.o*(1-cs); reason = 'time'; }
      else if(b.o <= slPx){ xp = b.o*(1-cs); reason = 'sl'; }
      else if(b.l <= slPx){ xp = slPx*(1-cs); reason = 'sl'; }
      else if(b.o >= tpPx){ xp = b.o*(1-cs); reason = 'tp'; }
      else if(b.h >= tpPx){ xp = tpPx*(1-cs); reason = 'tp'; }
      else if(hold >= p.hold){ timeDue = true; }
      if(xp != null){
        trades.push({ ei, xi: i, ep: entryPx, xp, r: xp/entryPx-1, hold, reason });
        eq *= xp/prevC; inP = false; equity[i] = eq; continue;
      }
      eq *= b.c/prevC; equity[i] = eq; continue;
    }
    if(i>0 && entries[i-1]){
      const raw = b.o;
      entryPx = raw*(1+cs); tpPx = raw*(1+p.tp/100); slPx = raw*(1-p.sl/100);
      ei = i; hold = 0; timeDue = false; inP = true;
      let xp = null, reason = null;                    // same-bar touch, stop first
      if(b.l <= slPx){ xp = slPx*(1-cs); reason = 'sl'; }
      else if(b.h >= tpPx){ xp = tpPx*(1-cs); reason = 'tp'; }
      if(xp != null){
        trades.push({ ei, xi: i, ep: entryPx, xp, r: xp/entryPx-1, hold: 0, reason });
        eq *= xp/entryPx; inP = false;
      } else eq *= b.c/entryPx;
      equity[i] = eq; continue;
    }
    equity[i] = eq;
  }
  let open = null;
  if(inP) open = { ei, ep: entryPx, r: bars[n-1].c/entryPx-1, hold, tpPx, slPx, timeDue };
  return { trades, open, equity };
}

const STRATS = {
  supertrend: { fn: sigSupertrend, label: 'Supertrend',
    fmt: p => 'ATR '+p.period+' × '+p.mult },
  smacross:   { fn: sigSMACross, label: 'SMA cross',
    fmt: p => p.fast+' / '+p.slow },
  donchian:   { fn: sigDonchian, label: 'Donchian breakout',
    fmt: p => 'in '+p.entryN+' / out '+p.exitN },
  lvrb:       { fn: sigLVRB, label: 'LVRB squeeze',
    fmt: p => 'N'+p.rangeN+' · p'+p.pct+' · trail '+p.trailMult+'×ATR' }
,
  rsidip:    { fn: sigRSIDip, kind: 'bracket', label: 'RSI dip',
    fmt: p => 'RSI'+p.rsiN+'<'+p.thr+(p.trend?' >SMA'+p.trend:'')+' · TP '+p.tp+'% SL '+p.sl+'% · '+p.hold+'d' },
  streak:    { fn: sigStreak, kind: 'bracket', label: 'Down-streak buy',
    fmt: p => p.n+' red closes'+(p.trend?' >SMA'+p.trend:'')+' · TP '+p.tp+'% SL '+p.sl+'% · '+p.hold+'d' },
  fastbreak: { fn: sigFastBreak, kind: 'bracket', label: 'Breakout TP/SL',
    fmt: p => 'break '+p.n+'d high · TP '+p.tp+'% SL '+p.sl+'% · '+p.hold+'d' }
};
const STRAT_LABEL = Object.fromEntries(Object.entries(STRATS).map(([k,v])=>[k,v.label]));

function gridFor(family){
  const g = [];
  if(family==='supertrend'){
    for(const period of [7,10,14,21,28])
      for(const mult of [1.5,2,2.5,3,3.5]) g.push({period,mult});
  }
  if(family==='smacross'){
    for(const fast of [5,8,10,15,20])
      for(const slow of [30,50,100,150,200]) if(fast<slow) g.push({fast,slow});
  }
  if(family==='donchian'){
    for(const entryN of [10,20,30,55])
      for(const exitN of [5,10,20]) if(exitN<entryN) g.push({entryN,exitN});
  }
  if(family==='lvrb'){
    for(const rangeN of [5,7,10])
      for(const pctl of [20,30,40])
        for(const trailMult of [2,2.5,3]) g.push({rangeN,pct:pctl,trailMult});
  }
  if(family==='rsidip'){
    for(const rsiN of [2,14])
      for(const thr of (rsiN===2 ? [15,25] : [30]))
        for(const trend of [0,200])
          for(const tp of [3,5]) for(const sl of [2,3]) for(const hold of [5,10])
            g.push({rsiN,thr,trend,tp,sl,hold});
  }
  if(family==='streak'){
    for(const nn of [2,3,4]) for(const trend of [0,200])
      for(const tp of [3,5]) for(const sl of [2,3]) for(const hold of [5,10])
        g.push({n:nn,trend,tp,sl,hold});
  }
  if(family==='fastbreak'){
    for(const nn of [5,10,20]) for(const tp of [3,5,8]) for(const sl of [2,3]) for(const hold of [10,20])
      g.push({n:nn,tp,sl,hold});
  }
  return g;
}

/* ---- backtester: fills at NEXT OPEN when pos changes ---- */
function backtest(bars, pos, costs){
  const n = bars.length;
  const cs = (costs.comm + costs.slip)/100;   // per-side cost fraction
  const trades = [];
  let inP=false, entryPx=0, entryIdx=-1;
  const equity = new Array(n).fill(1);
  let eq = 1;
  for(let i=0;i<n;i++){
    // execute yesterday's signal at today's open
    if(i>0){
      const want = pos[i-1];
      if(!inP && want===1 && i < n){
        entryPx = bars[i].o * (1+cs); entryIdx = i; inP = true;
        eq *= bars[i].c / entryPx;          // mark from entry to today's close
        equity[i] = eq; continue;
      }
      if(inP && want===0){
        const exitPx = bars[i].o * (1-cs);
        trades.push({ ei: entryIdx, xi: i, ep: entryPx, xp: exitPx,
                      r: exitPx/entryPx - 1, hold: i - entryIdx });
        eq *= exitPx / bars[i-1].c;
        inP = false; equity[i] = eq; continue;
      }
    }
    if(inP && i>0) eq *= bars[i].c / bars[i-1].c;
    equity[i] = eq;
  }
  let open = null;
  if(inP){
    const last = bars[n-1];
    open = { ei: entryIdx, ep: entryPx, r: last.c/entryPx - 1, hold: n-1-entryIdx };
  }
  return { trades, open, equity };
}
function tradeStats(trades){
  const n = trades.length;
  if(!n) return { trades:0, netPct:0, pf:null, winRate:null, maxDD:0, avgHold:null, expectancy:null };
  let eq=1, peak=1, dd=0, gw=0, gl=0, wins=0, hold=0;
  for(const t of trades){
    eq *= (1+t.r);
    if(eq>peak) peak=eq;
    dd = Math.max(dd, (peak-eq)/peak);
    if(t.r>0){ gw += t.r; wins++; } else gl += -t.r;
    hold += t.hold;
  }
  return {
    trades: n,
    netPct: (eq-1)*100,
    pf: gl===0 ? (gw>0 ? Infinity : null) : gw/gl,
    winRate: wins/n*100,
    maxDD: dd*100,
    avgHold: hold/n,
    expectancy: trades.reduce((a,t)=>a+t.r,0)/n*100
  };
}
function evalCombo(bars, family, params, costs, splitIdx){
  const def = STRATS[family];
  const sig = def.fn(bars, params);
  const bt = def.kind === 'bracket'
    ? backtestBracket(bars, sig.entries, params, costs)
    : backtest(bars, sig.pos, costs);
  const isTr = bt.trades.filter(t=>t.ei < splitIdx);
  const oosTr = bt.trades.filter(t=>t.ei >= splitIdx);
  return {
    family, params,
    is: tradeStats(isTr),
    oos: tradeStats(oosTr),
    full: tradeStats(bt.trades),
    openTrade: bt.open
  };
}
function rankResults(rows, minTr){
  // qualified: enough OOS trades and OOS PF >= 1
  const q = rows.filter(r => r.oos.trades >= minTr && r.oos.pf !== null && r.oos.pf >= 1);
  const key = r => [r.oos.netPct, r.oos.pf===Infinity?999:(r.oos.pf||0), r.full.netPct];
  const cmp = (a,b)=>{ const ka=key(a), kb=key(b);
    for(let i=0;i<ka.length;i++){ if(ka[i]!==kb[i]) return kb[i]-ka[i]; } return 0; };
  if(q.length) return { ranked: q.sort(cmp), weak: false,
    rest: rows.filter(r=>!q.includes(r)).sort((a,b)=>b.full.netPct-a.full.netPct) };
  // fallback: nothing validated — rank by full-sample, flag as weak
  return { ranked: rows.slice().sort((a,b)=>b.full.netPct-a.full.netPct), weak: true, rest: [] };
}
/*ENGINE_END*/

module.exports = { parseCSV, mergeBars, symbolFromFilename, smaArr, atrArr, rsiArr,
  supertrendArr, donchianPrev, STRATS, STRAT_LABEL, gridFor,
  backtest, backtestBracket, tradeStats, evalCombo, rankResults };
