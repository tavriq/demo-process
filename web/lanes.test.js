// Пул BPMN с дорожками ролей (экран от 900 px) и .bpmn — одни координаты: node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const E = require('./engine.js');
const { page, until } = require('./fakedom.js');

const NAMES = ['sales', 'invoice', 'returns', 'onboarding'];
const DIRS = [['', path.join(__dirname, 'fixtures')], ['запись ', path.join(__dirname, '..', 'app', 'recorded')]];
const fx = (n) => JSON.parse(fs.readFileSync(path.join(DIRS[0][1], n + '.json'), 'utf8'));
// фикстуры и записи с живой модели (если есть), «Было» и «Стало»
const variants = () => NAMES.flatMap((n) => DIRS.filter(([, d]) => fs.existsSync(path.join(d, n + '.json'))).flatMap(([t, d]) => {
  const f = JSON.parse(fs.readFileSync(path.join(d, n + '.json'), 'utf8'));
  return [[t + n + ' было', f.process], [t + n + ' стало', E.after(f.process, f.improve)]];
}));
const roleOf = (s) => (s.auto ? E.SYS : s.role);
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
const inside = (a, b) => a.x >= b.x && a.y >= b.y && a.x + a.w <= b.x + b.w && a.y + a.h <= b.y + b.h;
const shape = (n) => ({ x: n.sx, y: n.sy, w: n.sw, h: n.sh });

/* DI из .bpmn: границы фигур, точки и подписи рёбер, шаги дорожек — по id элемента. */
function di(xml) {
  const B = (s) => { const m = /<dc:Bounds x="(-?\d+)" y="(-?\d+)" width="(\d+)" height="(\d+)"/.exec(s); return m && { x: +m[1], y: +m[2], w: +m[3], h: +m[4] }; };
  const shapes = {};
  const edges = {};
  const labels = {};
  const lanes = {};
  for (const m of xml.matchAll(/<bpmndi:BPMNShape [^>]*bpmnElement="([^"]+)"[^>]*>(.*?)<\/bpmndi:BPMNShape>/g)) shapes[m[1]] = B(m[2]);
  for (const m of xml.matchAll(/<bpmndi:BPMNEdge [^>]*bpmnElement="([^"]+)">(.*?)<\/bpmndi:BPMNEdge>/g)) {
    edges[m[1]] = [...m[2].matchAll(/<di:waypoint x="(-?\d+)" y="(-?\d+)"/g)].map((w) => [+w[1], +w[2]]);
    labels[m[1]] = B(m[2].split('<bpmndi:BPMNLabel>')[1] || '');
  }
  for (const m of xml.matchAll(/<bpmn:lane id="([^"]+)"[^>]*>([\s\S]*?)<\/bpmn:lane>/g)) lanes[m[1]] = [...m[2].matchAll(/<bpmn:flowNodeRef>([^<]+)</g)].map((r) => r[1]);
  return { shapes, edges, labels, lanes };
}

test('пул: на каждую роль своя дорожка, дорожки делят пул сверху донизу — и так же в .bpmn', () => {
  variants().forEach(([name, p]) => {
    const P = E.pool(p);
    const D = di(E.toBpmn(p));
    const used = new Set(p.steps.map(roleOf));
    const ids = p.roles.map((r) => r.id);
    assert.deepEqual(P.lanes.map((l) => l.role), ids.filter((r) => used.has(r)).concat(used.has(E.SYS) && !ids.includes(E.SYS) ? [E.SYS] : []), name + ': дорожки в порядке ролей');
    let y = 0;
    P.lanes.forEach((l) => {
      assert.ok(l.y === y && l.h > 0 && l.x === P.head && l.x + l.w === P.width, name + ': ' + l.id + ' ' + JSON.stringify(l));
      y += l.h;
      assert.deepEqual(D.shapes[l.id], { x: l.x, y: l.y, w: l.w, h: l.h }, name + ': DI ' + l.id);
      assert.ok(l.lines.length && l.lines.length <= 2, name + ': имя роли ' + l.id);
    });
    assert.equal(y, P.height, name + ': дорожки до низа пула');
    assert.deepEqual(Object.keys(D.lanes), P.lanes.map((l) => l.id), name);
    assert.deepEqual(D.shapes.Participant_1, { x: 0, y: 0, w: P.width, h: P.height });
  });
  const f = fx('sales');
  const lanes = E.pool(E.after(f.process, f.improve)).lanes;
  assert.deepEqual(lanes.slice(-1).map((l) => [l.id, l.name]), [['Lane_sys', 'Система']], '«Стало»: дорожка «Система» снизу');
});

test('пул: каждый шаг лежит в дорожке своей роли, правее её заголовка; шаги не пересекаются, подписи целиком', () => {
  variants().forEach(([name, p]) => {
    const P = E.pool(p);
    const D = di(E.toBpmn(p));
    p.steps.forEach((s) => {
      const n = P.nodes[s.id];
      const l = P.lanes.find((x) => x.role === roleOf(s));
      const body = { x: l.x + P.head, y: l.y, w: l.w - P.head, h: l.h };
      assert.ok(inside(n.box, body), name + ': ' + s.id + ' вне дорожки ' + l.id);
      assert.ok(inside(D.shapes[s.id], body), name + ': DI ' + s.id + ' вне дорожки ' + l.id);
      assert.ok(l.ids.includes(s.id) && D.lanes[l.id].includes(s.id), name + ': flowNodeRef ' + s.id);
      assert.equal(n.lines.join(' ').replace(/- /g, '').replace(/\s/g, ''), s.name.replace(/\s/g, ''), name + ': подпись ' + s.id);
      if (n.kind === 'task') assert.ok(n.lines.length * 12 <= n.sh - 10, name + ': текст ' + s.id + ' не влез');
    });
    assert.equal(P.lanes.reduce((k, l) => k + l.ids.length, 0), p.steps.length, name + ': шаг — ровно в одной дорожке');
    const ns = Object.values(P.nodes);
    ns.forEach((a, i) => ns.slice(i + 1).forEach((b) => assert.ok(!overlap(a.box, b.box), name + ': ' + a.id + ' и ' + b.id)));
  });
});

test('пул: переходы под прямым углом, от края шага до края шага, через чужие шаги и подписи не идут', () => {
  let cross = 0;
  variants().forEach(([name, p]) => {
    const P = E.pool(p);
    const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
    assert.equal(P.edges.length, p.steps.reduce((k, s) => k + (s.kind === 'decision' ? s.branches.length : (s.next || []).length), 0), name);
    const edge = (q, n) => q[0] >= n.sx && q[0] <= n.sx + n.sw && q[1] >= n.sy && q[1] <= n.sy + n.sh &&
      (q[0] === n.sx || q[0] === n.sx + n.sw || q[1] === n.sy || q[1] === n.sy + n.sh);
    P.edges.forEach((e) => {
      const f = e.from + '→' + e.to;
      const u = P.nodes[e.from];
      const v = P.nodes[e.to];
      if (roleOf(by[e.from]) !== roleOf(by[e.to])) cross++;
      assert.ok(edge(e.pts[0], u) && edge(e.pts[e.pts.length - 1], v), name + ': ' + f + ' не от края до края ' + JSON.stringify(e.pts));
      // свой шаг ребро только касает: внутрь фигуры и через подпись не заходит
      const own = [u, v].flatMap((n) => [{ x: n.sx + 1, y: n.sy + 1, w: n.sw - 2, h: n.sh - 2 }].concat(n.label ? [n.label] : []));
      for (let i = 1; i < e.pts.length; i++) {
        const [a, b] = [e.pts[i - 1], e.pts[i]];
        assert.ok(a[0] === b[0] || a[1] === b[1], name + ': косой отрезок ' + f);
        const seg = { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(a[0] - b[0]) || 0.01, h: Math.abs(a[1] - b[1]) || 0.01 };
        Object.values(P.nodes).forEach((n) => {
          if (n !== u && n !== v) assert.ok(!overlap(seg, n.box), name + ': ' + f + ' через ' + n.id);
        });
        own.forEach((r) => assert.ok(!overlap(seg, r), name + ': ' + f + ' через свой шаг'));
      }
      assert.ok(e.pts.every((q) => q[0] > 2 * P.head && q[0] < P.width && q[1] > 0 && q[1] < P.height), name + ': ' + f + ' вне пула');
      if (e.label) assert.ok(Object.values(P.nodes).every((n) => !overlap(e.labBox, shape(n))), name + ': подпись ' + f + ' на шаге');
    });
  });
  assert.ok(cross >= 40, 'переходов между дорожками в примерах: ' + cross);
});

