/* Окно процесса и интервью (engine.js нужен раньше). Всё от модели и из ответов — только через textContent. */
(function () {
'use strict';
const E = window.PrEngine;
const NS = 'http://www.w3.org/2000/svg';
const EXAMPLES = [['sales', 'Заявка → КП → договор'], ['invoice', 'Счёт поставщика'], ['returns', 'Возврат товара'], ['onboarding', 'Выход сотрудника']];
const TOPICS = [['заявки', 'Заявки'], ['счета', 'Счета'], ['возвраты', 'Возвраты'], ['выход', 'Выход сотрудника']];
const ACT = { keep: 'оставить', automate: 'автоматизировать', remove: 'убрать', merge: 'объединить' };
const KIND = { start: 'старт', task: 'задача', decision: 'решение', end: 'конец' };
const ERR = {
  unavailable: 'Сервис недоступен — примеры работают без него.',
  offline: 'Живой режим выключен — работают примеры.',
  timeout: 'Отчёт не собрался — попробуйте ещё раз.',
  stt_failed: 'Не расслышал — повторите, пожалуйста. Вопрос не потрачен.',
  model_format: 'Модель ответила не по формату — попробуйте ещё раз.',
  not_found: 'Не найдено: интервью закрыто или ссылка старше 30 суток.',
};
const SRC = { answer: 'из ответа', assume: 'допущение', edit: 'ваша правка' };
const MAX_REC_S = 30;
const MIC = 'M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm6-3h-2a4 4 0 0 1-8 0H6a6 6 0 0 0 5 5.9V21h2v-3.1a6 6 0 0 0 5-5.9z';
const ASM = [
  ['perMonth', 'заявок в месяц', 1], ['rate', 'ставка часа, ₽', 10], ['hoursPerPerson', 'часов работы человека в месяц', 1],
  ['handoffH', 'ожидание при передаче между ролями, ч', 0.5], ['autoFactor', 'время шага «авто» от ручного, доля', 0.05],
  ['taskMin', 'минут на задачу, если в тексте нет', 1], ['decisionMin', 'минут на решение, если в тексте нет', 1],
  ['loopShare', 'доля ветки-петли назад', 0.05], ['dayH', 'рабочий день, ч', 1],
];
const MINUS = '−';
const STEP = { extract: 'Разбор', improve: 'Улучшения', stt: 'Речь', draft: 'Черновик', ask: 'Вопрос' };
let uid = 0;

// до 9 знаков: 5,2499999999 из итераций цепи Маркова — это 5,25
const nf = (min, max) => { const f = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: min, maximumFractionDigits: max }); return (v) => f.format(Math.round(v * 1e9) / 1e9); };
const n0 = nf(0, 0);
const n1 = nf(1, 1);
const n2 = nf(2, 2);
const nv = nf(0, 1);
const sec = (ms) => n1(ms / 1000) + ' с';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function h(tag, a, kids) {
  const el = document.createElement(tag);
  Object.keys(a || {}).forEach((k) => {
    if (k === 'text') el.textContent = a[k];
    else if (k === 'on') Object.keys(a.on).forEach((ev) => el.addEventListener(ev, a.on[ev]));
    else if (a[k] === true) el.setAttribute(k, '');
    else if (a[k] != null && a[k] !== false) el.setAttribute(k, a[k]);
  });
  (kids || []).forEach((c) => c && el.append(c));
  return el;
}
function s(tag, a, parent) {
  const el = document.createElementNS(NS, tag);
  Object.keys(a || {}).forEach((k) => { if (k === 'text') el.textContent = a[k]; else el.setAttribute(k, a[k]); });
  if (parent) parent.appendChild(el);
  return el;
}
const roleVar = (id) => (id === E.SYS ? 'var(--ok)' : /^r[1-6]$/.test(id) ? 'var(--pr-' + id + ')' : 'var(--muted)');
const fmtDays = (d) => (isFinite(d) ? n1(d) : '> ' + E.OVER_DAYS);

