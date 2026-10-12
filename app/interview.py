"""The interview (docs/SPEC-analyst.md): the code picks every question (app.gaps), ASK_MODEL words it,
DRAFT_MODEL redraws the process after every answer, EXTRACT_MODEL and IMPROVE_MODEL build the final
Process and «Стало» over the transcript.

Pure orchestration like app.pipeline: the gateway (chat and speech), the store, the settings and the
clock come in ``Deps``, so tests and the eval (evals/) drive it on fakes or with a capped store;
app.interview_api is the HTTP layer over it.

A turn: the answer (text, or audio -> STT_MODEL; the audio is only in memory) -> contacts masked ->
numbers for the asked gap by code (app.understand) -> the draft over the whole transcript, checked
like v1 but lenient (app.checks.check_draft) -> numbers of the draft model that hold up -> the next
gap -> its wording without digits. «Не знаю» closes the gap without a draft call. A draft or a
wording that fails (off format, the gateway, the budget, the time) never fails the turn: the last
draft stays, the question falls back to the gap's template. Recognition that fails or hears nothing
is ``stt_failed``: the question is not used up.

Money: the interview holds its worst case (app.pricing.interview_worst_rub) from the start; every call
reserves its own worst case inside the hold and settles the real cost after, giving the rest back to
the hold: an interview never spends more than its worst case, whatever a visitor does. A question
takes at most ``MAX_STT_FAILS`` recognitions that heard nothing. The hold is released when the
interview ends: done, error, closed after ``interview_idle_s`` without an answer.

Step ids of the drafts and of the final Process are the model's: a number from an answer is kept
with the name of its step or role, and moves to the final step or role of the closest name.
"""

from __future__ import annotations

import logging
import secrets
import threading
import weakref

from app import checks, gaps, prompts, schema, understand
from app.checks import cut, normalize
from app.gateway import GatewayError, is_noise
from app.pii import mask_pii
from app.pipeline import MESSAGES, Calls, Deps, RunFailed, parse_answer, totals
from app.pricing import interview_worst_rub, stt_worst_rub

log = logging.getLogger("demo_process")

MIN_AUDIO_TOKENS = 8  # 10 per second: under ~0.8 s there is nothing said (demo-voice)
MAX_STT_FAILS = 2     # recognitions of one question that heard nothing; then only text
SIMILAR = 0.5         # names of the same step in a draft and in the final Process, trigram Dice
PUBLIC_QUESTION = ("n", "max", "text", "gap")

TEXTS = {
    "not_found": "Интервью не найдено: оно старше 30 суток или ссылка неверная.",
    "timeout": "Интервью {m} минут было без ответа и закрыто. Начните новое — записанные примеры работают и сейчас.",
    "finished": "Интервью уже закончено, отчёт — по ссылке.",
    "too_early": "Отчёт строится после {n} ответов: ответьте ещё на {left}.",
    "in_progress": "Предыдущий ответ ещё обрабатывается.",
    "stt_failed": "Не расслышал. Повторите, пожалуйста, или напишите ответ текстом.",
    "stt_error": "Распознавание речи сейчас не ответило. Повторите или напишите ответ текстом.",
    "stt_enough": "Голос на этот вопрос не распознаётся — напишите ответ текстом.",
    "budget_exhausted": "Дневной бюджет демо на сегодня закончился. Записанные примеры работают и сейчас.",
}


