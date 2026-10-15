"""The interview on the fake gateway (docs/SPEC-analyst.md, «Готово, когда», 1): start, a text answer,
an audio answer (fake STT), the gap by priority, «не знаю», the limit of 8, «Хватит», the final, the
budget before the call, stt_failed, the report by its link, deletion after 30 days."""

import sqlite3
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import gaps, prompts
from app.config import Settings
from app.interview import Interviews, InterviewError, carry_answers
from app.interview_api import audio_part, parse_multipart
from app.main import create_app
from app.pipeline import Deps
from app.pricing import call_cost_rub, interview_step_worst_rub, interview_worst_rub, stt_worst_rub
from app.store import MSK, CapStore, Store
from tests.fakes import FakeClock, FakeGateway, GatewayError, Heard, Reply, dumps, fixture

URL = "/api/process/interviews"
F = fixture("interview")
FIRST = F["questions"]["start_end"].format(name="заявка клиента")
START_KEYS = {"id", "question", "process", "budget_left_rub"}
TURN_KEYS = {"answer_text", "understood", "process", "flags", "question", "done", "steps"}
GET_KEYS = {"status", "transcript", "process", "improve", "assumptions", "answered", "steps", "totals", "error"}


def q(text="Сколько времени это занимает?"):
    return dumps({"text": text})


def full_script():
    """The model's replies for the fixture's interview: the first question, then a draft (none after «не
    знаю») and a question per answer (none after the 8th), then the final."""
    replies = [q(FIRST)]
    for i, draft in enumerate(F["drafts"]):
        if draft is not None:
            replies.append(dumps(draft))
        if i < 7:
            replies.append(q())
    return replies + [dumps(F["extract"]), dumps(F["improve"])]


@pytest.fixture
def settings(tmp_path):
    return replace(Settings.from_env({}), api_key="k", base_url="http://gw.example", var_dir=tmp_path / "var",
                   recorded_dir=tmp_path / "recorded")


def make(settings, replies=(), stt=(), **overrides):
    gw = FakeGateway(replies, stt=stt)
    return TestClient(create_app(replace(settings, **overrides), client=gw)), gw


def start(client, **body):
    r = client.post(URL, json=body or {"topic": "заявки"})
    assert r.status_code == 201, r.text
    return r.json()


def say(client, interview_id, text):
    return client.post(f"{URL}/{interview_id}/answers", json={"text": text})


def speak(client, interview_id, audio=b"\x1aE\xdf\xa3 webm \r\n--x\r\n" * 200, content_type="audio/webm",
          name="audio", filename="answer.webm"):
    return client.post(f"{URL}/{interview_id}/answers", files={name: (filename, audio, content_type)})


