// Фикстуры в формате записанного примера и ограничения страницы: node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const NAMES = ['sales', 'invoice', 'returns', 'onboarding'];
const load = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', n + '.json'), 'utf8'));
const norm = (s) => s.toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();

test('формат записанного примера (тот же вид, что runs/{id})', () => {
  NAMES.forEach((n) => {
    const f = load(n);
    assert.deepEqual(Object.keys(f).sort(), ['error', 'improve', 'name', 'process', 'stage', 'status', 'steps', 'text', 'title', 'totals'].sort(), n);
    assert.equal(f.name, n);
    assert.equal(f.status, 'done');
    assert.equal(f.stage, 'done');
    assert.equal(f.error, null);
    assert.ok(f.text.length >= 600 && f.text.length <= 1200, n + ': ' + f.text.length + ' знаков');
    f.steps.forEach((r) => {
      assert.ok(['extract', 'improve'].includes(r.step) && r.attempt >= 1, n);
      ['ms', 'tokens_in', 'tokens_out', 'rub'].forEach((k) => assert.equal(typeof r[k], 'number'));
      assert.equal(r.ok === false, typeof r.error === 'string', n + ': error только у неудачных');
    });
    ['ms', 'tokens_in', 'tokens_out'].forEach((k) => assert.equal(f.totals[k], f.steps.reduce((a, r) => a + r[k], 0), n + ' ' + k));
    assert.ok(Math.abs(f.totals.rub - f.steps.reduce((a, r) => a + r.rub, 0)) < 0.005);
  });
});

test('Process по контракту: виды, id, ветки, связность', () => {
  NAMES.forEach((n) => {
    const p = load(n).process;
    const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
    const roles = new Set(p.roles.map((r) => r.id));
    p.roles.forEach((r) => { assert.match(r.id, /^r[1-6]$/); assert.equal(typeof r.external, 'boolean', n + ' ' + r.id); });
    assert.equal(p.steps.filter((s) => s.kind === 'start').length, 1, n);
    assert.ok(p.steps.some((s) => s.kind === 'end'), n);
    const outs = (s) => (s.kind === 'decision' ? s.branches.map((b) => b.to) : s.next);
    p.steps.forEach((s) => {
      assert.match(s.id, /^s([1-9]|1[0-5])$/);
      assert.ok(roles.has(s.role), n + ': роль ' + s.id);
      assert.ok(['start', 'task', 'decision', 'end'].includes(s.kind));
      if (s.kind === 'decision') assert.ok(s.branches.length >= 2 && s.branches.length <= 3 && s.next.length === 0, n + ' ' + s.id);
      if (s.kind === 'start' || s.kind === 'task') assert.equal(s.next.length, 1, n + ': у ' + s.id + ' ровно один next');
      if (s.kind === 'end') assert.equal(s.next.length, 0);
      outs(s).forEach((t) => assert.ok(by[t], n + ': ' + s.id + '→' + t));
      ['tool', 'minutes', 'wait_hours'].forEach((k) => assert.ok(k in s, n + ' ' + s.id + ' ' + k));
    });
    const start = p.steps.find((s) => s.kind === 'start');
    assert.ok(!p.steps.some((s) => outs(s).includes(start.id)), n + ': в start ничего не входит');
    const seen = new Set([start.id]);
    const q = [start.id];
    while (q.length) outs(by[q.shift()]).forEach((t) => { if (!seen.has(t)) { seen.add(t); q.push(t); } });
    assert.equal(seen.size, p.steps.length, n + ': из start достижим каждый шаг');
    p.steps.forEach((s) => {
      const r = new Set([s.id]);
      const qq = [s.id];
      while (qq.length) outs(by[qq.shift()]).forEach((t) => { if (!r.has(t)) { r.add(t); qq.push(t); } });
      assert.ok([...r].some((id) => by[id].kind === 'end'), n + ': из ' + s.id + ' есть путь до end');
    });
  });
});

test('цитаты дословно из текста, числа — только из цитаты', () => {
  NAMES.forEach((n) => {
    const f = load(n);
    const t = norm(f.text);
    const p = f.process;
    p.steps.forEach((s) => {
      if (s.quote == null) assert.ok(p.flags.unquoted.includes(s.id), n + ': ' + s.id + ' без цитаты не в flags');
      else {
        assert.ok(s.quote.length <= 120, n + ' ' + s.id);
        assert.ok(t.includes(norm(s.quote)), n + ': цитата ' + s.id + ' не из текста');
      }
      ['minutes', 'wait_hours'].forEach((k) => {
        if (s[k] != null) assert.ok((s.quote || '').includes(String(s[k])), n + ': ' + s.id + '.' + k + ' без опоры');
      });
    });
    p.flags.numbers_dropped.forEach((d) => {
      const s = p.steps.find((x) => x.id === d.step);
      assert.equal(s[d.field], null);
      assert.ok(!(s.quote || '').includes(String(d.value)));
    });
  });
});

