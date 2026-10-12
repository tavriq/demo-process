"""Local server for page development: the service on a fake gateway that answers with the
handwritten model answers from tests/fixtures/ (no key, no money). Runs, interviews and examples go
to a fresh temp dir; the examples are the fixtures run through the real pipeline at startup. Never
used in production.

    python3 scripts/dev_server.py [--port 8765] [--speed 1] [--budget 1000]

The API is at http://127.0.0.1:<port>/api/process/…, and web/ (if it is there) is served from the
same origin: http://127.0.0.1:<port>/dev.html.

Runs (v1): a text equal to a fixture's gets that fixture's answers; any other text gets the first
fixture's, so its quotes are not found and its numbers are dropped — the flags show on the page as
they would after a bad answer.

Interviews (docs/SPEC-analyst.md): the real code on the fake gateway, whole — up to 8 questions,
«Хватит» after 3 answers, the final, the report by its link. After the N-th answer the draft is the
N-th draft of tests/fixtures/interview.json; the questions are its wordings by the kind of gap (the
volume one carries digits on purpose: the code replaces it with its template). Answers typed as in
the fixture (``answers``) give the drafts their quotes and numbers; any others give the same drafts,
flagged. A recording gets the fixture's next answer as its transcript; a recording under 1 KB is not
heard (``stt_failed``). A finished interview is ready at startup: ``dev.html#report=<REPORT_ID>``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.checks import normalize  # noqa: E402
from app.config import Settings  # noqa: E402
from app.gateway import Chat, Transcript  # noqa: E402
from app.interview import Interviews  # noqa: E402
from app.main import create_app  # noqa: E402
from app.pipeline import Deps  # noqa: E402
from app.pricing import call_cost_rub  # noqa: E402
from app.store import Store  # noqa: E402
from scripts.record import record  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
WEB = ROOT / "web"
DELAYS_S = {"process": 1.5, "improve": 1.0, "draft": 1.0, "question": 0.4, "stt": 0.8}
REPORT_ID = "dev-report-zayavki-0001"
MIN_HEARD_BYTES = 1024

# What a gap asks about, by the start of its description in app.gaps (the ask prompt's «Что узнать»).
GAP_KINDS = (("start_end", r"Как проходит процесс|С чего начинается|Чем заканчивается"),
             ("volume", r"Сколько раз в месяц"), ("role", r"Кто делает шаг"), ("branches", r"Какие варианты"),
             ("rate", r"Во сколько компании"), ("minutes", r"Сколько минут работы"),
             ("wait", r"Сколько обычно ждёт"), ("people", r"Сколько человек"))


def load_fixtures(directory: Path = FIXTURES) -> list[dict]:
    """The v1 fixtures: a text and the extract and improve answers to it."""
    order = {"sales": 0, "invoice": 1}
    paths = sorted(directory.glob("*.json"), key=lambda p: (order.get(p.stem, len(order)), p.stem))
    out = []
    for p in paths:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data.get("text"), str) and "extract" in data and "drafts" not in data:
            out.append(data)
    return out


def load_interview(directory: Path = FIXTURES) -> dict:
    return json.loads((directory / "interview.json").read_text(encoding="utf-8"))


def gap_kind(prompt: str) -> tuple[str, str]:
    """(kind of the gap, the step or role it names — for the first question, the process) from the
    ask prompt."""
    about = prompt.split("Что узнать:", 1)[-1]
    kind = next((k for k, pattern in GAP_KINDS if re.search(pattern, about)), "start_end")
    name = re.search(r"«([^»]+)»", about)
    if kind == "start_end":
        topic = re.search(r"Тема: ([^.\n]+)", about)
        return kind, topic[1] if topic else "этот процесс"
    return kind, name[1] if name else ""


class FixtureGateway:
    """Answers every model step with the fixtures after a delay × ``speed``; recognition too."""

    def __init__(self, fixtures: list[dict], interview: dict | None = None, speed: float = 1.0):
        self.fixtures = fixtures
        self.interview = interview or load_interview()
        self.speed = speed
        self.lock = threading.Lock()
        self.calls = 0
        self.heard = 0

    def pick(self, schema_name: str, prompt: str) -> dict:
        for f in self.fixtures:
            key = normalize(f["text"]) if schema_name == "process" else json.dumps(f["extract"]["title"],
                                                                                    ensure_ascii=False)
            if key in (normalize(prompt) if schema_name == "process" else prompt):
                return f
        return self.fixtures[0]

    def answer(self, name: str, prompt: str) -> dict:
        iv = self.interview
        if name == "draft":
            n = prompt.count("Ответ:")
            drafts = [d for d in iv["drafts"][:max(1, n)] if d is not None]
            return drafts[-1] if drafts else iv["drafts"][0]
        if name == "question":
            kind, step = gap_kind(prompt)
            return {"text": iv["questions"][kind].format(name=step)}
        if name == "process" and "Вопрос 1:" in prompt:
            return iv["extract"]
        f = self.pick(name, prompt)
        return f["extract"] if name == "process" else f["improve"]

    def _wait(self, name: str) -> None:
        delay = DELAYS_S[name] * self.speed
        if delay:
            time.sleep(delay)

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        name = schema["name"]
        prompt = "".join(m["content"] for m in messages if m["role"] == "user")
        answer = json.dumps(self.answer(name, prompt), ensure_ascii=False)
        self._wait(name)
        with self.lock:
            self.calls += 1
        tin = sum(len(m["content"]) for m in messages) // 2
        tout = len(answer) // 3
        return Chat(answer, tin, tout, int(DELAYS_S[name] * 1000), call_cost_rub(model, tin, tout))

    def transcribe(self, model, audio, content_type, timeout_s=None):
        self._wait("stt")
        if len(audio) < MIN_HEARD_BYTES:
            return Transcript("", 3, int(DELAYS_S["stt"] * 1000), call_cost_rub(model, 43, 0), 43, 0)
        with self.lock:
            text = self.interview["answers"][self.heard % len(self.interview["answers"])]
            self.heard += 1
        seconds = max(2, len(text) // 15)
        tin, tout = seconds * 10 + 40, seconds * 4
        return Transcript(text, seconds * 10, int(DELAYS_S["stt"] * 1000), call_cost_rub(model, tin, tout), tin, tout)


def dev_settings(tmp: Path, budget: float = 1000.0) -> Settings:
    """A big budget: an interview holds its worst case (12+ ₽) and the page is tried many times."""
    return replace(Settings.from_env({}), api_key="dev-fake", base_url="http://fake-gateway.invalid",
                   var_dir=tmp / "var", recorded_dir=tmp / "recorded", runs_per_hour=1000, interviews_per_hour=1000,
                   daily_budget_rub=budget)


def ready_report(settings: Settings, fixtures: list[dict], interview: dict) -> str:
    """Run the fixture's interview through the real code (no delays) under REPORT_ID."""
    store = Store(settings.var_dir / "process.sqlite3", settings.daily_budget_rub, settings.runs_per_hour,
                  settings.retention_days)
    interviews = Interviews(Deps(FixtureGateway(fixtures, interview, speed=0), store, settings))
    interviews.start(interview["topic"], interview["text"], REPORT_ID)
    for answer in interview["answers"]:
        if interviews.answer(REPORT_ID, answer)["done"]:
            break
    else:
        interviews.finish(REPORT_ID)
    state = interviews.build(REPORT_ID)
    if state["status"] != "done":
        raise RuntimeError(f"the dev report did not build: {state['error']}")
    return REPORT_ID


