"""Scripted gateway (chat and speech), a fake clock and the handwritten model answers from
tests/fixtures/."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass
from pathlib import Path

from app.gateway import Chat, GatewayError, Transcript
from app.pricing import call_cost_rub

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str = "sales") -> dict:
    """``{"name", "title", "text", "extract", "improve"}``: a text and the answers a model gave to it."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Reply:
    content: str
    tokens_in: int = 1000
    tokens_out: int = 500
    ms: int = 20
    usage: bool = True
    finish: str = "stop"
    advance_s: float = 0.0  # moves the fake clock: a slow model


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class Heard:
    """A scripted recognition: the text and its usage (10 audio tokens a second)."""
    text: str
    seconds: float = 5
    usage: bool = True
    ms: int = 30
    details: bool = True  # usage.input_token_details.audio_tokens is there


class FakeGateway:
    def __init__(self, replies=(), clock: FakeClock | None = None, stt=()):
        """replies: strings, Reply, exceptions, or callables returning one of those (called at
        the moment of the call, e.g. to wait on an event). stt: the same for ``transcribe``, with
        strings or Heard."""
        self.replies = list(replies)
        self.stt = list(stt)
        self.clock = clock
        self.calls: list[dict] = []
        self.heard: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        with self.lock:
            self.calls.append({"model": model, "messages": [dict(m) for m in messages], "schema": schema,
                               "max_tokens": max_tokens, "timeout_s": timeout_s})
            if not self.replies:
                raise AssertionError("no more scripted replies")
            reply = self.replies.pop(0)
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply()
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            reply = Reply(reply)
        if self.clock is not None and reply.advance_s:
            self.clock.advance(reply.advance_s)
        tin, tout = (reply.tokens_in, reply.tokens_out) if reply.usage else (0, 0)
        return Chat(reply.content, tin, tout, reply.ms, call_cost_rub(model, tin, tout), reply.finish, reply.usage)

    def transcribe(self, model, audio, content_type, timeout_s=None):
        with self.lock:
            self.heard.append({"model": model, "bytes": len(audio), "audio": bytes(audio),
                               "content_type": content_type, "timeout_s": timeout_s})
            if not self.stt:
                raise AssertionError("no more scripted recognitions")
            reply = self.stt.pop(0)
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply()
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, str):
            reply = Heard(reply)
        audio_tokens = int(reply.seconds * 10)
        tin, tout = (audio_tokens + 40, int(reply.seconds * 4)) if reply.usage else (0, 0)
        return Transcript(reply.text, audio_tokens if reply.usage and reply.details else 0, reply.ms,
                          call_cost_rub(model, tin, tout), tin, tout, reply.usage)

    def steps(self) -> list[str]:
        """Which step each call was, by its schema name."""
        names = {"process": "extract", "improve": "improve", "draft": "draft", "question": "ask"}
        return [names[c["schema"]["name"]] for c in self.calls]

    def user(self, i: int) -> str:
        return self.calls[i]["messages"][-1]["content"]


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def extract_answer(name: str = "sales") -> dict:
    return copy.deepcopy(fixture(name)["extract"])


def improve_answer(name: str = "sales") -> dict:
    return copy.deepcopy(fixture(name)["improve"])


def step_of(answer: dict, sid: str) -> dict:
    return next(s for s in answer["steps"] if s["id"] == sid)


def happy(name: str = "sales") -> list[str]:
    return [dumps(extract_answer(name)), dumps(improve_answer(name))]


__all__ = ["FakeClock", "FakeGateway", "GatewayError", "Heard", "Reply", "dumps", "extract_answer", "fixture",
           "happy", "improve_answer", "step_of"]