test('Improve по контракту: действия, без цифр, flags.numbers_in_text в improve', () => {
  NAMES.forEach((n) => {
    const f = load(n);
    const by = Object.fromEntries(f.process.steps.map((s) => [s.id, s]));
    assert.ok(Array.isArray(f.improve.flags.numbers_in_text), n);
    assert.ok(!('numbers_in_text' in f.process.flags), n);
    assert.ok(f.improve.summary.length <= 140 && !/\d/.test(f.improve.summary), n + ' summary');
    f.improve.changes.forEach((c) => {
      const s = by[c.step];
      assert.ok(s, n + ': ' + c.step);
      const ok = s.kind === 'task' ? ['keep', 'automate', 'remove', 'merge'] : s.kind === 'decision' ? ['keep', 'automate'] : ['keep'];
      assert.ok(ok.includes(c.action), n + ': ' + c.step + ' ' + c.action);
      if (f.process.roles.find((r) => r.id === s.role).external) assert.equal(c.action, 'keep', n + ': внешний шаг ' + c.step);
      if (c.action === 'merge') {
        const t = by[c.merge_into];
        assert.ok(t && t.role === s.role, n + ': merge_into той же роли');
        assert.ok(t.next.includes(s.id) || s.next.includes(t.id), n + ': merge_into — соседний шаг');
      } else assert.equal(c.merge_into, null);
      ['how', 'risk'].forEach((k) => assert.ok(c[k].length <= 140 && !/\d/.test(c[k]), n + ' ' + c.step + '.' + k));
    });
  });
  const all = NAMES.map(load);
  assert.ok(all.some((f) => f.process.steps.some((s) => s.kind === 'decision' && s.branches.length === 3)), 'решение с тремя ветками');
  assert.ok(all.some((f) => f.improve.changes.some((c) => c.action === 'merge')), 'есть merge');
  assert.ok(all.some((f) => f.process.roles.some((r) => r.external)), 'есть внешняя роль');
  assert.ok(all.some((f) => f.improve.changes.some((c) => c.action === 'remove')), 'есть remove');
  assert.ok(all.every((f) => /\d/.test(f.text)), 'в каждом тексте есть числа');
});

// SPEC-analyst: JS окна вместе ≤ 85 КБ несжатого; КБ — 1000 байт, как в SPEC («сейчас 62» — это 61 852 байта).
test('страница: JS до 85 КБ, без библиотек, :has и innerHTML', () => {
  const js = ['engine.js', 'process.js'].map((f) => fs.readFileSync(path.join(__dirname, f)));
  const size = js.reduce((a, b) => a + b.length, 0);
  assert.ok(size <= 85 * 1000, 'JS ' + size + ' байт');
  const src = js.map(String).join('\n');
  assert.ok(!/innerHTML|insertAdjacentHTML|document\.write|eval\(/.test(src));
  assert.ok(!/https?:\/\/(?!www\.(w3|omg)\.org|bpmn\.io)/.test(src), 'внешние адреса');
  const css = fs.readFileSync(path.join(__dirname, 'process.css'), 'utf8');
  assert.ok(!/:has\(/.test(css));
  assert.ok(!/@import|url\(/.test(css));
  css.replace(/\/\*[\s\S]*?\*\//g, '').split('}').forEach((rule) => {
    const body = rule.split('{').pop();
    if (/--pr-r\d:/.test(body)) return;
    assert.ok(!/#[0-9a-f]{3,6}\b|rgba?\(/i.test(body), 'цвет не через переменные: ' + rule.trim().slice(0, 60));
  });
  for (const v of ['--pr-r1', '--pr-r6']) assert.equal((css.match(new RegExp(v + ':', 'g')) || []).length, 3, v + ' для светлой и тёмной темы');
  // окно стоит в секции сайта со своим h2 — само окно h2 не создаёт; пока грузится первый пример, высота зарезервирована
  assert.ok(!/h\('h[12]'/.test(src), 'окно не создаёт h1/h2');
  assert.match(src, /h\('h3', \{ class: 'pr-h' \}\)/);
  assert.match(css, /\.pr-first \.pr-body \{ min-height: var\(--pr-reserve\); \}/);
  const html = fs.readFileSync(path.join(__dirname, 'dev.html'), 'utf8');
  assert.ok(!/<script[^>]+src="https?:|<link[^>]+href="https?:/.test(html), 'без CDN');
});