def wait_built(client, interview_id, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = client.get(f"{URL}/{interview_id}").json()
        if state["status"] != "building":
            return state
        time.sleep(0.01)
    raise AssertionError("the final did not finish")


def store_of(client) -> Store:
    return client.app.state.store


# ---------- start ----------

def test_start(settings):
    client, gw = make(settings, [q(FIRST)])
    body = start(client)
    assert set(body) == START_KEYS and body["process"] is None
    assert body["question"] == {"n": 1, "max": 8, "text": FIRST, "gap": "start_end"}
    assert gw.steps() == ["ask"]
    ask = gw.calls[0]
    assert ask["model"] == "gemini/gemini-3.1-flash-lite" and ask["max_tokens"] == 300 and ask["timeout_s"] == 15
    assert "заявка клиента" in gw.user(0) and prompts.ANSWERS_NOTE in gw.user(0)
    # the whole worst case is held at once: the rest of the day is what the page shows
    worst = interview_worst_rub(settings)
    assert body["budget_left_rub"] == round(store_of(client).left_today(), 2)
    assert body["budget_left_rub"] == pytest.approx(15 - worst, abs=interview_step_worst_rub(settings, "ask"))
    status = client.get("/api/process/status").json()
    assert status["interviews_running"] == 1 and status["interview_worst_rub"] == round(worst, 2)


def test_start_with_a_phrase_masks_contacts(settings):
    client, gw = make(settings, [q()])
    start(client, text="Ремонт техники, пишите на boss@corp.example или 8 912 345 67 89")
    assert "boss@corp.example" not in gw.user(0) and "[email скрыт]" in gw.user(0) and "[телефон скрыт]" in gw.user(0)
    assert "boss@corp.example" not in "\n".join(str(r) for r in sqlite3.connect(
        settings.var_dir / "process.sqlite3").execute("SELECT state FROM interviews"))


@pytest.mark.parametrize("body", [{}, {"topic": "другое"}, {"topic": ["заявки"]}, {"text": ""}, {"text": "  "},
                                  {"text": "а" * 501},
                                  {"topic": None, "text": None}, {"topic": "заявки", "extra": 1}, {"text": 5}, [1]])
def test_start_bad_request(settings, body):
    client, gw = make(settings)
    r = client.post(URL, json=body)
    assert r.status_code == 422 and r.json()["code"] == "bad_request" and gw.calls == []


def test_start_offline_without_key(settings):
    client = TestClient(create_app(replace(settings, api_key="")))
    r = client.post(URL, json={"topic": "счета"})
    assert r.status_code == 503 and r.json()["code"] == "offline"


def test_start_budget_refused_before_the_call(settings):
    client, gw = make(settings, [q()])
    store_of(client).reserve(15 - interview_worst_rub(settings) / 2)
    r = client.post(URL, json={"topic": "заявки"})
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted" and gw.calls == []
    assert client.get("/api/process/status").json()["interviews_running"] == 0


def test_busy_and_rate_limited(settings):
    client, _ = make(settings, [q()] * 3, daily_budget_rub=100)
    start(client), start(client)
    spent = store_of(client).spent_today()
    r = client.post(URL, json={"topic": "заявки"})
    assert r.status_code == 429 and r.json()["code"] == "busy" and r.json()["retry_after_s"] > 0
    assert store_of(client).spent_today() == spent  # a busy refusal holds nothing
    client, _ = make(settings, [q()] * 4, daily_budget_rub=100, max_concurrent_interviews=10)
    for _ in range(3):
        start(client)
    r = client.post(URL, json={"topic": "заявки"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and "3 интервью в час" in r.json()["message"]


# ---------- a turn ----------

def test_text_answer(settings):
    client, gw = make(settings, [q(FIRST), dumps(F["drafts"][0]), q()])
    interview_id = start(client)["id"]
    r = say(client, interview_id, F["answers"][0])
    assert r.status_code == 200
    body = r.json()
    assert set(body) == TURN_KEYS and body["done"] is False and body["answer_text"] == F["answers"][0]
    assert body["understood"] == [] and body["flags"] == {"dont_know": False, "draft": "ok", "question_template": False,
                                                         "question_digits": 0, "contacts_masked": 0}
    assert body["question"] == {"n": 2, "max": 8, "text": "Сколько времени это занимает?", "gap": "volume"}
    assert [s["name"] for s in body["steps"]] == ["draft", "ask"] and set(body["steps"][0]) == {"name", "ms", "rub"}
    process = body["process"]
    assert process["flags"] == {"unquoted": [], "numbers_dropped": []}  # quotes from the answer are found
    assert [s["id"] for s in process["steps"]] == ["s1", "s2", "s3", "s5", "s6", "s7", "s9", "s10", "s11"]
    draft_call = gw.calls[1]
    assert draft_call["model"] == "gemini/gemini-3.1-flash-lite" and draft_call["max_tokens"] == 2000
    assert "Вопрос 1: " + FIRST in gw.user(1) and "Ответ: Заявка приходит" in gw.user(1)
    assert "<схема>\nПока пусто.\n</схема>" in gw.user(1)
    # the next draft sees this one, to keep its ids
    state = client.get(f"{URL}/{interview_id}").json()
    assert set(state) == GET_KEYS and state["status"] == "asking"
    assert state["transcript"] == [{"q": FIRST, "a": F["answers"][0]}]


def test_the_whole_interview_to_the_report(settings):
    client, gw = make(settings, full_script())
    interview_id = start(client)["id"]
    gaps_asked, bodies = ["start_end"], []
    for answer in F["answers"]:
        r = say(client, interview_id, answer)
        assert r.status_code == 200, r.text
        bodies.append(r.json())
        if r.json()["question"]:
            gaps_asked.append(r.json()["question"]["gap"])
    # the code's choice, by priority: start/end, volume, branches of s7, two handoffs between roles (what is
    # between the steps), rate, then minutes by share of hours
    assert gaps_asked == ["start_end", "volume", "s7.branches", "between.1", "between.2", "rate", "s3.minutes",
                          "s6.minutes"]
    asks = [gw.user(i) for i, step in enumerate(gw.steps()) if step == "ask" and "между шагами" in gw.user(i)]
    assert len(asks) == 2 and "«Собрать и отправить КП» и «Что ответил клиент?»" in asks[0]
    assert "«Уточнить объём у клиента» и «Посчитать смету»" in asks[1]
    assert [b["question"]["n"] for b in bodies[:-1]] == [2, 3, 4, 5, 6, 7, 8]
    assert bodies[-1]["done"] is True and bodies[-1]["question"] is None
    assert bodies[1]["understood"] == [{"field": "volume", "value": 120, "unit": "заявок/мес"}]
    # the answers about what is between: no number, the draft gets the new steps between the two
    assert bodies[3]["understood"] == [] and bodies[3]["flags"]["draft"] == "ok"
    links = {s["id"]: s.get("next") or [b["to"] for b in s.get("branches") or []]
             for s in bodies[4]["process"]["steps"]}
    assert (links["s6"], links["s13"], links["s3"], links["s4"]) == (["s13"], ["s7"], ["s4"], ["s5", "s3"])
    assert bodies[5]["understood"] == [{"field": "rate", "value": 417, "unit": "₽/ч"}]  # 70 000 a month / 168
    assert bodies[6]["flags"]["dont_know"] is True and bodies[6]["flags"]["draft"] == "skipped"
    # the draft model's number that holds up: the waiting before s5
    assert bodies[7]["understood"] == [{"field": "s6.minutes", "value": 30, "unit": "мин"},
                                       {"field": "s5.wait_hours", "value": 16, "unit": "ч"}]

    state = wait_built(client, interview_id)
    assert gw.steps() == ["ask"] + ["draft", "ask"] * 6 + ["ask", "draft", "extract", "improve"]
    assert set(state) == GET_KEYS and state["status"] == "done" and state["error"] is None
    assert len(state["transcript"]) == 8 and all(set(t) == {"q", "a"} for t in state["transcript"])
    assert state["process"]["flags"] == {"unquoted": [], "numbers_dropped": []}
    assert "Позвонить клиенту по КП" in [s["name"] for s in state["process"]["steps"]]
    assert state["improve"]["summary"] and [c["step"] for c in state["improve"]["changes"]] == ["s2", "s4", "s9", "s10"]
    assert state["assumptions"] == {"perMonth": 120, "rate": 417, "people": {}, "minutes": {"s6": 30},
                                    "wait_hours": {"s5": 16}}
    assert state["answered"] == ["volume", "rate", "s6.minutes", "s5.wait_hours"]
    names = [s["name"] for s in state["steps"]]
    assert names[-2:] == ["extract", "improve"] and state["steps"][-1]["n"] is None
    assert state["totals"]["rub"] == pytest.approx(sum(s["rub"] for s in state["steps"]), abs=1e-3)
    # the hold is gone: what the day spent is what the interview spent
    assert store_of(client).spent_today() == pytest.approx(state["totals"]["rub"], abs=1e-3)
    assert client.get("/api/process/status").json()["interviews_running"] == 0
    # the 9th answer and a second «Хватит»
    assert say(client, interview_id, "ещё").status_code == 409
    r = client.post(f"{URL}/{interview_id}/finish")
    assert r.status_code == 202 and r.json() == {"status": "done"}

    # the report by its link: another process, no model calls
    again, gw2 = make(settings)
    r = again.get(f"{URL}/{interview_id}")
    assert r.status_code == 200 and r.json() == state and r.headers["cache-control"] == "no-store"
    assert gw2.calls == [] and gw2.heard == []


def test_dont_know_closes_the_gap_and_keeps_the_assumption(settings):
    client, gw = make(settings, [q(), q(), q()])
    interview_id = start(client)["id"]
    body = say(client, interview_id, "Не знаю, всё по-разному").json()
    assert body["flags"]["dont_know"] is True and body["flags"]["draft"] == "skipped"
    assert body["question"]["gap"] == "volume" and body["process"] is None  # start_end is not asked twice
    body = say(client, interview_id, "Не знаю, никто не считал").json()
    assert body["understood"] == [] and body["question"]["gap"] == "rate"
    state = client.get(f"{URL}/{interview_id}").json()
    assert "perMonth" not in state["assumptions"] and state["answered"] == []
    assert gw.steps() == ["ask", "ask", "ask"]


BETWEEN_Q = "Что происходит между «Собрать и отправить КП» и ответом клиента — нет ли там ещё шагов?"
CALL = F["answers"][3]  # «Через пару дней менеджер звонит клиенту…»: a step between s6 and s7


def to_between(settings, *replies):
    """An interview at its first «between» question: the first answer tells the whole process and the
    volume, so the draft has all its branches."""
    client, gw = make(settings, [q(FIRST), dumps(F["drafts"][2]), q(BETWEEN_Q), *replies])
    interview_id = start(client)["id"]
    body = say(client, interview_id, F["answers"][0] + " " + F["answers"][1]).json()
    assert body["understood"] == [{"field": "volume", "value": 120, "unit": "заявок/мес"}]
    assert body["question"] == {"n": 2, "max": 8, "text": BETWEEN_Q, "gap": "between.1"}
    assert "«Собрать и отправить КП» и «Что ответил клиент?»" in gw.user(2)
    return client, gw, interview_id


def test_nothing_between_closes_the_gap_without_a_draft(settings):
    client, gw, interview_id = to_between(settings, q(), dumps(F["drafts"][2]), q())
    body = say(client, interview_id, "Ничего не происходит, КП сразу уходит клиенту.").json()
    assert body["understood"] == [] and body["flags"]["dont_know"] is False and body["flags"]["draft"] == "skipped"
    assert body["question"]["gap"] == "between.2" and gw.steps() == ["ask", "draft", "ask", "ask"]
    assert "«Уточнить объём у клиента» и «Посчитать смету»" in gw.user(3)
    # an answer that may hold a step goes to the draft
    body = say(client, interview_id, "Нет, ещё менеджер проверяет объём по фото.").json()
    assert body["flags"]["draft"] == "ok" and gw.steps()[-2:] == ["draft", "ask"]
    assert body["question"]["gap"] == "rate"  # two «between» an interview
    state = client.get(f"{URL}/{interview_id}").json()
    assert state["answered"] == ["volume"]  # a «between» key never goes there


def test_steps_from_a_between_answer_reach_the_final(settings):
    client, gw, interview_id = to_between(settings, dumps(F["drafts"][3]), q(), q(), dumps(F["extract"]),
                                          dumps(F["improve"]))
    body = say(client, interview_id, CALL).json()
    draft_call = gw.user(3)
    assert f"Вопрос 2: {BETWEEN_Q}\nОтвет: {CALL}" in draft_call
    assert "s6 task «Собрать и отправить КП» (r1) → s7" in draft_call  # the ids the draft keeps
    steps = {s["id"]: s for s in body["process"]["steps"]}
    assert steps["s6"]["next"] == ["s13"] and steps["s13"]["next"] == ["s7"]
    # s13 -> s7 is the pair just asked, split by its answer: the next handoff instead
    assert body["question"]["gap"] == "between.2" and "«Уточнить объём у клиента» и «Посчитать смету»" in gw.user(4)
    assert say(client, interview_id, "Не знаю").json()["flags"]["draft"] == "skipped"
    assert client.post(f"{URL}/{interview_id}/finish").status_code == 202
    state = wait_built(client, interview_id)
    assert state["status"] == "done" and gw.steps()[-2:] == ["extract", "improve"]
    extract = gw.calls[-2]["messages"][-1]["content"]
    assert CALL in extract and "s13 task «Позвонить клиенту по КП» (r1) → s7" in extract
    final = {s["id"]: s for s in state["process"]["steps"]}
    assert final["s13"]["name"] == "Позвонить клиенту по КП" and final["s13"]["role"] == "r1"
    assert final["s13"]["quote"] == "менеджер звонит клиенту и спрашивает, посмотрел ли он КП"
    assert "s13" not in state["process"]["flags"]["unquoted"]
    assert final["s6"]["next"] == ["s13"] and final["s13"]["next"] == ["s7"]
    assert state["answered"] == ["volume"]


def test_the_limit_of_questions(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q(), dumps(F["drafts"][1]), dumps(F["extract"]),
                                 dumps(F["improve"])], max_questions=2)
    interview_id = start(client)["id"]
    assert say(client, interview_id, F["answers"][0]).json()["question"]["max"] == 2
    body = say(client, interview_id, F["answers"][1]).json()
    assert body["done"] is True and body["question"] is None
    assert wait_built(client, interview_id)["status"] == "done"


def test_draft_that_fails_keeps_the_last_one(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q(), "не JSON", q(), GatewayError("chat HTTP 500"), q()])
    interview_id = start(client)["id"]
    first = say(client, interview_id, F["answers"][0]).json()["process"]
    body = say(client, interview_id, F["answers"][1]).json()
    assert body["flags"]["draft"] == "failed" and body["process"] == first
    assert body["understood"] == [{"field": "volume", "value": 120, "unit": "заявок/мес"}]  # the code's numbers stay
    body = say(client, interview_id, "Если клиент отказался, заявку закрываем.").json()
    assert body["flags"]["draft"] == "failed" and body["process"] == first and body["question"]
    steps = client.get(f"{URL}/{interview_id}").json()["steps"]
    assert [(s["name"], s["ok"], s.get("error")) for s in steps if s["name"] == "draft"] == [
        ("draft", True, None), ("draft", False, "model_format"), ("draft", False, "model_error")]


def test_digits_in_the_wording_are_cleaned(settings):
    client, gw = make(settings, [q("Сколько заявок у вас за месяц — 50, 100 или больше?"), dumps(F["drafts"][0]),
                                 q("Сколько примерно (например, 120) заявок за месяц?")])
    body = start(client)
    assert body["question"]["text"] == gaps.find_gaps(None, {}, "заявки")[0].template  # digits left: the template
    turn = say(client, body["id"], F["answers"][0]).json()
    assert turn["question"]["text"] == "Сколько примерно заявок за месяц?"
    assert turn["flags"]["question_digits"] == 1 and turn["flags"]["question_template"] is False


def test_contacts_in_an_answer_are_masked(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q()])
    interview_id = start(client)["id"]
    body = say(client, interview_id, F["answers"][0] + " Пишите на boss@corp.example, звоните +7 912 345-67-89.").json()
    assert "boss@corp.example" not in body["answer_text"] and "[email скрыт]" in body["answer_text"]
    assert body["flags"]["contacts_masked"] == 2 and "boss@corp.example" not in gw.user(1)
    assert "[email скрыт]" in client.get(f"{URL}/{interview_id}").json()["transcript"][0]["a"]