class InterviewError(Exception):
    """A request the interview refuses: ``code`` and ``message`` as the API's errors, ``status``
    the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


class _Lock:
    """A lock that can be weakly referenced (``threading.Lock`` cannot)."""

    def __init__(self):
        self.lock = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        return self.lock.acquire(blocking)

    def release(self) -> None:
        self.lock.release()

    def __enter__(self):
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


class Locks:
    """One lock per interview: a turn, a finish and a close never overlap. A lock lives while someone
    holds a reference to it, so the map does not grow with ids nobody uses."""

    def __init__(self):
        self.locks: weakref.WeakValueDictionary[str, _Lock] = weakref.WeakValueDictionary()
        self.guard = threading.Lock()

    def get(self, interview_id: str) -> _Lock:
        with self.guard:
            lock = self.locks.get(interview_id)
            if lock is None:
                lock = self.locks[interview_id] = _Lock()
            return lock


def public_question(q: dict | None) -> dict | None:
    return {k: q[k] for k in PUBLIC_QUESTION} if q else None


def view(state: dict) -> dict:
    """``GET interviews/{id}``: the contract's fields and ``error``."""
    return {"status": state["status"], "transcript": [{"q": t["q"], "a": t["a"]} for t in state["transcript"]],
            "process": state["process"], "improve": state["improve"], "assumptions": state["assumptions"],
            "answered": state["answered"], "steps": state["steps"], "totals": state["totals"],
            "error": state["error"]}


def visitor_text(state: dict) -> str:
    """The visitor's own words: the phrase and the answers. Quotes are looked for here only."""
    return "\n".join(x for x in [state.get("phrase") or ""] + [t["a"] for t in state["transcript"]] if x)


def transcript_text(state: dict, max_answer_chars: int) -> str:
    return prompts.transcript(state.get("topic"), state.get("phrase"), state["transcript"], max_answer_chars)


def _row(row: dict, n: int | None) -> dict:
    """A ``steps`` row of the interview: app.pipeline's row with ``name`` for ``step`` and the number of
    the answer it belongs to (0 — the first question, None — the final)."""
    out = {"name": row["step"], "n": n, **{k: row[k] for k in ("attempt", "ms", "rub", "model", "tokens_in",
                                                                "tokens_out", "ok")}}
    if row.get("error"):
        out["error"] = row["error"]
    return out


def _trigrams(text: str) -> set[str]:
    t = f"  {normalize(text)} "
    return {t[i:i + 3] for i in range(len(t) - 2)}


def similarity(a: str, b: str) -> float:
    x, y = _trigrams(a), _trigrams(b)
    return 2 * len(x & y) / (len(x) + len(y)) if x and y else 0.0


def _closest(name: str, old_id: str, candidates: dict[str, str]) -> str | None:
    """The id of the same thing in the final Process: the same id if the name is close, else the
    closest name; None if nothing is close."""
    if old_id in candidates and similarity(name, candidates[old_id]) >= SIMILAR:
        return old_id
    best = max(candidates, key=lambda c: similarity(name, candidates[c]), default=None)
    return best if best is not None and similarity(name, candidates[best]) >= SIMILAR else None


def carry_answers(assumptions: dict, answered: list[str], names: dict[str, str],
                  final: dict) -> tuple[dict, list[str], list[str]]:
    """Numbers by step and role from the drafts' ids to the final Process's: (assumptions, answered,
    keys that found no step or role)."""
    steps = {s["id"]: s["name"] for s in final["steps"] if s["kind"] in ("task", "decision")}
    roles = {r["id"]: r["name"] for r in final["roles"] if not r.get("external")}
    out = {k: v for k, v in assumptions.items() if k not in ("people", "minutes", "wait_hours")}
    out.update(people={}, minutes={}, wait_hours={})
    keys, lost = [], []
    for key in answered:
        if "." not in key:
            keys.append(key)
            continue
        old, field = key.split(".", 1)
        value = (assumptions.get(field) or {}).get(old)
        target = _closest(names.get(key, ""), old, roles if field == "people" else steps)
        if value is None or target is None:
            lost.append(key)
            continue
        out[field][target] = value
        new_key = f"{target}.{field}"
        keys = [k for k in keys if k != new_key] + [new_key]
    return out, keys, lost


