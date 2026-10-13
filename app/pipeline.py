"""The pipeline of one run (docs/SPEC.md, «Конвейер»): extract -> check -> improve.

Pure orchestration: the gateway, the store, the settings and the clock come in ``Deps``, so tests
run the whole thing on fakes. After every step the state is saved; the page polls
``GET runs/{id}``.

A step's answer that is off format or does not pass app.checks gets one retry with the list of
errors; the second miss fails the run with "model_format".

Money: the run holds its worst case (both steps with a retry each, app.pricing.run_worst_rub);
app.main holds it at admission. Every call reserves its own worst case out of that hold before it
starts and settles the real cost after, also on errors and timeouts. A reservation refused fails
the run with "budget_exhausted" before the call.

Time: the whole run gets ``run_timeout_s``; each call's timeout is the step's timeout or what is
left of the run, whichever is less. Past the deadline the run fails with "timeout".

``extract_process(text, settings, client)`` is the same extract with its check and retry for the
eval (evals/run.py): no run state, money only if a store is given.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from app import checks, prompts, schema
from app.config import Settings
from app.gateway import MODEL_PARAMS, Chat, ChatClient, GatewayError
from app.pii import mask_pii
from app.pricing import messages_worst_rub, run_worst_rub
from app.store import Store

log = logging.getLogger("demo_process")

MESSAGES = {
    "model_format": "Модель дважды ответила не по формату. Попробуйте ещё раз или посмотрите записанный пример.",
    "model_error": "Модель не ответила. Попробуйте ещё раз через минуту или посмотрите записанный пример.",
    "timeout": "Прогон не уложился в {s} с и остановлен. Попробуйте ещё раз или посмотрите записанный пример.",
    "budget_exhausted": "Дневной бюджет демо на сегодня закончился. Записанные примеры работают и сейчас.",
    "internal": "Внутренняя ошибка прогона. Попробуйте ещё раз.",
}


@dataclass
class Deps:
    gateway: ChatClient
    store: Store | None
    settings: Settings
    clock: Callable[[], float] = field(default=time.monotonic)


class RunFailed(Exception):
    def __init__(self, code: str, message: str | None = None, steps: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.message = message
        self.steps = steps or []


_JSON_FENCE = re.compile(r"\A\s*```(?:json)?[ \t]*\r?\n?(.*?)\r?\n?```\s*\Z", re.DOTALL | re.IGNORECASE)


def parse_answer(content: str, answer_schema: dict) -> dict | None:
    """The model's JSON answer, or None when it is off format: not JSON, not an object, a required
    field missing or a list/object field of another type. Deeper types are left to app.checks."""
    text = content.strip() if isinstance(content, str) else ""
    fenced = _JSON_FENCE.match(text)
    if fenced:
        text = fenced[1]
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    body = answer_schema["schema"]
    for key in body["required"]:
        if key not in data:
            return None
        kind = body["properties"][key].get("type")
        value = data[key]
        if (kind == "array" and not isinstance(value, list)) or (kind == "object" and not isinstance(value, dict)) \
                or (kind == "string" and not isinstance(value, str)):
            return None
    return data


class Calls:
    """Model calls of one run (or one eval item): money, time and the ``steps`` rows."""

    def __init__(self, deps: Deps, hold_id: int | None = None, on_step: Callable[[dict], None] | None = None,
                 label: str = "-", within_hold: bool = False):
        """``within_hold``: every call stays inside the hold and gives back what it did not spend
        (an interview; app.store.Store.reserve)."""
        self.deps = deps
        self.s = deps.settings
        self.store = deps.store
        self.deadline = deps.clock() + self.s.run_timeout_s
        self.hold_id = hold_id
        self.on_step = on_step
        self.label = label
        self.within_hold = within_hold
        self.steps: list[dict] = []

    def left_s(self) -> float:
        return self.deadline - self.deps.clock()

    def check_time(self) -> None:
        if self.left_s() <= 0:
            raise RunFailed("timeout")

    def call(self, step: str, messages: list[dict], answer_schema: dict, attempt: int) -> tuple[Chat, float]:
        """One model call with its reservation. Returns the answer and its cost (the reserve when
        the gateway gave no usage)."""
        conf = self.s.step(step)
        self.check_time()
        worst = messages_worst_rub(conf.model, messages, conf.max_tokens, self.s)
        reservation = None
        if self.store is not None:
            hold = (self.hold_id, worst) if self.hold_id is not None else None
            decision = self.store.reserve(worst, hold=hold, within_hold=self.within_hold and hold is not None)
            if not decision.allowed:
                raise RunFailed("budget_exhausted", decision.message)
            reservation = decision.reservation_id
        cost, started = worst, self.deps.clock()
        try:
            chat = self.deps.gateway.chat(conf.model, messages, answer_schema, conf.max_tokens,
                                          MODEL_PARAMS.get(conf.model), min(conf.timeout_s, self.left_s()))
            if chat.has_usage:
                cost = chat.cost_rub
        except GatewayError as e:
            log.warning("run=%s step=%s attempt=%s gateway error: %s", self.label, step, attempt, e.detail)
            failed = Chat("", 0, 0, int((self.deps.clock() - started) * 1000), worst)
            self.record(step, attempt, conf.model, failed, worst, ok=False, error="model_error")
            raise RunFailed("model_error") from None
        finally:
            if reservation is not None:
                self.store.settle(reservation, cost, self.hold_id if self.within_hold else None)
        return chat, cost

    def record(self, step: str, attempt: int, model: str, chat: Chat, cost: float, ok: bool,
               error: str | None = None) -> None:
        """A ``steps`` row; past the deadline the run then fails with "timeout"."""
        row = {"step": step, "attempt": attempt, "ms": chat.ms, "tokens_in": chat.tokens_in,
               "tokens_out": chat.tokens_out, "rub": round(cost, 4), "model": model, "ok": ok}
        if error:
            row["error"] = error
        self.steps.append(row)
        if self.on_step is not None:
            self.on_step(row)
        self.check_time()

    def json_step(self, step: str, messages: list[dict], answer_schema: dict,
                  check: Callable[[dict], tuple[dict, list[str]]]) -> dict:
        """A step with a JSON answer checked by code: one retry with the errors, then "model_format"."""
        conf = self.s.step(step)
        msgs = messages
        for attempt in (1, 2):
            chat, cost = self.call(step, msgs, answer_schema, attempt)
            data = parse_answer(chat.content, answer_schema)
            result, errors = check(data) if data is not None else (None, [prompts.NOT_JSON])
            self.record(step, attempt, conf.model, chat, cost, ok=not errors,
                        error="model_format" if errors else None)
            if not errors:
                return result
            log.info("run=%s step=%s attempt=%s errors: %s", self.label, step, attempt, "; ".join(errors))
            msgs = prompts.with_errors(messages, chat.content, errors, conf.max_tokens)
        raise RunFailed("model_format")


def extract(calls: Calls, text: str) -> dict:
    """Text (contacts masked) -> checked Process."""
    return calls.json_step("extract", prompts.extract_messages(text), schema.PROCESS,
                           lambda data: checks.check_process(data, text))


def improve(calls: Calls, process: dict) -> dict:
    """Checked Process -> checked Improve."""
    return calls.json_step("improve", prompts.improve_messages(process), schema.IMPROVE,
                           lambda data: checks.check_improve(data, process))


def totals(steps: list[dict]) -> dict:
    return {"ms": sum(s["ms"] for s in steps), "tokens_in": sum(s["tokens_in"] for s in steps),
            "tokens_out": sum(s["tokens_out"] for s in steps), "rub": round(sum(s["rub"] for s in steps), 4)}


def extract_process(text: str, settings: Settings, client: ChatClient, store: Store | None = None,
                    clock: Callable[[], float] = time.monotonic) -> tuple[dict, list[dict]]:
    """The eval's entry: the run's extract with its check and retry -> (process, steps). Raises
    RunFailed (with ``steps``) when the run would fail. ``store``: reserve and settle every call
    (a CapStore for ``--max-rub``)."""
    calls = Calls(Deps(client, store, settings, clock), label="eval")
    try:
        return extract(calls, mask_pii(text.strip())[0]), calls.steps
    except RunFailed as e:
        e.steps = calls.steps
        raise


def initial_state(run_id: str, text: str, created: float) -> dict:
    return {"id": run_id, "status": "running", "stage": "extract", "text": text, "created": created,
            "process": None, "improve": None, "steps": [], "totals": None, "error": None}


class Run:
    def __init__(self, run_id: str, text: str, deps: Deps, hold_id: int | None = None):
        self.id = run_id
        self.text = mask_pii(text)[0]  # app.main masks already; again here for scripts
        self.deps = deps
        self.s = deps.settings
        self.store = deps.store
        state = self.store.load_run(run_id)
        if state is None:
            state = initial_state(run_id, self.text, self.store.clock())
            self.store.create_run(run_id, state)
        self.state = state
        self.calls = Calls(deps, hold_id, self.on_step, run_id[:6])

    def save(self) -> None:
        self.store.save_run(self.id, self.state)

    def on_step(self, row: dict) -> None:
        self.state["steps"].append(row)
        self.save()

    def hold(self) -> None:
        """Hold the run's worst case unless app.main already did at admission."""
        if self.calls.hold_id is not None:
            return
        decision = self.store.hold(run_worst_rub(self.s))
        if not decision.allowed:
            raise RunFailed("budget_exhausted", decision.message)
        self.calls.hold_id = decision.reservation_id

    def release_hold(self) -> None:
        """What is left of the hold goes back to the day's budget. Idempotent, never raises."""
        hold, self.calls.hold_id = self.calls.hold_id, None
        if hold is None:
            return
        try:
            self.store.settle(hold, 0.0)
        except Exception:
            log.exception("run=%s could not release its hold", self.id[:6])

    def execute(self) -> None:
        self.hold()
        process = extract(self.calls, self.text)
        self.state.update(process=process, stage="improve")
        self.save()
        result = improve(self.calls, process)
        self.state.update(improve=result, stage="done", status="done", totals=totals(self.state["steps"]))
        self.release_hold()  # before the final save: the page asks for the budget right after it
        self.save()

    def fail(self, code: str, message: str | None) -> None:
        message = message or MESSAGES.get(code, MESSAGES["internal"]).format(s=int(self.s.run_timeout_s))
        self.state.update(status="failed", error={"code": code, "message": message},
                          totals=totals(self.state["steps"]))
        self.release_hold()
        self.save()