def build(tmp: Path, speed: float = 1.0, web: Path | None = WEB, budget: float = 1000.0):
    """The app on FixtureGateway(speed) with the fixtures recorded as examples and a ready interview
    report; with ``web`` (if it exists) also the page files at /."""
    settings = dev_settings(tmp, budget)
    fixtures = load_fixtures()
    interview = load_interview()
    for f in fixtures:
        record(f["name"], f["title"], f["text"], FixtureGateway(fixtures, interview, speed=0), settings,
               settings.recorded_dir)
    ready_report(settings, fixtures, interview)
    api = create_app(settings, client=FixtureGateway(fixtures, interview, speed=speed))
    if web is None or not web.is_dir():
        return api
    from starlette.staticfiles import StaticFiles

    static = StaticFiles(directory=web, html=True)

    async def app(scope, receive, send):
        target = api if scope["type"] != "http" or scope["path"].startswith("/api/") else static
        await target(scope, receive, send)

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--speed", type=float, default=1.0, help="scale of the fake model delays, 0 = instant")
    ap.add_argument("--budget", type=float, default=1000.0, help="daily budget, ₽ (15 as in production)")
    args = ap.parse_args(argv)
    import uvicorn

    tmp = Path(tempfile.mkdtemp(prefix="demo-process-dev-"))
    app = build(tmp, args.speed, budget=args.budget)
    print(f"demo-process dev: http://127.0.0.1:{args.port}/api/process/status")
    print(f"ready report: http://127.0.0.1:{args.port}/dev.html#report={REPORT_ID}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