def test_an_answer_while_the_last_is_in_progress(settings):
    release = threading.Event()

    def slow_draft():
        assert release.wait(5)
        return dumps(F["drafts"][0])

    client, gw = make(settings, [q(), slow_draft, q()])
    interview_id = start(client)["id"]
    first = {}
    t = threading.Thread(target=lambda: first.update(r=say(client, interview_id, F["answers"][0])))
    t.start()
    deadline = time.monotonic() + 5
    while len(gw.calls) < 2:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    r = say(client, interview_id, "второй раз")
    assert r.status_code == 409 and "обрабатывается" in r.json()["message"]
    release.set()
    t.join(5)
    assert first["r"].status_code == 200 and first["r"].json()["question"]["n"] == 2


@pytest.mark.parametrize("body, status", [({"text": ""}, 422), ({"text": "а" * 501}, 422), ({"answer": "да"}, 422)])
def test_bad_text_answer(settings, body, status):
    client, gw = make(settings, [q()])
    interview_id = start(client)["id"]
    r = client.post(f"{URL}/{interview_id}/answers", json=body)
    assert r.status_code == status and r.json()["code"] == "bad_request" and gw.steps() == ["ask"]


def test_unknown_interview(settings):
    client, _ = make(settings)
    for r in (client.get(f"{URL}/nope"), client.get(f"{URL}/" + "a" * 22), say(client, "a" * 22, "да"),
              client.post(f"{URL}/{'a' * 22}/finish")):
        assert r.status_code == 404 and r.json()["code"] == "not_found"


