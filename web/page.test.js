// Окно v2 в DOM-заглушке (fakedom.js): #report=, интервью целиком, голос и его отсутствие. node --test web/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const E = require('./engine.js');
const { page, until } = require('./fakedom.js');

const fx = (n) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', n + '.json'), 'utf8'));
const sp = (s) => String(s).replace(/[\s  ]+/g, ' ').trim();
const n0 = (v) => sp(new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 }).format(Math.round(v * 1e9) / 1e9));

/* Сервер по контракту API на фикстурах interview-*.json; log — запросы, sttFail — сколько записей «не расслышать». */
function server(o) {
  o = Object.assign({ sttFail: 0 }, o);
  const start = fx('interview-start');
  const answers = fx('interview-answers');
  const errors = fx('interview-errors');
  const log = [];
  const bodies = [];
  let n = 0;
  const res = (status, body) => new Response(body == null ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const fetch = async (url, init) => {
    const method = (init && init.method) || 'GET';
    log.push(method + ' ' + url);
    if (url.startsWith('fixtures/')) return res(200, fx(url.slice(9, -5)));
    const p = url.replace('/api/process/', '');
    if (p === 'status') return res(200, { live: true, budget_rub: 15, spent_rub: 3 });
    if (p === 'interviews' && method === 'POST') { bodies.push(JSON.parse(init.body)); return res(201, start); }
    if (p === 'interviews/' + start.id + '/answers') {
      bodies.push(init.body);
      if (init.body instanceof FormData && o.sttFail-- > 0) return res(422, errors.stt_failed);
      return res(200, answers[n++]);
    }
    if (p === 'interviews/' + start.id + '/finish') return res(202, null);
    if (p === 'interviews/' + start.id) return res(200, fx('interview-done'));
    return res(404, errors.not_found);
  };
  return { fetch, log, bodies };
}

const say = async (P, text) => {
  const n = P.api.iv.count;
  P.$('.pr-iv textarea').value = text;
  P.$('.pr-iv .pr-row .pr-btn').click();
  await until(() => P.api.iv.count > n || !P.$('.pr-iv .pr-err').hidden || P.api.state.rep);
};

test('#report=<id> открывает отчёт без интервью: числа из engine.js, рядом — откуда они', async () => {
  const srv = server();
  let copied = null;
  const P = page({ hash: '#report=dev-sales', fetch: srv.fetch, navigator: { clipboard: { writeText: async (t) => { copied = t; } } } });
  await until(() => !P.$('.pr-rep').hidden);
  assert.ok(srv.log.includes('GET /api/process/interviews/dev-sales'));
  assert.ok(!srv.log.some((l) => l.startsWith('POST') || l.includes('fixtures/')), 'без интервью и без примера: ' + srv.log);
  const d = fx('interview-done');
  const R = E.report(d.process, d.improve, {}, d.assumptions, d.answered);
  assert.equal(P.$('.pr-h').textContent, d.process.title);
  assert.equal(P.$$('.pr-node').length, d.process.steps.length);
  const cmp = P.$('.pr-cmp').children.map((c) => sp(c.textContent));
  assert.deepEqual(cmp.slice(3, 9), ['ч работы в месяц', n0(R.before.hours), n0(R.after.hours), '₽ в месяц', n0(R.before.rub), n0(R.after.rub)]);
  assert.equal(sp(P.$('.pr-num').textContent), n0(R.before.hours), 'счётчик под схемой — тот же расчёт');
  const worst = P.$$('.pr-worst li').map((li) => sp(li.textContent));
  assert.equal(worst.length, 2);
  assert.ok(worst[0].startsWith('Посчитать смету' + n0(100) + ' ч · ' + n0(90000) + ' ₽ в месяц'), worst[0]);
  assert.match(worst[0], /40 мин за проход из ответа/);
  assert.match(worst[1], /15 мин за проход допущение/);
  const rep = sp(P.$('.pr-rep').textContent);
  assert.ok(rep.includes('Собрать КП и отправить клиенту' + '−' + n0(30) + ' ч и −' + n0(27000) + ' ₽ в месяц'), rep);
  assert.ok(rep.includes('заявок в месяц120 из ответа') && rep.includes('ставка часа900 ₽ из ответа'), rep);
  assert.ok(rep.includes('Менеджер — 1 допущение') && rep.includes('Инженер — 1 из ответа'), rep);
  // ссылка — фрагмент без ?r= (метки каналов не копируются), mailto с адресом сайта и ссылкой
  assert.equal(P.win.location.hash, '#report=dev-sales');
  P.$$('.pr-rep button').find((b) => b.textContent === 'Ссылка на отчёт').click();
  await until(() => copied);
  assert.equal(copied, 'https://site.test/process/#report=dev-sales');
  const mail = P.$('.pr-rep a').getAttribute('href');
  assert.ok(mail.startsWith('mailto:hello@example.com?subject=') && mail.includes(encodeURIComponent('Отчёт: https://site.test/process/#report=dev-sales')), mail);
  // Было/Стало доступно, пример по кнопке уводит из отчёта и чистит фрагмент
  assert.ok(!P.$('.pr-seg button[aria-pressed="false"]').disabled);
  P.$('.pr-ex button').click();
  await until(() => P.api.state.name === 'sales');
  assert.ok(P.$('.pr-rep').hidden);
  assert.equal(P.win.location.hash, '');
  assert.equal(P.api.state.A.perMonth, 100, 'у примера — допущения по умолчанию');
});

test('#report= с неизвестным id: пример и понятное сообщение', async () => {
  const srv = server();
  const P = page({ hash: '#report=old-123456', fetch: srv.fetch });
  await until(() => P.api.state.name === 'sales' && !P.$('.pr-err').hidden);
  assert.match(P.$('.pr-err').textContent, /не найден/);
  assert.ok(P.$('.pr-rep').hidden);
});

test('интервью целиком текстом: вопрос, «записал», схема растёт, «Хватит» после трёх, отчёт по ссылке', async () => {
  const srv = server();
  const P = page({ fetch: srv.fetch });
  await until(() => P.api.state.name === 'sales');
  const q = () => P.$('.pr-ask').textContent;
  assert.equal(q(), 'Какой процесс разберём?');
  assert.deepEqual(P.$$('.pr-topics button').map((b) => b.textContent), ['Заявки', 'Счета', 'Возвраты', 'Выход сотрудника']);
  P.$('.pr-topics button').click();
  await until(() => P.api.iv.id);
  assert.deepEqual(srv.bodies[0], { topic: 'заявки', text: null });
  const answers = fx('interview-answers');
  assert.equal(q(), fx('interview-start').question.text);
  assert.equal(P.$('.pr-iv .pr-row .pr-small').textContent, 'вопрос 1 из 8');
  assert.equal(P.$$('.pr-node').length, 0, 'схемы ещё нет');
  assert.ok(P.root.children.indexOf(P.$('.pr-iv')) < P.root.children.indexOf(P.$('.pr-body')), 'интервью — над схемой');
  const enough = () => P.$('.pr-left .pr-btn');
  for (let i = 0; i < 3; i++) {
    assert.ok(enough().disabled, '«Хватит» до трёх ответов недоступна');
    await say(P, answers[i].answer_text);
    assert.equal(q(), answers[i].question.text);
    assert.equal(P.$$('.pr-node').length, answers[i].process.steps.length, 'схема после ответа ' + (i + 1));
  }
  assert.ok(!P.$('.pr-seg button[aria-pressed="false"]').hidden && P.$('.pr-seg button[aria-pressed="false"]').disabled, 'только «Было»');
  assert.deepEqual(JSON.parse(srv.bodies[1]), { text: answers[0].answer_text });
  // после ответа 2 — «записал: 120 заявок в месяц», и счётчики уже считают по 120 заявкам
  assert.ok(!enough().disabled);
  const got = (i) => sp(answers[i].answer_text);
  await say(P, answers[3].answer_text);
  assert.equal(sp(P.$('.pr-got b').textContent), 'записал: 900 ₽ в час');
  assert.equal(sp(P.$('.pr-got q').textContent), got(3));
  await say(P, answers[4].answer_text);
  assert.equal(sp(P.$('.pr-got b').textContent), 'записал: «Посчитать смету» — 40 мин');
  const A = E.applyAnswers(null, answers.slice(0, 5).flatMap((a) => a.understood));
  assert.equal(sp(P.$('.pr-num').textContent), n0(E.calc(E.withAnswers(answers[4].process, A), A).hours));
  await say(P, answers[5].answer_text);
  assert.equal(sp(P.$('.pr-got b').textContent), 'записал: ожидание перед «Посчитать смету» — 8 ч');
  await say(P, 'не знаю');
  assert.equal(sp(P.$('.pr-got b').textContent), 'понял: не знаете — остаётся допущение');
  await say(P, answers[7].answer_text);
  // последний ответ: done → finish → GET → отчёт, блок интервью — снова внизу и с начала
  await until(() => P.api.state.rep === 'dev-sales');
  assert.ok(srv.log.includes('POST /api/process/interviews/dev-sales/finish'));
  assert.ok(!P.$('.pr-rep').hidden);
  assert.equal(P.win.location.hash, '#report=dev-sales');
  assert.equal(q(), 'Какой процесс разберём?');
  assert.ok(P.root.children.indexOf(P.$('.pr-iv')) > P.root.children.indexOf(P.$('.pr-rep')));
  assert.equal(srv.log.filter((l) => l.endsWith('/answers')).length, 8);
});

test('«Хватит — строй отчёт» после трёх ответов; ошибка сборки — вопрос остаётся', async () => {
  const srv = server();
  const fail = { on: true };
  const fetch = async (url, init) => (fail.on && url.endsWith('/dev-sales') ? new Response(JSON.stringify({ status: 'error', error: { code: 'model_format' } }), { status: 200 }) : srv.fetch(url, init));
  const P = page({ fetch });
  await until(() => P.api.state.name === 'sales');
  P.$('.pr-iv textarea').value = 'склад: приёмка поставки';
  P.$('.pr-iv .pr-row .pr-btn').click();
  await until(() => P.api.iv.id);
  assert.deepEqual(srv.bodies[0], { topic: null, text: 'склад: приёмка поставки' });
  const answers = fx('interview-answers');
  for (let i = 0; i < 3; i++) await say(P, answers[i].answer_text);
  P.$('.pr-left .pr-btn').click();
  await until(() => !P.$('.pr-iv .pr-err').hidden);
  assert.match(P.$('.pr-iv .pr-err').textContent, /не по формату/);
  assert.equal(P.$('.pr-ask').textContent, answers[2].question.text);
  assert.ok(!P.$('.pr-left .pr-btn').disabled);
  fail.on = false;
  P.$('.pr-left .pr-btn').click();
  await until(() => P.api.state.rep === 'dev-sales');
  assert.equal(srv.log.filter((l) => l.endsWith('/finish')).length, 2);
});

/* MediaRecorder и микрофон — заглушки: запись даёт байты при stop(). */
function media(o) {
  const tracks = [];
  class Rec {
    constructor(stream, opt) { this.mimeType = (opt && opt.mimeType) || 'audio/webm'; this.state = 'inactive'; Rec.last = this; }
    static isTypeSupported(t) { return t === 'audio/ogg;codecs=opus'; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.ondataavailable({ data: new Blob([new Uint8Array(o.bytes || 4000)]) }); setTimeout(() => this.onstop(), 0); }
  }
  const getUserMedia = async () => { if (o.deny) throw new Error('NotAllowedError'); const t = { stopped: false, stop() { this.stopped = true; } }; tracks.push(t); return { getTracks: () => [t] }; };
  return { MediaRecorder: Rec, navigator: { mediaDevices: { getUserMedia } }, tracks };
}

test('голос: тап — запись, тап — отправка аудио; по тишине не отправляет; stt_failed не тратит вопрос', async () => {
  const srv = server({ sttFail: 1 });
  const m = media({});
  const P = page({ fetch: srv.fetch, navigator: m.navigator, MediaRecorder: m.MediaRecorder });
  await until(() => P.api.state.name === 'sales');
  P.$('.pr-topics button').click();
  await until(() => P.api.iv.id);
  const mic = P.$('.pr-mic');
  assert.ok(!mic.hidden);
  mic.click();
  await until(() => !P.$('.pr-recbox').hidden);
  assert.equal(mic.getAttribute('aria-pressed'), 'true');
  assert.ok(P.$('.pr-iv textarea').hidden);
  await new Promise((r) => setTimeout(r, 60));
  assert.ok(!srv.log.some((l) => l.endsWith('/answers')), 'без второго тапа ничего не уходит');
  mic.click();
  await until(() => !P.$('.pr-iv .pr-err').hidden);
  const fd = srv.bodies[1];
  assert.ok(fd instanceof FormData);
  assert.equal(fd.get('audio').name, 'answer.ogg');
  assert.equal(fd.get('audio').size, 4000);
  assert.ok(m.tracks.every((t) => t.stopped), 'микрофон отпущен');
  assert.match(P.$('.pr-iv .pr-err').textContent, /расслыш/);
  assert.equal(P.api.iv.count, 0, 'вопрос не потрачен');
  assert.equal(P.$('.pr-ask').textContent, fx('interview-start').question.text);
  mic.click();
  await until(() => !P.$('.pr-recbox').hidden);
  mic.click();
  await until(() => P.api.iv.count === 1);
  assert.equal(P.$('.pr-ask').textContent, fx('interview-answers')[0].question.text);
  assert.ok(!P.$('.pr-iv textarea').hidden);
});

test('нет микрофона или нет доступа — остаётся поле текста', async () => {
  const P1 = page({ fetch: server().fetch });
  P1.$('.pr-topics button').click();
  await until(() => P1.api.iv.id);
  assert.ok(P1.$('.pr-mic').hidden, 'без mediaDevices кнопки нет');
  assert.ok(!P1.$('.pr-iv textarea').hidden);
  const m = media({ deny: true });
  const P2 = page({ fetch: server().fetch, navigator: m.navigator, MediaRecorder: m.MediaRecorder });
  P2.$('.pr-topics button').click();
  await until(() => P2.api.iv.id);
  P2.$('.pr-mic').click();
  await until(() => P2.$('.pr-mic').hidden);
  assert.match(P2.$('.pr-iv .pr-err').textContent, /текстом/);
  assert.ok(!P2.$('.pr-iv textarea').hidden && !P2.$('.pr-iv textarea').disabled);
  await say(P2, 'Заявки приходят на почту');
  assert.equal(P2.api.iv.count, 1);
});