test('пул и .bpmn: те же координаты шагов и рёбер, тот же порядок шагов слева направо', () => {
  variants().forEach(([name, p]) => {
    const P = E.pool(p);
    const x = E.toBpmn(p, { mode: 'было' });
    const D = di(x);
    p.steps.forEach((s) => assert.deepEqual(D.shapes[s.id], shape(P.nodes[s.id]), name + ': ' + s.id));
    const flows = [...x.matchAll(/<bpmn:sequenceFlow id="([^"]+)"(?: name="[^"]*")? sourceRef="([^"]+)" targetRef="([^"]+)"/g)].map((m) => m.slice(1, 4).join(' '));
    assert.deepEqual(flows, P.edges.map((e) => [e.id, e.from, e.to].join(' ')), name + ': потоки');
    P.edges.forEach((e) => {
      assert.deepEqual(D.edges[e.id], e.pts, name + ': точки ' + e.id);
      const r = e.labBox;
      assert.deepEqual(D.labels[e.id], r ? { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.w), h: Math.round(r.h) } : null, name + ': подпись ' + e.id);
    });
    // слева направо — по центру фигуры (в одном столбце событие уже задачи), затем сверху вниз
    const c = (r) => [r.x + r.w / 2, r.y + r.h / 2];
    const order = (pos) => p.steps.map((s) => s.id).sort((a, b) => c(pos(a))[0] - c(pos(b))[0] || c(pos(a))[1] - c(pos(b))[1]);
    const screen = order((id) => shape(P.nodes[id]));
    assert.deepEqual(screen, order((id) => D.shapes[id]), name + ': порядок на экране и в .bpmn');
    P.edges.filter((e) => !e.back).forEach((e) => {
      const [a, b] = [D.shapes[e.from], D.shapes[e.to]];
      assert.ok(a.x + a.w < b.x, name + ': ' + e.from + '→' + e.to + ' идёт слева направо');
    });
    P.lanes.forEach((l) => assert.deepEqual(D.lanes[l.id], screen.filter((id) => l.ids.includes(id)), name + ': порядок в дорожке ' + l.id));
  });
});

test('окно от 900 px: пул с дорожками в координатах .bpmn, «Стало» и тап по шагу; уже — схема сверху вниз', async () => {
  const fetch = async (url) => (url.startsWith('fixtures/') ? new Response(fs.readFileSync(path.join(__dirname, url))) : new Response('{}', { status: 503 }));
  const P = page({ fetch, media: ['(min-width: 900px)'] });
  await until(() => P.api.state.name === 'sales');
  const f = fx('sales');
  const check = (p) => {
    const L = E.pool(p);
    const D = di(E.toBpmn(p));
    const lanes = P.$$('.pr-hd .pr-lane');
    assert.deepEqual(lanes.map((g) => g.textContent), L.lanes.map((l) => l.lines.join('')));
    const box = (r) => ['x', 'y', 'width', 'height'].map((k) => +r.getAttribute(k));
    // границы дорожек — в схеме, заголовки — в закреплённом слое; и те и другие — как в .bpmn
    P.$$('.pr-svg .pr-lb').forEach((r, i) => assert.deepEqual(box(r), [D.shapes[L.lanes[i].id].x, D.shapes[L.lanes[i].id].y, D.shapes[L.lanes[i].id].w, D.shapes[L.lanes[i].id].h]));
    lanes.forEach((g, i) => assert.deepEqual(box(g.querySelector('.pr-lh')), [D.shapes[L.lanes[i].id].x, D.shapes[L.lanes[i].id].y, L.head, D.shapes[L.lanes[i].id].h]));
    assert.equal(P.$$('.pr-svg .pr-lb').length, L.lanes.length);
    const nodes = P.$$('.pr-node').filter((g) => !g.classList.contains('pr-out'));
    assert.equal(nodes.length, p.steps.length);
    nodes.forEach((g) => {
      const d = D.shapes[g.getAttribute('data-id')];
      assert.equal(g.style.transform, 'translate(' + (d.x + d.w / 2).toFixed(1) + 'px,' + (d.y + d.h / 2).toFixed(1) + 'px)');
    });
    assert.equal(P.$('.pr-svg').getAttribute('viewBox'), view(L));
    return lanes;
  };
  check(f.process);
  assert.ok(P.$('.pr-wrap').classList.contains('pr-wide'));
  // узкое место (Инженер, загрузка > 85 %) подсвечено и в дорожке
  assert.deepEqual(P.$$('.pr-lane.pr-hot').map((g) => g.textContent), ['Инженер']);
  assert.ok(P.$$('.pr-node.pr-hot').length > 0);
  P.$('.pr-node').fire('click');
  assert.ok(!P.$('.pr-sheet').hidden, 'карточка шага открылась');
  P.api.setMode('after');
  const after = check(E.after(f.process, f.improve));
  assert.equal(after[after.length - 1].textContent, 'Система');
  // уже 900 px — прежняя вертикальная схема, без дорожек
  const N = page({ fetch });
  await until(() => N.api.state.name === 'sales');
  assert.equal(N.$$('.pr-lane').length, 0);
  assert.equal(N.$('.pr-hd').children.length, 0);
  assert.ok(!N.$('.pr-wrap').classList.contains('pr-wide'));
  assert.equal(N.$('.pr-svg').getAttribute('viewBox'), '0 0 360 ' + E.layout(f.process, { width: 360 }).height);
});

/* Мини-каскад process.css поверх fakedom: значение свойства у элемента — из совпавшего правила с наибольшей
   специфичностью (при равной — из последнего). Правило внутри @media действует, только если все его условия есть
   в media страницы (как в page({ media })). Селекторы с «:», «>», «[» пропущены; rules — свой набор правил вместо process.css. */