function mount(root, opts) {
  opts = Object.assign({ api: '/api/process/', examplesBase: null, first: 'sales', motion: true, mail: null, poll: 1000 }, opts);
  const mq = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  // ans — поля из ответов (null у примеров), A0 — допущения без правок, rep — id отчёта
  const st = { data: null, name: null, mode: 'before', A: E.assume(), A0: null, ans: null, edited: {}, rep: null, mins: {}, over: {}, sel: null, width: 0, shown: {}, ext: new Set() };
  const iv = { id: null, count: 0, busy: false, view: 'start' };
  const cache = {};
  const arrow = 'pr-a' + ++uid;
  let calcB, calcA, cur, lay, sim, raf = 0, last = 0, onScreen = true;

  /* ---------- каркас ---------- */
  root.classList.add('pr');
  root.textContent = '';
  const exBar = h('div', { class: 'pr-ex', role: 'group', 'aria-label': 'Примеры' });
  const exBtns = EXAMPLES.map(([name, label]) => exBar.appendChild(h('button', { type: 'button', 'aria-pressed': 'false', text: label, on: { click: () => { root.classList.remove('pr-first'); loadExample(name); } } })));
  const title = h('h3', { class: 'pr-h' });
  const meta = h('p', { class: 'pr-meta' });
  const legend = h('ul', { class: 'pr-legend', 'aria-label': 'Роли' });
  const note = h('p', { class: 'pr-note' });
  const svg = s('svg', { class: 'pr-svg', role: 'group', 'aria-label': 'Схема процесса' });
  const defs = s('defs', {}, svg);
  [['', 'pr-arrow'], ['b', 'pr-arrow-b']].forEach(([k, cls]) => {
    const m = s('marker', { id: arrow + k, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 6.5, markerHeight: 6.5, orient: 'auto' }, defs);
    s('path', { d: 'M0 0.5L8 4L0 7.5z', class: cls }, m);
  });
  const pat = s('pattern', { id: arrow + 'h', width: 4, height: 4, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' }, defs);
  s('rect', { width: 4, height: 4, class: 'pr-hbg' }, pat);
  s('rect', { width: 1.6, height: 4, class: 'pr-hl' }, pat);
  const col = (id) => (st.ext && st.ext.has(id) ? 'var(--muted)' : roleVar(id));
  const gE = s('g', {}, svg);
  const gN = s('g', {}, svg);
  const gD = s('g', { 'aria-hidden': 'true' }, svg);
  const dots = [];
  for (let i = 0; i < 60; i++) dots.push(s('circle', { r: 3, class: 'pr-dot', visibility: 'hidden' }, gD));
  const more = {};
  const msg = h('p', { class: 'pr-msg', hidden: true });
  svg.setAttribute('hidden', '');
  title.textContent = 'Загружаю пример…';
  title.classList.add('pr-wait');
  const wrap = h('div', { class: 'pr-wrap' }, [msg, svg]);

  const seg = h('div', { class: 'pr-seg', role: 'group', 'aria-label': 'Было или стало' });
  const modeBtn = {};
  [['before', 'Было'], ['after', 'Стало']].forEach(([m, t]) => {
    modeBtn[m] = seg.appendChild(h('button', { type: 'button', 'aria-pressed': String(m === 'before'), text: t, on: { click: () => setMode(m) } }));
  });
  const stat = {};
  const stats = h('div', { class: 'pr-stats' }, [['hours', 'ч работы в месяц'], ['rub', '₽ в месяц'], ['days', 'раб. дней на заявку']].map(([k, u]) => {
    stat[k] = { num: h('div', { class: 'pr-num', text: '—' }), diff: h('div', { class: 'pr-diff' }), val: null, raf: 0 };
    return h('div', { class: 'pr-stat' }, [stat[k].num, h('div', { class: 'pr-unit', text: u }), stat[k].diff]);
  }));
  const bar = h('div', { class: 'pr-bar' }, [h('div', { class: 'pr-row' }, [seg, h('span', { class: 'pr-hint', text: 'тап по шагу — подробности' })]), stats]);
  const sum = h('p', { class: 'pr-sum', hidden: true });

  const asmBox = h('div', { class: 'pr-fields' });
  const asm = h('details', { class: 'pr-sec' }, [h('summary', { text: 'Допущения' }),
    h('p', { class: 'pr-small', text: 'Цифры считает код из этих допущений.' }), asmBox,
    h('div', { class: 'pr-acts' }, [h('button', { type: 'button', class: 'pr-btn pr-ghost', text: 'Сбросить допущения', on: { click: () => { st.A = E.assume(st.A0); st.edited = {}; buildAsm(); recalc(); } } })])]);

  /* интервью */
  const ivQ = h('p', { class: 'pr-ask', 'aria-live': 'polite' });
  const ivCnt = h('span', { class: 'pr-small' });
  const ivGot = h('p', { class: 'pr-got', hidden: true });
  const topics = h('div', { class: 'pr-topics', role: 'group', 'aria-label': 'Какой процесс' }, TOPICS.map(([t, l]) =>
    h('button', { type: 'button', text: l, on: { click: () => begin(t, null) } })));
  const ta = h('textarea', { class: 'pr-ta', maxlength: 500, rows: 2 });
  const left = h('span', { class: 'pr-small', text: '0 / 500' });
  const send = h('button', { type: 'button', class: 'pr-btn', on: { click: submit } });
  let micOk = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  const mic = h('button', { type: 'button', class: 'pr-mic', 'aria-label': 'Ответить голосом', on: { click: micTap } });
  const micI = s('svg', { viewBox: '0 0 24 24', width: 22, height: 22, 'aria-hidden': 'true' }, mic);
  const micP = s('path', { d: MIC }, micI);
  const recT = h('span', { class: 'pr-small' });
  const lvI = h('i');
  const recBox = h('div', { class: 'pr-recbox', hidden: true }, [h('span', { class: 'pr-lv' }, [lvI]), recT]);
  const enough = h('button', { type: 'button', class: 'pr-btn pr-ghost', text: 'Хватит — строй отчёт', on: { click: finish } });
  const enoughHint = h('span', { class: 'pr-small', text: 'после трёх ответов' });
  const ivErr = h('p', { class: 'pr-err', hidden: true, role: 'status' });
  const ivInfo = h('p', { class: 'pr-small', text: 'проверяю бюджет…' });
  const sendRow = h('div', { class: 'pr-row' }, [left, send]);
  const ans = h('div', { class: 'pr-ans' }, [mic, ta, recBox]);
  const enoughRow = h('div', { class: 'pr-row pr-left' }, [enough, enoughHint]);
  const clearTa = () => { ta.value = ''; left.textContent = '0 / 500'; };
  ta.addEventListener('input', () => { left.textContent = ta.value.length + ' / 500'; });
  ta.addEventListener('focus', () => { if (send.disabled && !iv.busy) status(); });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); } });
  const ivSec = h('section', { class: 'pr-sec pr-iv' }, [h('div', { class: 'pr-row' }, [h('h3', { text: 'Интервью' }), ivCnt]),
    ivQ, ivGot, topics, ans, sendRow, enoughRow, ivErr, ivInfo]);

  const rep = h('section', { class: 'pr-rep', hidden: true, 'aria-label': 'Отчёт' });
  const top = h('p', { class: 'pr-err', hidden: true, role: 'status' });

  const dl = h('section', { class: 'pr-sec' }, [h('h3', { text: 'Файл BPMN 2.0' }),
    h('p', { class: 'pr-small', text: 'Открывается в bpmn.io и Camunda Modeler: дорожки по ролям, подписи веток, координаты схемы.' }),
    h('div', { class: 'pr-acts' }, [['before', 'Скачать .bpmn · Было'], ['after', 'Скачать .bpmn · Стало']].map(([m, t]) =>
      h('button', { type: 'button', class: 'pr-btn pr-ghost', text: t, on: { click: () => download(m) } })))]);

  const shade = h('div', { class: 'pr-shade', hidden: true, on: { click: closeSheet } });
  const sheet = h('div', { class: 'pr-sheet', hidden: true, role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Шаг процесса' });
  // Пока первый пример не пришёл (и до переключения), блок держит высоту --pr-reserve: без сдвига макета.
  root.classList.add('pr-first');
  const body = h('div', { class: 'pr-body' }, [title, meta, legend, note, wrap]);
  root.append(exBar, top, body, bar, sum, rep, asm, ivSec, dl, shade, sheet);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !sheet.hidden) closeSheet(); });

  /* ---------- данные ---------- */
  /* GET или POST: JSON / FormData (аудио); 202 без тела — норма. */
  async function api(path, data) {
    const fd = data instanceof FormData;
    const init = data === undefined ? { cache: 'no-store' } : { cache: 'no-store', method: 'POST', body: fd ? data : JSON.stringify(data), headers: fd ? {} : { 'Content-Type': 'application/json' } };
    let r;
    try { r = await fetch(opts.api + path, init); } catch (e) { throw { code: 'unavailable' }; }
    let body = null;
    try { body = await r.json(); } catch (e) { body = null; }
    if (!r.ok || (!body && r.status !== 202)) throw { code: (body && body.code) || (r.status >= 500 || !body ? 'unavailable' : 'http_' + r.status), message: body && body.message };
    return body || {};
  }
  const ivPath = (tail) => 'interviews/' + encodeURIComponent(iv.id) + (tail || '');

  const press = (n) => exBtns.forEach((b, i) => b.setAttribute('aria-pressed', String(EXAMPLES[i][0] === n)));
  async function loadExample(name) {
    press(name);
    top.hidden = true;
    if (iv.id) quit();
    if (st.ans) {
      st.ans = st.A0 = st.rep = null;
      st.A = E.assume();
      st.edited = {};
      setHash(null);
    }
    try {
      if (!cache[name]) {
        cache[name] = opts.examplesBase
          ? await fetch(opts.examplesBase + name + '.json').then((r) => { if (!r.ok) throw 0; return r.json(); })
          : await api('examples/' + name);
      }
    } catch (e) {
      msg.textContent = 'Пример не загрузился: сервис недоступен.';
      msg.hidden = false;
      return;
    }
    if (!iv.id && !st.rep) show(cache[name], name); // пока грузился, начали интервью или открыли отчёт
  }

  function show(data, name) {
    st.data = data;
    st.name = name;
    st.mins = {};
    st.over = {};
    st.shown = {};
    closeSheet();
    st.ext = E.externals(data.process);
    const people = {};
    data.process.roles.forEach((r) => { people[r.id] = st.A.people[r.id] || 1; });
    st.A.people = people;
    title.textContent = data.process.title || data.title || 'Процесс';
    meta.textContent = '';
    // строки steps: v1 — {step}, интервью — {name}
    const rows = (data.steps || []).map((x) => Object.assign({ k: x.step || x.name }, x));
    const names = rows.map((x) => x.k).filter((k, i, a) => a.indexOf(k) === i);
    if (names.length || name === 'draft') meta.append(name === 'draft' ? 'После ответа ' + iv.count + (names.length ? ': ' : '') : name === 'report' ? 'Интервью: ' : 'Записанный прогон: ');
    modeBtn.after.disabled = name === 'draft';
    names.forEach((k, i) => {
      const rs = rows.filter((x) => x.k === k);
      const ms = rs.reduce((a, x) => a + (x.ms || 0), 0);
      const rub = rs.reduce((a, x) => a + (x.rub || 0), 0);
      if (i) meta.append(' → ');
      meta.append(h('b', { text: STEP[k] || k }), ' ' + sec(ms) + ' · ' + n2(rub) + ' ₽' + (rs.length < 2 ? '' : name === 'report' ? ' ×' + rs.length : ' (попыток: ' + rs.length + ')'));
    });
    const fl = data.process.flags || {};
    const nt = ((data.improve && data.improve.flags) || {}).numbers_in_text || [];
    const extra = [];
    if ((fl.unquoted || []).length) extra.push('без цитаты в тексте: ' + fl.unquoted.length);
    if ((fl.numbers_dropped || []).length) extra.push('код отсёк чисел без опоры: ' + fl.numbers_dropped.length);
    if (nt.length) extra.push('цифр в советах очищено: ' + nt.length);
    if (extra.length) meta.append(' · ' + extra.join(' · '));
    buildAsm();
    msg.hidden = true;
    svg.removeAttribute('hidden');
    title.classList.remove('pr-wait');
    recalc();
  }

  /* Процесс с минутами и ожиданием из ответов, поверх — правки посетителя. */
  function base() {
    const p = E.withAnswers(st.data.process, st.A);
    return Object.assign({}, p, { steps: p.steps.map((x) => (st.mins[x.id] != null ? Object.assign({}, x, { minutes: st.mins[x.id] }) : x)) });
  }
  // откуда допущение: правка посетителя, ответ интервью или по умолчанию
  const srcOf = (k) => (st.edited[k] ? 'edit' : (st.ans || []).indexOf(k === 'perMonth' ? 'volume' : k) >= 0 ? 'answer' : 'assume');

  function recalc() {
    if (!st.data) return;
    const b = base();
    const a = E.after(b, st.data.improve, st.over, st.A);
    calcB = E.calc(b, st.A);
    calcA = E.calc(a, st.A);
    cur = st.mode === 'before' ? b : a;
    render();
  }

  /* ---------- схема ---------- */
  function render() {
    const W = Math.round(Math.max(300, Math.min(520, wrap.clientWidth || 360)));
    st.width = W;
    const res = st.mode === 'before' ? calcB : calcA;
    lay = E.layout(cur, { width: W });
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + lay.height);
    svg.setAttribute('width', W);
    svg.setAttribute('height', lay.height);
    gE.textContent = '';
    lay.edges.forEach((e) => {
      s('path', { d: E.pathD(e.pts, 6), class: 'pr-e' + (e.back ? ' pr-back' : ''), 'marker-end': 'url(#' + arrow + (e.back ? 'b' : '') + ')' }, gE);
      if (e.lab) s('text', { x: e.lab.x, y: e.lab.y, 'text-anchor': e.lab.a, class: 'pr-el pr-halo', text: e.label }, gE);
    });
    const byId = {};
    cur.steps.forEach((x) => { byId[x.id] = x; });
    const seen = {};
    Object.keys(lay.nodes).forEach((id) => {
      const n = lay.nodes[id];
      let g = gN.querySelector('[data-id="' + id + '"]');
      const fresh = !g;
      if (fresh) {
        g = s('g', { 'data-id': id, class: 'pr-node pr-in', tabindex: 0, role: 'button' }, gN);
        g.addEventListener('click', () => openSheet(id));
        g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openSheet(id); } });
      }
      seen[id] = 1;
      g.classList.remove('pr-out');
      drawNode(g, n, byId[id], res.steps[id]);
      g.style.transform = 'translate(' + n.cx.toFixed(1) + 'px,' + n.cy.toFixed(1) + 'px)';
    });
    Array.prototype.slice.call(gN.children).forEach((g) => {
      if (seen[g.getAttribute('data-id')]) return;
      g.classList.add('pr-out');
      setTimeout(() => { if (g.classList.contains('pr-out')) g.remove(); }, 400);
    });
    Object.keys(more).forEach((k) => { more[k].remove(); delete more[k]; });
    drawLegend(res);
    drawStats();
    drawReport();
    sum.hidden = st.mode !== 'after' || !!st.rep || !(st.data.improve && st.data.improve.summary);
    sum.textContent = st.data.improve && st.data.improve.summary ? 'Модель: ' + st.data.improve.summary : '';
    sim = E.createSim(cur, res, lay, { seed: 11 });
    drawDots();
    kick();
    if (st.sel) fillSheet();
  }

  function drawNode(g, n, step, info) {
    g.textContent = '';
    g.setAttribute('class', 'pr-node' + (g.classList.contains('pr-in') ? ' pr-in' : '') + (st.sel === n.id ? ' pr-sel' : ''));
    g.style.setProperty('--c', col(step.auto ? E.SYS : step.role));
    g.setAttribute('aria-label', KIND[step.kind] + ': ' + step.name);
    const hot = info && info.level !== 'ok' && (step.kind === 'task' || step.kind === 'decision');
    if (hot) g.classList.add('pr-hot');
    const lines = (x, y0, cls, anchor) => {
      const t = s('text', { class: cls, 'text-anchor': anchor || 'start' }, g);
      n.lines.forEach((l, i) => s('tspan', { x, y: y0 + i * 12, text: l }, t));
    };
    const badge = (x, y) => {
      s('rect', { x: x - 15, y: y - 7, width: 30, height: 14, rx: 7, class: 'pr-auto' }, g);
      s('text', { x, y: y + 3.5, 'text-anchor': 'middle', class: 'pr-autot', text: 'авто' }, g);
    };
    if (n.kind === 'task') {
      const w = n.sw;
      const hh = n.sh;
      if (hot) s('rect', { x: -w / 2 - 3, y: -hh / 2 - 3, width: w + 6, height: hh + 6, rx: 12, class: 'pr-hotbg' }, g);
      s('rect', { x: -w / 2, y: -hh / 2, width: w, height: hh, rx: 9, class: 'pr-box' }, g);
      const ex = st.ext.has(step.role) && !step.auto;
      s('rect', { x: -w / 2 + 4, y: -hh / 2 + 6, width: ex ? 5 : 3.5, height: hh - 12, rx: 1.75, class: ex ? 'pr-hatch' : 'pr-stripe', fill: ex ? 'url(#' + arrow + 'h)' : null }, g);
      lines(-w / 2 + 13, 4 - ((n.lines.length - 1) * 12) / 2, 'pr-t');
      if (step.auto) badge(w / 2 - 19, -hh / 2);
    } else {
      const r = n.sw / 2;
      if (n.kind === 'decision') {
        if (hot) s('path', { d: `M0 ${-r - 4}L${r + 4} 0L0 ${r + 4}L${-r - 4} 0Z`, class: 'pr-hotbg' }, g);
        s('path', { d: `M0 ${-r}L${r} 0L0 ${r}L${-r} 0Z`, class: 'pr-gw' }, g);
        s('path', { d: 'M-5 -5L5 5M5 -5L-5 5', class: 'pr-gwx' }, g);
        if (step.auto) badge(-r - 21, 0);
      } else {
        s('circle', { r, class: 'pr-ev', 'stroke-width': n.kind === 'end' ? 3 : 1.8 }, g);
        if (n.kind === 'end') s('circle', { r: 5, class: 'pr-evd' }, g);
      }
      lines(n.lx - n.cx, 4 - ((n.lines.length - 1) * 12) / 2, 'pr-ts');
    }
    if (hot && n.kind === 'task' && info.queueN >= 0.5) {
      const q = isFinite(info.queueN) ? 'очередь ' + n0(Math.max(1, Math.round(info.queueN))) + ' · ждут ' + fmtDays(info.queueDays) + ' дн' : 'очередь растёт · ждут > ' + E.OVER_DAYS + ' дн';
      s('text', { x: -n.sw / 2, y: n.sh / 2 + 13, class: 'pr-ql pr-halo', text: q }, g);
    }
  }

  function drawLegend(res) {
    legend.textContent = '';
    cur.roles.forEach((r) => {
      const R = res.roles[r.id];
      const ex = st.ext.has(r.id);
      if (!R && r.id !== E.SYS && !(ex && cur.steps.some((x) => x.role === r.id))) return;
      const li = h('li', { class: R ? 'pr-' + R.level : ex ? 'pr-ext' : '' }, [h('i'), r.name, h('em', { text: R ? n0(R.rho * 100) + '%' : ex ? 'внешний' : 'авто' })]);
      li.style.setProperty('--c', col(r.id));
      legend.append(li);
    });
    const b = res.bottleneck && res.roles[res.bottleneck];
    const name = b && (cur.roles.find((r) => r.id === res.bottleneck) || {}).name;
    note.className = 'pr-note' + (b && b.level !== 'ok' ? ' pr-bad' : '');
    note.textContent = !b ? '' : b.level === 'over'
      ? name + ' не справляется: загрузка ' + n0(b.rho * 100) + '%, очередь растёт.'
      : b.level === 'hot' ? 'Узкое место — ' + name + ': загрузка ' + n0(b.rho * 100) + '%, заявки копятся.'
        : 'Узких мест нет: больше всех загружен ' + name + ' — ' + n0(b.rho * 100) + '%.';
  }

  /* ---------- счётчики ---------- */
  function drawStats() {
    const c = st.mode === 'before' ? calcB : calcA;
    tween('hours', c.hours, n0);
    tween('rub', c.rub, n0);
    tween('days', c.days, fmtDays);
    const after = st.mode === 'after';
    [['hours', n0, ' ч/мес'], ['rub', n0, ' ₽'], ['days', n1, ' дн']].forEach(([k, f, unit]) => {
      const el = stat[k].diff;
      const v = calcA[k] - calcB[k];
      const was = isFinite(calcB[k]);
      const r = Math.round(v * (k === 'days' ? 10 : 1));
      el.className = 'pr-diff' + (after && (r || !was) ? (r < 0 || !was ? ' pr-good' : ' pr-worse') : '');
      el.textContent = !after || !isFinite(calcA[k]) ? '' : !was ? 'было > ' + E.OVER_DAYS : r ? (r < 0 ? MINUS : '+') + f(Math.abs(v)) + unit : 'без изменений';
    });
  }

  function tween(k, to, f) {
    const o = stat[k];
    cancelAnimationFrame(o.raf);
    const from = o.val;
    o.val = to;
    if (from == null || !isFinite(from) || !isFinite(to) || reduced()) { o.num.textContent = f(to); return; }
    const t0 = performance.now();
    const step = (t) => {
      const p = Math.min(1, (t - t0) / 700);
      const e = 1 - Math.pow(1 - p, 3);
      o.num.textContent = f(from + (to - from) * e);
      if (p < 1) o.raf = requestAnimationFrame(step);
    };
    o.raf = requestAnimationFrame(step);
  }

  /* ---------- отчёт: числа из engine.js, рядом — откуда ---------- */
  function drawReport() {
    rep.hidden = !st.rep;
    dl.hidden = !!st.rep;
    if (!st.rep) return;
    const R = E.report(base(), st.data.improve, st.over, Object.assign({}, st.A, { minutes: null, wait_hours: null }), st.ans);
    const A = R.A;
    const p = st.data.process;
    const roleName = (id) => (p.roles.find((r) => r.id === id) || { name: id }).name;
    const tag = (k) => h('span', { class: 'pr-src pr-' + k, text: SRC[k] });
    const minTag = (id) => tag(st.mins[id] != null ? 'edit' : R.src.minutes[id]);
    const sub = (t) => h('h4', { text: t });
    rep.textContent = '';
    const cmp = h('div', { class: 'pr-cmp' }, [h('span'), h('b', { text: 'Было' }), h('b', { text: 'Стало' })]);
    [['hours', 'ч работы в месяц', n0], ['rub', '₽ в месяц', n0], ['days', 'раб. дней на заявку', fmtDays]].forEach(([k, l, f]) =>
      cmp.append(h('span', { text: l }), h('span', { text: f(R.before[k]) }), h('span', { text: f(R.after[k]) })));
    const worst = h('ol', { class: 'pr-worst' }, R.worst.map((w) => {
      const why = h('div', { class: 'pr-why' }, [n0(w.minutes) + ' мин за проход ', minTag(w.id)]);
      if (w.v > 1.005) why.append(' · ' + n2(w.v) + ' прохода на заявку — возвраты на доработку');
      why.append(' · загрузка роли «' + roleName(w.role) + '» ' + n0(w.rho * 100) + '%' + (w.level === 'over' ? ' — не справляется' : w.level === 'hot' ? ' — копится очередь' : ''));
      return h('li', {}, [h('b', { text: w.name }), h('div', { class: 'pr-big', text: n0(w.hours) + ' ч · ' + n0(w.rub) + ' ₽ в месяц' }), why]);
    }));
    const f = R.first;
    const firstBox = f ? h('div', {}, [h('b', { text: f.name }),
      h('div', { class: 'pr-big pr-good', text: MINUS + n0(f.hours) + ' ч и ' + MINUS + n0(f.rub) + ' ₽ в месяц' }),
      h('div', { class: 'pr-why', text: 'дней на заявку: ' + fmtDays(R.before.days) + ' → ' + fmtDays(f.days) + (f.how ? ' · как: ' + f.how : '') })])
      : h('p', { class: 'pr-small', text: 'Модель не предлагает автоматизировать шаги.' });
    const kv = h('dl', { class: 'pr-kv' });
    const row = (k, v) => kv.append(h('dt', { text: k }), h('dd', {}, v));
    row('заявок в месяц', [n0(A.perMonth) + ' ', tag(srcOf('perMonth'))]);
    row('ставка часа', [n0(A.rate) + ' ₽ ', tag(srcOf('rate'))]);
    const ms = Object.keys(R.src.minutes);
    const mAns = ms.filter((id) => st.mins[id] == null && R.src.minutes[id] === 'answer').length;
    row('минуты шагов', [mAns + ' из ' + ms.length + ' ', tag('answer'), ' · остальные ' + n0(A.taskMin) + ' на задачу, ' + n0(A.decisionMin) + ' на решение ', tag('assume')]);
    const ws = Object.keys(R.src.wait);
    const wAns = ws.filter((id) => R.src.wait[id] === 'answer').length;
    row('ожидание', [wAns + ' из ' + ws.length + ' передач ', tag('answer'), ' · остальные по ' + nv(A.handoffH) + ' ч ', tag('assume')]);
    row('людей в ролях', Object.keys(R.before.roles).map((r) => h('span', { class: 'pr-nw' }, [roleName(r) + ' — ' + nv(A.people[r] || 1) + ' ', tag(srcOf(r + '.people'))])));
    row('часов человека', [n0(A.hoursPerPerson) + ' в месяц ', tag(srcOf('hoursPerPerson'))]);
    const link = location.origin + location.pathname + '#report=' + st.rep;
    const said = h('p', { class: 'pr-small', role: 'status' });
    const copy = () => (navigator.clipboard ? navigator.clipboard.writeText(link) : Promise.reject())
      .then(() => { said.textContent = 'Ссылка скопирована — отчёт открывается по ней 30 суток.'; }, () => { said.textContent = 'Скопируйте ссылку: ' + link; });
    const btn = (t, fn) => h('button', { type: 'button', class: 'pr-btn pr-ghost', text: t, on: { click: fn } });
    const mail = /^[^\s@?&/]+@[^\s@?&/]+$/.test(opts.mail || '') && h('a', { class: 'pr-btn', text: 'Обсудить внедрение',
      href: 'mailto:' + opts.mail + '?subject=' + encodeURIComponent('Обсудить внедрение: ' + (p.title || 'процесс')) + '&body=' + encodeURIComponent('Отчёт: ' + link) });
    const summary = st.data.improve && st.data.improve.summary;
    rep.append(h('h3', { text: 'Где теряете часы и рубли' }), cmp, sub('Больше всего теряется'), worst, sub('Автоматизировать первым'), firstBox,
      summary ? h('p', { class: 'pr-small', text: 'Модель: ' + summary }) : '', sub('Откуда цифры'), kv,
      h('div', { class: 'pr-acts' }, [h('span', { text: 'Скачать .bpmn' }), btn('Было', () => download('before')), btn('Стало', () => download('after'))]),
      h('div', { class: 'pr-acts' }, [btn('Ссылка на отчёт', copy), mail]), said);
  }

  function setMode(m) {
    if (m === st.mode || !st.data) return;
    st.mode = m;
    Object.keys(modeBtn).forEach((k) => modeBtn[k].setAttribute('aria-pressed', String(k === m)));
    recalc();
  }

  /* ---------- точки-заявки ---------- */
  const reduced = () => !opts.motion || mq.matches;
  const running = () => sim && !reduced() && onScreen && !document.hidden;
  function kick() { if (!raf && running()) { last = 0; raf = requestAnimationFrame(frame); } }
  function frame(t) {
    raf = 0;
    if (!running()) { drawDots(); return; }
    if (last) sim.advance((t - last) / 1000);
    last = t;
    drawDots();
    raf = requestAnimationFrame(frame);
  }
  function drawDots() {
    const snap = sim && !reduced() ? sim.snapshot() : { dots: [], more: {} };
    dots.forEach((c, i) => {
      const d = snap.dots[i];
      if (!d) { c.setAttribute('visibility', 'hidden'); return; }
      c.setAttribute('visibility', 'visible');
      c.setAttribute('cx', d.x.toFixed(1));
      c.setAttribute('cy', d.y.toFixed(1));
      c.setAttribute('class', 'pr-dot' + (d.kind === 'queue' ? ' pr-q' : ''));
    });
    Object.keys(lay ? lay.nodes : {}).forEach((id) => {
      const m = snap.more[id];
      if (!m) { if (more[id]) more[id].setAttribute('visibility', 'hidden'); return; }
      const n = lay.nodes[id];
      if (!more[id]) more[id] = s('text', { class: 'pr-more pr-halo' }, gD);
      more[id].setAttribute('visibility', 'visible');
      more[id].setAttribute('x', (n.sx + 4 + 12 * 6.5).toFixed(1));
      more[id].setAttribute('y', (n.top - 4).toFixed(1));
      more[id].textContent = '+' + m;
    });
  }
  if (window.IntersectionObserver) new IntersectionObserver((es) => { onScreen = es[es.length - 1].isIntersecting; kick(); }).observe(svg);
  document.addEventListener('visibilitychange', kick);
  if (mq.addEventListener) mq.addEventListener('change', () => { drawDots(); kick(); });
  let rt = 0;
  const relayout = () => { clearTimeout(rt); rt = setTimeout(() => { if (cur && Math.round(Math.max(300, Math.min(520, wrap.clientWidth))) !== st.width) render(); }, 150); };
  window.addEventListener('resize', relayout);

  /* ---------- панель шага ---------- */
  function openSheet(id) {
    st.sel = id;
    gN.querySelectorAll('.pr-node').forEach((g) => g.classList.toggle('pr-sel', g.getAttribute('data-id') === id));
    fillSheet();
    shade.hidden = false;
    sheet.hidden = false;
    const x = sheet.querySelector('.pr-x');
    if (x) x.focus({ preventScroll: true });
  }
  function closeSheet() {
    if (sheet.hidden) return;
    sheet.hidden = true;
    shade.hidden = true;
    const g = st.sel && gN.querySelector('[data-id="' + st.sel + '"]');
    st.sel = null;
    gN.querySelectorAll('.pr-sel').forEach((x) => x.classList.remove('pr-sel'));
    if (g) g.focus({ preventScroll: true });
  }

  function fillSheet() {
    const p = st.data.process;
    const step = p.steps.find((x) => x.id === st.sel);
    if (!step) { closeSheet(); return; }
    const role = p.roles.find((r) => r.id === step.role) || { name: step.role };
    const acts = E.actions(p, st.data.improve, st.over);
    const a = acts[step.id] || { action: 'keep', how: '', risk: '' };
    const advice = ((st.data.improve && st.data.improve.changes) || []).find((c) => c.step === step.id);
    const info = (st.mode === 'before' ? calcB : calcA).steps[step.id] || calcB.steps[step.id];
    const fl = p.flags || {};
    const unq = !step.quote || (fl.unquoted || []).indexOf(step.id) >= 0;
    const dropped = (fl.numbers_dropped || []).filter((d) => d.step === step.id);
    const work = step.kind === 'task' || step.kind === 'decision';
    sheet.textContent = '';
    const kv = h('dl', { class: 'pr-kv' });
    const row = (k, v) => kv.append(h('dt', { text: k }), h('dd', {}, [].concat(v)));
    const chip = (id, name) => { const r = h('span', { class: 'pr-role' }, [h('i'), name]); r.style.setProperty('--c', col(id)); return r; };
    row('Кто делает', a.action === 'automate'
      ? [chip(step.role, role.name), h('span', { class: 'pr-tag', text: 'в «Стало» — Система, авто' })] : chip(step.role, role.name));
    row('Откуда', unq ? h('span', { class: 'pr-tag', text: 'шаг добавила модель, в тексте цитаты нет' }) : [h('span', { class: 'pr-quote', text: '«' + step.quote + '»' }), h('span', { class: 'pr-tag', text: 'цитата из описания' })]);
    if (work) {
      const ansMin = st.A.minutes[step.id];
      const orig = ansMin != null ? ansMin : step.minutes;
      const edited = st.mins[step.id] != null;
      const val = edited ? st.mins[step.id] : orig != null ? +orig : E.rawMinutes(step, st.A);
      const tag = h('span', { class: 'pr-tag' });
      const setTag = () => { tag.textContent = st.mins[step.id] != null ? 'ваша правка' : orig == null ? 'допущение' : st.ans ? 'из ответа' : 'число из текста'; };
      setTag();
      const inp = h('input', { class: 'pr-in', type: 'number', min: 0, max: 960, step: 1, inputmode: 'decimal', value: val, 'aria-label': 'минут на шаг' });
      inp.addEventListener('input', () => {
        const v = parseFloat(String(inp.value).replace(',', '.'));
        if (!(v >= 0)) return;
        st.mins[step.id] = v;
        setTag();
        const keep = st.sel;
        st.sel = null;
        recalc();
        st.sel = keep;
        gN.querySelectorAll('.pr-node').forEach((g) => g.classList.toggle('pr-sel', g.getAttribute('data-id') === keep));
      });
      row('Минут на шаг', [h('div', { class: 'pr-mf' }, [inp, tag])].concat(dropped.filter((d) => d.field === 'minutes').map((d) =>
        h('span', { class: 'pr-tag', text: 'модель назвала ' + d.value + ', в цитате такого числа нет — отброшено' }))));
      const w = info ? info.wait : 0;
      const wd = dropped.filter((d) => d.field === 'wait_hours').map((d) => h('span', { class: 'pr-tag', text: 'модель назвала ' + d.value + ' ч, в цитате числа нет — отброшено' }));
      row('Ждёт перед', [h('span', { text: w > 0 ? n1(w) + ' ч — ' + (info.waitSrc === 'text' ? (st.ans ? 'из ответа' : 'из текста') : 'передача между ролями, допущение') : 'не ждёт' })].concat(wd));
      if (info && Math.abs(info.v - 1) > 0.005) row('Проходов', h('span', { text: n2(info.v) + ' на заявку — из-за возвратов' }));
    }
    const ex = st.ext.has(step.role);
    if (ex) row('Внешний', h('span', { class: 'pr-tag', text: 'участник вне компании: его время идёт в дни на заявку, не в часы и рубли' }));
    const allowed = ex ? ['keep'] : step.kind === 'task' ? ['keep', 'automate', 'remove'] : step.kind === 'decision' ? ['keep', 'automate'] : ['keep'];
    if (advice && advice.action === 'merge') allowed.push('merge');
    const sg = h('div', { class: 'pr-seg', role: 'group', 'aria-label': 'Решение для «Стало»' });
    ['keep', 'automate', 'remove', 'merge'].forEach((k) => {
      if (k === 'merge' && allowed.indexOf('merge') < 0) return;
      sg.append(h('button', { type: 'button', 'aria-pressed': String(a.action === k), disabled: allowed.indexOf(k) < 0,
        text: ACT[k], on: { click: () => { st.over[step.id] = k; if (advice && advice.action === k) delete st.over[step.id]; recalc(); } } }));
    });
    const mergeTo = advice && advice.merge_into && p.steps.find((x) => x.id === advice.merge_into);
    row('В «Стало»', [sg].concat(mergeTo ? [h('span', { class: 'pr-tag', text: 'объединить — с шагом «' + mergeTo.name + '»' })] : [],
      !work ? [h('span', { class: 'pr-tag', text: 'старт и конец остаются' })] : []));
    row('Совет модели', advice ? h('span', { text: (advice.action !== 'keep' ? ACT[advice.action] + '. ' : '') + (advice.how || '') }) : h('span', { class: 'pr-tag', text: 'модель не предлагает менять' }));
    if (advice && advice.risk) row('Риск', h('span', { text: advice.risk }));
    sheet.append(h('div', { class: 'pr-grab' }), h('button', { type: 'button', class: 'pr-x', 'aria-label': 'Закрыть', text: '×', on: { click: closeSheet } }),
      h('h3', { text: step.name }), h('div', { class: 'pr-small', text: KIND[step.kind] + ' · ' + step.id }), kv);
  }

  /* ---------- допущения ---------- */
  function buildAsm() {
    asmBox.textContent = '';
    const field = (label, val, stepv, key, set) => {
      const inp = h('input', { class: 'pr-in', type: 'number', min: 0, step: stepv, inputmode: 'decimal', value: +val.toFixed(3) });
      const src = srcOf(key);
      const small = h('small', { class: src === 'answer' ? 'pr-ans-t' : null, text: SRC[src] });
      inp.addEventListener('input', () => {
        const v = parseFloat(String(inp.value).replace(',', '.'));
        if (v >= 0) { set(v); st.edited[key] = 1; small.textContent = SRC.edit; small.className = ''; recalc(); }
      });
      asmBox.append(h('label', { class: 'pr-f' }, [h('span', {}, [label, small]), inp]));
    };
    ASM.forEach(([k, label, stp]) => field(label, st.A[k], stp, k, (v) => { st.A[k] = k === 'dayH' || k === 'hoursPerPerson' ? Math.max(v, 0.5) : v; }));
    (st.data ? st.data.process.roles : []).filter((r) => !st.ext.has(r.id)).forEach((r) => field('людей в роли «' + r.name + '»', st.A.people[r.id] || 1, 1, r.id + '.people', (v) => { st.A.people[r.id] = Math.max(v, 0.1); }));
  }

  /* ---------- интервью ---------- */
  async function status() {
    let on = false;
    try {
      const x = await api('status');
      on = x.live !== false;
      if (iv.view === 'start') ivInfo.textContent = on ? 'осталось на сегодня ' + n2(Math.max(0, (x.budget_rub || 0) - (x.spent_rub || 0))) + ' ₽ из ' + n0(x.budget_rub || 15) : ERR.offline;
    } catch (e) {
      if (iv.view === 'start') ivInfo.textContent = ERR.unavailable;
    }
    if (iv.view === 'start' && !iv.busy) [send].concat(Array.from(topics.children)).forEach((b) => { b.disabled = !on; });
  }

  function ivFail(e) {
    ivErr.hidden = false;
    ivErr.textContent = e.message || ERR[e.code] || 'Не получилось: ' + e.code;
  }

  // start — подсказки и своя фраза; ask — вопрос и ответ; build — строится отчёт
  function view(v) {
    iv.view = v;
    const ask = v === 'ask';
    topics.hidden = v !== 'start';
    ans.hidden = sendRow.hidden = v === 'build' || (ask && !iv.q);
    mic.hidden = !ask || !micOk;
    enoughRow.hidden = ivCnt.hidden = !ask;
    send.textContent = ask ? 'Ответить' : 'Начать';
    ta.placeholder = ask ? 'Ответ своими словами или «не знаю»' : 'или своими словами: какой процесс разобрать';
    if (v === 'start') { ivQ.textContent = 'Какой процесс разберём?'; ivGot.hidden = true; }
  }

  let tick = 0;
  function lock(on, label) {
    iv.busy = on;
    [send, mic, enough, ta].concat(Array.from(topics.children)).forEach((b) => { b.disabled = on; });
    if (!on) enough.disabled = iv.count < 3;
    clearInterval(tick);
    if (!on) return;
    const t0 = Date.now();
    const paint = () => { ivInfo.textContent = label + '… ' + sec(Date.now() - t0); };
    paint();
    tick = setInterval(paint, 250);
  }

  function submit() {
    const text = ta.value.trim();
    ivErr.hidden = true;
    if (iv.view === 'start') begin(null, text);
    else if (!text) ivFail({ message: 'Ответьте текстом, голосом или «не знаю».' });
    else answer({ text });
  }

  async function begin(topic, text) {
    if (iv.busy) return;
    ivErr.hidden = true;
    if (!topic && !text) { ivFail({ message: 'Выберите подсказку или опишите процесс парой слов.' }); return; }
    lock(true, 'Готовлю первый вопрос');
    let r;
    try { r = await api('interviews', { topic, text: topic ? null : text }); } catch (e) { lock(false); ivFail(e); return; }
    lock(false);
    Object.assign(iv, { id: r.id, count: 0 });
    Object.assign(st, { ans: [], A0: E.assume(), A: E.assume(), edited: {}, rep: null, data: null, mode: 'before' });
    Object.keys(modeBtn).forEach((k) => modeBtn[k].setAttribute('aria-pressed', String(k === 'before')));
    press();
    root.classList.remove('pr-first');
    top.hidden = rep.hidden = sum.hidden = true;
    setHash(null);
    closeSheet();
    gN.textContent = '';
    svg.setAttribute('hidden', '');
    cur = sim = null;
    drawDots();
    title.textContent = 'Интервью: ' + (topic ? TOPICS.find((t) => t[0] === topic)[1] : text);
    meta.textContent = legend.textContent = note.textContent = '';
    msg.textContent = 'Схема появится после первого ответа.';
    msg.hidden = false;
    Object.keys(stat).forEach((k) => { cancelAnimationFrame(stat[k].raf); stat[k].val = null; stat[k].num.textContent = '—'; stat[k].diff.textContent = ''; });
    buildAsm();
    // интервью — над схемой: ответил и видишь, как она растёт
    root.insertBefore(ivSec, body);
    clearTa();
    ask(r.question);
    ivInfo.textContent = r.budget_left_rub != null ? 'осталось на сегодня ' + n2(r.budget_left_rub) + ' ₽' : '';
    ivSec.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' });
  }

  function ask(q) {
    iv.q = q;
    view('ask');
    if (!q) return;
    ivQ.textContent = q.text;
    ivCnt.textContent = 'вопрос ' + q.n + ' из ' + q.max;
    enough.disabled = iv.busy || iv.count < 3;
    enoughHint.hidden = iv.count >= 3;
  }

  // под вопросом — что понял код
  function heard(r) {
    const p = st.data && st.data.process;
    const items = (r.understood || []).map((u) => {
      const m = /^([sr]\d+)\.(\w+)$/.exec(u.field) || [];
      const o = p && m[1] ? p.steps.concat(p.roles).find((x) => x.id === m[1]) : null;
      return (o ? (m[2] === 'wait_hours' ? 'ожидание перед «' : m[2] === 'people' ? 'людей в роли «' : '«') + o.name + '» — ' : '') +
        nv(+u.value) + ' ' + String(u.unit || '').replace('/мес', ' в месяц').replace('/ч', ' в час');
    });
    ivGot.textContent = '';
    ivGot.append(h('q', { text: r.answer_text || '' }), h('b', { text: items.length ? 'записал: ' + items.join(', ')
      : /не\s+знаю/i.test(r.answer_text || '') ? 'понял: не знаете — остаётся допущение' : 'записал ответ, чисел в нём нет' }));
    ivGot.hidden = false;
  }

  async function answer(data) {
    const id = iv.id;
    if (iv.busy || !id) return;
    ivErr.hidden = true;
    lock(true, data instanceof FormData ? 'Распознаю и разбираю ответ' : 'Разбираю ответ');
    let r;
    try { r = await api(ivPath('/answers'), data); } catch (e) {
      lock(false);
      if (iv.id !== id) return;
      ivFail(e);
      if (e.code === 'not_found') quit();
      return;
    }
    lock(false);
    if (iv.id !== id) return;
    iv.count++;
    clearTa();
    ivInfo.textContent = '';
    st.A0 = E.applyAnswers(st.A0, r.understood);
    st.A = E.applyAnswers(st.A, r.understood);
    st.ans = st.ans.concat((r.understood || []).map((u) => u.field));
    if (r.process) show({ process: r.process, improve: null, steps: r.steps }, 'draft');
    else if (st.data) { buildAsm(); recalc(); }
    heard(r);
    if (r.done || !r.question) finish(); else ask(r.question);
  }

  async function finish() {
    const id = iv.id;
    if (iv.busy || !id) return;
    stopRec(false);
    ivErr.hidden = true;
    view('build');
    ivQ.textContent = 'Строю схему «Было / Стало» и отчёт.';
    lock(true, 'Разбор и улучшения');
    let fe = null;
    try { await api(ivPath('/finish'), {}); } catch (e) { fe = e; }
    const t0 = Date.now();
    try {
      for (;;) {
        await sleep(opts.poll);
        if (iv.id !== id) return;
        const r = await api('interviews/' + encodeURIComponent(id));
        if (r.status === 'done') { lock(false); showReport(r, id); return; }
        if (r.status === 'error') throw r.error || { code: 'failed' };
        if (r.status === 'asking' && fe) throw fe;
        if (Date.now() - t0 > 150000) throw { code: 'timeout' };
      }
    } catch (e) {
      lock(false);
      if (iv.id !== id) return;
      ask(iv.q);
      if (iv.q) ivQ.textContent = iv.q.text;
      enough.disabled = false;
      ivFail(e);
    }
  }

  // прервать интервью
  function quit() {
    stopRec(false);
    Object.assign(iv, { id: null, count: 0, q: null });
    view('start');
    root.insertBefore(ivSec, dl);
    status();
  }

  function showReport(r, id) {
    if (iv.id) quit();
    Object.assign(st, { rep: id, ans: r.answered || [], A0: E.assume(r.assumptions), edited: {} });
    st.A = E.assume(st.A0);
    press();
    root.classList.remove('pr-first');
    top.hidden = true;
    show({ process: r.process, improve: r.improve || { changes: [] }, steps: r.steps }, 'report');
    setHash(id);
    title.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'start' });
  }

  /* ---------- #report=<id>: фрагмент, ?r= на сайте занят метками ---------- */
  const hashId = () => { const m = /(?:^#|&)report=([\w-]{4,64})(?:&|$)/.exec(location.hash); return m && m[1]; };
  function setHash(id) {
    if (!id && !hashId()) return;
    try { history.replaceState(null, '', id ? '#report=' + id : location.pathname + location.search); } catch (e) { /* ссылка всё равно копируется */ }
  }
  async function openReport(id) {
    title.textContent = 'Загружаю отчёт…';
    try {
      const t0 = Date.now();
      for (;;) {
        const r = await api('interviews/' + encodeURIComponent(id));
        if (r.status === 'done') { showReport(r, id); return; }
        if (r.status !== 'building' || Date.now() - t0 > 150000) throw { message: r.status === 'error' ? 'Отчёт по ссылке не собрался.' : 'Интервью по ссылке ещё не закончено.' };
        await sleep(opts.poll);
      }
    } catch (e) {
      if (!st.data) await loadExample(opts.first);
      top.textContent = e.code === 'not_found' ? 'Отчёт не найден: он старше 30 суток или ссылка неверная.' : e.message || ERR[e.code] || 'Отчёт не открылся: ' + e.code;
      top.hidden = false;
    }
  }

  /* ---------- микрофон: тап — запись, тап — отправить; по тишине — нет ---------- */
  let rec = null;
  let micWait = false;
  function recUi(on) {
    recBox.hidden = !on;
    ta.hidden = sendRow.hidden = on;
    mic.classList.toggle('pr-rec', on);
    mic.setAttribute('aria-pressed', String(on));
    mic.setAttribute('aria-label', on ? 'Остановить и отправить' : 'Ответить голосом');
    micP.setAttribute('d', on ? 'M7 7h10v10H7z' : MIC);
  }
  function stopRec(go) { if (rec && rec.mr.state !== 'inactive') { rec.go = go; rec.mr.stop(); } }
  function micOff() {
    micOk = false;
    mic.hidden = true;
    ivFail({ message: 'Микрофон недоступен — ответьте текстом.' });
  }
  async function micTap() {
    if (rec) { stopRec(true); return; }
    if (iv.busy || !iv.id || micWait) return;
    ivErr.hidden = true;
    let stream;
    micWait = true;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch (e) { micOff(); return; } finally { micWait = false; }
    const type = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm'].find((t) => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t));
    const r = { chunks: [], t0: Date.now() };
    try { r.mr = new MediaRecorder(stream, type ? { mimeType: type } : undefined); } catch (e) { stream.getTracks().forEach((t) => t.stop()); micOff(); return; }
    rec = r;
    r.mr.ondataavailable = (e) => { if (e.data && e.data.size) r.chunks.push(e.data); };
    r.mr.onstop = () => {
      clearInterval(r.tick);
      cancelAnimationFrame(r.raf);
      stream.getTracks().forEach((t) => t.stop());
      if (r.ac) r.ac.close();
      rec = null;
      recUi(false);
      if (!r.go || !iv.id) return;
      const t = r.mr.mimeType || type || 'audio/webm';
      const blob = new Blob(r.chunks, { type: t });
      if (blob.size > 600 * 1024) { ivFail({ message: 'Запись слишком длинная — ответьте короче.' }); return; }
      const fd = new FormData();
      fd.append('audio', blob, 'answer.' + (/ogg/.test(t) ? 'ogg' : /mp4|aac/.test(t) ? 'mp4' : 'webm'));
      answer(fd);
    };
    r.mr.start(500);
    recUi(true);
    const paint = () => {
      const t = (Date.now() - r.t0) / 1000;
      recT.textContent = 'запись 0:' + String(Math.min(MAX_REC_S, Math.floor(t))).padStart(2, '0') + ' из 0:' + MAX_REC_S + ' · тап — отправить';
      if (t >= MAX_REC_S) stopRec(true);
    };
    paint();
    r.tick = setInterval(paint, 250);
    try {
      r.ac = new (window.AudioContext || window.webkitAudioContext)();
      const an = r.ac.createAnalyser();
      r.ac.createMediaStreamSource(stream).connect(an);
      const buf = new Uint8Array(an.fftSize);
      const loop = () => {
        an.getByteTimeDomainData(buf);
        let m = 0;
        for (let i = 0; i < buf.length; i++) m = Math.max(m, Math.abs(buf[i] - 128));
        lvI.style.transform = 'scaleX(' + Math.min(1, m / 48).toFixed(2) + ')';
        r.raf = requestAnimationFrame(loop);
      };
      loop();
    } catch (e) { /* запись идёт и без индикатора */ }
  }

  /* ---------- .bpmn ---------- */
  function download(m) {
    if (!st.data) return;
    const b = base();
    const p = m === 'before' ? b : E.after(b, st.data.improve, st.over, st.A);
    const blob = new Blob([E.toBpmn(p, { mode: m === 'before' ? 'было' : 'стало' })], { type: 'application/xml' });
    const a = h('a', { href: URL.createObjectURL(blob), download: (st.name || 'process') + (m === 'before' ? '-bylo' : '-stalo') + '.bpmn' });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  }

  view('start');
  buildAsm();
  status();
  window.addEventListener('hashchange', () => { const id = hashId(); if (id && id !== st.rep) openReport(id); });
  if (hashId()) openReport(hashId()); else loadExample(opts.first);
  return { load: loadExample, setMode, state: st, iv };
}

window.PrProcess = { mount };
})();
