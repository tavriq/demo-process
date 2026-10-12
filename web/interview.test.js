// Интервью v2 (docs/SPEC-analyst.md): фикстуры по контракту API, отчёт, ответы поверх допущений. node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const E = require('./engine.js');

const fx = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'interview-' + n + '.json'), 'utf8'));
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, a + ' ≠ ' + b);
const norm = (s) => s.toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim();
const FIELD = /^(volume|rate|s([1-9]|1[0-5])\.(minutes|wait_hours)|r[1-6]\.people)$/;

/* Process по контракту v1; цитаты — дословно из ответов, данных к этому моменту; числа — только из цитаты. */
function checkProcess(p, said, where) {
  const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
  const roles = new Set(p.roles.map((r) => r.id));
  p.roles.forEach((r) => { assert.match(r.id, /^r[1-6]$/); assert.equal(typeof r.external, 'boolean', where); });
  assert.equal(p.steps.filter((s) => s.kind === 'start').length, 1, where);
  assert.ok(p.steps.some((s) => s.kind === 'end'), where);
  const outs = (s) => (s.kind === 'decision' ? s.branches.map((b) => b.to) : s.next);
  p.steps.forEach((s) => {
    assert.match(s.id, /^s([1-9]|1[0-5])$/, where);
    assert.ok(roles.has(s.role), where + ': роль ' + s.id);
    if (s.kind === 'decision') assert.ok(s.branches.length >= 2 && s.branches.length <= 3 && s.branches.every((b) => b.label), where + ' ' + s.id);
    else assert.equal(s.next.length, s.kind === 'end' ? 0 : 1, where + ' ' + s.id);
    outs(s).forEach((t) => assert.ok(by[t], where + ': ' + s.id + '→' + t));
    assert.ok(s.quote && s.quote.length <= 120 && norm(said).includes(norm(s.quote)), where + ': цитата ' + s.id + ' не из ответов');
    ['minutes', 'wait_hours'].forEach((k) => { if (s[k] != null) assert.ok(s.quote.includes(String(s[k])), where + ': ' + s.id + '.' + k); });
  });
  const seen = new Set();
  const q = [p.steps.find((s) => s.kind === 'start').id];
  while (q.length) { const id = q.shift(); if (!seen.has(id)) { seen.add(id); q.push(...outs(by[id])); } }
  assert.equal(seen.size, p.steps.length, where + ': из start достижим каждый шаг');
  assert.deepEqual(p.flags, { unquoted: [], numbers_dropped: [] }, where);
}

