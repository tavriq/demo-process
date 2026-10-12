"""evals/interview/: the interview eval over HTTP (docs/SPEC-analyst.md, «Контракт API», «Замер»).

The server here is a fake written from the contract (stdlib http.server), not app/: the real one is
written in parallel. The owner simulator answers through a fake gateway; nothing goes out."""

import itertools
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app import pricing
from evals.interview import metrics as im
from evals.interview import owner
from evals.interview import run as irun
from evals.interview.api import Api, ApiError
from evals.score import load_processes, load_reference

PREFIX = "/api/process"
MODEL = owner.DEFAULT_MODEL
PROCESSES = {p["id"]: p for p in load_processes()}

QUESTIONS = [
    {"gap": "start_end", "text": "С чего начинается работа с заявкой и чем она заканчивается?"},
    {"gap": "volume", "text": "Сколько заявок приходит за месяц?"},
    {"gap": "s2.minutes", "text": "Сколько минут уходит на перенос заявки в таблицу?"},
    {"gap": "rate", "text": "Сколько стоит час работы менеджера?"},
]
REPLIES = ["Заявка приходит на общую почту, заканчивается подписанным договором или отказом.", "не знаю",
           "Минут 10 на одну.", ""]


def as_process(ref: dict, minutes: dict | None = None) -> dict:
    """The reference written as a model Process: every reference step is found."""
    nxt: dict[str, list] = {}
    for a, b in ref["edges"]:
        nxt.setdefault(a, []).append(b)
    steps = []
    for s in ref["steps"]:
        step = {"id": s["id"], "kind": s["kind"], "name": s["name"], "role": s["role"], "quote": None}
        if s["kind"] == "decision":
            step["branches"] = [{"label": "", "to": b} for b in nxt[s["id"]]]
        else:
            step["next"] = nxt.get(s["id"], [])
        if s["kind"] == "task":
            step.update(minutes=(minutes or {}).get(s["id"]), wait_hours=None, tool=None)
        steps.append(step)
    roles = [{"id": r["id"], "name": r["name"], "external": False} for r in ref["roles"]]
    return {"title": ref["title"], "roles": roles, "steps": steps, "flags": {"unquoted": [], "numbers_dropped": []}}


# --- the fake server -------------------------------------------------------------------------------

class Fake:
    """The interview API by the contract, scripted. ``finals``: topic or phrase → final Process."""

    def __init__(self, questions=QUESTIONS, *, finals=None, assumptions=None, rub=0.4, report_cost=True,
                 auto_build=False, builds=2, start_error=None, answer_error=None, build_error=None):
        self.questions = [dict(q) for q in questions]
        self.finals = finals if finals is not None else {
            topic: as_process(load_reference(pid)) for pid, topic in irun.TOPICS.items()}
        self.assumptions = assumptions if assumptions is not None else {"perMonth": 100}
        self.rub, self.report_cost, self.auto_build, self.builds = rub, report_cost, auto_build, builds
        self.start_error, self.answer_error, self.build_error = start_error, answer_error, build_error
        self.requests: list[tuple] = []
        self.interviews: dict[str, dict] = {}
        self.lock = threading.Lock()

    def calls(self, method: str, suffix: str) -> list:
        return [r for r in self.requests if r[0] == method and r[1].endswith(suffix)]

    def question(self, i: int):
        return {"n": i + 1, "max": 8, **self.questions[i]} if i < len(self.questions) else None

    def handle(self, method: str, path: str, body):
        with self.lock:
            self.requests.append((method, path, body))
            return self._route(method, path, body)

    def _route(self, method, path, body):
        bad = (422, {"code": "bad_request", "message": "не по контракту"})
        m = re.fullmatch(re.escape(PREFIX) + r"/interviews(?:/([^/]+)(/answers|/finish)?)?", path)
        if not m:
            return 404, {"code": "not_found", "message": ""}
        iid, action = m.groups()
        if method == "POST" and iid is None:
            if not isinstance(body, dict) or set(body) != {"topic", "text"}:
                return bad
            if self.start_error:
                return self.start_error
            iid = f"iv{len(self.interviews) + 1:02d}-0123456789abcdef"
            self.interviews[iid] = {"key": body["topic"] or body["text"], "answers": [], "status": "asking",
                                    "gets": 0}
            return 201, {"id": iid, "question": self.question(0), "process": None, "budget_left_rub": 12.5}
        iv = self.interviews.get(iid)
        if iv is None:
            return 404, {"code": "not_found", "message": ""}
        if method == "POST" and action == "/answers":
            text = body.get("text") if isinstance(body, dict) else None
            if iv["status"] != "asking" or not isinstance(text, str) or not text.strip() or len(text) > 500:
                return bad
            if self.answer_error and len(iv["answers"]) == self.answer_error[0]:
                return self.answer_error[1]
            iv["answers"].append(text)
            question = self.question(len(iv["answers"]))
            if question is None and self.auto_build:
                iv["status"] = "building"
            return 200, {"answer_text": text, "understood": [], "process": None, "flags": {}, "question": question,
                         "done": question is None, "steps": [{"name": "draft", "ms": 5, "rub": 0.01}]}
        if method == "POST" and action == "/finish":
            if iv["status"] != "asking":
                return 409, {"code": "bad_request", "message": "интервью уже строится"}
            iv["status"] = "building"
            return 202, None
        if method == "GET" and action is None:
            if iv["status"] == "building":
                iv["gets"] += 1
                if iv["gets"] >= self.builds:
                    iv["status"] = "error" if self.build_error else "done"
            done = iv["status"] == "done"
            return 200, {
                "status": iv["status"],
                "transcript": [{"q": self.questions[i]["text"], "a": a} for i, a in enumerate(iv["answers"])],
                "process": self.finals[iv["key"]] if done else None,
                "improve": {"changes": [], "summary": ""} if done else None,
                "assumptions": self.assumptions if done else {},
                "answered": [q["gap"] for q in self.questions[:len(iv["answers"])]],
                "steps": [{"name": "draft", "ms": 5, "rub": 0.01}] if self.report_cost else [],
                "totals": {"ms": 50, "rub": self.rub} if self.report_cost and iv["status"] != "asking" else {},
                **({"error": self.build_error} if iv["status"] == "error" else {}),
            }
        return 405, {"code": "bad_request", "message": ""}