const CSS = fs.readFileSync(path.join(__dirname, 'process.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
function rulesOf(css) {
  const out = [];
  const at = [];
  const re = /([^{}]*)([{}])/g;
  let m;
  while ((m = re.exec(css))) {
    const head = m[1].trim();
    if (m[2] === '}') at.pop();
    else if (head.startsWith('@')) at.push(head.replace(/^@media\s*/, ''));
    else {
      const end = css.indexOf('}', re.lastIndex);
      out.push({ sel: head, body: css.slice(re.lastIndex, end), media: at.slice() });
      re.lastIndex = end + 1;
    }
  }
  return out;
}
const RULES = rulesOf(CSS);
const PROPS = /(?:^|;)\s*(font|font-size|font-weight|max-width|overflow|overflow-x|display|position|left|bottom|z-index|padding|fill|fill-opacity|opacity|background|background-color|outline|outline-offset|stroke|stroke-width|stroke-dasharray):/;
function cascade(doc, media, list) {
  const rules = [];
  (list || RULES).filter((r) => PROPS.test(r.body) && r.media.every((q) => (media || []).includes(q))).forEach((r) => {
    r.sel.split(',').map((x) => x.trim()).filter((x) => /^[\w.\s-]+$/.test(x)).forEach((sel) => {
      rules.push({ els: new Set(doc.querySelectorAll(sel)), spec: (sel.match(/\.[\w-]+/g) || []).length * 100 + (sel.match(/(?:^|\s)[a-z]/g) || []).length, body: r.body });
    });
  });
  const val = (el, prop) => {
    let best = null;
    rules.forEach((r) => {
      const v = r.els.has(el) && new RegExp('(?:^|;)\\s*' + prop + ':\\s*([^;]+)').exec(r.body);
      if (v && (!best || r.spec >= best.spec)) best = { spec: r.spec, v: v[1].trim() };
    });
    return best && best.v;
  };
  // кегль — свой или от предка (font-size или font)
  const size = (el) => {
    for (let e = el; e; e = e.parentNode) { const v = val(e, 'font-size') || val(e, 'font'); if (v) return parseFloat(v); }
    return 16;
  };
  return { val, size };
}
// ширина блока пула при ширине окна vw — по --pr-pw из process.css: не шире 1400 px, окна без полей и самого пула (--pr-w)
const PW = /--pr-pw: min\((\d+)px, calc\(100vw - (\d+)px\), var\(--pr-w, \d+px\)\)/.exec(CSS);
const wrapW = (vw, poolW) => Math.min(+PW[1], vw - +PW[2], poolW || Infinity);
const WIDE = ['(min-width: 900px)'];
// пул на экране — с x = head: полоса имени процесса есть только в .bpmn, координаты те же
const view = (L) => L.head + ' 0 ' + (L.width - L.head) + ' ' + L.height;
const pageFetch = async (url) => (url.startsWith('fixtures/') ? new Response(fs.readFileSync(path.join(__dirname, url))) : new Response('{}', { status: 503 }));

test('пул от 900 px: на экране текст шагов, событий, развилок, веток, очередей и дорожек — не меньше 12 px на 900, 1200 и 1440 px', async () => {
  assert.ok(PW, '--pr-pw в process.css');
  for (const vw of [900, 1200, 1440]) {
    const width = wrapW(vw);
    const P = page({ fetch: pageFetch, media: ['(min-width: 900px)'], width });
    const seen = new Set();
    for (const [i, name] of NAMES.entries()) {
      P.$$('.pr-ex button')[i].click();
      await until(() => P.api.state.name === name);
      const f = fx(name);
      for (const [mode, p] of [['before', f.process], ['after', E.after(f.process, f.improve)]]) {
        P.api.setMode(mode);
        const L = E.pool(p);
        const C = cascade(P.doc, WIDE);
        assert.equal(P.$('.pr-svg').getAttribute('viewBox'), view(L));
        // масштаб svg на экране: ширина (style.width, иначе атрибут width, но не шире max-width; 100 % — ширина блока) / viewBox
        const scale = (svg) => {
          const mw = C.val(svg, 'max-width');
          const shown = Math.min(parseFloat(svg.style.width) || +svg.getAttribute('width'), mw === 'none' ? Infinity : mw === '100%' ? width : mw ? parseFloat(mw) : Infinity);
          return shown / +svg.getAttribute('viewBox').split(' ')[2];
        };
        const texts = P.$$('.pr-stage text');
        assert.ok(texts.length > p.steps.length);
        texts.forEach((t) => {
          let svg = t;
          while (svg.localName !== 'svg') svg = svg.parentNode;
          seen.add(t.className);
          const px = C.size(t) * scale(svg);
          assert.ok(px >= 12, vw + ' px, ' + name + ' ' + mode + ': «' + t.textContent + '» (' + t.className + ') — ' + px.toFixed(1) + ' px');
        });
        // не влез — прокрутка вбок, а не ужатие
        for (const svg of P.$$('.pr-stage svg')) assert.equal(scale(svg), 1, vw + ' px, ' + name + ' ' + mode + ': масштаб ' + svg.className);
        if (L.width - L.head > width) assert.equal(C.val(P.$('.pr-wrap'), 'overflow-x'), 'auto');
      }
    }
    for (const c of ['pr-t', 'pr-ts', 'pr-el pr-halo', 'pr-ql pr-halo', 'pr-autot', 'pr-lt']) assert.ok(seen.has(c), vw + ' px: проверен ' + c);
  }
});

test('пул шире блока: при прокрутке вбок полоса процесса и заголовки дорожек остаются слева поверх схемы', async () => {
  const width = wrapW(900);
  const P = page({ fetch: pageFetch, media: ['(min-width: 900px)'], width });
  await until(() => P.api.state.name === 'sales');
  const f = fx('sales');
  for (const [mode, p] of [['before', f.process], ['after', E.after(f.process, f.improve)]]) {
    P.api.setMode(mode);
    const L = E.pool(p);
    const D = di(E.toBpmn(p));
    const C = cascade(P.doc, WIDE);
    const wrap = P.$('.pr-wrap');
    const hd = P.$('.pr-hd');
    const svg = P.$('.pr-svg');
    const SW = L.width - L.head;
    assert.ok(SW > width, 'пул шире блока — есть что прокручивать');
    // прокручивается .pr-wrap; слой заголовков и схема — в одной ячейке .pr-stage, между ними и .pr-wrap прокрутки нет
    assert.equal(C.val(wrap, 'overflow-x'), 'auto');
    assert.equal(hd.parentNode, svg.parentNode);
    assert.equal(hd.parentNode.parentNode, wrap);
    for (const k of ['overflow', 'overflow-x', 'padding']) assert.equal(C.val(hd.parentNode, k), null, k);
    // ячейка общая: слой и схема начинаются в одной точке, слой — в координатах пула
    assert.match(CSS, /\.pr-wide \.pr-stage \{ display: grid;[^}]*\}\s*\.pr-wide \.pr-stage > svg \{ grid-area: 1 \/ 1; \}/);
    // слой закреплён у левого края видимой части и лежит поверх схемы
    assert.equal(C.val(hd, 'display'), 'block');
    assert.equal(C.val(hd, 'position'), 'sticky');
    assert.equal(parseFloat(C.val(hd, 'left')), 0);
    assert.ok(+C.val(hd, 'z-index') >= 1);
    // в слое — заголовок каждой дорожки в координатах пула (с x = head, как схема), под ними — сплошной фон
    const w = L.head + 1;
    assert.equal(hd.getAttribute('viewBox'), L.head + ' 0 ' + w + ' ' + L.height);
    assert.deepEqual([+hd.getAttribute('width'), +hd.getAttribute('height')], [w, L.height]);
    const bg = hd.children[0];
    assert.ok(bg.classList.contains('pr-hbk'));
    assert.deepEqual([+bg.getAttribute('x'), bg.getAttribute('y'), +bg.getAttribute('width'), +bg.getAttribute('height')], [L.head, null, w, L.height]);
    assert.equal(C.val(bg, 'fill'), 'var(--bg)');
    assert.equal(C.val(bg, 'fill-opacity') || C.val(bg, 'opacity'), null);
    const heads = P.$$('.pr-hd .pr-lane');
    assert.deepEqual(heads.map((g) => g.textContent), L.lanes.map((l) => l.lines.join('')));
    heads.forEach((g, i) => {
      const r = g.querySelector('.pr-lh');
      const d = D.shapes[L.lanes[i].id];
      assert.deepEqual(['x', 'y', 'width', 'height'].map((k) => +r.getAttribute(k)), [d.x, d.y, L.head, d.h]);
      assert.ok(d.x + L.head < L.head + w);
    });
    assert.equal(P.$$('.pr-svg .pr-lt').length, 0, 'имена дорожек — только в закреплённом слое');
    // без прокрутки слой ничего не закрывает: шаги, подписи и рёбра — правее него
    const r = L.head + w;
    Object.values(L.nodes).forEach((n) => assert.ok(n.box.x >= r, n.id));
    L.edges.forEach((e) => { e.pts.forEach((q) => assert.ok(q[0] >= r, e.id)); if (e.labBox) assert.ok(e.labBox.x >= r, e.id); });
    // прокрутка на x: sticky с left: 0 ставит слой на x (не правее конца пула) — он целиком в видимой части [x, x + width]
    for (const x of [0, 1, Math.round((SW - width) / 2), SW - width]) {
      const at = Math.min(Math.max(x + parseFloat(C.val(hd, 'left')), 0), SW - w);
      assert.ok(at >= x && at + w <= x + width, 'прокрутка ' + x);
    }
  }
});

test('блок «Было/Стало» с цифрами: от 900 px — обычным потоком под пулом, уже — липкий у нижнего края', async () => {
  for (const [media, width, pos] of [[WIDE, wrapW(1440), 'static'], [[], 360, 'sticky']]) {
    const P = page({ fetch: pageFetch, media, width });
    await until(() => P.api.state.name === 'sales');
    const C = cascade(P.doc, media);
    const bar = P.$('.pr-bar');
    assert.equal(C.val(bar, 'position'), pos, media.join() || 'уже 900 px');
    if (pos === 'sticky') assert.equal(parseFloat(C.val(bar, 'bottom')), 0);
    // блок идёт в потоке после схемы (пул или вертикальная — в .pr-body)
    assert.ok(P.root.children.indexOf(bar) > P.root.children.indexOf(P.$('.pr-body')));
    assert.equal(P.$('.pr-wrap').classList.contains('pr-wide'), media.length > 0);
    // в широком режиме поверх пула липнет только слой заголовков дорожек внутри него; карточка шага закрыта
    const all = [P.root].concat(...['div', 'section', 'svg', 'p', 'details'].map((t) => P.root.querySelectorAll(t)));
    const shown = (el) => { for (let e = el; e; e = e.parentNode) if (e.hidden) return false; return true; };
    const pinned = all.filter((el) => shown(el) && /sticky|fixed/.test(C.val(el, 'position') || ''));
    assert.deepEqual(pinned.map((el) => el.className), media.length ? ['pr-hd'] : ['pr-bar']);
  }
});

test('пул от 900 px: сплошной фон окна и рамка по краю пула — в светлой и тёмной теме', async () => {
  const P = page({ fetch: pageFetch, media: WIDE, width: wrapW(1440) });
  await until(() => P.api.state.name === 'sales');
  for (const [i, name] of [[0, 'sales'], [1, 'invoice']]) {
    P.$$('.pr-ex button')[i].click();
    await until(() => P.api.state.name === name);
    const L = E.pool(fx(name).process);
    const C = cascade(P.doc, WIDE);
    const wrap = P.$('.pr-wrap');
    const stage = P.$('.pr-stage');
    // блок пула не шире пула: process.js отдаёт ширину пула в --pr-w, а --pr-pw берёт меньшее
    assert.equal(wrap.style['--pr-w'], L.width - L.head + 'px');
    assert.equal(wrapW(1440, L.width - L.head), Math.min(1376, L.width - L.head));
    // фон — фон окна, сплошной: у блока, у ячейки со схемой и под заголовками дорожек; прозрачности нет
    const bg = (el) => C.val(el, 'background') || C.val(el, 'background-color');
    assert.equal(bg(wrap), 'var(--bg)');
    assert.equal(bg(stage), 'var(--bg)');
    assert.equal(C.val(P.$('.pr-hbk'), 'fill'), 'var(--bg)');
    for (const el of [wrap, stage, P.$('.pr-hd'), P.$('.pr-svg')]) assert.equal(C.val(el, 'opacity'), null, el.className);
    // рамка — по краю блока внутрь (на линии рамки пула .pr-pool), без сдвига раскладки; цвет — как у рамки пула на обложке
    assert.equal(C.val(wrap, 'outline'), '1px solid var(--pr-ink2)');
    assert.equal(C.val(wrap, 'outline-offset'), '-1px');
    const frame = P.$('.pr-svg .pr-pool');
    assert.deepEqual(['x', 'y', 'width', 'height'].map((k) => +frame.getAttribute(k)), [L.head + 0.5, 0.5, L.width - L.head - 1, L.height - 1]);
  }
  // уже 900 px ширина пула в блок не передаётся
  const N = page({ fetch: pageFetch });
  await until(() => N.api.state.name === 'sales');
  assert.ok(!N.$('.pr-wrap').style['--pr-w']);
  // --bg сайта (на стенде — dev.html) — сплошной цвет и в светлой теме, и в тёмной (по системе и по переключателю)
  const html = fs.readFileSync(path.join(__dirname, 'dev.html'), 'utf8');
  const themes = [/:root \{([^}]*)\}/, /:root:not\(\[data-theme="light"\]\) \{([^}]*)\}/, /:root\[data-theme="dark"\] \{([^}]*)\}/].map((re) => /--bg: ([^;]+);/.exec(re.exec(html)[1])[1].trim());
  assert.equal(themes.length, 3);
  themes.forEach((c) => assert.match(c, /^#([0-9a-f]{3}|[0-9a-f]{6})$/i, '--bg непрозрачный: ' + c));
  assert.notEqual(themes[0], themes[1]);
  assert.equal(themes[1], themes[2]);
});

