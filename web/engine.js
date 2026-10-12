/* Расчёт, «Стало», раскладка, симуляция, .bpmn — без DOM: window.PrEngine или require(). Контракт — docs/SPEC.md. */
(function (root, make) {
  if (typeof module === 'object' && module.exports) module.exports = make();
  else root.PrEngine = make();
})(typeof self !== 'undefined' ? self : this, function () {
'use strict';

const DEFAULTS = {
  perMonth: 100, rate: 700, people: {}, hoursPerPerson: 120, taskMin: 15, decisionMin: 5,
  handoffH: 4, loopShare: 0.2, autoFactor: 0.1, dayH: 8,
};
const SYS = 'sys';
const SYS_NAME = 'Система';
const OVER_DAYS = 20;

/* Числа ≥ 0 (день и часы человека > 0), иначе по умолчанию; minutes / wait_hours — по шагам из ответов. */
function assume(a) {
  a = a || {};
  const r = {};
  Object.keys(DEFAULTS).forEach((k) => {
    const v = a[k] == null || a[k] === '' ? NaN : +a[k];
    r[k] = isFinite(v) && v >= 0 && (v > 0 || (k !== 'dayH' && k !== 'hoursPerPerson')) ? v : DEFAULTS[k];
  });
  r.people = Object.assign({}, a.people);
  r.minutes = Object.assign({}, a.minutes);
  r.wait_hours = Object.assign({}, a.wait_hours);
  return r;
}

/* Поле ответа интервью → допущение: volume, rate, sN.minutes, sN.wait_hours, rN.people. */
const FIELD = { volume: 'perMonth', rate: 'rate' };
function applyAnswers(a, understood) {
  const A = assume(a);
  (understood || []).forEach((u) => {
    const v = u && u.value != null && u.value !== '' ? +u.value : NaN;
    const f = String(u && u.field);
    const m = /^(s\d+)\.(minutes|wait_hours)$|^(r\d+)\.people$/.exec(f);
    if (!(isFinite(v) && v >= 0)) return;
    if (m) (m[1] ? A[m[2]] : A.people)[m[1] || m[3]] = v;
    else if (FIELD[f]) A[FIELD[f]] = v;
  });
  return A;
}

/* Процесс с минутами и ожиданием из ответов поверх чисел модели. */
function withAnswers(proc, a) {
  const A = assume(a);
  return Object.assign({}, proc, { steps: proc.steps.map((s) => {
    const m = A.minutes[s.id];
    const w = A.wait_hours[s.id];
    return m == null && w == null ? s : Object.assign({}, s, m != null ? { minutes: +m } : {}, w != null ? { wait_hours: +w } : {});
  }) });
}
const peopleOf = (A, role) => (+A.people[role] > 0 ? +A.people[role] : 1);
const index = (proc) => { const by = {}; proc.steps.forEach((s) => { by[s.id] = s; }); return by; };
const startOf = (proc) => proc.steps.find((s) => s.kind === 'start') || proc.steps[0];
const isWork = (s) => s.kind === 'task' || s.kind === 'decision';
const roleOf = (s) => (s.auto ? SYS : s.role);
/* Внешние роли (клиент, поставщик): нет поля — false. */
const externals = (proc) => new Set((proc.roles || []).filter((r) => r.external === true).map((r) => r.id));

function outs(s) {
  if (s.kind === 'decision') return (s.branches || []).map((b) => ({ to: b.to, label: b.label || '' }));
  return (s.next || []).map((to) => ({ to, label: '' }));
}

function reach(by, from) {
  const seen = new Set([from]);
  const stack = [from];
  while (stack.length) {
    const s = by[stack.pop()];
    if (s) outs(s).forEach((o) => { if (by[o.to] && !seen.has(o.to)) { seen.add(o.to); stack.push(o.to); } });
  }
  return seen;
}

/* Рёбра с долями: ветка-петля (из цели достижимо само решение) — loopShare, прочие — поровну. */
function edges(proc, A) {
  A = assume(A);
  const by = index(proc);
  const out = [];
  proc.steps.forEach((s) => {
    const os = outs(s).filter((o) => by[o.to]);
    if (!os.length || s.kind === 'end') return;
    const loop = os.map((o) => s.kind === 'decision' && reach(by, o.to).has(s.id));
    const nLoop = loop.filter(Boolean).length;
    const nFwd = os.length - nLoop;
    const split = nLoop && nFwd && nLoop * A.loopShare < 1;
    os.forEach((o, i) => {
      const p = split ? (loop[i] ? A.loopShare : (1 - nLoop * A.loopShare) / nFwd) : 1 / os.length;
      out.push({ from: s.id, to: o.to, label: o.label, p, loop: loop[i] });
    });
  });
  return out;
}

/* v(s) — проходы заявки через шаг: поглощающая цепь Маркова. */
function visits(proc, A, E) {
  E = E || edges(proc, A);
  const start = startOf(proc).id;
  let v = {};
  proc.steps.forEach((s) => { v[s.id] = 0; });
  for (let it = 0; it < 200; it++) {
    const nv = {};
    proc.steps.forEach((s) => { nv[s.id] = s.id === start ? 1 : 0; });
    E.forEach((e) => { nv[e.to] += v[e.from] * e.p; });
    let diff = 0;
    proc.steps.forEach((s) => { diff = Math.max(diff, Math.abs(nv[s.id] - v[s.id])); });
    v = nv;
    if (diff < 1e-12) break;
  }
  return v;
}

function rawMinutes(s, A) {
  if (!isWork(s)) return 0;
  if (s.minutes != null && isFinite(+s.minutes)) return +s.minutes;
  return s.kind === 'decision' ? A.decisionMin : A.taskMin;
}
const minutesOf = (s, A) => rawMinutes(s, A) * (s.auto ? A.autoFactor : 1);

/* Ожидание на ребре: wait_hours из текста, иначе передача между ролями (с «Системой» — 0). */
function edgeWait(from, to, A) {
  if (!isWork(to)) return 0;
  if (to.wait_hours != null && isFinite(+to.wait_hours)) return +to.wait_hours;
  const a = roleOf(from);
  const b = roleOf(to);
  return a !== b && a !== SYS && b !== SYS ? A.handoffH : 0;
}

function calc(proc, assumptions) {
  const A = assume(assumptions);
  const by = index(proc);
  const E = edges(proc, A);
  const v = visits(proc, A, E);
  const ext = externals(proc);
  const roles = {};
  proc.steps.forEach((s) => {
    const r = roleOf(s);
    if (!isWork(s) || r === SYS || ext.has(r)) return;
    const R = roles[r] || (roles[r] = { vmin: 0, vis: 0 });
    R.vmin += v[s.id] * minutesOf(s, A);
    R.vis += v[s.id];
  });
  let hours = 0;
  let bottleneck = null;
  let over = false;
  Object.keys(roles).forEach((r) => {
    const R = roles[r];
    R.people = peopleOf(A, r);
    R.hours = (A.perMonth * R.vmin) / 60;
    R.rho = R.hours / (R.people * A.hoursPerPerson);
    R.mean = R.vis ? R.vmin / R.vis / 60 : 0;
    R.wq = R.rho >= 1 ? Infinity : (R.rho / (1 - R.rho)) * R.mean;
    R.level = R.rho >= 1 ? 'over' : R.rho > 0.85 ? 'hot' : 'ok';
    hours += R.hours;
    if (R.rho >= 1) over = true;
    if (!bottleneck || R.rho > roles[bottleneck].rho) bottleneck = r;
  });
  const waitIn = {};
  const fromText = {};
  E.forEach((e) => {
    const t = by[e.to];
    fromText[e.to] = t.wait_hours != null && isWork(t);
    waitIn[e.to] = (waitIn[e.to] || 0) + v[e.from] * e.p * edgeWait(by[e.from], t, A);
  });
  const steps = {};
  let total = 0;
  proc.steps.forEach((s) => {
    const r = roleOf(s);
    const R = isWork(s) ? roles[r] : null;
    const m = minutesOf(s, A);
    const wait = v[s.id] > 1e-9 ? (waitIn[s.id] || 0) / v[s.id] : 0;
    const wq = R ? R.wq : 0;
    const lam = (A.perMonth * v[s.id]) / A.hoursPerPerson;
    steps[s.id] = {
      v: v[s.id], minutes: m, wait, waitSrc: fromText[s.id] ? 'text' : wait > 0 ? 'handoff' : null,
      role: r, ext: ext.has(r), level: R ? R.level : 'ok',
      queueN: R ? (isFinite(wq) ? lam * wq : Infinity) : 0, queueDays: wq / A.dayH,
    };
    if (isFinite(wq)) total += v[s.id] * (m / 60 + wait + wq);
  });
  return {
    A, edges: E, visits: v, roles, steps, bottleneck, over,
    hours, rub: hours * A.rate, days: over ? Infinity : total / A.dayH,
  };
}

/* Действие по шагу: совет модели, поверх — выбор посетителя. */
function actions(proc, improve, overrides) {
  const by = index(proc);
  const ext = externals(proc);
  const out = {};
  ((improve && improve.changes) || []).forEach((c) => {
    if (by[c.step]) out[c.step] = { action: c.action, merge_into: c.merge_into || null, how: c.how || '', risk: c.risk || '' };
  });
  Object.keys(overrides || {}).forEach((id) => {
    if (by[id]) out[id] = Object.assign({ merge_into: null, how: '', risk: '' }, out[id], { action: overrides[id] });
  });
  Object.keys(out).forEach((id) => {
    const s = by[id];
    const a = out[id];
    const ok = ext.has(s.role) ? ['keep'] : s.kind === 'task' ? ['keep', 'automate', 'remove', 'merge'] : s.kind === 'decision' ? ['keep', 'automate'] : ['keep'];
    if (ok.indexOf(a.action) < 0 || (a.action === 'merge' && (!by[a.merge_into] || a.merge_into === id))) a.action = 'keep';
  });
  return out;
}

/* «Стало»: remove — входы идут на next; merge — минуты к merge_into; automate — «Система». */
function after(proc, improve, overrides, assumptions) {
  const A = assume(assumptions);
  const acts = actions(proc, improve, overrides);
  const steps = proc.steps.map((s) => Object.assign({}, s,
    s.next ? { next: s.next.slice() } : {}, s.branches ? { branches: s.branches.map((b) => Object.assign({}, b)) } : {}));
  const by = index({ steps });
  const gone = {};
  steps.forEach((s) => {
    const a = acts[s.id] && acts[s.id].action;
    if (a === 'automate') s.auto = true;
    else if (a === 'remove') gone[s.id] = 'remove';
    else if (a === 'merge') gone[s.id] = 'merge';
  });
  const final = (id) => { const seen = {}; while (gone[id] === 'merge' && !seen[id]) { seen[id] = 1; id = acts[id].merge_into; } return id; };
  steps.forEach((s) => {
    if (gone[s.id] !== 'merge') return;
    const t = by[final(s.id)];
    if (!t || gone[t.id]) return;
    t.minutes = rawMinutes(t, A) + rawMinutes(proc.steps.find((x) => x.id === s.id), A);
    (t.merged || (t.merged = [])).push(s.id);
  });
  const resolve = (id) => {
    const seen = {};
    while (id && gone[id]) {
      if (seen[id]) return null;
      seen[id] = 1;
      id = (by[id].next || [])[0];
    }
    return id && by[id] ? id : null;
  };
  const kept = steps.filter((s) => !gone[s.id]);
  kept.forEach((s) => {
    if (s.next) s.next = s.next.map(resolve).filter((t, i, a) => t && t !== s.id && a.indexOf(t) === i);
    if (s.branches) s.branches = s.branches.map((b) => Object.assign(b, { to: resolve(b.to) })).filter((b) => b.to);
  });
  const roles = proc.roles.slice();
  if (kept.some((s) => s.auto)) roles.push({ id: SYS, name: SYS_NAME });
  return Object.assign({}, proc, { steps: kept, roles, removed: Object.keys(gone), actions: acts });
}

/* Отчёт (SPEC-analyst): Было → Стало, два шага с большими потерями (часы × ставка), первым — automate,
   который один даёт больше ₽. Минуты шага — из ответа, если число есть (цитата — из стенограммы). */
function report(proc, improve, overrides, assumptions, answered) {
  const A = assume(assumptions);
  const p = withAnswers(proc, A);
  const ans = new Set(answered || []);
  const b = calc(p, A);
  const a = calc(after(p, improve, overrides, A), A);
  const acts = actions(p, improve, overrides);
  const src = (f) => (ans.has(f) ? 'answer' : 'assume');
  const S = { volume: src('volume'), rate: src('rate'), people: {}, minutes: {}, wait: {} };
  Object.keys(b.roles).forEach((r) => { S.people[r] = src(r + '.people'); });
  p.steps.forEach((s) => {
    const x = b.steps[s.id];
    if (isWork(s)) S.minutes[s.id] = s.minutes != null ? 'answer' : 'assume';
    if (x.wait > 0) S.wait[s.id] = x.waitSrc === 'text' ? 'answer' : 'assume';
  });
  const worst = p.steps.filter((s) => b.roles[b.steps[s.id].role] && isWork(s)).map((s) => {
    const x = b.steps[s.id];
    const R = b.roles[x.role];
    const hours = (A.perMonth * x.v * x.minutes) / 60;
    return { id: s.id, name: s.name, role: x.role, hours, rub: hours * A.rate, minutes: x.minutes, v: x.v, rho: R.rho, level: R.level };
  }).filter((w) => w.hours > 0).sort((u, v) => v.hours - u.hours).slice(0, 2);
  let first = null;
  Object.keys(acts).forEach((id) => {
    if (acts[id].action !== 'automate') return;
    const r = calc(after(p, null, { [id]: 'automate' }, A), A);
    const e = { id, name: p.steps.find((s) => s.id === id).name, how: acts[id].how, hours: b.hours - r.hours, rub: b.rub - r.rub, days: r.days };
    if (!first || e.rub > first.rub + 1e-9) first = e;
  });
  return { A, before: b, after: a, worst, first, src: S };
}

/* ---------- раскладка ---------- */

const G = { pad: 8, gap: 36, taskH: 44, evR: 12, dHalf: 17, cw: 6.1, cwSmall: 5.6, lh: 12, lane: 9 };

function wrap(text, max, lines) {
  const words = String(text || '').split(/\s+/).filter(Boolean);
  const out = [];
  let cur = '';
  max = Math.max(4, max);
  words.forEach((w) => {
    while (w.length > max) { if (cur) { out.push(cur); cur = ''; } out.push(w.slice(0, max - 1) + '-'); w = w.slice(max - 1); }
    if (!cur) cur = w;
    else if ((cur + ' ' + w).length <= max) cur += ' ' + w;
    else { out.push(cur); cur = w; }
  });
  if (cur) out.push(cur);
  if (out.length > lines) {
    const last = out.slice(lines - 1).join(' ');
    out.length = lines - 1;
    out.push(last.length > max ? last.slice(0, max - 1).replace(/[\s,.;:-]+$/, '') + '…' : last);
  }
  return out;
}

function dfsBack(proc) {
  const by = index(proc);
  const state = {};
  const back = new Set();
  const order = [];
  const go = (id) => {
    state[id] = 1;
    order.push(id);
    outs(by[id]).forEach((o) => {
      if (!by[o.to]) return;
      if (state[o.to] === 1) back.add(id + '>' + o.to);
      else if (!state[o.to]) go(o.to);
    });
    state[id] = 2;
  };
  go(startOf(proc).id);
  proc.steps.forEach((s) => { if (!state[s.id]) go(s.id); });
  return { back, order };
}

function ranks(proc, E) {
  const indeg = {};
  const rank = {};
  proc.steps.forEach((s) => { indeg[s.id] = 0; rank[s.id] = 0; });
  E.forEach((e) => { if (!e.back) indeg[e.to]++; });
  const q = proc.steps.filter((s) => !indeg[s.id]).map((s) => s.id);
  while (q.length) {
    const id = q.shift();
    E.forEach((e) => {
      if (e.back || e.from !== id) return;
      rank[e.to] = Math.max(rank[e.to], rank[id] + 1);
      if (--indeg[e.to] === 0) q.push(e.to);
    });
  }
  return rank;
}

/* По рангам сверху вниз, в ряд до 3 мест (решение — 2). Обратные рёбра — справа, обходы — слева. */
function layout(proc, opts) {
  const W = (opts && opts.width) || 360;
  const by = index(proc);
  const { back, order } = dfsBack(proc);
  const E = [];
  proc.steps.forEach((s) => outs(s).forEach((o) => {
    if (by[o.to]) E.push({ from: s.id, to: o.to, label: o.label, back: back.has(s.id + '>' + o.to) });
  }));
  const rank = ranks(proc, E);
  const nBack = E.filter((e) => e.back).length;
  let leftN = 0;
  let res = null;
  for (let pass = 0; pass < 3; pass++) {
    res = place(leftN);
    if (res.leftUsed <= leftN) break;
    leftN = res.leftUsed;
  }
  return res;

  function place(leftLanes) {
    const xL = G.pad + leftLanes * G.lane;
    const xR = W - G.pad - (nBack ? 8 + nBack * G.lane : 0);
    const CW = xR - xL;
    const nodes = {};
    const rows = [];
    const maxRank = Math.max.apply(null, proc.steps.map((s) => rank[s.id]));
    let y = G.pad + (nBack ? 8 : 0);
    for (let r = 0; r <= maxRank; r++) {
      const ids = proc.steps.filter((s) => rank[s.id] === r).map((s) => s.id);
      const bary = (id) => {
        const xs = E.filter((e) => !e.back && e.to === id && nodes[e.from]).map((e) => nodes[e.from].cx);
        return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : Infinity;
      };
      ids.sort((a, b) => (bary(a) - bary(b)) || (order.indexOf(a) - order.indexOf(b)));
      let row = [];
      let wt = 0;
      const flush = () => { if (row.length) rows.push(row); row = []; wt = 0; };
      ids.forEach((id) => {
        const w = by[id].kind === 'decision' ? 2 : 1;
        if (wt + w > 3) flush();
        row.push(id);
        wt += w;
      });
      flush();
    }
    rows.forEach((row, ri) => {
      const wts = row.map((id) => (by[id].kind === 'decision' ? 2 : 1));
      const sum = wts.reduce((a, b) => a + b, 0);
      let cx0 = xL;
      let rowH = 0;
      const made = row.map((id, i) => {
        const s = by[id];
        const cellL = cx0;
        const cellR = cx0 + (CW * wts[i]) / sum;
        cx0 = cellR;
        const n = { id, kind: s.kind, row: ri, col: i, rowN: row.length, cellL, cellR };
        if (s.kind === 'task') {
          n.cx = (cellL + cellR) / 2;
          n.sw = Math.min(cellR - cellL - (row.length > 2 ? 6 : 10), 210);
          n.lines = wrap(s.name, Math.floor((n.sw - 16) / G.cw), 9);
          n.sh = Math.max(G.taskH, n.lines.length * G.lh + 12);
        } else {
          const half = s.kind === 'decision' ? G.dHalf : G.evR;
          n.cx = s.kind === 'decision' && row.length === 1 ? (cellL + cellR) / 2 : cellL + Math.min((cellR - cellL) / 2, half + 34);
          if (row.length === 1) n.cx = (xL + xR) / 2;
          n.sw = n.sh = half * 2;
          n.lx = n.cx + half + 6;
          n.lines = wrap(s.name, Math.floor((cellR - 4 - n.lx) / G.cwSmall), 9);
        }
        n.sx = n.cx - n.sw / 2;
        rowH = Math.max(rowH, n.sh, n.lines.length * G.lh * (s.kind === 'task' ? 0 : 1));
        return n;
      });
      made.forEach((n) => {
        n.cy = y + rowH / 2;
        n.sy = n.cy - n.sh / 2;
        n.top = n.sy;
        n.bottom = n.sy + n.sh;
        n.rowTop = y;
        n.rowBottom = y + rowH;
        const lw = n.lx ? Math.max.apply(null, n.lines.map((l) => l.length)) * G.cwSmall : 0;
        n.box = { x: n.sx, y: Math.min(n.sy, n.cy - (n.lines.length * G.lh) / 2), w: n.lx ? n.lx + lw - n.sx : n.sw, h: 0 };
        n.box.h = Math.max(n.sy + n.sh, n.cy + (n.lx ? (n.lines.length * G.lh) / 2 : 0)) - n.box.y;
        n.ly = n.cy - ((n.lines.length - 1) * G.lh) / 2;
        nodes[n.id] = n;
      });
      y += rowH + G.gap;
    });
    const all = Object.keys(nodes).map((k) => nodes[k]);
    const hits = (x1, y1, x2, y2, skip) => all.some((n) => {
      if (skip.indexOf(n.id) >= 0) return false;
      const b = n.box;
      return Math.max(x1, x2) > b.x - 3 && Math.min(x1, x2) < b.x + b.w + 3 && Math.max(y1, y2) > b.y - 3 && Math.min(y1, y2) < b.y + b.h + 3;
    });
    let leftUsed = 0;
    const out = [];
    const backs = E.filter((e) => e.back).sort((a, b) => (nodes[a.from].cy - nodes[a.to].cy) - (nodes[b.from].cy - nodes[b.to].cy));
    E.filter((e) => !e.back).forEach((e) => {
      const u = nodes[e.from];
      const v = nodes[e.to];
      const off = (u.col - (u.rowN - 1) / 2) * 5;
      const myV = v.rowTop - G.gap / 2 + off;
      const myU = u.rowBottom + G.gap / 2 + off;
      const skip = [u.id, v.id];
      let pts;
      if (!hits(u.cx, u.bottom, u.cx, myV, skip)) pts = [[u.cx, u.bottom], [u.cx, myV], [v.cx, myV], [v.cx, v.top]];
      else if (!hits(v.cx, myU, v.cx, v.top, skip)) pts = [[u.cx, u.bottom], [u.cx, myU], [v.cx, myU], [v.cx, v.top]];
      else {
        const lx = xL - (++leftUsed) * G.lane + 2;
        pts = [[u.cx, u.bottom], [u.cx, myU], [lx, myU], [lx, myV], [v.cx, myV], [v.cx, v.top]];
      }
      out.push(Object.assign({}, e, { pts: simplify(pts), lab: e.label ? { x: v.cx, y: v.top - 7, a: 'middle' } : null }));
    });
    backs.forEach((e, k) => {
      const u = nodes[e.from];
      const v = nodes[e.to];
      const lane = xR + 8 + k * G.lane;
      const exitY = u.rowBottom + 9;
      const pts = [[u.cx, u.bottom], [u.cx, exitY], [lane, exitY]];
      if (v.kind === 'task' && v.col === v.rowN - 1) pts.push([lane, v.cy], [v.sx + v.sw, v.cy]);
      else pts.push([lane, v.rowTop - 9], [v.cx, v.rowTop - 9], [v.cx, v.top]);
      out.push(Object.assign({}, e, { pts: simplify(pts), lab: e.label ? { x: lane - 4, y: exitY + 12, a: 'end' } : null }));
    });
    const H = Math.max.apply(null, all.map((n) => n.box.y + n.box.h)) + G.pad + (nBack ? 12 : 0) + 6;
    return { width: W, height: Math.ceil(H), nodes, edges: out, leftUsed, xL, xR };
  }
}

function simplify(pts) {
  const p = pts.filter((q, i) => i === 0 || q[0] !== pts[i - 1][0] || q[1] !== pts[i - 1][1]);
  return p.filter((q, i) => {
    if (i === 0 || i === p.length - 1) return true;
    const a = p[i - 1];
    const b = p[i + 1];
    return !((a[0] === q[0] && q[0] === b[0]) || (a[1] === q[1] && q[1] === b[1]));
  });
}

/* SVG-путь со скруглёнными углами. */
function pathD(pts, r) {
  r = r == null ? 6 : r;
  let d = 'M' + pts[0][0].toFixed(1) + ' ' + pts[0][1].toFixed(1);
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i];
    if (i < pts.length - 1) {
      const a = pts[i - 1];
      const b = pts[i + 1];
      const l1 = Math.hypot(p[0] - a[0], p[1] - a[1]);
      const l2 = Math.hypot(b[0] - p[0], b[1] - p[1]);
      const k = Math.min(r, l1 / 2, l2 / 2);
      const s = [p[0] - ((p[0] - a[0]) / l1) * k, p[1] - ((p[1] - a[1]) / l1) * k];
      const e = [p[0] + ((b[0] - p[0]) / l2) * k, p[1] + ((b[1] - p[1]) / l2) * k];
      d += 'L' + s[0].toFixed(1) + ' ' + s[1].toFixed(1) + 'Q' + p[0].toFixed(1) + ' ' + p[1].toFixed(1) + ' ' + e[0].toFixed(1) + ' ' + e[1].toFixed(1);
    } else d += 'L' + p[0].toFixed(1) + ' ' + p[1].toFixed(1);
  }
  return d;
}