test('фикстуры интервью по контракту: старт, ответы, отчёт по ссылке, ошибки', () => {
  const start = fx('start');
  const answers = fx('answers');
  const done = fx('done');
  assert.deepEqual(Object.keys(start).sort(), ['budget_left_rub', 'id', 'process', 'question'].sort());
  assert.equal(start.process, null);
  assert.match(start.id, /^[\w-]{4,64}$/);
  const qs = [start.question].concat(answers.map((a) => a.question).filter(Boolean));
  assert.equal(qs.length, 8);
  qs.forEach((q, i) => {
    assert.deepEqual(Object.keys(q).sort(), ['gap', 'max', 'n', 'text']);
    assert.equal(q.n, i + 1);
    assert.equal(q.max, 8);
    assert.ok(q.text.length <= 160 && !/\d/.test(q.text), 'вопрос без цифр: ' + q.text);
  });
  assert.equal(new Set(qs.map((q) => q.gap)).size, qs.length, 'тот же пробел дважды не спрашивается');
  let said = '';
  answers.forEach((a, i) => {
    const where = 'ответ ' + (i + 1);
    assert.deepEqual(Object.keys(a).sort(), ['answer_text', 'done', 'flags', 'process', 'question', 'steps', 'understood'], where);
    assert.ok(a.answer_text.length >= 1 && a.answer_text.length <= 500, where);
    said += ' ' + a.answer_text;
    checkProcess(a.process, said, where);
    a.understood.forEach((u) => {
      assert.match(u.field, FIELD, where);
      assert.ok(typeof u.value === 'number' && u.value >= 0 && typeof u.unit === 'string', where);
      assert.ok(a.answer_text.includes(String(u.value)), where + ': число из ответа');
    });
    assert.equal(a.done, i === answers.length - 1, where);
    assert.equal(a.question === null, a.done, where);
    a.steps.forEach((s) => { assert.ok(['stt', 'draft', 'ask'].includes(s.name), where); assert.equal(typeof s.ms, 'number'); assert.equal(typeof s.rub, 'number'); });
  });
  // схема растёт: роль «кто-то» уточнена, возврат на доработку появился после ответа 3
  assert.equal(answers[0].process.roles.find((r) => r.id === 'r2').name, 'кто-то');
  assert.ok(!answers[1].process.steps.some((s) => s.id === 's5') && answers[2].process.steps.some((s) => s.id === 's5'));

  assert.deepEqual(Object.keys(done).sort(), ['answered', 'assumptions', 'improve', 'process', 'status', 'steps', 'totals', 'transcript'].sort());
  assert.equal(done.status, 'done');
  assert.deepEqual(done.transcript, qs.map((q, i) => ({ q: q.text, a: answers[i].answer_text })));
  checkProcess(done.process, said, 'отчёт');
  const keys = Object.keys(E.DEFAULTS).concat(['minutes', 'wait_hours']);
  Object.keys(done.assumptions).forEach((k) => assert.ok(keys.includes(k), 'ключ допущений ' + k));
  // числа в assumptions — ровно те, что код понял из ответов; «не знаю» (s6.wait_hours) оставляет допущение
  const heard = answers.flatMap((a) => a.understood);
  assert.deepEqual(E.applyAnswers(null, heard), E.assume(done.assumptions));
  heard.forEach((u) => assert.ok(done.answered.includes(u.field), u.field));
  assert.ok(!done.answered.includes('s6.wait_hours'));
  assert.equal(done.totals.ms, done.steps.reduce((a, s) => a + s.ms, 0));
  near(done.totals.rub, done.steps.reduce((a, s) => a + s.rub, 0), 0.005);
  const by = Object.fromEntries(done.process.steps.map((s) => [s.id, s]));
  assert.ok(done.improve.summary.length <= 140 && !/\d/.test(done.improve.summary));
  done.improve.changes.forEach((c) => {
    const ok = by[c.step].kind === 'task' ? ['keep', 'automate', 'remove', 'merge'] : by[c.step].kind === 'decision' ? ['keep', 'automate'] : ['keep'];
    assert.ok(ok.includes(c.action), c.step);
    ['how', 'risk'].forEach((k) => assert.ok(c[k].length <= 140 && !/\d/.test(c[k])));
  });

  const errors = fx('errors');
  Object.keys(errors).forEach((k) => assert.deepEqual(Object.keys(errors[k]).sort(), ['code', 'message']));
  assert.equal(errors.stt_failed.code, 'stt_failed');
});

