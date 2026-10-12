"""HTTP routes of the interview (docs/SPEC-analyst.md, «Контракт API»), under /api/process/:

- ``POST interviews`` ``{"topic", "text"}`` -> 201 ``{"id", "question", "process": null, "budget_left_rub"}``;
- ``POST interviews/{id}/answers`` JSON ``{"text"}`` or multipart with the audio (the part named
  ``audio``, or the first part with an audio type or a file name) -> 200 the turn (app.interview);
- ``POST interviews/{id}/finish`` -> 202 ``{"status": "building"}``, then ``GET``;
- ``GET interviews/{id}`` -> the interview, also the report by its link for ``retention_days``.

Admission as v1 runs: at most MAX_CONCURRENT_INTERVIEWS asking or building at once (429 ``busy``),
the worst case held at once (503 ``budget_exhausted``), INTERVIEWS_PER_HOUR per client (429
``rate_limited``). The audio is read into memory with a size limit and parsed here, never spooled
to a file (Starlette's form parser would spool, and it needs python-multipart).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.gateway import AUDIO_TYPES
from app.gaps import TOPICS
from app.interview import InterviewError, Interviews, view
from app.netutil import client_ip, rate_limit_key
from app.pricing import interview_worst_rub

log = logging.getLogger("demo_process")
INTERVIEW_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")
BUSY_RETRY_S = 60
MULTIPART_SLACK = 16 * 1024  # the multipart headers around the audio
EXTENSIONS = {"webm": "audio/webm", "weba": "audio/webm", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
              "mp4": "audio/mp4", "m4a": "audio/x-m4a", "mp3": "audio/mpeg", "mpeg": "audio/mpeg", "wav": "audio/wav"}


def _error(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, **extra}, status_code=status)


def _refused(e: InterviewError) -> JSONResponse:
    return _error(e.status, e.code, e.message)


def body_limit(settings) -> int:
    """The largest request body of an answer: the audio and its multipart headers."""
    return settings.max_audio_bytes + MULTIPART_SLACK


async def read_body(request: Request, limit: int) -> bytes | None:
    """The body in memory, or None as soon as it is longer than ``limit``."""
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart(body: bytes, content_type: str) -> list[dict]:
    """``multipart/form-data`` parts: ``{"name", "filename", "type", "data"}``. Bytes only, no files."""
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        return []
    delimiter = b"--" + m[1].strip().encode("latin-1")
    parts = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk[2:] if chunk.startswith(b"\r\n") else chunk
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        data = data[:-2] if data.endswith(b"\r\n") else data
        headers = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition)
        filename = re.search(r'filename="([^"]*)"', disposition)
        parts.append({"name": name[1] if name else "", "filename": filename[1] if filename else None,
                      "type": headers.get("content-type", "").split(";")[0].strip().lower(), "data": data})
    return parts


def audio_part(parts: list[dict]) -> tuple[bytes, str] | None:
    """The recording and its type: the part named ``audio`` or the first part that is a file or audio;
    the type from the part or from the file's extension."""
    candidates = [p for p in parts if p["name"] == "audio"] + [p for p in parts if p["name"] != "audio"
                                                                 and (p["filename"] or p["type"].startswith("audio/"))]
    for p in candidates:
        kind = p["type"] if p["type"] in AUDIO_TYPES else None
        if kind is None and p["filename"] and "." in p["filename"]:
            kind = EXTENSIONS.get(p["filename"].rsplit(".", 1)[1].lower())
        if kind:
            return p["data"], kind
        return None
    return None


