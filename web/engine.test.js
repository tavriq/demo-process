// Расчёт и «Стало»: node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const E = require('./engine.js');

const fx = (name) => JSON.parse(JSON.stringify(require('./fixtures/' + name + '.json')));
const near = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, a + ' ≠ ' + b);

/* Ручной счёт на фикстуре invoice, допущения по умолчанию:
   100 заявок, 700 ₽/ч, 1 человек на роль, 120 ч в месяц, задача 15 мин, решение 5 мин, передача 4 ч, петля 0,2, день 8 ч.

   Проходы. s4 «Счёт без ошибок?»: ветка «ошибка» → s5 → s2 → s3 → s4 возвращается к решению, это петля: 0,2; «да» → 0,8.
     v(s2) = 1 + v(s5) = 1 + 0,2·v(s2)  ⇒  v(s2) = v(s3) = v(s4) = 1,25;  v(s5) = 0,25;  v(s6) = v(s7) = v(s8) = 1.
   Минуты: s2 10 (текст), s3 15 (допущение), s4 5 (решение), s5 15 (текст), s6 5, s7 7, s8 15 (допущение).
   Бухгалтер r1:  1,25·10 + 0,25·15 + 1·7 + 1·15 = 38,25 мин/заявку → ×100/60 = 63,75 ч, ρ = 63,75/120 = 0,53125
   Руководитель r2: 1,25·15 + 1,25·5 = 25 мин → 41,667 ч, ρ = 0,34722
   Фин. директор r3: 1·5 = 5 мин → 8,333 ч, ρ = 0,06944
   Часы = 113,75 ч/мес; рубли = 113,75 · 700 = 79 625 ₽/мес. Узкое место — r1, но ρ < 0,85: «ok».
   Ожидание (на заявку): s3 — 6 ч из текста ×1,25 = 7,5; s4→s5 передача r2→r1: 4·0,25 = 1; s4→s6 r2→r3: 4·1 = 4;
     s6→s7 r3→r1: 4·1 = 4. Итого 16,5 ч.
   Очередь роли — M/M/1: ρ/(1−ρ) × среднее время шага роли.
     r1: среднее 38,25/3,5 мин = 0,182143 ч; 0,53125/0,46875 · 0,182143 = 0,206429 ч; ×Σv 3,5 = 0,7225 ч
     r2: среднее 25/2,5 = 10 мин = 0,166667 ч; 0,347222/0,652778 · 0,166667 = 0,0886525 ч; ×2,5 = 0,221631 ч
     r3: 5 мин = 0,083333 ч; 0,069444/0,930556 · 0,083333 = 0,0062189 ч; ×1 = 0,0062189 ч
   Работа 68,25 мин = 1,1375 ч. Всего 1,1375 + 16,5 + 0,950350 = 18,587850 ч → / 8 = 2,323481 дня. */
test('расчёт на фикстуре invoice совпадает с ручным счётом', () => {
  const r = E.calc(fx('invoice').process);
  near(r.visits.s2, 1.25);
  near(r.visits.s5, 0.25);
  near(r.visits.s6, 1);
  near(r.hours, 113.75);
  near(r.rub, 79625, 1e-4);
  near(r.roles.r1.rho, 0.53125);
  near(r.roles.r2.rho, 41.666666667 / 120);
  near(r.roles.r3.rho, 8.333333333 / 120);
  assert.equal(r.bottleneck, 'r1');
  assert.equal(r.roles.r1.level, 'ok');
  near(r.steps.s3.wait, 6);
  assert.equal(r.steps.s3.waitSrc, 'text');
  near(r.steps.s6.wait, 4);
  assert.equal(r.steps.s6.waitSrc, 'handoff');
  near(r.days, 2.323481, 1e-5);
});

/* Внешняя роль: тот же invoice, но «Руководитель отдела» r2 — вне компании.
   Его 25 мин на заявку уходят из часов и рублей: 113,75 − 41,667 = 72,083 ч, 72,083 · 700 = 50 458,33 ₽.
   В днях его минуты остаются (как ожидание), пропадает только его очередь 0,221631 ч:
   (18,587850 − 0,221631) / 8 = 2,295777 дня. */
test('внешняя роль: не в часах, рублях, загрузке и очереди, но в днях', () => {
  const p = fx('invoice').process;
  p.roles.find((r) => r.id === 'r2').external = true;
  const r = E.calc(p);
  near(r.hours, 113.75 - 25 * 100 / 60);
  near(r.rub, (113.75 - 25 * 100 / 60) * 700, 1e-4);
  assert.ok(!r.roles.r2);
  assert.equal(r.steps.s3.ext, true);
  assert.equal(r.steps.s3.queueN, 0);
  near(r.days, 2.295777, 1e-5);
  // нет поля — false: исходный invoice считает r2 как сотрудника
  assert.ok(E.calc(fx('invoice').process).roles.r2);
  // у шагов внешней роли в «Стало» — только keep
  const acts = E.actions(p, { changes: [{ step: 's3', action: 'automate' }, { step: 's2', action: 'automate' }] });
  assert.equal(acts.s3.action, 'keep');
  assert.equal(acts.s2.action, 'automate');
  const a = E.after(p, null, { s3: 'remove' });
  assert.ok(a.steps.some((s) => s.id === 's3'));
});