/* Ручной счёт отчёта на interview-done: ответы — 120 заявок, 900 ₽/ч, смета s4 — 40 мин, перед сметой ждут 8 ч;
   остальное — допущения: задача 15 мин, решение 5, передача 4 ч, петля 0,2, 1 человек на роль, 120 ч в месяц, день 8 ч.

   Проходы. s5 «Хватает данных?»: «на доработку» → s3 → s4 → s5 — петля 0,2, «да» 0,8.
     v(s3) = v(s4) = v(s5) = 1 / 0,8 = 1,25;  v(s2) = v(s6) = v(s7) = 1;  s7 «Клиент согласен?» — две ветки вперёд: v(s8) = v(s10) = 0,5.
   Часы шага в месяц = 120 × v × минут / 60 = 2 × v × минут:
     s2 2·1·5 = 10 (минуты из ответа: цитата «минут 5»)   s3 2·1,25·15 = 37,5   s4 2·1,25·40 = 100 (из ответа)
     s5 2·1,25·5 = 12,5   s6 2·1·15 = 30   s7 2·1·5 = 10   s8 2·0,5·15 = 15   s10 2·0,5·15 = 15
   Было: 230 ч, × 900 = 207 000 ₽. Клиент (r4) внешний — его старт в часы не входит.
   Где теряется больше всего: s4 — 100 ч, 90 000 ₽; s3 — 37,5 ч, 33 750 ₽.
   Загрузка: Менеджер r1 = (10 + 37,5 + 30 + 10 + 15) / 120 = 0,854167 (> 0,85 — копится очередь); Инженер r2 = 112,5 / 120 = 0,9375.
   Первым автоматизировать — шаг из «automate», который один снимает больше часов людей (у «Системы» часов нет):
     s2 — 10 ч, s5 — 12,5 ч, s6 — 30 ч ⇒ s6: −30 ч, −27 000 ₽ в месяц.
   Стало (s2, s5, s6 — авто, s10 убран): s3 37,5 + s4 100 + s7 10 + s8 15 = 162,5 ч, 146 250 ₽.
   Дни. Было: работа Σ v·мин = 115 мин = 1,916667 ч. Ожидание: s1(Клиент)→s2 4; s3→s4 8 (ответ) ×1,25 = 10;
     s5→s6 4 ×1; s5→s3 4 ×0,25 = 1; s7→s8 4 ×0,5 = 2 — итого 21 ч. Очередь M/M/1 ρ/(1−ρ) × среднее время шага роли × Σv роли:
     r1 41/7 × (51,25 мин / 4,75 / 60) × 4,75 = 5,002976; r2 15 × 0,375 × 2,5 = 14,0625; r3 (1/7) × 0,25 × 0,5 = 0,017857.
     42 ч / 8 = 5,25 дня.
   С одной автоматизацией s6: работа 101,5 мин = 1,691667 ч; ожидание без передач в «Систему» и из неё: 17 ч;
     r1 — 72,5 ч, ρ = 0,604167, среднее 36,25 / 3,75 мин: 1,526316 × 0,161111 × 3,75 = 0,922149; r2, r3 — как было.
     (1,691667 + 17 + 0,922149 + 14,0625 + 0,017857) / 8 = 4,211772 дня. */
test('отчёт на фикстуре интервью совпадает с ручным счётом', () => {
  const d = fx('done');
  const r = E.report(d.process, d.improve, {}, d.assumptions, d.answered);
  near(r.before.hours, 230, 1e-6);
  near(r.before.rub, 207000, 1e-4);
  near(r.before.days, 5.25, 1e-6);
  assert.deepEqual(r.worst.map((w) => w.id), ['s4', 's3']);
  near(r.worst[0].hours, 100);
  near(r.worst[0].rub, 90000, 1e-4);
  near(r.worst[0].v, 1.25);
  assert.equal(r.worst[0].minutes, 40);
  near(r.worst[1].hours, 37.5);
  near(r.worst[1].rub, 33750, 1e-4);
  near(r.worst[1].rho, 102.5 / 120);
  assert.equal(r.worst[1].level, 'hot');
  assert.equal(r.first.id, 's6');
  near(r.first.hours, 30);
  near(r.first.rub, 27000, 1e-4);
  near(r.first.days, 4.211772, 1e-5);
  assert.equal(r.first.how, d.improve.changes.find((c) => c.step === 's6').how);
  near(r.after.hours, 162.5);
  near(r.after.rub, 146250, 1e-4);
  // откуда числа
  assert.equal(r.src.volume, 'answer');
  assert.equal(r.src.rate, 'answer');
  assert.deepEqual(r.src.people, { r1: 'assume', r2: 'answer', r3: 'assume' });
  assert.equal(r.src.minutes.s4, 'answer');
  assert.equal(r.src.minutes.s2, 'answer');
  assert.equal(r.src.minutes.s3, 'assume');
  assert.equal(r.src.wait.s4, 'answer');
  assert.equal(r.src.wait.s6, 'assume', '«не знаю» — остаётся допущение');
  // выбор посетителя в «Стало» тоже учитывается: без automate у s6 первой становится s5
  const r2 = E.report(d.process, d.improve, { s6: 'keep' }, d.assumptions, d.answered);
  assert.equal(r2.first.id, 's5');
  near(r2.first.hours, 12.5);
  // нет «automate» — первой автоматизации нет
  assert.equal(E.report(d.process, { changes: [] }, {}, d.assumptions, d.answered).first, null);
});