/* Вид пула от 900 px — как обложка «Процесса» на сайте (site.css, .bp-*): рамка пула, линии и подписи — --pr-ink2
   (--ink-2 сайта), дорожки делит --line-2, у шага рамка 1.6 цветом роли. Каждый пример — из фикстур и записей, «Было» и «Стало». */
const INK2 = 'var(--pr-ink2)';
const dirFetch = (dir) => async (url) => (url.startsWith('ex/') ? new Response(fs.readFileSync(path.join(dir, url.slice(3)))) : new Response('{}', { status: 503 }));
async function eachView(media, fn) {
  for (const [t, dir] of DIRS.filter(([, d]) => fs.existsSync(path.join(d, 'sales.json')))) {
    const P = page({ fetch: dirFetch(dir), media, width: media.length ? wrapW(1440) : 360, opts: { examplesBase: 'ex/' } });
    for (const [i, name] of NAMES.entries()) {
      P.$$('.pr-ex button')[i].click();
      await until(() => P.api.state.name === name);
      const f = JSON.parse(fs.readFileSync(path.join(dir, name + '.json'), 'utf8'));
      for (const [mode, p] of [['before', f.process], ['after', E.after(f.process, f.improve)]]) {
        P.api.setMode(mode);
        fn(P, cascade(P.doc, media), t + name + ' ' + mode, p);
      }
    }
  }
}
const live = (P) => P.$$('.pr-node').filter((g) => !g.classList.contains('pr-out'));