# ---------- audio ----------

def test_audio_answer(settings, monkeypatch):
    def no_files(*a, **k):
        raise AssertionError("the audio must not go to a file")

    for name in ("TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, name, no_files)
    stt_settings = replace(settings, stt_model=Settings.from_env({"STT_MODEL": "openai/gpt-4o-mini-transcribe"}).stt_model)
    client, gw = make(stt_settings, [q(), dumps(F["drafts"][0]), q()], stt=[Heard(F["answers"][0], seconds=2)])
    interview_id = start(client)["id"]
    marker = b"AUDIO-MARKER-\x00\r\n\r\n--boundary-like--\r\n" + bytes(range(256)) * 40
    r = speak(client, interview_id, marker)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer_text"] == F["answers"][0] and [s["name"] for s in body["steps"]] == ["stt", "draft", "ask"]
    heard = gw.heard[0]
    assert heard["model"] == "openai/gpt-4o-mini-transcribe" and heard["audio"] == marker
    assert heard["content_type"] == "audio/webm" and heard["timeout_s"] == 30
    # the reserve was the worst case of this upload, the spend is the real one
    stt_row = client.get(f"{URL}/{interview_id}").json()["steps"][1]
    assert stt_row["name"] == "stt" and stt_row["ok"] and stt_row["rub"] < stt_worst_rub(
        "openai/gpt-4o-mini-transcribe", len(marker), 30)
    for path in settings.var_dir.rglob("*"):
        if path.is_file():
            assert b"AUDIO-MARKER" not in path.read_bytes()


def test_audio_type_from_the_file_name(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q()], stt=[F["answers"][0]])
    interview_id = start(client)["id"]
    r = speak(client, interview_id, b"m4a" * 1000, "application/octet-stream", name="file", filename="answer.m4a")
    assert r.status_code == 200 and gw.heard[0]["content_type"] == "audio/x-m4a"