test('допущения меняют счёт: два бухгалтера, другая ставка', () => {
  const p = fx('invoice').process;
  const r = E.calc(p, { people: { r1: 2 }, rate: 1000, perMonth: 50 });
  near(r.hours, 113.75 / 2);
  near(r.rub, 56875, 1e-4);
  near(r.roles.r1.rho, 0.53125 / 4);
});

test('петля: ветка назад получает 0,2, проходы растут', () => {
  const p = fx('sales').process;
  const es = E.edges(p).filter((e) => e.from === 's5');
  assert.deepEqual(es.map((e) => [e.to, e.p, e.loop]), [['s6', 0.8, false], ['s3', 0.2, true]]);
  const v = E.visits(p);
  near(v.s3, 1.25);
  near(v.s4, 1.25);
  near(v.s6, 1);
  near(v.s9, 0.5);
  near(v.s13, 0.5);
  near(v.s12 + v.s14, 1);
  // своя доля петли
  near(E.visits(p, { loopShare: 0.5 }).s3, 2);
});

test('решение с тремя ветками делит заявки поровну', () => {
  const p = fx('returns').process;
  const es = E.edges(p).filter((e) => e.from === 's3');
  assert.equal(es.length, 3);
  es.forEach((e) => near(e.p, 1 / 3));
  const r = E.calc(p);
  ['s4', 's5', 's6'].forEach((id) => near(r.visits[id], 1 / 3));
  near(r.visits.s7, 1);
  // s7 ждёт передачу только с веток других ролей: склад (r2) и оператор (r1) → бухгалтер (r3), все три ветки — 4 ч
  near(r.steps.s7.wait, 4);
});

test('узкое место и перегрузка: ρ > 0,85 — hot, ρ ≥ 1 — очередь растёт, больше 20 дней', () => {
  const s = E.calc(fx('sales').process);
  assert.equal(s.bottleneck, 'r2');
  assert.equal(s.roles.r2.level, 'hot');
  assert.ok(s.steps.s4.queueN > 1 && isFinite(s.days));
  const o = E.calc(fx('onboarding').process);
  assert.equal(o.roles.r2.level, 'over');
  assert.equal(o.over, true);
  assert.equal(o.days, Infinity);
  assert.equal(o.steps.s2.queueN, Infinity);
  assert.ok(isFinite(E.calc(fx('onboarding').process, { people: { r2: 3 } }).days));
});

test('«Стало»: automate, remove и merge', () => {
  const f = fx('onboarding');
  const a = E.after(f.process, f.improve);
  const by = Object.fromEntries(a.steps.map((s) => [s.id, s]));
  // merge: s4 уходит, его 15 минут (допущение) прибавляются к s3 (15, допущение) — 30, а s3 ещё и «авто»
  assert.ok(!by.s4);
  assert.equal(by.s3.minutes, 30);
  assert.deepEqual(by.s3.merged, ['s4']);
  assert.equal(by.s3.auto, true);
  assert.deepEqual(by.s3.next, ['s5']);
  near(E.minutesOf(by.s3, E.assume()), 3);
  // remove: s10 уходит, ветка «да» решения s9 ведёт сразу на конец
  assert.ok(!by.s10);
  assert.deepEqual(by.s9.branches.map((b) => b.to), ['s11', 's8']);
  assert.deepEqual(a.removed.sort(), ['s10', 's4']);
  assert.ok(a.roles.some((r) => r.id === E.SYS && r.name === 'Система'));
  // HR после: только s2 (40 мин) → 66,67 ч; сисадмин 25 ч, завхоз 25 ч, директор 8,33 ч = 125 ч
  const r = E.calc(a);
  near(r.roles.r2.hours, 40 * 100 / 60);
  near(r.hours, 125);
  assert.ok(!r.roles[E.SYS], 'система не грузит людей');
  assert.ok(isFinite(r.days));
  // исходный процесс не тронут
  assert.equal(f.process.steps.find((s) => s.id === 's3').minutes, null);
});

test('«Стало» на invoice: ручной пересчёт', () => {
  const f = fx('invoice');
  const r = E.calc(E.after(f.process, f.improve));
  // люди: s5 0,25·15 = 3,75 + s3 18,75 + s4 6,25 + s6 5 = 33,75 мин → 56,25 ч
  near(r.hours, 56.25);
  // передача в «Систему» и из неё — без ожидания: s6 → s7 (авто) не ждёт
  near(r.steps.s7.wait, 0);
  assert.ok(r.days < E.calc(f.process).days);
});

test('выбор посетителя поверх совета и правила SPEC', () => {
  const f = fx('invoice');
  const a = E.after(f.process, f.improve, { s2: 'keep', s3: 'remove', s1: 'remove', s4: 'remove', s9: 'automate' });
  const by = Object.fromEntries(a.steps.map((s) => [s.id, s]));
  assert.ok(!by.s2.auto);
  assert.ok(!by.s3);
  assert.deepEqual(by.s2.next, ['s4']);
  assert.ok(by.s1 && by.s4 && by.s9, 'старт, решение и конец не убираются');
  assert.ok(!by.s9.auto);
  const acts = E.actions(f.process, f.improve, { s5: 'merge' });
  assert.equal(acts.s5.action, 'keep', 'merge без merge_into — keep');
});