test('пул от 900 px — как обложка: рамка шага 1.6 цветом роли, у внешней роли — пунктир --pr-ink2, полоски нет, текст по центру 650', async () => {
  let ext = 0;
  let hot = 0;
  await eachView(WIDE, (P, C, name, p) => {
    const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
    const xs = E.externals(p);
    const tasks = live(P).filter((g) => g.querySelector('.pr-box'));
    assert.equal(tasks.length, p.steps.filter((s) => s.kind === 'task').length, name);
    tasks.forEach((g) => {
      const s = by[g.getAttribute('data-id')];
      const box = g.querySelector('.pr-box');
      const id = name + ': ' + s.id;
      assert.equal(g.querySelector('.pr-stripe') || g.querySelector('.pr-hatch'), null, id + ' — полоска слева');
      const isExt = xs.has(s.role) && !s.auto;
      assert.equal(box.classList.contains('pr-bx'), isExt, id + ' — внешняя роль');
      if (g.classList.contains('pr-hot')) { hot++; assert.equal(C.val(box, 'stroke'), 'var(--red)', id + ' — узкое место красным'); } else if (isExt) {
        ext++;
        assert.deepEqual([C.val(box, 'stroke'), C.val(box, 'stroke-width')], [INK2, '1.6'], id);
        assert.ok(C.val(box, 'stroke-dasharray'), id + ' — пунктир');
      } else {
        // цвет роли — в рамке: --c шага (роль или «Система») и stroke: var(--c)
        assert.equal(g.style['--c'], s.auto ? 'var(--ok)' : 'var(--pr-' + s.role + ')', id);
        assert.deepEqual([C.val(box, 'stroke'), C.val(box, 'stroke-width'), C.val(box, 'stroke-dasharray')], ['var(--c)', '1.6', null], id);
      }
      // без color-mix (здесь @supports не включён) — сплошной фон окна, у внешней роли — нейтральный --soft
      assert.equal(C.val(box, 'fill'), isExt ? 'var(--soft)' : 'var(--bg)', id);
      const t = g.querySelector('.pr-t');
      assert.equal(t.getAttribute('text-anchor'), 'middle', id);
      t.children.forEach((sp) => assert.equal(sp.getAttribute('x'), '0', id));
      assert.deepEqual([C.val(t, 'font-weight'), C.val(t, 'fill'), C.size(t)], ['650', 'var(--ink)', 12], id);
    });
  });
  assert.ok(ext > 0, 'проверен шаг внешней роли (записи с живой модели)');
  assert.ok(hot > 0, 'проверен шаг узкого места');
});