@pytest.mark.parametrize("files, code, status", [
    ({"note": (None, "без файла")}, "bad_request", 422),
    ({"audio": ("a.txt", b"text", "text/plain")}, "bad_request", 422),
    ({"audio": ("a.webm", b"", "audio/webm")}, "bad_request", 422),
])
def test_bad_audio(settings, files, code, status):
    client, gw = make(settings, [q()])
    interview_id = start(client)["id"]
    r = client.post(f"{URL}/{interview_id}/answers", files=files)
    assert r.status_code == status and r.json()["code"] == code and gw.heard == []


def test_audio_over_the_limit(settings):
    client, gw = make(settings, [q()], max_audio_bytes=50_000)
    interview_id = start(client)["id"]
    r = speak(client, interview_id, b"x" * 50_001)
    assert r.status_code == 413 and r.json()["code"] == "too_long" and gw.heard == []
    # the limit holds without a Content-Length too
    r = client.post(f"{URL}/{interview_id}/answers", content=(b"x" * 10_000 for _ in range(80)),
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
    # other routes keep the small limit
    assert client.post(URL, content=b"{" + b" " * 20_000 + b"}",
                       headers={"content-type": "application/json"}).status_code == 413


@pytest.mark.parametrize("heard, status", [
    (Heard(""), 422), (Heard("Продолжение следует..."), 422), (Heard("Thank you."), 422),
    (Heard("да", seconds=0.5), 422), (Heard("Владелец бизнеса по-русски рассказывает"), 422),
    (GatewayError("stt HTTP 500"), 502)])
def test_stt_failed_keeps_the_question(settings, heard, status):
    client, gw = make(settings, [q("Расскажите по шагам?"), dumps(F["drafts"][0]), q()], stt=[heard])
    interview_id = start(client)["id"]
    r = speak(client, interview_id)
    assert r.status_code == status and r.json()["code"] == "stt_failed" and "Повторите" in r.json()["message"]
    assert gw.steps() == ["ask"]  # no draft, no new question
    state = client.get(f"{URL}/{interview_id}").json()
    assert state["transcript"] == [] and state["steps"][-1]["name"] == "stt" and state["steps"][-1]["ok"] is False
    assert state["totals"]["rub"] > 0  # a recognition is paid even when it heard nothing
    body = say(client, interview_id, F["answers"][0]).json()
    assert body["question"]["n"] == 2  # the first question was not used up


def test_stt_budget_refused_before_the_call(settings):
    client, gw = make(settings, [q()], stt=["не будет"])
    interview_id = start(client)["id"]
    store = store_of(client)
    hold = sqlite3.connect(store.db_path).execute("SELECT id FROM spend WHERE status = 'held'").fetchone()[0]
    store.settle(hold, 0)  # the hold is gone (as after a restart) and someone takes the rest of the day
    store.reserve(store.left_today())
    r = speak(client, interview_id)
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted" and gw.heard == []
    # a text answer still goes: the draft and the question are refused before their calls
    body = say(client, interview_id, F["answers"][0]).json()
    assert body["flags"]["draft"] == "failed" and body["flags"]["question_template"] is True
    assert body["question"]["n"] == 2 and gw.steps() == ["ask"]


# ---------- «Хватит», the final ----------

def test_finish_after_three_answers(settings):
    replies = [q()] + [dumps(F["drafts"][0]), q(), dumps(F["drafts"][1]), q(), dumps(F["drafts"][2]), q()]
    client, gw = make(settings, replies + [dumps(F["extract"]), dumps(F["improve"])])
    interview_id = start(client)["id"]
    for i in range(2):
        say(client, interview_id, F["answers"][i])
    r = client.post(f"{URL}/{interview_id}/finish")
    assert r.status_code == 409 and r.json()["code"] == "bad_request" and "3" in r.json()["message"]
    say(client, interview_id, F["answers"][2])
    r = client.post(f"{URL}/{interview_id}/finish")
    assert r.status_code == 202 and r.json()["status"] in ("building", "done")
    state = wait_built(client, interview_id)
    assert state["status"] == "done" and len(state["transcript"]) == 3
    assert say(client, interview_id, "ещё").status_code == 409
    # the final saw the transcript and the last draft's ids
    extract = gw.calls[-2]
    assert extract["schema"]["name"] == "process" and "Вопрос 3:" in extract["messages"][-1]["content"]
    assert "s7 decision «Что ответил клиент?»" in extract["messages"][-1]["content"]


def test_final_that_fails_twice(settings):
    replies = [q()] + [dumps(F["drafts"][0]), q(), dumps(F["drafts"][1]), q(), dumps(F["drafts"][2]), q()]
    client, gw = make(settings, replies + ["{}", "не JSON"])
    interview_id = start(client)["id"]
    for i in range(3):
        say(client, interview_id, F["answers"][i])
    client.post(f"{URL}/{interview_id}/finish")
    state = wait_built(client, interview_id)
    assert state["status"] == "error" and state["error"]["code"] == "model_format"
    assert store_of(client).spent_today() == pytest.approx(state["totals"]["rub"], abs=1e-3)  # hold released


def test_carry_answers_to_the_final_ids():
    final = {"roles": [{"id": "r1", "name": "Менеджер", "external": False}, {"id": "r2", "name": "Сметчик",
                                                                              "external": False}],
             "steps": [{"id": "s1", "kind": "start", "name": "Пришла заявка"},
                       {"id": "s2", "kind": "task", "name": "Посчитать смету в Excel"},
                       {"id": "s3", "kind": "task", "name": "Собрать КП и отправить"},
                       {"id": "s4", "kind": "end", "name": "Готово"}]}
    assumptions = {"perMonth": 120, "people": {"r3": 2}, "minutes": {"s5": 40, "s6": 30, "s9": 60},
                   "wait_hours": {"s5": 16}}
    answered = ["volume", "s5.minutes", "s6.minutes", "s9.minutes", "s5.wait_hours", "r3.people"]
    names = {"s5.minutes": "Посчитать смету", "s6.minutes": "Собрать и отправить КП", "s9.minutes": "Подготовить договор",
             "s5.wait_hours": "Посчитать смету", "r3.people": "Сметчик"}
    out, keys, lost = carry_answers(assumptions, answered, names, final)
    assert out == {"perMonth": 120, "people": {"r2": 2}, "minutes": {"s2": 40, "s3": 30}, "wait_hours": {"s2": 16}}
    assert keys == ["volume", "s2.minutes", "s3.minutes", "s2.wait_hours", "r2.people"] and lost == ["s9.minutes"]


# ---------- closing, restart, retention ----------

def test_no_answer_for_ten_minutes_closes_it(settings):
    client, _ = make(settings, [q()])
    clock = FakeClock(time.time())
    store_of(client).clock = clock
    interview_id = start(client)["id"]
    clock.advance(599)
    assert client.get(f"{URL}/{interview_id}").json()["status"] == "asking"
    clock.advance(2)
    state = client.get(f"{URL}/{interview_id}").json()
    assert state["status"] == "error" and state["error"]["code"] == "timeout" and "10 минут" in state["error"]["message"]
    assert store_of(client).spent_today() == pytest.approx(state["totals"]["rub"], abs=1e-3)  # the hold is back
    assert client.get("/api/process/status").json()["interviews_running"] == 0
    r = say(client, interview_id, "я вернулся")
    assert r.status_code == 409 and r.json()["code"] == "timeout"


def test_restart_ends_an_interview_in_progress(settings):
    client, _ = make(settings, [q()])
    interview_id = start(client)["id"]
    again, _ = make(settings)
    state = again.get(f"{URL}/{interview_id}").json()
    assert state["status"] == "error" and state["error"]["code"] == "restart"
    assert store_of(again).spent_today() == pytest.approx(state["totals"]["rub"], abs=1e-3)
    assert say(again, interview_id, "да").json()["code"] == "restart"


def test_report_lives_thirty_days(settings):
    client, _ = make(settings, full_script())
    clock = FakeClock(datetime(2026, 10, 12, 12, 0, tzinfo=MSK).timestamp())
    store_of(client).clock = clock
    interview_id = start(client)["id"]
    for answer in F["answers"]:
        say(client, interview_id, answer)
    assert wait_built(client, interview_id)["status"] == "done"
    clock.advance(30 * 86400 - 60)
    assert client.get(f"{URL}/{interview_id}").status_code == 200
    clock.advance(61)
    r = client.get(f"{URL}/{interview_id}")
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    store_of(client).purge()
    assert sqlite3.connect(store_of(client).db_path).execute("SELECT COUNT(*) FROM interviews").fetchone()[0] == 0


# ---------- in-process, as the eval drives it ----------

def test_the_eval_drives_it_in_process(tmp_path):
    settings = replace(Settings.from_env({}), var_dir=tmp_path)
    store = CapStore(tmp_path / "eval.sqlite3", 20, 1)
    gw = FakeGateway(full_script())
    interviews = Interviews(Deps(gw, store, settings))
    body = interviews.start("заявки", None)
    interview_id = body["id"]
    for answer in F["answers"]:
        if interviews.answer(interview_id, answer)["done"]:
            break
    state = interviews.build(interview_id)
    assert state["status"] == "done" and state["answered"][0] == "volume"
    assert store.spent_today() == pytest.approx(state["totals"]["rub"], abs=1e-3)
    with pytest.raises(InterviewError) as e:
        Interviews(Deps(FakeGateway(), CapStore(tmp_path / "small.sqlite3", 1, 1), settings)).start("заявки", None)
    assert e.value.code == "budget_exhausted"


# ---------- multipart ----------

def test_parse_multipart_keeps_binary_as_is():
    audio = b"\r\n--b\r\n\r\n" + bytes(range(256)) + b"\r\n"
    body = (b"--XyZ\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\n" + "текст".encode() + b"\r\n"
            b"--XyZ\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"a.ogg\"\r\n"
            b"Content-Type: audio/ogg; codecs=opus\r\n\r\n" + audio + b"\r\n--XyZ--\r\n")
    parts = parse_multipart(body, "multipart/form-data; boundary=XyZ")
    assert [p["name"] for p in parts] == ["note", "audio"] and parts[1]["data"] == audio
    assert audio_part(parts) == (audio, "audio/ogg")
    assert parse_multipart(body, "multipart/form-data") == []
    assert parts[0]["data"].decode() == "текст" and parts[0]["filename"] is None


# ---------- money stays inside the hold ----------

def test_the_hold_gets_back_what_a_call_did_not_spend(settings):
    client, gw = make(settings, [Reply(q(), tokens_in=400, tokens_out=60)])
    start(client)
    store = store_of(client)
    hold = sqlite3.connect(store.db_path).execute("SELECT id FROM spend WHERE status = 'held'").fetchone()[0]
    real = call_cost_rub(gw.calls[0]["model"], 400, 60)
    assert store.hold_left(hold) == pytest.approx(interview_worst_rub(settings) - real, abs=1e-6)
    # the hold and the real cost: the worst case, no more
    assert store.spent_today() == pytest.approx(interview_worst_rub(settings), abs=1e-6)


def test_a_question_takes_two_recognitions_that_heard_nothing(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q()], stt=[Heard(""), GatewayError("stt HTTP 500"),
                                                                      F["answers"][0]])
    interview_id = start(client)["id"]
    assert speak(client, interview_id).status_code == 422
    assert speak(client, interview_id).status_code == 502
    r = speak(client, interview_id)
    assert r.status_code == 422 and r.json()["code"] == "stt_failed" and "текстом" in r.json()["message"]
    assert len(gw.heard) == 2  # the third was not sent to recognition
    assert say(client, interview_id, F["answers"][0]).json()["question"]["n"] == 2  # text still goes