class Interviews:
    def __init__(self, deps: Deps):
        self.deps = deps
        self.s = deps.settings
        self.store = deps.store
        self.locks = Locks()

    # ---------- start ----------

    def new(self, topic: str | None, text: str | None, hold_id: int | None, interview_id: str | None = None) -> dict:
        """A new interview's row, before its first question. ``hold_id``: its worst case held;
        ``interview_id``: a fixed id (scripts/dev_server.py's ready report), else a random one."""
        interview_id = interview_id or secrets.token_urlsafe(16)
        phrase = cut(" ".join(mask_pii(text)[0].split()), self.s.max_answer_chars) if text else None
        state = {"id": interview_id, "status": "asking", "created": self.store.clock(), "topic": topic,
                 "phrase": phrase or None, "question": None, "transcript": [], "asked": [], "draft": None,
                 "process": None, "improve": None, "assumptions": understand.apply({}, []), "answered": [],
                 "names": {}, "carried_lost": [], "steps": [], "totals": None, "hold_id": hold_id, "error": None}
        self.store.create_interview(interview_id, state)
        return state

    def first_question(self, interview_id: str) -> dict:
        """Ask the first question; returns the body of ``POST interviews``."""
        with self.locks.get(interview_id):
            state = self._load(interview_id)
            calls = Calls(self.deps, state["hold_id"], label=interview_id[:6], within_hold=True)
            flags = self._next_question(calls, state)
            state["steps"] += [_row(r, 0) for r in calls.steps]
            state["totals"] = totals(state["steps"])
            self.store.save_interview(interview_id, state)
        log.info("interview=%s started gap=%s template=%s", interview_id[:6], state["question"]["gap"],
                 flags["question_template"])
        return {"id": interview_id, "question": public_question(state["question"]), "process": None,
                "budget_left_rub": round(self.store.left_today(), 2)}

    def start(self, topic: str | None, text: str | None, interview_id: str | None = None) -> dict:
        """Hold the worst case, open and ask: the eval's entry (app.interview_api admits on its own)."""
        decision = self.store.hold(interview_worst_rub(self.s))
        if not decision.allowed:
            raise InterviewError("budget_exhausted", decision.message, 503)
        try:
            state = self.new(topic, text, decision.reservation_id, interview_id)
        except BaseException:
            self.store.settle(decision.reservation_id, 0.0)
            raise
        try:
            return self.first_question(state["id"])
        except BaseException:
            self.abandon(state["id"], {"code": "internal", "message": MESSAGES["internal"]})
            raise

    # ---------- a turn ----------

    def answer(self, interview_id: str, text: str | None = None, audio: bytes | None = None,
               content_type: str = "") -> dict:
        """One answer: text or audio. Returns the body of ``POST interviews/{id}/answers``."""
        lock = self.locks.get(interview_id)
        if not lock.acquire(blocking=False):
            raise InterviewError("bad_request", TEXTS["in_progress"], 409)
        try:
            state = self._asking(interview_id)
            q = state["question"]
            n = q["n"]
            rows: list[dict] = []
            flags = {"dont_know": False, "draft": "skipped", "question_template": False, "question_digits": 0,
                     "contacts_masked": 0}
            if audio is not None:
                heard, row = self._transcribe(state, audio, content_type, n)
                rows.append(row)
                text = heard
            masked, found = mask_pii(text or "")
            flags["contacts_masked"] = sum(found.values())
            answer_text = cut(" ".join(masked.split()), self.s.max_answer_chars)
            state["transcript"].append({"n": n, "gap": q["gap"], "q": q["text"], "a": answer_text})
            state["question"] = None

            field = q.get("field")
            understood = understand.from_answer(answer_text, q["gap"] if field else None, field, state["draft"])
            self._names(state, understood)
            flags["dont_know"] = not understood and understand.dont_know(answer_text, field)
            calls = Calls(self.deps, state["hold_id"], label=interview_id[:6], within_hold=True)
            if not flags["dont_know"]:
                draft, answers, flags["draft"] = self._draft(calls, state)
                if draft is not None:
                    state["draft"] = state["process"] = draft
                more = understand.from_model(answers, answer_text, state["draft"], {u["field"] for u in understood})
                self._names(state, more)
                understood += more
            state["assumptions"] = understand.apply(state["assumptions"], understood)
            for u in understood:
                state["answered"] = [k for k in state["answered"] if k != u["field"]] + [u["field"]]

            if n < self.s.max_questions:
                flags.update({k: v for k, v in self._next_question(calls, state).items() if k.startswith("question")})
            done = state["question"] is None
            if done:
                state["status"] = "building"
            rows += [_row(r, n) for r in calls.steps]
            state["steps"] += rows
            state["totals"] = totals(state["steps"])
            self.store.save_interview(interview_id, state)
        finally:
            lock.release()
        log.info("interview=%s answer=%s audio=%s understood=%s draft=%s done=%s", interview_id[:6], n,
                 audio is not None, len(understood), flags["draft"], done)
        return {"answer_text": answer_text, "understood": understood, "process": state["process"], "flags": flags,
                "question": public_question(state["question"]), "done": done,
                "steps": [{"name": r["name"], "ms": r["ms"], "rub": r["rub"]} for r in rows]}

    def _transcribe(self, state: dict, audio: bytes, content_type: str, n: int) -> tuple[str, dict]:
        """STT of one answer with its reservation; the audio goes nowhere else. Refusals leave the
        question as it was; a recognition that was paid is saved in ``steps``."""
        model = self.s.stt_model
        fails = state.setdefault("stt_fails", {})
        if fails.get(str(n), 0) >= MAX_STT_FAILS:
            raise InterviewError("stt_failed", TEXTS["stt_enough"], 422)
        worst = stt_worst_rub(model, len(audio), self.s.max_audio_s)
        hold_id = state["hold_id"]
        hold = (hold_id, worst) if hold_id is not None else None
        decision = self.store.reserve(worst, hold=hold, within_hold=hold is not None)
        if not decision.allowed:
            raise InterviewError("budget_exhausted", decision.message, 503)
        cost, started = worst, self.deps.clock()
        try:
            heard = self.deps.gateway.transcribe(model, audio, content_type, timeout_s=self.s.stt_timeout_s)
            if heard.has_usage:
                cost = heard.cost_rub
        except GatewayError as e:
            log.warning("interview=%s stt error: %s", state["id"][:6], e.detail)
            row = {"name": "stt", "n": n, "attempt": 1, "ms": int((self.deps.clock() - started) * 1000),
                   "rub": round(worst, 4), "model": model, "tokens_in": 0, "tokens_out": 0, "ok": False,
                   "error": "stt_failed"}
            fails[str(n)] = fails.get(str(n), 0) + 1
            self._keep_row(state, row)
            raise InterviewError("stt_failed", TEXTS["stt_error"], 502) from None
        finally:
            self.store.settle(decision.reservation_id, cost, hold_id)
        row = {"name": "stt", "n": n, "attempt": 1, "ms": heard.ms, "rub": round(cost, 4), "model": model,
               "tokens_in": heard.tokens_in, "tokens_out": heard.tokens_out, "ok": True}
        # the duration check needs the audio tokens: a gateway may not report them
        too_short = 0 < heard.audio_tokens < MIN_AUDIO_TOKENS
        if not heard.text or is_noise(heard.text) or too_short:
            row.update(ok=False, error="stt_failed")
            fails[str(n)] = fails.get(str(n), 0) + 1
            self._keep_row(state, row)
            raise InterviewError("stt_failed", TEXTS["stt_failed"], 422)
        return heard.text, row

    def _keep_row(self, state: dict, row: dict) -> None:
        state["steps"].append(row)
        state["totals"] = totals(state["steps"])
        self.store.save_interview(state["id"], state)

    def _draft(self, calls: Calls, state: dict) -> tuple[dict | None, list, str]:
        """The draft over the whole transcript: (checked draft or None, the model's ``answers``,
        "ok" | "failed")."""
        conf = self.s.step("draft")
        messages = prompts.draft_messages(transcript_text(state, self.s.max_answer_chars), state["draft"])
        try:
            chat, cost = calls.call("draft", messages, schema.DRAFT, 1)
            data = parse_answer(chat.content, schema.DRAFT)
            draft, errors = checks.check_draft(data, visitor_text(state)) if data else (None, [prompts.NOT_JSON])
            answers = data.get("answers") if data else []
            if errors:
                log.info("interview=%s draft errors: %s", state["id"][:6], "; ".join(errors))
            calls.record("draft", 1, conf.model, chat, cost, ok=draft is not None,
                         error=None if draft is not None else "model_format")
        except RunFailed as e:
            log.info("interview=%s draft failed: %s", state["id"][:6], e.code)
            return None, [], "failed"
        return draft, answers, "ok" if draft is not None else "failed"

    def _next_question(self, calls: Calls, state: dict) -> dict:
        """The next gap and its wording into ``state["question"]`` (None — nothing to ask)."""
        flags = {"question_template": False, "question_digits": 0}
        gap = gaps.next_gap(state["draft"], state["assumptions"], state["asked"], state["topic"], state["phrase"])
        if gap is None:
            state["question"] = None
            return flags
        conf = self.s.step("ask")
        messages = prompts.ask_messages(gaps.TOPICS.get(state["topic"] or "") or state["topic"], state["phrase"],
                                        (state["draft"] or {}).get("title"), gap.about, self.s.max_answer_chars)
        worded = None
        try:
            chat, cost = calls.call("ask", messages, schema.QUESTION, 1)
            data = parse_answer(chat.content, schema.QUESTION)
            worded = gaps.clean_question(mask_pii(data["text"])[0] if data else "", gap.template)
            calls.record("ask", 1, conf.model, chat, cost, ok=data is not None,
                         error=None if data is not None else "model_format")
        except RunFailed as e:
            log.info("interview=%s ask failed: %s", state["id"][:6], e.code)
        text, flags["question_digits"], flags["question_template"] = worded or gaps.clean_question("", gap.template)
        state["asked"].append(gap.key)
        state["question"] = {"n": len(state["transcript"]) + 1, "max": self.s.max_questions, "text": text,
                             "gap": gap.key, "field": gap.field}
        return flags

    def _names(self, state: dict, understood: list[dict]) -> None:
        """The name of the step or role a number is about, as the draft calls it now."""
        draft = state["draft"] or {}
        names = {s["id"]: s["name"] for s in draft.get("steps") or []}
        names.update({r["id"]: r["name"] for r in draft.get("roles") or []})
        for u in understood:
            if "." in u["field"]:
                state["names"][u["field"]] = names.get(u["field"].split(".")[0], "")

    # ---------- the end ----------

    def finish(self, interview_id: str) -> dict:
        """«Хватит — строй отчёт»: the interview goes to "building" (idempotent); app.interview_api
        or the caller runs ``build``."""
        with self.locks.get(interview_id):
            state = self._close_if_idle(self._load(interview_id))
            if state["status"] in ("building", "done"):
                return state
            if state["status"] != "asking":
                raise self._closed(state)
            answers = len(state["transcript"])
            if answers < self.s.min_answers_to_finish:
                raise InterviewError("bad_request", TEXTS["too_early"].format(
                    n=self.s.min_answers_to_finish, left=self.s.min_answers_to_finish - answers), 409)
            state.update(status="building", question=None)
            self.store.save_interview(interview_id, state)
        return state

    def build(self, interview_id: str) -> dict:
        """The final Process and «Стало» over the transcript (each with its retry), then the numbers
        carried to the final ids. Never raises; returns the final state."""
        state = self.store.load_interview(interview_id)
        if state is None or state["status"] != "building":
            return state
        steps_before = len(state["steps"])

        def on_step(row: dict) -> None:
            state["steps"].append(_row(row, None))
            state["totals"] = totals(state["steps"])
            self.store.save_interview(interview_id, state)

        calls = Calls(self.deps, state["hold_id"], on_step, interview_id[:6], within_hold=True)
        try:
            visitor = visitor_text(state)
            messages = prompts.interview_extract_messages(transcript_text(state, self.s.max_answer_chars),
                                                          state["draft"])
            final = calls.json_step("extract", messages, schema.PROCESS, lambda d: checks.check_process(d, visitor))
            state["process"] = final
            self.store.save_interview(interview_id, state)
            improve = calls.json_step("improve", prompts.improve_messages(final), schema.IMPROVE,
                                      lambda d: checks.check_improve(d, final))
            assumptions, answered, lost = carry_answers(state["assumptions"], state["answered"], state["names"],
                                                        final)
            state.update(improve=improve, assumptions=assumptions, answered=answered, carried_lost=lost,
                         status="done")
        except RunFailed as e:
            log.info("interview=%s final failed: %s", interview_id[:6], e.code)
            message = e.message or MESSAGES.get(e.code, MESSAGES["internal"]).format(s=int(self.s.run_timeout_s))
            state.update(status="error", error={"code": e.code, "message": message})
        except Exception:
            log.exception("interview=%s final internal error", interview_id[:6])
            state.update(status="error", error={"code": "internal", "message": MESSAGES["internal"]})
        finally:
            self._release(state)
            state["totals"] = totals(state["steps"])
            try:
                self.store.save_interview(interview_id, state)
            except Exception:
                log.exception("interview=%s could not save the final", interview_id[:6])
        log.info("interview=%s status=%s final_rub=%s", interview_id[:6], state["status"],
                 round(sum(r["rub"] for r in state["steps"][steps_before:]), 4))
        return state

    def get(self, interview_id: str) -> dict:
        """The interview as it is now (an idle one is closed first)."""
        return self._load(interview_id, expire=True)

    # ---------- closing ----------

    def expire_idle(self) -> int:
        """Close the interviews with no answer for ``interview_idle_s``: "error" with code "timeout",
        the hold released. Skips one whose turn is going on."""
        closed = 0
        for interview_id in self.store.idle_interviews(self.s.interview_idle_s):
            lock = self.locks.get(interview_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                state = self.store.load_interview(interview_id)
                if state is not None and self._close_if_idle(state)["status"] == "error":
                    closed += 1
            finally:
                lock.release()
        return closed

    def abandon(self, interview_id: str, error: dict) -> None:
        """An interview that could not start: "error", hold released. Never raises."""
        try:
            state = self.store.load_interview(interview_id)
            if state is not None:
                state.update(status="error", question=None, error=error)
                self._release(state)
                self.store.save_interview(interview_id, state)
        except Exception:
            log.exception("interview=%s could not be closed", interview_id[:6])

    def _release(self, state: dict) -> None:
        """What is left of the hold goes back to the day's budget. Never raises."""
        hold, state["hold_id"] = state.get("hold_id"), None
        if hold is None:
            return
        try:
            self.store.settle(hold, 0.0)
        except Exception:
            log.exception("interview=%s could not release its hold", state["id"][:6])

    # ---------- loading ----------

    def _load(self, interview_id: str, expire: bool = False) -> dict:
        if expire:
            self.expire_idle()
        state = self.store.load_interview(interview_id)
        if state is None:
            raise InterviewError("not_found", TEXTS["not_found"], 404)
        return state

    def _close_if_idle(self, state: dict) -> dict:
        """Under the interview's lock: close it if it waited for an answer too long ("error",
        "timeout", the hold released)."""
        if state["status"] == "asking" and state["id"] in self.store.idle_interviews(self.s.interview_idle_s):
            message = TEXTS["timeout"].format(m=int(self.s.interview_idle_s // 60))
            state.update(status="error", question=None, error={"code": "timeout", "message": message})
            self._release(state)
            self.store.save_interview(state["id"], state)
            log.info("interview=%s closed after %s s without an answer", state["id"][:6], int(self.s.interview_idle_s))
        return state

    def _asking(self, interview_id: str) -> dict:
        state = self._close_if_idle(self._load(interview_id))
        if state["status"] != "asking" or state["question"] is None:
            raise self._closed(state)
        return state

    @staticmethod
    def _closed(state: dict) -> InterviewError:
        error = state.get("error") or {}
        if state["status"] == "error" and error.get("code") in ("timeout", "restart"):
            return InterviewError(error["code"], error["message"], 409)
        if state["status"] == "error":
            return InterviewError("bad_request", error.get("message") or TEXTS["finished"], 409)
        return InterviewError("bad_request", TEXTS["finished"], 409)