test('пул от 900 px — рёбра --pr-ink2 1.3 со стрелкой на конце, обратные — пунктир того же цвета; подписи шагов — 600 --pr-ink2', async () => {
  let back = 0;
  await eachView(WIDE, (P, C, name) => {
    const edges = P.$$('.pr-svg .pr-e');
    assert.ok(edges.length > 0, name);
    edges.forEach((e) => {
      assert.deepEqual([C.val(e, 'stroke'), C.val(e, 'stroke-width'), C.val(e, 'fill')], [INK2, '1.3', 'none'], name);
      const m = /^url\(#([\w-]+)\)$/.exec(e.getAttribute('marker-end') || '');
      assert.ok(m, name + ': marker-end');
      const head = P.$('marker[id="' + m[1] + '"]').children[0];
      assert.equal(C.val(head, 'fill'), INK2, name + ': стрелка ' + m[1]);
      if (e.classList.contains('pr-back')) { back++; assert.ok(C.val(e, 'stroke-dasharray'), name + ': обратная ветка — пунктир'); } else assert.equal(C.val(e, 'stroke-dasharray'), null, name);
    });
    P.$$('.pr-svg .pr-ts').forEach((t) => assert.deepEqual([C.val(t, 'font-weight'), C.val(t, 'fill')], ['600', INK2], name + ': «' + t.textContent + '»'));
  });
  assert.ok(back > 0, 'проверены обратные ветки');
});

test('пул от 900 px — колонка дорожек без заливки, подпись 650 --pr-ink2, вертикаль --line-2; рамка пула --pr-ink2 1.2; слой закреплён', async () => {
  let lx = 0;
  await eachView(WIDE, (P, C, name) => {
    const hd = P.$('.pr-hd');
    assert.deepEqual([C.val(hd, 'display'), C.val(hd, 'position')], ['block', 'sticky'], name);
    P.$$('.pr-hd .pr-lane').forEach((g) => {
      const r = g.querySelector('.pr-lh');
      if (r.classList.contains('pr-lx')) lx++;
      // заливки нет (и у внешней роли — без штриховки); рамка ячейки — разделители дорожек и вертикаль после подписей
      assert.deepEqual([C.val(r, 'fill'), C.val(r, 'stroke')], ['none', 'var(--line-2)'], name + ': ' + g.textContent);
      const t = g.querySelector('.pr-lt');
      assert.deepEqual([C.val(t, 'font-weight'), C.val(t, 'fill')], ['650', g.classList.contains('pr-hot') ? 'var(--red)' : INK2], name + ': ' + g.textContent);
    });
    P.$$('.pr-svg .pr-lb').forEach((r) => assert.deepEqual([C.val(r, 'fill'), C.val(r, 'stroke')], ['none', 'var(--line-2)'], name));
    P.$$('.pr-pool').forEach((r) => assert.deepEqual([C.val(r, 'fill'), C.val(r, 'stroke'), C.val(r, 'stroke-width')], ['none', INK2, '1.2'], name));
    assert.match(C.val(P.$('.pr-wrap'), 'outline'), /var\(--pr-ink2\)/, name);
  });
  assert.ok(lx > 0, 'проверена дорожка внешней роли');
  // --pr-ink2 — --ink-2 сайта, на стенде без него — из переменных окна, не жёсткий цвет; без color-mix — --ink
  assert.match(CSS, /\.pr \{[^}]*--pr-ink2: var\(--ink-2, var\(--ink\)\);/);
  assert.match(CSS, /@supports \(color: color-mix\(in srgb, red, red\)\) \{\s*\.pr \{ --pr-ink2: var\(--ink-2, color-mix\(in srgb, var\(--ink\), var\(--muted\)\)\); \}/);
});

test('пул от 900 px — старт --ok 1.6, конец --ink 3.2 без точки, развилка --pr-ink2 1.4 с крестом 1.8', async () => {
  await eachView(WIDE, (P, C, name, p) => {
    const kinds = (k) => p.steps.filter((s) => s.kind === k).length;
    const ev = (k) => live(P).map((g) => g.querySelector('.pr-ev.pr-' + k)).filter(Boolean);
    const starts = ev('start');
    const ends = ev('end');
    assert.deepEqual([starts.length, ends.length], [kinds('start'), kinds('end')], name);
    starts.forEach((c) => assert.deepEqual([C.val(c, 'fill'), C.val(c, 'stroke'), C.val(c, 'stroke-width')], ['var(--bg)', 'var(--ok)', '1.6'], name));
    ends.forEach((c) => assert.deepEqual([C.val(c, 'fill'), C.val(c, 'stroke'), C.val(c, 'stroke-width')], ['var(--bg)', 'var(--ink)', '3.2'], name));
    P.$$('.pr-svg .pr-evd').forEach((c) => assert.equal(C.val(c, 'display'), 'none', name + ': точка в конце'));
    live(P).filter((g) => g.querySelector('.pr-gw')).forEach((g) => {
      const gw = g.querySelector('.pr-gw');
      assert.deepEqual([C.val(gw, 'fill'), C.val(gw, 'stroke'), C.val(gw, 'stroke-width')], ['var(--bg)', g.classList.contains('pr-hot') ? 'var(--red)' : INK2, g.classList.contains('pr-hot') ? '1.8' : '1.4'], name);
      assert.deepEqual([C.val(g.querySelector('.pr-gwx'), 'stroke'), C.val(g.querySelector('.pr-gwx'), 'stroke-width')], [INK2, '1.8'], name);
    });
  });
});

test('уже 900 px — как было: правила вида обложки только под .pr-wide, у шага полоска слева и текст от неё, линии и рамки прежние', async () => {
  // правила с --pr-ink2 и с новыми классами — только для пула (.pr-wrap.pr-wide есть только от 900 px)
  const pool = RULES.filter((r) => /var\(--pr-ink2\)|\.pr-(bx|start|end)\b/.test(r.body + ' ' + r.sel));
  assert.ok(pool.length >= 8, 'правил вида обложки: ' + pool.length);
  pool.forEach((r) => r.sel.split(',').forEach((x) => assert.match(x.trim(), /^\.pr-(wrap\.pr-)?wide\b/, r.sel)));
  await eachView([], (P, C, name, p) => {
    assert.ok(!P.$('.pr-wrap').classList.contains('pr-wide'), name);
    const xs = E.externals(p);
    const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
    live(P).forEach((g) => {
      const s = by[g.getAttribute('data-id')];
      const id = name + ': ' + s.id;
      if (s.kind === 'task') {
        const box = g.querySelector('.pr-box');
        const bar = g.querySelector(xs.has(s.role) && !s.auto ? '.pr-hatch' : '.pr-stripe');
        assert.ok(bar, id + ' — полоска слева');
        if (bar.classList.contains('pr-stripe')) assert.equal(C.val(bar, 'fill'), 'var(--c)', id);
        if (!g.classList.contains('pr-hot')) assert.deepEqual([C.val(box, 'stroke'), C.val(box, 'stroke-width'), C.val(box, 'stroke-dasharray')], ['var(--line-2)', '1.2', null], id);
        const t = g.querySelector('.pr-t');
        assert.equal(t.getAttribute('text-anchor'), 'start', id);
        t.children.forEach((sp) => assert.equal(+sp.getAttribute('x'), -+box.getAttribute('width') / 2 + 13, id));
        assert.deepEqual([C.val(t, 'font-weight'), C.size(t)], [null, 11], id);
      } else if (s.kind === 'decision') {
        if (!g.classList.contains('pr-hot')) assert.deepEqual([C.val(g.querySelector('.pr-gw'), 'stroke'), C.val(g.querySelector('.pr-gw'), 'stroke-width')], ['var(--c)', '1.8'], id);
        assert.equal(C.val(g.querySelector('.pr-gwx'), 'stroke'), 'var(--c)', id);
      } else {
        const c = g.querySelector('.pr-ev');
        assert.deepEqual([C.val(c, 'stroke'), C.val(c, 'stroke-width'), c.getAttribute('stroke-width')], ['var(--c)', '1.8', s.kind === 'end' ? '3' : '1.8'], id);
        if (s.kind === 'end') assert.deepEqual([C.val(g.querySelector('.pr-evd'), 'fill'), C.val(g.querySelector('.pr-evd'), 'display')], ['var(--c)', null], id);
      }
    });
    P.$$('.pr-svg .pr-e').forEach((e) => {
      const b = e.classList.contains('pr-back');
      assert.deepEqual([C.val(e, 'stroke'), C.val(e, 'stroke-width')], [b ? 'var(--muted)' : 'var(--line-2)', '1.4'], name);
      const head = P.$('marker[id="' + /#([\w-]+)/.exec(e.getAttribute('marker-end'))[1] + '"]').children[0];
      assert.equal(C.val(head, 'fill'), b ? 'var(--muted)' : 'var(--line-2)', name);
    });
    P.$$('.pr-svg .pr-el').forEach((t) => assert.deepEqual([C.val(t, 'fill'), C.val(t, 'font-weight'), C.size(t)], ['var(--muted)', null, 10], name));
    assert.equal(P.$$('.pr-pool').length + P.$$('.pr-lane').length, 0, name);
  });
});

/* Второй заход по пулу: заливка шагов, рёбра в своих дорожках, точки-заявки, вид без полосы процесса, подсказка прокрутки. */
const MIXQ = '@supports (fill: color-mix(in srgb, red, red))';
const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
const lum = (rgb) => rgb.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }).reduce((a, v, i) => a + v * [0.2126, 0.7152, 0.0722][i], 0);
const contrast = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
// color-mix(in srgb, c p, bg): доля p цвета c, остальное — bg, по каналам sRGB
const mix = (c, bg, p) => c.map((v, i) => v * p + bg[i] * (1 - p));
const varsOf = (body) => Object.fromEntries([...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
// светлая и тёмная тема стенда (dev.html) и окна (process.css): фон, текст, цвета ролей, доля цвета роли в заливке шага
function themes() {
  const html = fs.readFileSync(path.join(__dirname, 'dev.html'), 'utf8');
  const site = [/:root \{([^}]*)\}/, /:root\[data-theme="dark"\] \{([^}]*)\}/].map((re) => varsOf(re.exec(html)[1]));
  const pr = [/\n\.pr \{([^}]*)\}/, /\[data-theme="dark"\] \.pr \{([^}]*)\}/].map((re) => varsOf(re.exec(CSS)[1]));
  return [['светлая', Object.assign({}, site[0], pr[0])], ['тёмная', Object.assign({}, site[0], site[1], pr[0], pr[1])]];
}

test('пул от 900 px — заливка шага: светлый (в тёмной теме — тёмный) тон цвета роли, узкое место — красный тон, внешняя роль — --soft; сплошная; текст ≥ 4.5:1', async () => {
  assert.ok(CSS.includes(MIXQ + ' {'), 'заливка — под @supports, без color-mix — сплошной фон окна');
  let hot = 0;
  let ext = 0;
  await eachView(WIDE, (P, C0, name) => {
    const C = cascade(P.doc, WIDE.concat([MIXQ]));
    live(P).filter((g) => g.querySelector('.pr-box')).forEach((g) => {
      const box = g.querySelector('.pr-box');
      const id = name + ': ' + g.getAttribute('data-id');
      const want = box.classList.contains('pr-bx') ? (ext++, 'var(--soft)')
        : g.classList.contains('pr-hot') ? (hot++, 'color-mix(in srgb, var(--red) var(--pr-tint), var(--bg))') : 'color-mix(in srgb, var(--c) var(--pr-tint), var(--bg))';
      assert.equal(C.val(box, 'fill'), want, id);
      // сплошная заливка: рёбра под шагом не просвечивают; без color-mix — тоже сплошная (фон окна или --soft)
      for (const k of ['fill-opacity', 'opacity']) assert.equal(C.val(box, k) || C.val(g, k), null, id + ' ' + k);
      assert.match(C0.val(box, 'fill'), /^var\(--(bg|soft)\)$/, id);
    });
  });
  assert.ok(hot > 0 && ext > 0, 'проверены узкое место и внешняя роль');
  // контраст текста шага (--ink) на заливке — расчётом по цветам стенда в обеих темах
  for (const [t, v] of themes()) {
    const bg = hex(v.bg);
    const p = parseFloat(v['pr-tint']) / 100;
    assert.ok(p > 0 && p <= 0.3, t + ': доля цвета роли ' + p);
    const fills = [1, 2, 3, 4, 5, 6].map((i) => ['--pr-r' + i, mix(hex(v['pr-r' + i]), bg, p)])
      .concat([['--ok (Система)', mix(hex(v.ok), bg, p)], ['--red (узкое место)', mix(hex(v.red), bg, p)], ['--soft (внешняя роль)', hex(v.soft)]]);
    fills.forEach(([k, f]) => assert.ok(contrast(hex(v.ink), f) >= 4.5, t + ', ' + k + ': ' + contrast(hex(v.ink), f).toFixed(2)));
    // заливка отличима от фона окна
    fills.slice(0, 8).forEach(([k, f]) => assert.ok(f.some((c, i) => Math.abs(c - bg[i]) >= 6), t + ', ' + k + ' — тон заметен'));
  }
});

