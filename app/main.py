"""FastAPI app. Run: uvicorn --factory app.main:create_app --no-proxy-headers

``--no-proxy-headers`` matters: the client address (and X-Forwarded-For trust) is resolved by
app.netutil according to TRUSTED_PROXY. Contract: docs/SPEC.md, «API»; the interview's routes
(docs/SPEC-analyst.md, «Контракт API») are in app.interview_api.

A run goes to a thread pool of MAX_CONCURRENT_RUNS (app.pipeline) with its worst case held in the
budget from admission; the page polls ``GET runs/{id}`` once a second: two steps, no SSE.
Recorded examples (app/recorded/<name>.json, scripts/record.py) are read once at startup.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.datastructures import MutableHeaders

from app.config import Settings
from app.gateway import ChatClient, Router
from app.interview import Interviews
from app.interview_api import INTERVIEW_ID, add_interview_routes, body_limit
from app.netutil import client_ip, rate_limit_key
from app.pii import mask_pii
from app.pipeline import Deps, initial_state, run
from app.pricing import interview_worst_rub, run_worst_rub
from app.store import Store

log = logging.getLogger("demo_process")
PREFIX = "/api/process"
MAX_BODY_BYTES = 16 * 1024
BUSY_RETRY_S = 30

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
EXAMPLES_CACHE = "public, max-age=300"
# The page's buttons in this order (docs/SPEC.md, «Что видит посетитель»); others follow by name.
EXAMPLES_ORDER = ("sales", "invoice", "returns", "onboarding")
EXAMPLE_KEYS = ("name", "title", "text", "process", "improve", "steps", "totals")

RUN_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
EXAMPLE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status)


ANSWERS_PATH = re.compile(rf"{PREFIX}/interviews/{INTERVIEW_ID.pattern}/answers")


class Guard:
    """Plain ASGI middleware: a body size limit and the security headers on every response;
    headers a route sets itself win. An interview's answer may carry audio: ``audio_body`` there.
    A body without Content-Length (chunked) is read up to the limit first and then handed on."""

    def __init__(self, app, max_body: int = MAX_BODY_BYTES, audio_body: int = MAX_BODY_BYTES):
        self.app = app
        self.max_body = max_body
        self.audio_body = audio_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    headers.setdefault(k, v)
            await send(message)

        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        limit = self.audio_body if ANSWERS_PATH.fullmatch(scope.get("path") or "") else self.max_body
        too_long = _error(413, "too_long", "Слишком большой запрос.")
        if length.isdigit():
            if int(length) > limit:
                await too_long(scope, receive, send_with_headers)
                return
            await self.app(scope, receive, send_with_headers)
            return
        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                return  # the client is gone
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if size > limit:
                await too_long(scope, receive, send_with_headers)
                return
            more = message.get("more_body", False)
        body, replayed = b"".join(chunks), False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send_with_headers)


class Slots:
    """At most ``limit`` runs at once for the whole service (one worker process)."""

    def __init__(self, limit: int):
        self.limit = limit
        self.busy = 0
        self.lock = threading.Lock()

    def acquire(self) -> bool:
        with self.lock:
            if self.busy >= self.limit:
                return False
            self.busy += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.busy = max(0, self.busy - 1)


def load_examples(directory: Path) -> dict[str, dict]:
    """Recorded runs ``<name>.json`` (scripts/record.py) in the page's order. Bad names and broken
    files are skipped."""
    out = {}
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    rank = {name: i for i, name in enumerate(EXAMPLES_ORDER)}
    for path in sorted(paths, key=lambda p: (rank.get(p.stem, len(rank)), p.stem)):
        name = path.stem
        if not EXAMPLE_NAME.fullmatch(name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("recorded example %s is unreadable", path.name)
            continue
        if not isinstance(data, dict) or not isinstance(data.get("process"), dict) \
                or not isinstance(data.get("improve"), dict):
            log.warning("recorded example %s has no process or improve", path.name)
            continue
        example = {k: data.get(k) for k in EXAMPLE_KEYS}
        example.update(name=name, title=str(data.get("title") or name), text=str(data.get("text") or ""),
                       steps=data.get("steps") if isinstance(data.get("steps"), list) else [])
        out[name] = example
    return out


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def create_app(settings: Settings | None = None, client: ChatClient | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.var_dir / "process.sqlite3", settings.daily_budget_rub, settings.runs_per_hour,
                  settings.retention_days)
    stale = store.fail_stale_runs()
    if stale:
        log.warning("marked %s runs left from a previous process as failed", stale)
    stale = store.fail_stale_interviews()
    if stale:
        log.warning("marked %s interviews left from a previous process as error", stale)
    store.release_holds()  # one worker (Dockerfile): no live run or interview holds anything yet
    if client is None and settings.live:
        client = Router.from_settings(settings)  # Timeweb, and Anthropic directly when that is on
    deps = Deps(client, store, settings)
    runner = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_runs), thread_name_prefix="run")
    slots = Slots(settings.max_concurrent_runs)
    worst = run_worst_rub(settings)
    interview_worst = interview_worst_rub(settings)
    interviews = Interviews(deps)
    examples = load_examples(settings.recorded_dir)
    bad_text = f"Нужно описание процесса от 1 до {settings.max_text_chars} знаков."

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, audio_body=body_limit(settings))
    app.state.store, app.state.slots, app.state.client = store, slots, client  # for tests and scripts

    @app.exception_handler(RequestValidationError)
    async def bad_request(_request: Request, _exc: RequestValidationError):
        return _error(422, "bad_request", bad_text)

    def visitor_ip(request: Request) -> str:
        return client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"),
                         settings.trusted_proxies)

    @app.get(f"{PREFIX}/health")
    def health():
        try:
            store.maybe_purge()  # hourly at most; health is what calls it when nobody visits
            interviews.expire_idle()  # the healthcheck comes every 30 s: idle interviews close on time
        except sqlite3.Error:
            log.exception("purge failed")
        return {"ok": True}

    @app.get(f"{PREFIX}/status")
    def status():
        store.maybe_purge()
        spent = store.spent_today()
        interviews.expire_idle()
        body = {"live": client is not None and spent + worst <= settings.daily_budget_rub,
                "budget_rub": settings.daily_budget_rub, "spent_rub": round(spent, 2),
                "run_worst_rub": round(worst, 2), "runs_running": slots.busy,
                "interview_worst_rub": round(interview_worst, 2), "interviews_running": store.active_interviews(),
                "models": settings.models()}
        if settings.anthropic_on:  # docs/SPEC.md, «Шлюзы»: with the route off the answer is as before
            body["routes"] = settings.routes()
        return body

    @app.post(f"{PREFIX}/runs")
    def create_run(req: RunRequest, request: Request):
        text = req.text.strip()
        if not text or len(text) > settings.max_text_chars:
            return _error(422, "bad_request", bad_text)
        if client is None:
            return _error(503, "offline", "Живой режим выключен. Записанные примеры работают.")
        if not slots.acquire():
            return _error(429, "busy", f"Сервис занят: одновременно идёт не больше {settings.max_concurrent_runs} "
                                       "прогонов. Попробуйте через минуту или посмотрите записанный пример.",
                          retry_after_s=BUSY_RETRY_S)
        submitted, hold = False, None
        try:
            # the run's worst case is held now, atomically: two runs never start on money for one
            hold = store.hold(worst)
            if not hold.allowed:
                return _error(503, "budget_exhausted", store.budget_message())
            decision = store.start(rate_limit_key(visitor_ip(request)))
            if not decision.allowed:
                return _error(429, decision.code, decision.message, retry_after_s=decision.retry_after_s)
            run_id = secrets.token_urlsafe(16)
            masked = mask_pii(text)[0]
            store.create_run(run_id, initial_state(run_id, masked, store.clock()))
            future = runner.submit(run, run_id, masked, deps, hold.reservation_id)
            submitted = True
            future.add_done_callback(lambda _f: slots.release())
        finally:
            if not submitted:
                slots.release()
                if hold is not None and hold.allowed:
                    store.settle(hold.reservation_id, 0.0)
        log.info("run=%s started chars=%s", run_id[:6], len(masked))
        return {"id": run_id}

    @app.get(f"{PREFIX}/runs/{{run_id}}")
    def get_run(run_id: str):
        state = store.load_run(run_id) if RUN_ID.fullmatch(run_id) else None
        if state is None:
            return _error(404, "not_found", "Прогон не найден: он старше 30 суток или ссылка неверная.")
        return state

    @app.get(f"{PREFIX}/examples")
    def list_examples():
        rows = [{"name": e["name"], "title": e["title"]} for e in examples.values()]
        return JSONResponse(rows, headers={"Cache-Control": EXAMPLES_CACHE})

    @app.get(f"{PREFIX}/examples/{{name}}")
    def get_example(name: str):
        e = examples.get(name) if EXAMPLE_NAME.fullmatch(name) else None
        if e is None:
            return _error(404, "not_found", "Нет такого примера.")
        body = {**e, "status": "done", "stage": "done", "error": None}
        return JSONResponse(body, headers={"Cache-Control": EXAMPLES_CACHE})

    add_interview_routes(app, PREFIX, settings, store, client, interviews)
    return app