test('ответы переопределяют допущения E.assume()', () => {
  const A = E.applyAnswers({ rate: 1000 }, [
    { field: 'volume', value: 120, unit: 'заявок/мес' }, { field: 's4.minutes', value: 40, unit: 'мин' },
    { field: 's4.wait_hours', value: 8, unit: 'ч' }, { field: 'r2.people', value: 2, unit: 'чел.' },
    { field: 'rate', value: -5 }, { field: 'volume', value: 'много' }, { field: 'people', value: 3 }, { field: 's4.role', value: 1 },
  ]);
  assert.equal(A.perMonth, 120);
  assert.equal(A.rate, 1000, 'отрицательное и не число — не ответ');
  assert.deepEqual(A.minutes, { s4: 40 });
  assert.deepEqual(A.wait_hours, { s4: 8 });
  assert.deepEqual(A.people, { r2: 2 });
  assert.equal(A.taskMin, 15);
  // допущения с сервера чистятся: строки-числа, нули и мусор
  const B = E.assume({ perMonth: '150', dayH: 0, hoursPerPerson: -1, rate: null, loopShare: 0 });
  assert.deepEqual([B.perMonth, B.dayH, B.hoursPerPerson, B.rate, B.loopShare], [150, 8, 120, 700, 0]);
  // минуты и ожидание из ответов — поверх чисел модели и допущений
  const d = fx('done');
  const p = E.withAnswers(d.process, { minutes: { s2: 7, s4: 40 }, wait_hours: { s4: 8 } });
  const by = Object.fromEntries(p.steps.map((s) => [s.id, s]));
  assert.equal(by.s2.minutes, 7, 'ответ сильнее числа модели');
  assert.equal(by.s4.minutes, 40);
  assert.equal(by.s4.wait_hours, 8);
  assert.equal(d.process.steps.find((s) => s.id === 's4').minutes, null, 'исходный процесс не тронут');
  // тот же процесс без ответов: 100 заявок, 700 ₽, смета 15 мин, передача 4 ч
  const r0 = E.report(d.process, d.improve, {}, null, []);
  const r1 = E.report(d.process, d.improve, {}, d.assumptions, d.answered);
  near(r0.before.hours, (100 / 60) * (5 + 1.25 * 15 + 1.25 * 15 + 1.25 * 5 + 15 + 5 + 0.5 * 15 + 0.5 * 15));
  near(r0.before.rub, r0.before.hours * 700, 1e-4);
  assert.equal(r0.src.volume, 'assume');
  assert.equal(r0.src.minutes.s4, 'assume');
  near(r1.before.steps.s4.wait, 8);
  near(r0.before.steps.s4.wait, 4);
  assert.ok(r1.before.days > r0.before.days);
});

test('черновик посреди интервью: без конца, ветка в никуда, роль не из roles — счёт и раскладка не падают', () => {
  const p = { title: 'черновик', roles: [{ id: 'r1', name: 'Менеджер', external: false }], steps: [
    { id: 's1', kind: 'start', name: 'Заявка', role: 'r1', next: ['s2'] },
    { id: 's2', kind: 'task', name: 'Перенести', role: 'r9', next: ['s3'], minutes: null },
    { id: 's3', kind: 'decision', name: 'Есть данные?', role: 'r1', branches: [{ label: 'да', to: 's7' }] },
  ] };
  const r = E.report(p, null, {}, { perMonth: 120 }, ['volume']);
  assert.ok(isFinite(r.before.hours) && r.before.hours > 0);
  assert.ok(isFinite(r.before.days));
  assert.equal(r.first, null);
  const L = E.layout(E.withAnswers(p, r.A), { width: 360 });
  assert.equal(Object.keys(L.nodes).length, 3);
  assert.ok(E.toBpmn(p).includes('s3'));
});
