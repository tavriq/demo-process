// Раскладка, .bpmn и симуляция: node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('./engine.js');

const NAMES = ['sales', 'invoice', 'returns', 'onboarding'];
const fx = (name) => JSON.parse(JSON.stringify(require('./fixtures/' + name + '.json')));
// Записи с живой модели (app/recorded) — тоже проверяем, если они есть рядом.
const REC = require('node:path').join(__dirname, '..', 'app', 'recorded');
const recorded = () => (require('node:fs').existsSync(REC) ? NAMES.filter((n) => require('node:fs').existsSync(REC + '/' + n + '.json'))
  .map((n) => ['запись ' + n, JSON.parse(require('node:fs').readFileSync(REC + '/' + n + '.json', 'utf8'))]) : []);
const variants = () => NAMES.map((n) => [n, fx(n)]).concat(recorded()).flatMap(([n, f]) =>
  [[n + ' было', f.process], [n + ' стало', E.after(f.process, f.improve)]]);
const overlap = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

test('раскладка на 360 px: узлы не пересекаются и не вылезают', () => {
  variants().forEach(([name, p]) => {
    const L = E.layout(p, { width: 360 });
    const ns = Object.values(L.nodes);
    assert.equal(ns.length, p.steps.length, name);
    ns.forEach((n, i) => {
      assert.ok(n.box.x >= 0 && n.box.x + n.box.w <= 360, name + ': ' + n.id + ' за краем');
      ns.slice(i + 1).forEach((m) => assert.ok(!overlap(n.box, m.box), name + ': ' + n.id + ' и ' + m.id));
    });
    const rows = {};
    ns.forEach((n) => { rows[n.row] = (rows[n.row] || 0) + 1; });
    Object.values(rows).forEach((k) => assert.ok(k <= 3, name + ': больше 3 в ряд'));
  });
});

test('рёбра — под прямым углом и не проходят сквозь чужие узлы', () => {
  variants().forEach(([name, p]) => {
    const L = E.layout(p, { width: 360 });
    let edges = 0;
    p.steps.forEach((s) => { edges += s.kind === 'decision' ? s.branches.length : (s.next || []).length; });
    assert.equal(L.edges.length, edges, name);
    L.edges.forEach((e) => {
      for (let i = 1; i < e.pts.length; i++) {
        const [a, b] = [e.pts[i - 1], e.pts[i]];
        assert.ok(a[0] === b[0] || a[1] === b[1], name + ': косой отрезок ' + e.from + '→' + e.to);
        const seg = { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), w: Math.abs(a[0] - b[0]) || 0.01, h: Math.abs(a[1] - b[1]) || 0.01 };
        Object.values(L.nodes).forEach((n) => {
          if (n.id !== e.from && n.id !== e.to) assert.ok(!overlap(seg, n.box), name + ': ' + e.from + '→' + e.to + ' через ' + n.id);
        });
      }
      assert.ok(e.pts.every((q) => q[0] >= 0 && q[0] <= 360), name + ': ребро за краем');
    });
  });
});

test('ранги сверху вниз, обратные рёбра — пунктиром справа с подписью', () => {
  const L = E.layout(fx('sales').process, { width: 360 });
  const n = L.nodes;
  assert.ok(n.s1.cy < n.s2.cy && n.s2.cy < n.s3.cy && n.s5.cy < n.s6.cy);
  const back = L.edges.filter((e) => e.back);
  assert.deepEqual(back.map((e) => e.from + '>' + e.to), ['s5>s3']);
  assert.equal(back[0].label, 'на доработку');
  const maxX = Math.max(...back[0].pts.map((q) => q[0]));
  Object.values(n).forEach((x) => assert.ok(maxX > x.box.x + x.box.w, 'дорожка правее узлов'));
  assert.ok(E.pathD(back[0].pts).startsWith('M'));
});

test('три ветки решения встают в один ряд', () => {
  const L = E.layout(fx('returns').process, { width: 360 });
  const n = L.nodes;
  assert.equal(n.s4.row, n.s5.row);
  assert.equal(n.s5.row, n.s6.row);
  assert.ok(n.s4.cx < n.s5.cx && n.s5.cx < n.s6.cx);
  assert.deepEqual(L.edges.filter((e) => e.from === 's3').map((e) => e.label), ['брак', 'не подошёл', 'повреждён в пути']);
});

test('подписи узлов на 360 px не обрезаются', () => {
  variants().forEach(([name, p]) => {
    const L = E.layout(p, { width: 360 });
    p.steps.forEach((s) => {
      const n = L.nodes[s.id];
      const text = n.lines.join(' ').replace(/- /g, '');
      assert.equal(text.replace(/\s/g, ''), s.name.replace(/\s/g, ''), name + ': ' + s.id + ' ' + JSON.stringify(n.lines));
      if (n.kind === 'task') assert.ok(n.lines.length * 12 <= n.sh - 10, name + ': текст ' + s.id + ' не влез в высоту');
    });
  });
  const L = E.layout(fx('returns').process, { width: 360 });
  assert.ok(L.nodes.s5.lines.length >= 3, 'в узкой ячейке — перенос в несколько строк');
});