def add_interview_routes(app: FastAPI, prefix: str, settings, store, client, interviews: Interviews) -> None:
    worst = interview_worst_rub(settings)
    admission = threading.Lock()
    builder = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_interviews),
                                 thread_name_prefix="interview")
    building: set[str] = set()
    building_lock = threading.Lock()
    limit = body_limit(settings)
    app.state.interviews = interviews

    def visitor_ip(request: Request) -> str:
        return client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"),
                         settings.trusted_proxies)

    def build_later(interview_id: str) -> None:
        with building_lock:
            if interview_id in building:
                return
            building.add(interview_id)

        def go():
            try:
                interviews.build(interview_id)
            finally:
                with building_lock:
                    building.discard(interview_id)

        builder.submit(go)

    def start(topic: str | None, text: str | None, ip: str):
        with admission:
            interviews.expire_idle()
            if store.active_interviews() >= settings.max_concurrent_interviews:
                return _error(429, "busy", f"Сервис занят: одновременно идёт не больше "
                                           f"{settings.max_concurrent_interviews} интервью. Попробуйте через "
                                           "пару минут или посмотрите записанный пример.", retry_after_s=BUSY_RETRY_S)
            hold = store.hold(worst)
            if not hold.allowed:
                return _error(503, "budget_exhausted", store.budget_message())
            opened = False
            try:
                decision = store.start("interview|" + rate_limit_key(ip), settings.interviews_per_hour, "интервью")
                if not decision.allowed:
                    return _error(429, decision.code, decision.message, retry_after_s=decision.retry_after_s)
                state = interviews.new(topic, text, hold.reservation_id)
                opened = True
            finally:
                if not opened:
                    store.settle(hold.reservation_id, 0.0)
        try:
            body = interviews.first_question(state["id"])
        except Exception:
            log.exception("interview=%s could not ask the first question", state["id"][:6])
            interviews.abandon(state["id"], {"code": "internal", "message": "Внутренняя ошибка интервью."})
            return _error(500, "internal", "Внутренняя ошибка интервью. Попробуйте ещё раз.")
        return JSONResponse(body, status_code=201)

    @app.post(f"{prefix}/interviews")
    async def create_interview(request: Request):
        bad = _error(422, "bad_request", f"Нужна тема ({', '.join(TOPICS)}) или своя фраза до "
                                         f"{settings.max_answer_chars} знаков.")
        try:
            data = json.loads(await request.body() or b"{}")
        except ValueError:
            return bad
        if not isinstance(data, dict) or set(data) - {"topic", "text"}:
            return bad
        topic, text = data.get("topic"), data.get("text")
        if topic is not None and (not isinstance(topic, str) or topic not in TOPICS):
            return bad
        if text is not None and (not isinstance(text, str) or len(text.strip()) > settings.max_answer_chars):
            return bad
        text = text.strip() if text else None
        if topic is None and not text:
            return bad
        if client is None:
            return _error(503, "offline", "Живой режим выключен. Записанные примеры работают.")
        return await run_in_threadpool(start, topic, text, visitor_ip(request))

    @app.post(f"{prefix}/interviews/{{interview_id}}/answers")
    async def answer(interview_id: str, request: Request):
        if not INTERVIEW_ID.fullmatch(interview_id):
            return _error(404, "not_found", "Интервью не найдено: оно старше 30 суток или ссылка неверная.")
        if client is None:
            return _error(503, "offline", "Живой режим выключен. Записанные примеры работают.")
        ctype = (request.headers.get("content-type") or "").lower()
        body = await read_body(request, limit)
        if body is None:
            return _error(413, "too_long", f"Запись больше {settings.max_audio_bytes // 1024} КБ: ответ — до "
                                           f"{settings.max_audio_s} секунд.")
        text = audio = kind = None
        if ctype.startswith("application/json"):
            try:
                text = json.loads(body).get("text")
            except (ValueError, AttributeError):
                text = None
            if not isinstance(text, str) or not text.strip() or len(text.strip()) > settings.max_answer_chars:
                return _error(422, "bad_request", f"Нужен ответ от 1 до {settings.max_answer_chars} знаков.")
            text = text.strip()
        elif ctype.startswith("multipart/form-data"):
            found = audio_part(parse_multipart(body, request.headers.get("content-type") or ""))
            if found is None:
                return _error(422, "bad_request", "Нужна запись голоса: webm, ogg, mp4, mpeg, wav или m4a.")
            audio, kind = found
            if not audio:
                return _error(422, "bad_request", "Пустая запись.")
            if len(audio) > settings.max_audio_bytes:
                return _error(413, "too_long", f"Запись больше {settings.max_audio_bytes // 1024} КБ: ответ — до "
                                               f"{settings.max_audio_s} секунд.")
        else:
            return _error(422, "bad_request", "Нужен ответ текстом (JSON) или запись голоса (multipart).")
        del body
        try:
            result = await run_in_threadpool(interviews.answer, interview_id, text, audio, kind or "")
        except InterviewError as e:
            return _refused(e)
        if result["done"]:
            build_later(interview_id)
        return result

    @app.post(f"{prefix}/interviews/{{interview_id}}/finish")
    def finish(interview_id: str):
        if not INTERVIEW_ID.fullmatch(interview_id):
            return _error(404, "not_found", "Интервью не найдено: оно старше 30 суток или ссылка неверная.")
        try:
            state = interviews.finish(interview_id)
        except InterviewError as e:
            return _refused(e)
        if state["status"] == "building":
            build_later(interview_id)
        return JSONResponse({"status": state["status"]}, status_code=202)

    @app.get(f"{prefix}/interviews/{{interview_id}}")
    def get_interview(interview_id: str):
        if not INTERVIEW_ID.fullmatch(interview_id):
            return _error(404, "not_found", "Интервью не найдено: оно старше 30 суток или ссылка неверная.")
        try:
            state = interviews.get(interview_id)
        except InterviewError as e:
            return _refused(e)
        if state["status"] == "building":
            build_later(interview_id)  # a no-op while its build runs; restarts one a crash left behind
        return view(state)