/* Все процессы из JSON репо в формате окна (шаги с next/branches; эталоны evals/reference — пары рёбер, окно их не рисует). */
const ROOT = path.join(__dirname, '..');
function repoProcs() {
  const out = [];
  const take = (o, imp, at) => {
    if (!o || typeof o !== 'object') return;
    if (Array.isArray(o.steps) && Array.isArray(o.roles) && o.steps.some((s) => s && (s.next || s.branches))) { out.push([at, o, imp]); return; }
    Object.keys(o).forEach((k) => take(o[k], /^(process|extract)$/.test(k) ? o.improve : null, at + '.' + k));
  };
  const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).forEach((d) => {
    if (d.name.startsWith('.') || d.name === 'node_modules') return;
    const f = path.join(dir, d.name);
    if (d.isDirectory()) walk(f);
    else if (d.name.endsWith('.json') && !d.name.startsWith('package')) take(JSON.parse(fs.readFileSync(f, 'utf8')), null, path.relative(ROOT, f));
  });
  walk(ROOT);
  return out.flatMap(([at, p, imp]) => [[at + ' было', p]].concat(imp ? [[at + ' стало', E.after(p, imp)]] : []));
}

test('пул на всех примерах репо: ребро не заходит в чужую дорожку (вершины — в дорожках начала и конца), вертикали разных рёбер не ближе 16 px; экран = .bpmn', () => {
  const all = repoProcs();
  assert.ok(all.length >= 40, 'вариантов: ' + all.length);
  assert.ok(all.some(([n]) => n.startsWith(path.join('web', 'fixtures', 'sales.json'))), 'sales — пример сайта по умолчанию');
  let back = 0;
  all.forEach(([name, p]) => {
    const P = E.pool(p);
    const D = di(E.toBpmn(p));
    const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
    const laneAt = (y) => P.lanes.find((l, i) => y >= l.y && (y < l.y + l.h || i === P.lanes.length - 1));
    const V = [];
    P.edges.forEach((e) => {
      const f = name + ': ' + e.from + '→' + e.to;
      const own = [roleOf(by[e.from]), roleOf(by[e.to])];
      if (e.back) back++;
      e.pts.forEach((q) => assert.ok(own.includes(laneAt(q[1]).role), f + (e.back ? ' (обратная)' : '') + ' — вершина ' + JSON.stringify(q) + ' в дорожке ' + laneAt(q[1]).role));
      assert.deepEqual(D.edges[e.id], e.pts, f + ' — точки в .bpmn');
      for (let i = 1; i < e.pts.length; i++) {
        const [a, b] = [e.pts[i - 1], e.pts[i]];
        if (a[0] === b[0] && a[1] !== b[1]) V.push({ e, x: a[0], lo: Math.min(a[1], b[1]), hi: Math.max(a[1], b[1]) });
      }
    });
    // параллельно = общий отрезок по y; на одной линии (0 px) — только входы в один шаг
    V.forEach((a, i) => V.slice(i + 1).forEach((b) => {
      if (a.e === b.e || Math.min(a.hi, b.hi) <= Math.max(a.lo, b.lo)) return;
      const d = Math.abs(a.x - b.x);
      assert.ok(d >= 16 || (d === 0 && a.e.to === b.e.to), name + ': ' + a.e.from + '→' + a.e.to + ' и ' + b.e.from + '→' + b.e.to + ' — ' + d + ' px');
    }));
    p.steps.forEach((s) => assert.deepEqual(D.shapes[s.id], shape(P.nodes[s.id]), name + ': ' + s.id));
    P.lanes.forEach((l) => assert.deepEqual(D.shapes[l.id], { x: l.x, y: l.y, w: l.w, h: l.h }, name + ': ' + l.id));
  });
  assert.ok(back >= 20, 'обратных веток: ' + back);
});