def run(run_id: str, text: str, deps: Deps, hold_id: int | None = None) -> dict:
    """Run the pipeline to the end; never raises (an unexpected error fails the run "internal").
    ``hold_id``: the run's worst case already held by the caller (app.main), released at the end.
    Returns the final state."""
    error = {"code": "internal", "message": MESSAGES["internal"]}
    try:
        r = Run(run_id, text, deps, hold_id)
    except Exception:  # the store is broken: try to give back the hold and end the run's row
        log.exception("run=%s could not start", run_id[:6])
        _abandon(deps.store, run_id, hold_id, error)
        return {"id": run_id, "status": "failed", "error": error}
    try:
        r.execute()
    except RunFailed as e:
        log.info("run=%s failed: %s", run_id[:6], e.code)
        _fail_quietly(r, e.code, e.message)
    except Exception:
        log.exception("run=%s internal error", run_id[:6])
        _fail_quietly(r, "internal", None)
    finally:
        r.release_hold()
    log.info("run=%s status=%s rub=%s", run_id[:6], r.state["status"], (r.state.get("totals") or {}).get("rub"))
    return r.state


def _abandon(store: Store, run_id: str, hold_id: int | None, error: dict) -> None:
    try:
        if hold_id is not None:
            store.settle(hold_id, 0.0)
        state = store.load_run(run_id)
        if state is not None and state.get("status") == "running":
            state.update(status="failed", error=error)
            store.save_run(run_id, state)
    except Exception:
        log.exception("run=%s could not be marked failed", run_id[:6])


def _fail_quietly(r: Run, code: str, message: str | None) -> None:
    try:
        r.fail(code, message)
    except Exception:
        log.exception("run=%s could not save the failure", r.id[:6])
