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
    assert.equal(P.$('.pr-svg').getAttribute('viewBox'), '0 0 ' + L.width + ' ' + L.height);
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
   специфичностью (при равной — из последнего). @media не различаются; селекторы с «:», «>», «[» пропущены. */
const CSS = fs.readFileSync(path.join(__dirname, 'process.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
function cascade(doc) {
  const rules = [];
  for (const m of CSS.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!/(?:^|;)\s*(font|font-size|max-width|overflow|overflow-x|display|position|left|z-index|padding|fill|fill-opacity|opacity):/.test(m[2])) continue;
    m[1].split(',').map((x) => x.trim()).filter((x) => /^[\w.\s-]+$/.test(x)).forEach((sel) => {
      rules.push({ els: new Set(doc.querySelectorAll(sel)), spec: (sel.match(/\.[\w-]+/g) || []).length * 100 + (sel.match(/(?:^|\s)[a-z]/g) || []).length, body: m[2] });
    });
  }
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
// ширина блока пула при ширине окна vw — по --pr-pw из process.css
const PW = /--pr-pw: min\((\d+)px, calc\(100vw - (\d+)px\)\)/.exec(CSS);
const wrapW = (vw) => Math.min(+PW[1], vw - +PW[2]);
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
        const C = cascade(P.doc);
        assert.equal(P.$('.pr-svg').getAttribute('viewBox'), '0 0 ' + L.width + ' ' + L.height);
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
        if (L.width > width) assert.equal(C.val(P.$('.pr-wrap'), 'overflow-x'), 'auto');
      }
    }
    for (const c of ['pr-t', 'pr-ts', 'pr-el pr-halo', 'pr-ql pr-halo', 'pr-autot', 'pr-lt', 'pr-pt']) assert.ok(seen.has(c), vw + ' px: проверен ' + c);
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
    const C = cascade(P.doc);
    const wrap = P.$('.pr-wrap');
    const hd = P.$('.pr-hd');
    const svg = P.$('.pr-svg');
    assert.ok(L.width > width, 'пул шире блока — есть что прокручивать');
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
    // в слое — полоса процесса и заголовок каждой дорожки в координатах пула, под ними — сплошной фон
    const w = 2 * L.head + 1;
    assert.equal(hd.getAttribute('viewBox'), '0 0 ' + w + ' ' + L.height);
    assert.deepEqual([+hd.getAttribute('width'), +hd.getAttribute('height')], [w, L.height]);
    const bg = hd.children[0];
    assert.ok(bg.classList.contains('pr-hbk'));
    assert.deepEqual([bg.getAttribute('x'), bg.getAttribute('y'), +bg.getAttribute('width'), +bg.getAttribute('height')], [null, null, w, L.height]);
    assert.equal(C.val(bg, 'fill'), 'var(--bg)');
    assert.equal(C.val(bg, 'fill-opacity') || C.val(bg, 'opacity'), null);
    assert.equal(hd.querySelector('.pr-pt').textContent, L.titleLines.join(''));
    const heads = P.$$('.pr-hd .pr-lane');
    assert.deepEqual(heads.map((g) => g.textContent), L.lanes.map((l) => l.lines.join('')));
    heads.forEach((g, i) => {
      const r = g.querySelector('.pr-lh');
      const d = D.shapes[L.lanes[i].id];
      assert.deepEqual(['x', 'y', 'width', 'height'].map((k) => +r.getAttribute(k)), [d.x, d.y, L.head, d.h]);
      assert.ok(d.x + L.head < w);
    });
    assert.equal(P.$$('.pr-svg .pr-lt').length, 0, 'имена дорожек — только в закреплённом слое');
    // без прокрутки слой ничего не закрывает: шаги, подписи и рёбра — правее него
    Object.values(L.nodes).forEach((n) => assert.ok(n.box.x >= w, n.id));
    L.edges.forEach((e) => { e.pts.forEach((q) => assert.ok(q[0] >= w, e.id)); if (e.labBox) assert.ok(e.labBox.x >= w, e.id); });
    // прокрутка на x: sticky с left: 0 ставит слой на x (не правее конца пула) — он целиком в видимой части [x, x + width]
    for (const x of [0, 1, Math.round((L.width - width) / 2), L.width - width]) {
      const at = Math.min(Math.max(x + parseFloat(C.val(hd, 'left')), 0), L.width - w);
      assert.ok(at >= x && at + w <= x + width, 'прокрутка ' + x);
    }
  }
});