test('точки-заявки: на ребре не ближе двух диаметров, красная очередь — только у узкого места; легенда 12 px над пулом и над вертикальной схемой', async () => {
  let wait = 0;
  let red = 0;
  for (const name of NAMES) {
    const f = fx(name);
    for (const p of [f.process, E.after(f.process, f.improve)]) {
      const res = E.calc(p);
      for (const L of [E.pool(p), E.layout(p, { width: 360 })]) {
        const sim = E.createSim(p, res, L, { seed: 11 });
        for (let i = 0; i < 300; i++) {
          sim.advance(0.05);
          const snap = sim.snapshot();
          const moves = snap.dots.filter((d) => d.kind === 'move');
          moves.forEach((a, j) => moves.slice(j + 1).forEach((b) => {
            if (a.e === b.e) assert.ok(Math.hypot(a.x - b.x, a.y - b.y) >= 12, name + ': точки на ребре ' + a.e.from + '→' + a.e.to + ' ближе 12 px');
          }));
          // в очереди: красная (queue) — у шага роли hot/over, у остальных — обычным цветом (wait); число — как в очереди, не больше 12
          const hotQ = Object.keys(snap.queues).filter((id) => res.steps[id].level !== 'ok');
          const want = (ids) => ids.reduce((k, id) => k + Math.min(12, snap.queues[id]), 0);
          const kinds = (k) => snap.dots.filter((d) => d.kind === k).length;
          if (snap.dots.length < 60) {
            assert.equal(kinds('queue'), want(hotQ), name + ': красных точек');
            assert.equal(kinds('wait'), want(Object.keys(snap.queues).filter((id) => !hotQ.includes(id))), name + ': ждут обычным цветом');
          }
          if (res.roles[res.bottleneck] && res.roles[res.bottleneck].level === 'ok') assert.equal(kinds('queue'), 0, name + ': узких мест нет — красных точек нет');
          wait += kinds('wait');
          red += kinds('queue');
        }
      }
    }
  }
  assert.ok(wait > 0 && red > 0, 'проверены обе очереди: ' + wait + ' / ' + red);
  // на экране: красная точка — только pr-q (kind queue), очередь без узкого места — обычная pr-dot
  const src = fs.readFileSync(path.join(__dirname, 'process.js'), 'utf8');
  assert.match(src, /'pr-dot' \+ \(d\.kind === 'queue' \? ' pr-q' : ''\)/);
  // легенда — одна строка 12 px над схемой, цветами точек; есть, когда точки рисуются (без reduced-motion)
  for (const media of [WIDE, []]) {
    const P = page({ fetch: pageFetch, media, width: media.length ? wrapW(1440) : 360, opts: { motion: true } });
    await until(() => P.api.state.name === 'sales');
    const C = cascade(P.doc, media);
    const dl = P.$('.pr-dl');
    assert.ok(dl && !dl.hidden, 'легенда видна');
    assert.equal(dl.textContent, 'заявка в пути ·ждёт у шага');
    assert.deepEqual(dl.children.map((i) => i.localName + '.' + i.className), ['i.', 'i.pr-q']);
    const kids = P.$('.pr-body').children;
    assert.equal(kids.indexOf(dl) + 1, kids.indexOf(P.$('.pr-wrap')), 'сразу над схемой');
    assert.equal(kids[kids.indexOf(dl) - 1], P.$('.pr-note'));
    assert.deepEqual([C.val(dl, 'font-size'), C.val(dl, 'display')], ['12px', 'flex']);
    // кружки — размер точки (r = 3), «в пути» — цвет .pr-dot; «ждёт» красная, только когда узкое место есть (.pr-note.pr-bad)
    assert.match(CSS, /\.pr-dl i \{[^}]*width: 6px; height: 6px; border-radius: 50%; background: var\(--ink\); opacity: \.72;/);
    assert.match(CSS, /\.pr-dot \{ fill: var\(--ink\); fill-opacity: \.72; \}/);
    assert.match(CSS, /\.pr-bad \+ \.pr-dl \.pr-q \{ background: var\(--red\);/);
    assert.ok(P.$('.pr-note').classList.contains('pr-bad'), 'в sales узкое место есть');
    P.$$('.pr-ex button')[1].click();
    await until(() => P.api.state.name === 'invoice');
    assert.ok(!P.$('.pr-note').classList.contains('pr-bad') && /Узких мест нет/.test(P.$('.pr-note').textContent), 'в invoice узких мест нет — «ждёт» обычным цветом');
  }
  // без движения точек нет — и легенды нет
  const R = page({ fetch: pageFetch, media: WIDE, width: wrapW(1440) });
  await until(() => R.api.state.name === 'sales');
  assert.ok(R.$('.pr-dl').hidden);
});

test('пул от 900 px — как обложка до мелочей: полосы имени процесса на экране нет (координаты те же, что в .bpmn), подписи «да/нет» — --muted 600, контраст ≥ 4.5:1', async () => {
  await eachView(WIDE, (P, C, name, p) => {
    const L = E.pool(p);
    assert.equal(P.$$('.pr-pt').length, 0, name + ': полоса имени процесса');
    assert.equal(P.$('.pr-svg').getAttribute('viewBox'), view(L), name);
    assert.equal(+P.$('.pr-svg').getAttribute('width'), L.width - L.head, name);
    assert.equal(P.$('.pr-hd').getAttribute('viewBox'), L.head + ' 0 ' + (L.head + 1) + ' ' + L.height, name);
    assert.equal(P.$('.pr-wrap').style['--pr-w'], L.width - L.head + 'px', name);
    // в .bpmn у участника своя полоса имени: участник с x = 0, дорожки и шаги — на тех же координатах, что на экране
    const D = di(E.toBpmn(p));
    assert.deepEqual(D.shapes.Participant_1, { x: 0, y: 0, w: L.width, h: L.height });
    L.lanes.forEach((l) => assert.equal(D.shapes[l.id].x, L.head, name));
    P.$$('.pr-svg .pr-lb').forEach((r, i) => assert.equal(+r.getAttribute('x'), D.shapes[L.lanes[i].id].x, name));
    const labs = P.$$('.pr-svg .pr-el');
    labs.forEach((t) => assert.deepEqual([C.val(t, 'fill'), C.val(t, 'font-weight'), C.size(t)], ['var(--muted)', '600', 12], name + ': «' + t.textContent + '»'));
  });
  for (const [t, v] of themes()) assert.ok(contrast(hex(v.muted), hex(v.bg)) >= 4.5, t + ': «да/нет» ' + contrast(hex(v.muted), hex(v.bg)).toFixed(2));
});

test('пул шире блока: у правого края — узкое затемнение под фон и плашка «→ прокрутите» 12 px в нижнем углу; докрутили до конца или пул весь в блоке — гаснут; без анимации при reduced-motion', async () => {
  const P = page({ fetch: pageFetch, media: WIDE, width: wrapW(1440) });
  await until(() => P.api.state.name === 'sales');
  const L = E.pool(fx('sales').process);
  const wrap = P.$('.pr-wrap');
  const at = (left, scroll) => { wrap.scrollWidth = scroll; wrap.scrollLeft = left; wrap.fire('scroll'); return wrap.classList.contains('pr-fin'); };
  const SW = L.width - L.head;
  assert.ok(SW > wrap.clientWidth, 'sales шире блока');
  assert.equal(at(0, SW), false, 'в начале — подсказка видна');
  assert.equal(at(Math.round((SW - wrap.clientWidth) / 2), SW), false, 'посередине — видна');
  assert.equal(at(SW - wrap.clientWidth, SW), true, 'докрутили — гаснет');
  assert.equal(at(0, wrap.clientWidth), true, 'пул весь в блоке — подсказки нет');
  // после смены вида (новый пул, прокрутка в начале) — снова видна
  wrap.scrollWidth = SW;
  wrap.scrollLeft = 0;
  P.api.setMode('after');
  assert.ok(!wrap.classList.contains('pr-fin'));
  const hint = /\.pr-wide \.pr-stage::after \{([^}]*)\}/.exec(CSS);
  assert.ok(hint, 'подсказка — ::after ячейки пула');
  const v = varsOf(hint[1].replace(/(^|;)\s*([a-z-]+):/g, '$1 --$2:'));
  assert.equal(v.content, '"→ прокрутите"');
  assert.deepEqual([v['font-size'], v.position, v['pointer-events'], v['justify-self'], v['align-self']], ['12px', 'sticky', 'none', 'end', 'end']);
  assert.match(v.right, /^\d+px$/);
  assert.equal(v.background, 'var(--bg)', 'плашка — на фоне окна');
  // затемнение — узкая полоса ::before: крайние шаги и рёбра к ним видны
  const fade = /\.pr-wide \.pr-stage::before \{([^}]*)\}/.exec(CSS);
  assert.ok(fade, 'затемнение — ::before ячейки пула');
  const f = varsOf(fade[1].replace(/(^|;)\s*([a-z-]+):/g, '$1 --$2:'));
  assert.ok(parseFloat(f.width) <= 32, 'затемнение не шире 32 px: ' + f.width);
  assert.equal(f.background, 'linear-gradient(to right, transparent, var(--bg))', 'затемнение — под фон окна, без сплошной части');
  assert.deepEqual([f.position, f['pointer-events'], f['justify-self']], ['sticky', 'none', 'end']);
  assert.match(CSS, /\.pr-wide\.pr-fin \.pr-stage::before, \.pr-wide\.pr-fin \.pr-stage::after \{ opacity: 0; visibility: hidden; \}/);
  // переход гасит общий reduced-motion: .pr *::after без transition
  assert.match(CSS, /@media \(prefers-reduced-motion: reduce\) \{\s*\.pr \*, \.pr \*::before, \.pr \*::after \{ animation: none !important; transition: none !important; \}/);
  // уже 900 px подсказки нет: правило — только под .pr-wide
  assert.ok(!/(^|\})\s*\.pr-stage::(after|before)/.test(CSS));
});