test('перенос подписи в узле', () => {
  assert.deepEqual(E.wrap('Перенести заявку в таблицу', 12, 3), ['Перенести', 'заявку в', 'таблицу']);
  const w = E.wrap('Очень длинное название шага, которое не помещается в три коротких строки', 10, 3);
  assert.equal(w.length, 3);
  assert.ok(w[2].endsWith('…'));
});

test('.bpmn: все шаги, рёбра, дорожки и DI', () => {
  variants().forEach(([name, p]) => {
    const x = E.toBpmn(p, { mode: 'было' });
    assert.ok(x.startsWith('<?xml') && x.includes('<bpmn:definitions') && x.trim().endsWith('</bpmn:definitions>'), name);
    const tag = { start: 'startEvent', end: 'endEvent', decision: 'exclusiveGateway' };
    p.steps.forEach((s) => {
      const t = tag[s.kind] || (s.auto ? 'serviceTask' : 'task');
      assert.ok(x.includes('<bpmn:' + t + ' id="' + s.id + '"'), name + ': ' + s.id);
      assert.ok(x.includes('bpmnElement="' + s.id + '"'), name + ': DI ' + s.id);
      assert.ok(x.includes('<bpmn:flowNodeRef>' + s.id + '</bpmn:flowNodeRef>'), name + ': дорожка ' + s.id);
    });
    p.steps.forEach((s) => (s.kind === 'decision' ? s.branches.map((b) => b.to) : s.next).forEach((to) => {
      assert.match(x, new RegExp('sourceRef="' + s.id + '" targetRef="' + to + '"'), name + ': ' + s.id + '→' + to);
    }));
    const flows = (x.match(/<bpmn:sequenceFlow /g) || []).length;
    assert.equal((x.match(/<bpmndi:BPMNEdge /g) || []).length, flows);
    assert.equal((x.match(/<di:waypoint /g) || []).length >= flows * 2, true);
    const lanes = new Set(p.steps.map((s) => (s.auto ? E.SYS : s.role)));
    assert.equal((x.match(/<bpmn:lane /g) || []).length, lanes.size, name);
    assert.equal((x.match(/<bpmndi:BPMNShape id="Lane_/g) || []).length, lanes.size, name);
    assert.ok(x.includes('<bpmn:participant') && x.includes('<bpmndi:BPMNPlane'));
    // подписи веток и экранирование
    p.steps.filter((s) => s.kind === 'decision').forEach((s) => s.branches.forEach((b) => assert.ok(x.includes('name="' + b.label + '"'))));
    assert.ok(!/[<>]"/.test(x.replace(/<[^>]*>/g, '')));
  });
  const p = fx('invoice').process;
  p.steps[1].name = 'Сверить <счёт> & "заказ"';
  assert.ok(E.toBpmn(p).includes('name="Сверить &lt;счёт&gt; &amp; &quot;заказ&quot;"'));
});

test('.bpmn «Стало»: автоматизированный шаг — serviceTask в дорожке «Система»', () => {
  const f = fx('invoice');
  const x = E.toBpmn(E.after(f.process, f.improve), { mode: 'стало' });
  assert.ok(x.includes('<bpmn:serviceTask id="s2"'));
  assert.ok(x.includes('<bpmn:lane id="Lane_sys" name="Система">'));
  assert.ok(!x.includes('id="s8"'));
});

test('симуляция: детерминирована, не больше 60 точек, очередь у перегруженной роли', () => {
  const p = fx('onboarding').process;
  const res = E.calc(p);
  const L = E.layout(p, { width: 360 });
  const run = () => {
    const sim = E.createSim(p, res, L, { seed: 3 });
    for (let i = 0; i < 400; i++) sim.advance(0.05);
    return sim.snapshot();
  };
  const a = run();
  assert.deepEqual(a, run());
  assert.ok(a.dots.length > 0 && a.dots.length <= 60);
  const hr = p.steps.filter((s) => s.role === 'r2').map((s) => s.id);
  const q = hr.reduce((k, id) => k + (a.queues[id] || 0), 0);
  assert.ok(q >= 5, 'очередь у HR: ' + q);
  Object.keys(a.more).forEach((id) => assert.equal(a.more[id], a.queues[id] - 12));
  assert.ok(a.dots.filter((d) => d.kind === 'queue').length <= 12 * Object.keys(a.queues).length);
});

test('симуляция без перегрузки: очередь небольшая, точки на схеме', () => {
  const p = fx('invoice').process;
  const L = E.layout(p, { width: 360 });
  const sim = E.createSim(p, E.calc(p), L, { seed: 5 });
  for (let i = 0; i < 300; i++) sim.advance(0.05);
  const s = sim.snapshot();
  const q = Object.values(s.queues).reduce((a, b) => a + b, 0);
  assert.ok(q <= 6, 'очередь ' + q);
  s.dots.forEach((d) => assert.ok(d.x >= 0 && d.x <= 360 && d.y >= 0 && d.y <= L.height));
});