class _Handler(BaseHTTPRequestHandler):
    def _go(self, method):
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b""
        status, payload = self.server.fake.handle(method, self.path, json.loads(raw) if raw else None)
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")

    def log_message(self, *args):
        pass


@pytest.fixture
def serve():
    servers = []

    def start(fake: Fake) -> str:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.fake = fake
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}{PREFIX}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


class Owner:
    """The gateway as the simulator sees it: scripted replies, a fixed real cost per call."""

    def __init__(self, replies=(), cost=0.002):
        self.replies, self.cost, self.calls = list(replies), cost, []

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        self.calls.append({"model": model, "messages": messages, "schema": schema, "max_tokens": max_tokens})
        content = self.replies.pop(0) if self.replies else "не знаю"
        return SimpleNamespace(content=content, cost_rub=self.cost, has_usage=True)


def go(base, client, tmp_path, ids=("sales",), max_rub=10.0, worst=1.0, **kw):
    kw.setdefault("sleep", lambda s: None)
    procs = [PROCESSES[i] for i in ids]
    return irun.run(procs, Api(base, timeout_s=5), client, MODEL, max_rub, worst, tmp_path / "run.jsonl", **kw)


# --- one interview end to end ----------------------------------------------------------------------

def test_interview_over_http_is_scored(serve, tmp_path):
    sales = load_reference("sales")
    fake = Fake(finals={"заявки": as_process(sales, minutes={"s2": 10})},
                assumptions={"perMonth": 100, "minutes": {"s5": 40, "s3": 25, "s1": 3}})
    client = Owner(REPLIES)
    [line] = go(serve(fake), client, tmp_path)

    assert line["ok"] and line["error"] is None and line["status"] == "done"
    assert line["start"] == {"topic": "заявки", "text": None}
    assert [(t["n"], t["gap"], t["a"]) for t in line["transcript"]] == [
        (1, "start_end", REPLIES[0]), (2, "volume", "не знаю"), (3, "s2.minutes", "Минут 10 на одну."),
        (4, "rate", "не знаю")]
    assert [b["text"] for _, _, b in fake.calls("POST", "/answers")] == [t["a"] for t in line["transcript"]]
    assert len(fake.calls("POST", "/finish")) == 1  # the server waited for «Хватит» after the last answer
    assert (line["questions"], line["answers"], line["repeats"], line["questions_with_digits"]) == (4, 4, 0, 0)
    # s2 from the step, s5 and s3 from the assumptions; s1 is the start event, no minutes there.
    assert (line["steps_total"], line["minutes_answered"], line["minutes_assumed"], line["minutes_grounded"]) == \
        (12, 3, 6, 2)  # 10 and 40 are in the text, 25 is not
    assert line["score"]["steps_found"] == line["score"]["steps_ref"] == 10
    assert (line["rub_owner"], line["rub_interview"], line["rub"]) == (0.008, 0.4, 0.408)
    assert line["rub_interview_reported"] is True
    saved = [json.loads(x) for x in (tmp_path / "run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert saved == [line]


def test_owner_sees_only_the_text_the_rules_and_the_question(serve, tmp_path):
    client = Owner(REPLIES)
    go(serve(Fake()), client, tmp_path)
    p = PROCESSES["sales"]
    assert len(client.calls) == len(QUESTIONS)
    for call, q in zip(client.calls, QUESTIONS):
        assert (call["model"], call["schema"], call["max_tokens"]) == (MODEL, None, owner.MAX_TOKENS)
        assert call["messages"] == owner.messages(p["text"], q["text"])
        everything = json.dumps(call["messages"], ensure_ascii=False)
        assert p["title"] not in everything and REPLIES[0] not in everything  # nor its own answers
        assert all(other["text"] not in everything for other in QUESTIONS if other is not q)  # no history
    assert "не знаю" in owner.SYSTEM and "Ничего не выдумывай" in owner.SYSTEM


def test_answers_are_cut_like_the_page_and_empty_is_dont_know():
    assert owner.clean(None) == owner.clean("  \n ") == "не знаю"
    assert owner.clean("  Минут   10\nна одну. ") == "Минут 10 на одну."
    assert len(owner.clean("слово " * 200)) == owner.MAX_ANSWER_CHARS


def test_the_start_is_the_page_button_or_the_title_as_a_phrase():
    assert irun.start_body(PROCESSES["onboarding"]) == {"topic": "выход", "text": None}
    assert irun.start_body(PROCESSES["warehouse"]) == {"topic": None, "text": "Приёмка товара на складе"}


def test_server_that_builds_by_itself_gets_no_finish(serve, tmp_path):
    fake = Fake(auto_build=True, builds=3)
    [line] = go(serve(fake), Owner(), tmp_path)
    assert line["ok"] and fake.calls("POST", "/finish") == []
    assert len(fake.calls("GET", "")) == 3  # the check for «asking» is the first poll


def test_no_answer_past_eight_questions(serve, tmp_path):
    questions = [{"gap": f"s{i}.minutes", "text": f"Вопрос про шаг номер {chr(1040 + i)}?"} for i in range(10)]
    fake = Fake(questions)
    client = Owner()
    [line] = go(serve(fake), client, tmp_path)
    assert len(client.calls) == 8 and len(fake.calls("POST", "/answers")) == 8
    assert (line["questions"], line["answers"]) == (9, 8) and line["transcript"][-1]["a"] is None
    assert len(fake.calls("POST", "/finish")) == 1


def test_no_questions_at_all_goes_straight_to_the_build(serve, tmp_path):
    fake = Fake([])
    client = Owner()
    [line] = go(serve(fake), client, tmp_path)
    assert line["ok"] and client.calls == [] and line["questions"] == 0


# --- metrics ---------------------------------------------------------------------------------------

def test_repeats_and_digits_in_questions():
    transcript = [
        {"gap": "volume", "q": "Сколько заявок в месяц?", "a": "не знаю"},
        {"gap": "volume", "q": "А заявок за месяц сколько?", "a": "не знаю"},  # the same gap
        {"gap": "rate", "q": "  сколько  заявок в МЕСЯЦ? ", "a": "не знаю"},  # the same text
        {"gap": "s3.minutes", "q": "Сколько минут на проводку в 1С?", "a": "не знаю"},  # 1С is a name
        {"gap": "s4.minutes", "q": "Шаг 2 — сколько минут?", "a": None},
    ]
    assert im.question_counts(transcript) == {"questions": 5, "answers": 4, "repeats": 2, "questions_with_digits": 1}
    assert im.has_digits("через 2-3 дня") and not im.has_digits("в 1C и почте")


def test_minutes_from_answers_only_on_work_steps():
    process = {"steps": [
        {"id": "s1", "kind": "start", "minutes": 5},
        {"id": "s2", "kind": "task", "minutes": 10},
        {"id": "s3", "kind": "task", "minutes": None},
        {"id": "s4", "kind": "decision"},
        {"id": "s5", "kind": "end"},
    ]}
    assert im.answered_minutes(process, {"minutes": {"s3": 30, "s5": 1, "s2": 12}}) == {"s2": 12, "s3": 30}
    assert im.answered_minutes(process, {"minutes": "много"}) == {"s2": 10}
    assert im.answered_minutes(process, None) == {"s2": 10}
    counts = im.minutes_counts(process, {"minutes": {"s4": True}}, {"numbers": []})
    assert counts == {"steps_total": 5, "minutes_answered": 1, "minutes_assumed": 2, "minutes_grounded": 0}


def test_table_pools_the_last_line_per_process(serve, tmp_path):
    fake = Fake(finals={"заявки": as_process(load_reference("sales"), minutes={"s2": 10})})
    lines = go(serve(fake), Owner(REPLIES), tmp_path)
    failed = {"id": "invoice", "ok": False, "error": "rate_limited: лимит | час", "rub": 0, "transcript": []}
    table = im.table([dict(lines[0], id="invoice"), *lines, failed])
    assert "| sales | 4 | 0 | 0 | 10 из 10 | 1 / 8 | 0.41 |  |" in table
    assert "| invoice | — | — | — | — | — | 0.00 | rate_limited: лимит / час |" in table
    assert "| всего | 4 | 0 | 0 | 100% [72%–100%] (10/10) | 1 / 8 | 0.41 | не дошли 1 из 2 |" in table
    assert "Минуты из ответов — у 1 из 12 шагов (в v1 из текста — 5 из 54), есть в описании — 1 из 1." in table

    path = tmp_path / "run.jsonl"
    assert im.read_lines([path]) == lines


# --- money -----------------------------------------------------------------------------------------

def test_the_worst_case_line():
    procs = [PROCESSES[i] for i in irun.DEFAULT_ONLY]
    owners = sum(owner.worst_rub(MODEL, p["text"]) for p in procs)
    msg, fit = irun.worst_case(procs, 2.5, MODEL, 20)
    assert msg == (f"худший случай: 4 × интервью 2.50 ₽ + владелец {owners:.2f} ₽ = {10 + owners:.2f} ₽; "
                   f"потолок --max-rub 20.00 ₽") and fit == 4
    msg, fit = irun.worst_case(procs, 2.5, MODEL, 6)
    assert msg.endswith("— влезут 2 из 4") and fit == 2


def test_owner_worst_case_covers_eight_answers():
    text = PROCESSES["sales"]["text"]
    question = "в" * owner.MAX_QUESTION_CHARS
    one = pricing.messages_worst_rub(MODEL, owner.messages(text, question), owner.MAX_TOKENS)
    assert owner.worst_rub(MODEL, text) == pytest.approx(8 * one)


def test_budget_stops_before_an_interview_that_may_not_fit(serve, tmp_path):
    fake = Fake(rub=0.8)
    client = Owner(cost=0.002)
    max_rub = 1.0 + owner.worst_rub(MODEL, PROCESSES["sales"]["text"]) + 0.5
    lines = go(serve(fake), client, tmp_path, ids=("sales", "invoice", "returns"), max_rub=max_rub, worst=1.0)
    assert [(x["id"], x["ok"]) for x in lines] == [("sales", True), ("invoice", False)]
    assert lines[1]["error"].startswith("budget: худший случай") and lines[1]["rub"] == 0
    assert len(fake.calls("POST", "/interviews")) == 1
    assert sum(x["rub"] for x in lines) <= max_rub


def test_unreported_interview_cost_counts_as_its_worst_case(serve, tmp_path):
    [line] = go(serve(Fake(report_cost=False)), Owner(), tmp_path, worst=1.7)
    assert line["ok"] and line["rub_interview"] == 1.7 and line["rub_interview_reported"] is False


def test_a_simulator_call_past_the_ceiling_is_not_made(serve, tmp_path):
    long_question = [{"gap": "start_end", "text": "Расскажите " + "очень " * 8000 + "подробно?"}]
    client = Owner()
    max_rub = 1.0 + owner.worst_rub(MODEL, PROCESSES["sales"]["text"])
    lines = go(serve(Fake(long_question)), client, tmp_path, ids=("sales", "invoice"), max_rub=max_rub)
    assert len(lines) == 1 and lines[0]["error"].startswith("budget: следующий вызов")
    assert client.calls == []
    assert lines[0]["rub_interview"] == 1.0  # started: the server's worst case is counted
    assert lines[0]["rub"] <= max_rub


# --- errors ----------------------------------------------------------------------------------------

def test_rate_limited_stops_the_run(serve, tmp_path):
    fake = Fake(start_error=(429, {"code": "rate_limited", "message": "3 интервью в час"}))
    client = Owner()
    lines = go(serve(fake), client, tmp_path, ids=("sales", "invoice"))
    assert len(lines) == 1 and lines[0]["error"] == "rate_limited: 3 интервью в час"
    assert client.calls == [] and lines[0]["rub"] == 0 and lines[0]["interview"] is None


def test_a_failed_answer_does_not_stop_the_others(serve, tmp_path):
    fake = Fake(answer_error=(1, (502, {"code": "model_format", "message": "черновик не по схеме"})))
    lines = go(serve(fake), Owner(), tmp_path, ids=("sales", "invoice"), worst=1.0)
    assert [x["error"] for x in lines] == ["model_format: черновик не по схеме"] * 2
    assert [x["rub_interview"] for x in lines] == [1.0, 1.0]  # started, the cost is unknown
    assert all(x["questions"] == 2 and x["answers"] == 2 for x in lines)


def test_a_failed_build_keeps_the_question_counts(serve, tmp_path):
    fake = Fake(build_error={"code": "model_format", "message": "финал не по схеме"})
    [line] = go(serve(fake), Owner(), tmp_path)
    assert not line["ok"] and line["status"] == "error" and line["error"] == "model_format: финал не по схеме"
    assert line["questions"] == 4 and "score" not in line and line["rub_interview"] == 0.4


def test_a_build_that_never_ends_times_out(serve, tmp_path):
    ticks = itertools.count()
    [line] = go(serve(Fake(builds=10**6)), Owner(), tmp_path, worst=1.3, wait_s=5, clock=lambda: next(ticks))
    assert line["error"] == "TimeoutError: интервью не достроилось за 5 с"
    assert line["rub_interview"] == 1.3


def test_an_unreachable_server_stops_the_run(tmp_path):
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    closed = f"http://127.0.0.1:{probe.server_address[1]}{PREFIX}"
    probe.server_close()
    lines = go(closed, Owner(), tmp_path, ids=("sales", "invoice"))
    assert len(lines) == 1 and lines[0]["error"].startswith("unreachable") and lines[0]["rub"] == 0


def test_a_local_server_is_reached_without_the_env_proxy(serve, monkeypatch):
    base = serve(Fake())
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert Api(base, timeout_s=5).start("заявки")["question"]["n"] == 1
    with pytest.raises(ApiError) as e:
        Api(base, timeout_s=5).get("нет-такого")
    assert (e.value.status, e.value.code) == (404, "not_found")


# --- the command -----------------------------------------------------------------------------------

def test_dry_run_prints_the_worst_case_and_calls_nothing(monkeypatch, capsys):
    monkeypatch.setattr(pricing, "interview_worst_rub", lambda settings: 1.25, raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert irun.main(["--max-rub", "8", "--dry-run", "--base-url", "http://127.0.0.1:9/api/process"]) == 0
    procs = [PROCESSES[i] for i in irun.DEFAULT_ONLY]
    assert capsys.readouterr().out.strip() == irun.worst_case(procs, 1.25, MODEL, 8)[0]


def test_live_command_against_the_fake(serve, monkeypatch, tmp_path, capsys):
    import app.gateway

    base = serve(Fake(builds=1))
    client = Owner(REPLIES)
    monkeypatch.setattr(pricing, "interview_worst_rub", lambda settings: 1.0, raising=False)
    monkeypatch.setattr(app.gateway, "Gateway", lambda base_url, api_key: client)
    monkeypatch.setattr(irun, "RUNS_DIR", tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("LLM_BASE_URL", "http://gateway.invalid/v1")
    assert irun.main(["--max-rub", "5", "--base-url", base, "--only", "sales"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("худший случай: 1 × интервью 1.00 ₽") and "| sales | 4 | 0 | 0 |" in out
    assert "1 из 1 интервью, 0.41 ₽ из 5.00" in out
    [path] = tmp_path.glob("*.jsonl")
    assert [x["id"] for x in im.read_lines([path])] == ["sales"]


def test_without_a_key_the_live_command_refuses(monkeypatch):
    monkeypatch.setattr(pricing, "interview_worst_rub", lambda settings: 1.0, raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="LLM_API_KEY"):
        irun.main(["--max-rub", "5", "--base-url", "http://127.0.0.1:9/api/process"])


def test_a_ceiling_below_one_interview_refuses(monkeypatch):
    monkeypatch.setattr(pricing, "interview_worst_rub", lambda settings: 3.0, raising=False)
    with pytest.raises(SystemExit, match="потолок меньше"):
        irun.main(["--max-rub", "2"])


def test_missing_interview_worst_is_a_clear_error(monkeypatch):
    monkeypatch.delattr(pricing, "interview_worst_rub", raising=False)
    with pytest.raises(SystemExit, match="app.pricing.interview_worst_rub"):
        irun.load_interview_worst()


def test_unknown_ids_are_refused():
    with pytest.raises(SystemExit, match="нет таких id: nope"):
        irun.select(load_processes(), "sales,nope")