/* Точка на ломаной при доле пути t ∈ [0, 1]. */
function along(pts, t) {
  let len = 0;
  for (let i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  let d = Math.max(0, Math.min(1, t)) * len;
  for (let i = 1; i < pts.length; i++) {
    const l = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (d <= l || i === pts.length - 1) {
      const k = l ? Math.min(1, d / l) : 0;
      return [pts[i - 1][0] + (pts[i][0] - pts[i - 1][0]) * k, pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * k];
    }
    d -= l;
  }
  return pts[0];
}

/* ---------- симуляция: визуальная модель того же расчёта ---------- */

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* Сжатое время: схема за ~12 с. Точка — thin заявок, обслуживается в thin раз дольше: загрузка та же. */
function createSim(proc, res, lay, opts) {
  opts = opts || {};
  const A = res.A;
  const by = index(proc);
  const rand = rng(opts.seed || 7);
  const maxDots = opts.maxDots || 60;
  const cap = opts.cap || 400;
  const showQ = 12;
  const lam = A.perMonth / A.hoursPerPerson;
  let base = 0;
  Object.keys(res.steps).forEach((id) => { const s = res.steps[id]; base += s.v * (s.minutes / 60 + s.wait); });
  base = Math.max(base, 1);
  const thin = Math.max(1, (lam * base) / 24);
  const k = Math.max(0.25, base / 12);
  const minTravel = 0.55 * k;
  const outE = {};
  res.edges.forEach((e) => {
    const le = lay.edges.find((x) => x.from === e.from && x.to === e.to && x.label === e.label);
    if (!le) return;
    (outE[e.from] || (outE[e.from] = [])).push({ e, pts: le.pts, wait: edgeWait(by[e.from], by[e.to], A) });
  });
  const expo = (m) => -m * Math.log(1 - rand() * 0.999999);
  const toks = [];
  const busy = {};
  const queue = {};
  let now = 0;
  let nextArr = 0;
  let seq = 0;
  const ext = externals(proc);
  const servers = (r) => (r === SYS || ext.has(r) ? Infinity : peopleOf(A, r));

  function move(t, sid) {
    const list = outE[sid] || [];
    if (!list.length) { t.dead = true; return; }
    let x = rand();
    let pick = list[list.length - 1];
    for (const o of list) { if ((x -= o.e.p) <= 0) { pick = o; break; } }
    t.st = 'move';
    t.edge = pick;
    t.t0 = now;
    t.until = now + Math.max(pick.wait, minTravel);
  }
  function arrive(t, sid) {
    const s = by[sid];
    if (!isWork(s)) { if (s.kind === 'end') t.dead = true; else move(t, sid); return; }
    const r = roleOf(s);
    t.step = sid;
    if ((busy[r] || 0) < servers(r)) serve(t, sid);
    else { t.st = 'queue'; (queue[r] || (queue[r] = [])).push(t); }
  }
  function serve(t, sid) {
    const r = roleOf(by[sid]);
    busy[r] = (busy[r] || 0) + 1;
    t.st = 'serve';
    t.step = sid;
    t.t0 = now;
    t.until = now + expo((res.steps[sid].minutes / 60) * thin);
  }
  function advanceHours(h) {
    const T = now + h;
    for (let guard = 0; guard < 20000; guard++) {
      let ev = null;
      for (const t of toks) if (!t.dead && t.st !== 'queue' && t.until <= T && (!ev || t.until < ev.until)) ev = t;
      if (nextArr <= T && (!ev || nextArr < ev.until)) {
        now = nextArr;
        nextArr += expo(thin / lam);
        if (toks.length < cap) { const t = { id: ++seq }; toks.push(t); arrive(t, startOf(proc).id); }
        continue;
      }
      if (!ev) break;
      now = ev.until;
      if (ev.st === 'move') arrive(ev, ev.edge.e.to);
      else if (ev.st === 'serve') {
        const r = roleOf(by[ev.step]);
        busy[r]--;
        move(ev, ev.step);
        const q = queue[r];
        if (q && q.length) { const nt = q.shift(); serve(nt, nt.step); }
      }
      for (let i = toks.length - 1; i >= 0; i--) if (toks[i].dead) toks.splice(i, 1);
    }
    now = T;
  }
  advanceHours(base * 1.5);

  return {
    speed: k,
    thin,
    advance(sec) { advanceHours(Math.min(sec, 0.1) * k); },
    count() { return toks.length; },
    snapshot() {
      const dots = [];
      const more = {};
      const queues = {};
      const add = (x, y, kind) => { if (dots.length < maxDots) dots.push({ x, y, kind }); };
      const perStep = {};
      Object.keys(queue).forEach((r) => queue[r].forEach((t) => { (perStep[t.step] || (perStep[t.step] = [])).push(t); }));
      Object.keys(perStep).forEach((sid) => {
        const n = lay.nodes[sid];
        queues[sid] = perStep[sid].length;
        if (!n) return;
        perStep[sid].forEach((t, i) => { if (i < showQ) add(n.sx + 4 + i * 6.5, n.top - 7, 'queue'); });
        if (perStep[sid].length > showQ) more[sid] = perStep[sid].length - showQ;
      });
      const serving = {};
      toks.forEach((t) => {
        if (t.st !== 'serve') return;
        const n = lay.nodes[t.step];
        const j = (serving[t.step] = (serving[t.step] || 0) + 1) - 1;
        if (n) add(n.kind === 'task' ? n.sx + n.sw - 8 - j * 7 : n.cx - j * 7, n.kind === 'task' ? n.sy + n.sh - 8 : n.cy, 'serve');
      });
      toks.forEach((t) => {
        if (t.st !== 'move') return;
        const p = along(t.edge.pts, (now - t.t0) / Math.max(1e-9, t.until - t.t0));
        add(p[0], p[1], 'move');
      });
      return { dots, more, queues };
    },
  };
}

/* ---------- BPMN 2.0 с DI ---------- */

const esc = (s) => String(s == null ? '' : s).replace(/[<>&"]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' })[c]);

/* Дорожки — горизонтальные полосы ролей, столбец узла — ранг из раскладки. */
function toBpmn(proc, opts) {
  opts = opts || {};
  const by = index(proc);
  const { back, order } = dfsBack(proc);
  const E = [];
  proc.steps.forEach((s) => outs(s).forEach((o) => {
    if (by[o.to]) E.push({ id: 'f' + (E.length + 1), from: s.id, to: o.to, label: o.label, back: back.has(s.id + '>' + o.to) });
  }));
  const rank = ranks(proc, E);
  const roles = proc.roles.filter((r) => proc.steps.some((s) => roleOf(s) === r.id));
  proc.steps.forEach((s) => { if (!roles.some((r) => r.id === roleOf(s))) roles.push({ id: roleOf(s), name: roleOf(s) === SYS ? SYS_NAME : roleOf(s) }); });
  const LX = 30;
  const X0 = 70;
  const COL = 150;
  const ROW = 110;
  const pos = {};
  let y = 0;
  const lanes = roles.map((r) => {
    const ids = order.filter((id) => roleOf(by[id]) === r.id);
    const sub = {};
    ids.forEach((id) => {
      const k = ids.filter((o) => rank[o] === rank[id]).indexOf(id);
      sub[id] = k;
    });
    const depth = Math.max(1, Math.max.apply(null, ids.map((id) => sub[id] + 1)));
    ids.forEach((id) => {
      const s = by[id];
      const [w, h] = s.kind === 'task' ? [100, 80] : s.kind === 'decision' ? [50, 50] : [36, 36];
      const cx = LX + X0 + rank[id] * COL + 50;
      const cy = y + ROW / 2 + sub[id] * ROW;
      pos[id] = { x: cx - w / 2, y: cy - h / 2, w, h, cx, cy };
    });
    const lane = { r, ids, y, h: depth * ROW };
    y += lane.h;
    return lane;
  });
  const width = LX + X0 + (Math.max.apply(null, proc.steps.map((s) => rank[s.id])) + 1) * COL + 40;
  const tag = (s) => (s.kind === 'start' ? 'startEvent' : s.kind === 'end' ? 'endEvent' : s.kind === 'decision' ? 'exclusiveGateway' : s.auto ? 'serviceTask' : 'task');
  const L = [];
  L.push('<?xml version="1.0" encoding="UTF-8"?>');
  L.push('<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" ' +
    'xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" ' +
    'id="Definitions_1" targetNamespace="http://bpmn.io/schema/bpmn">');
  L.push('  <bpmn:collaboration id="Collaboration_1">');
  L.push('    <bpmn:participant id="Participant_1" name="' + esc((proc.title || 'Процесс') + (opts.mode ? ' · ' + opts.mode : '')) + '" processRef="Process_1" />');
  L.push('  </bpmn:collaboration>');
  L.push('  <bpmn:process id="Process_1" isExecutable="false">');
  L.push('    <bpmn:laneSet id="LaneSet_1">');
  lanes.forEach((l) => {
    L.push('      <bpmn:lane id="Lane_' + l.r.id + '" name="' + esc(l.r.name) + '">');
    l.ids.forEach((id) => L.push('        <bpmn:flowNodeRef>' + id + '</bpmn:flowNodeRef>'));
    L.push('      </bpmn:lane>');
  });
  L.push('    </bpmn:laneSet>');
  proc.steps.forEach((s) => {
    L.push('    <bpmn:' + tag(s) + ' id="' + s.id + '" name="' + esc(s.name) + '">');
    if (s.quote) L.push('      <bpmn:documentation>' + esc(s.quote) + '</bpmn:documentation>');
    L.push('    </bpmn:' + tag(s) + '>');
  });
  E.forEach((e) => L.push('    <bpmn:sequenceFlow id="' + e.id + '"' + (e.label ? ' name="' + esc(e.label) + '"' : '') +
    ' sourceRef="' + e.from + '" targetRef="' + e.to + '" />'));
  L.push('  </bpmn:process>');
  L.push('  <bpmndi:BPMNDiagram id="BPMNDiagram_1">');
  L.push('    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Collaboration_1">');
  const b = (x, yy, w, h) => '<dc:Bounds x="' + Math.round(x) + '" y="' + Math.round(yy) + '" width="' + Math.round(w) + '" height="' + Math.round(h) + '" />';
  L.push('      <bpmndi:BPMNShape id="Participant_1_di" bpmnElement="Participant_1" isHorizontal="true">' + b(0, 0, width, y) + '</bpmndi:BPMNShape>');
  lanes.forEach((l) => L.push('      <bpmndi:BPMNShape id="Lane_' + l.r.id + '_di" bpmnElement="Lane_' + l.r.id + '" isHorizontal="true">' + b(LX, l.y, width - LX, l.h) + '</bpmndi:BPMNShape>'));
  proc.steps.forEach((s) => {
    const p = pos[s.id];
    L.push('      <bpmndi:BPMNShape id="' + s.id + '_di" bpmnElement="' + s.id + '"' + (s.kind === 'decision' ? ' isMarkerVisible="true"' : '') + '>' + b(p.x, p.y, p.w, p.h) + '</bpmndi:BPMNShape>');
  });
  E.forEach((e) => {
    const a = pos[e.from];
    const c = pos[e.to];
    let w;
    if (e.back) {
      const low = Math.max(a.y + a.h, c.y + c.h) + 18;
      w = [[a.cx, a.y + a.h], [a.cx, low], [c.cx, low], [c.cx, c.y + c.h]];
    } else if (Math.abs(a.cy - c.cy) < 1) w = [[a.x + a.w, a.cy], [c.x, c.cy]];
    else {
      const mx = c.x - 25;
      w = [[a.x + a.w, a.cy], [mx, a.cy], [mx, c.cy], [c.x, c.cy]];
    }
    L.push('      <bpmndi:BPMNEdge id="' + e.id + '_di" bpmnElement="' + e.id + '">' +
      w.map((p) => '<di:waypoint x="' + Math.round(p[0]) + '" y="' + Math.round(p[1]) + '" />').join('') + '</bpmndi:BPMNEdge>');
  });
  L.push('    </bpmndi:BPMNPlane>');
  L.push('  </bpmndi:BPMNDiagram>');
  L.push('</bpmn:definitions>');
  return L.join('\n') + '\n';
}

return {
  DEFAULTS, SYS, SYS_NAME, OVER_DAYS,
  assume, applyAnswers, withAnswers, report, externals, edges, visits, calc, actions, after, minutesOf, rawMinutes, edgeWait,
  wrap, layout, pathD, along, rng, createSim, toBpmn,
};
});