def test_recognition_without_audio_token_details(settings):
    client, gw = make(settings, [q(), dumps(F["drafts"][0]), q()], stt=[Heard(F["answers"][0], details=False)])
    interview_id = start(client)["id"]
    r = speak(client, interview_id)
    assert r.status_code == 200 and r.json()["answer_text"] == F["answers"][0]


def test_finish_after_ten_minutes_without_an_answer(settings):
    replies = [q()] + [dumps(F["drafts"][0]), q(), dumps(F["drafts"][1]), q(), dumps(F["drafts"][2]), q()]
    client, gw = make(settings, replies)
    clock = FakeClock(time.time())
    store_of(client).clock = clock
    interview_id = start(client)["id"]
    for i in range(3):
        say(client, interview_id, F["answers"][i])
    clock.advance(601)
    r = client.post(f"{URL}/{interview_id}/finish")
    assert r.status_code == 409 and r.json()["code"] == "timeout"
    assert gw.steps().count("extract") == 0 and client.get(f"{URL}/{interview_id}").json()["status"] == "error"


def test_a_failed_first_question_gives_the_hold_back(tmp_path):
    settings = replace(Settings.from_env({}), var_dir=tmp_path)
    store = CapStore(tmp_path / "eval.sqlite3", 20, 1)
    with pytest.raises(AssertionError):  # the fake gateway has no reply: the first question fails
        Interviews(Deps(FakeGateway(), store, settings)).start("заявки", None)
    # the hold is released; the failed call keeps its worst case (whether it was billed is unknown)
    assert sqlite3.connect(store.db_path).execute("SELECT COUNT(*) FROM spend WHERE status = 'held'").fetchone()[0] == 0
    assert 0 < store.spent_today() < interview_step_worst_rub(settings, "ask")


def test_a_chunked_body_has_the_same_limit(settings):
    client, gw = make(settings, [q()])
    r = client.post(URL, content=(b" " * 4096 for _ in range(5)), headers={"content-type": "application/json"})
    assert r.status_code == 413 and gw.calls == []
    r = client.post(URL, content=(part for part in [b'{"topic": ', b'"\xd0\xb7\xd0\xb0\xd1\x8f\xd0\xb2\xd0\xba\xd0\xb8"}']),
                    headers={"content-type": "application/json"})
    assert r.status_code == 201

