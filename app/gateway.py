"""OpenAI-compatible gateway (Timeweb AI Gateway): chat completions, with a strict JSON schema or
plain text, and speech to text (``audio/transcriptions``, as in demo-voice). Stdlib only.

Recognition gets a short Russian prompt: without it noise came back in other languages (demo-voice,
08.10). On an empty or cut recording the model may return the prompt itself as the transcript, or a
phrase it invents on silence; ``is_noise`` tells those apart. The audio is only in memory: it goes
into the request body and nowhere else.

No retries after a timeout: it could be billed twice. 429 is not billed: wait once and repeat
(the key is shared with other demos). 400 and 5xx are repeated once too: on 08.10 two of four
identical Haiku calls got HTTP 400 and the same request passed a minute later. The error body goes
to the log, never to the visitor. Errors never carry the key or the URL.

``AnthropicGateway`` is the Anthropic Messages API directly, with the same ``chat`` and the same
``Chat``; ``Router`` sends anthropic/* models there when the route is on and repeats a failed call
once through Timeweb (docs/SPEC.md, «Шлюзы»). A failure Anthropic may have billed (a timeout, a
broken connection, an unreadable answer) adds Anthropic's worst case to the fallback's cost.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.pricing import STT_PROMPT_TOKENS, anthropic_cost_rub, anthropic_worst_rub, call_cost_rub, prompt_chars

# Per-model request settings, checked by probes in demo-arena and demo-voice (07–08.10).
MODEL_PARAMS: dict[str, dict] = {
    "timeweb/gpt-oss-120b": {"reasoning_effort": "low"},
    "timeweb/gemma4:31b": {"reasoning_effort": "none"},
}


class GatewayError(Exception):
    """The gateway failed. ``detail`` is safe to log. Whether a failed call was billed is
    unknown (a timeout may be), so callers settle the reserved worst case. ``maybe_billed`` is
    False only when the call surely cost nothing (an error status, a request never sent)."""

    def __init__(self, detail: str, maybe_billed: bool = True):
        super().__init__(detail)
        self.detail = detail
        self.maybe_billed = maybe_billed


log = logging.getLogger("demo_process")
RETRY_CODES = {400, 429, 500, 502, 503, 504, 529}

STT_PROMPT = "Владелец бизнеса по-русски рассказывает, как устроен рабочий процесс."
# The page records webm/opus (Chrome, Firefox, Android) or mp4/aac (Safari, iPhone); the rest are
# what the contract allows (docs/SPEC-analyst.md, «Контракт API»).
AUDIO_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4", "audio/mpeg": "mp3",
               "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/x-m4a": "m4a",
               "audio/m4a": "m4a"}

_NORM = re.compile(r"[^0-9a-zа-яё]+")
_CYR_OR_DIGIT = re.compile(r"[а-яёА-ЯЁ0-9]")
# What recognition invents on noise and silence (demo-voice 08.10 and the usual ones in Russian).
_INVENTED = re.compile(r"продолжение следует|субтитр|спасибо за просмотр|подписывайтесь|редактор субтитров",
                       re.IGNORECASE)


def is_noise(text: str) -> bool:
    """A transcript that is not the visitor's speech: no Cyrillic and no digits, the prompt echoed
    back (three words of it or more: «Владелец» alone is a fair answer to «Кто это делает?»), or a
    phrase recognition invents on silence."""
    if not _CYR_OR_DIGIT.search(text) or _INVENTED.search(text):
        return True
    norm, prompt = _NORM.sub(" ", text.lower()).strip(), _NORM.sub(" ", STT_PROMPT.lower()).strip()
    return len(norm.split()) >= 3 and f" {norm} " in f" {prompt} "

@dataclass
class Chat:
    content: str
    tokens_in: int
    tokens_out: int
    ms: int
    cost_rub: float
    finish_reason: str | None = None
    # False when the gateway returned no usage: the cost is unknown, callers keep the reserve.
    has_usage: bool = True
    route: str = "timeweb"  # or "anthropic": which gateway answered


@dataclass
class Transcript:
    text: str
    audio_tokens: int  # 10 per second of speech (demo-voice probe 08.10)
    ms: int
    cost_rub: float
    tokens_in: int = 0
    tokens_out: int = 0
    has_usage: bool = True


class ChatClient(Protocol):
    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat: ...


class SpeechClient(Protocol):
    def transcribe(self, model: str, audio: bytes, content_type: str,
                   timeout_s: float | None = None) -> Transcript: ...


def _text(content) -> str:
    """Message content: a string, or a list of parts (some providers) joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return ""


@dataclass
class Gateway:
    base_url: str
    api_key: str
    timeout_s: float = 60.0
    wait_429_s: float = 2.0
    wait_retry_s: float = 1.0

    def _post(self, path: str, data: bytes, timeout_s: float,
              content_type: str = "application/json") -> tuple[int, bytes, int]:
        def once() -> tuple[int, bytes]:
            req = urllib.request.Request(self.base_url.rstrip("/") + "/" + path, data=data,
                                         headers={"Authorization": "Bearer " + self.api_key,
                                                  "Content-Type": content_type})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as r:
                    return 200, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()[:300]
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise GatewayError(type(e).__name__) from None

        started = time.perf_counter()
        code, body = once()
        if code in RETRY_CODES:
            log.warning("gateway HTTP %s, one retry: %s", code, body[:200].decode("utf-8", "replace"))
            time.sleep(self.wait_429_s if code == 429 else self.wait_retry_s)
            started = time.perf_counter()  # the wait is the gateway's queue, not the model's time
            code, body = once()
        if code != 200:
            log.warning("gateway HTTP %s: %s", code, body[:200].decode("utf-8", "replace"))
        return code, body, int((time.perf_counter() - started) * 1000)

    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat:
        """``schema`` None: a plain text answer (no response_format)."""
        payload = {"model": model, "messages": messages, "max_completion_tokens": max_tokens, "temperature": 0,
                   **(params or {})}
        if schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": schema}
        code, body, ms = self._post("chat/completions", json.dumps(payload).encode(),
                                    self.timeout_s if timeout_s is None else timeout_s)
        if code != 200:
            raise GatewayError(f"chat HTTP {code}")
        try:
            data = json.loads(body)
            choice = data["choices"][0]
            content = _text(choice["message"].get("content"))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise GatewayError("chat: unexpected response") from None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        return Chat(content, tin, tout, ms, call_cost_rub(model, tin, tout), choice.get("finish_reason"),
                    bool(tin or tout))

    def transcribe(self, model: str, audio: bytes, content_type: str,
                   timeout_s: float | None = None) -> Transcript:
        """Speech to text. No usage in the answer: the cost is unknown, callers keep the reserve."""
        base_type = content_type.split(";")[0].strip().lower()
        ext = AUDIO_TYPES.get(base_type, "webm")
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (("model", model), ("language", "ru"), ("response_format", "json"),
                              ("prompt", STT_PROMPT))]
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="answer.{ext}"\r\n'
                     f'Content-Type: {base_type if base_type in AUDIO_TYPES else "audio/webm"}\r\n\r\n'.encode())
        parts += [audio, f"\r\n--{boundary}--\r\n".encode()]
        code, body, ms = self._post("audio/transcriptions", b"".join(parts),
                                    self.timeout_s if timeout_s is None else timeout_s,
                                    f"multipart/form-data; boundary={boundary}")
        if code != 200:
            raise GatewayError(f"stt HTTP {code}")
        try:
            data = json.loads(body)
            text = str(data.get("text") or "")
        except (ValueError, AttributeError):
            raise GatewayError("stt: unexpected response") from None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        tin = int(usage.get("input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
        audio_tokens = int((usage.get("input_token_details") or {}).get("audio_tokens") or 0)
        return Transcript(text.strip(), audio_tokens, ms, call_cost_rub(model, tin or STT_PROMPT_TOKENS, tout),
                          tin, tout, bool(tin or tout))


ANTHROPIC_VERSION = "2023-06-01"
# Anthropic's stop_reason as the OpenAI-compatible finish_reason the rest of the code knows.
FINISH = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "stop", "max_tokens": "length",
          "refusal": "content_filter"}


@dataclass
class AnthropicGateway:
    """The Anthropic Messages API directly: ``chat`` as ``Gateway.chat``, ``model`` is the id at
    Anthropic. A schema is a forced tool (``tool_choice`` of type "tool"); its ``input`` goes on as
    a JSON string, as Timeweb returns the model's JSON. No sampling parameters: Haiku 5.5 answers
    any but the default with HTTP 400. No retries: ``Router`` repeats a failed call through Timeweb.
    The error body goes to the log without the key; errors never carry the key or the URL."""

    api_key: str = field(repr=False)
    usd_rub: float
    base_url: str = "https://api.anthropic.com"
    timeout_s: float = 60.0

    def _hide(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat:
        """``params`` (MODEL_PARAMS) are the OpenAI-compatible API's and are not sent."""
        system = "\n\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
        # an empty turn is a 400 there (a retry after an empty answer has one): it says nothing anyway
        turns = [{"role": m["role"], "content": _text(m.get("content"))} for m in messages
                 if m.get("role") in ("user", "assistant") and _text(m.get("content"))]
        payload = {"model": model, "max_tokens": max_tokens, "messages": turns}
        if system:
            payload["system"] = system
        if schema is not None:
            payload["tools"] = [{"name": schema["name"], "input_schema": schema["schema"]}]
            payload["tool_choice"] = {"type": "tool", "name": schema["name"]}
        started = time.perf_counter()
        try:  # a bad ANTHROPIC_BASE_URL is a ValueError here: it falls back to Timeweb like the rest
            req = urllib.request.Request(self.base_url.rstrip("/") + "/v1/messages",
                                         data=json.dumps(payload).encode(),
                                         headers={"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION,
                                                  "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout_s if timeout_s is None else timeout_s) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read()[:200].decode("utf-8", "replace")
            except (http.client.HTTPException, OSError):
                detail = "-"
            log.warning("anthropic HTTP %s: %s", e.code, self._hide(detail))
            raise GatewayError(f"anthropic HTTP {e.code}", maybe_billed=False) from None  # errors are not billed
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            # a timeout or a broken connection may come after the answer was billed; a bad URL is never sent
            raise GatewayError(f"anthropic {type(e).__name__}", maybe_billed=not isinstance(e, ValueError)) from None
        ms = int((time.perf_counter() - started) * 1000)
        try:
            data = json.loads(body)
            blocks = [b for b in data["content"] if isinstance(b, dict)]
            tool = next((b for b in blocks if b.get("type") == "tool_use"), None) if schema is not None else None
            if tool is not None:
                content = json.dumps(tool["input"], ensure_ascii=False)
            else:  # plain text, or no tool call at all (a refusal): the pipeline sees it is off format
                content = "".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            # no cache_control is sent, so the cache counts are 0; summed so nothing is lost
            tin = sum(int(usage.get(k) or 0) for k in
                      ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            tout = int(usage.get("output_tokens") or 0)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GatewayError("anthropic: unexpected response") from None
        stop = data.get("stop_reason")
        return Chat(content, tin, tout, ms, anthropic_cost_rub(model, tin, tout, self.usd_rub),
                    FINISH.get(stop, stop), bool(tin or tout), route="anthropic")


@dataclass
class Router:
    """Chat and speech over both gateways (docs/SPEC.md, «Шлюзы»). A model in ``routes`` (our
    name -> the id at Anthropic; app.config.Settings.anthropic_id decides) goes to Anthropic; any
    error there (network, timeout, 4xx, 5xx, 529) repeats the same call once through Timeweb. An
    error Anthropic may have billed adds its worst case to the fallback's cost, so the day's spend
    stays whole (the reserve holds both routes, app.pricing.call_worst_rub). The rest, and
    recognition, go to Timeweb as before. The route and why it fell back go to the log."""

    timeweb: Gateway
    anthropic: AnthropicGateway | None = None
    routes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings) -> "Router":
        routes = {model: settings.anthropic_id(model) for model, _ in settings.anthropic_models}
        routes = {model: model_id for model, model_id in routes.items() if model_id}
        anthropic = AnthropicGateway(settings.anthropic_api_key, settings.usd_rub,
                                     settings.anthropic_base_url) if routes else None
        return cls(Gateway(settings.base_url, settings.api_key), anthropic, routes)

    def route(self, model: str) -> str:
        return "anthropic" if self.anthropic is not None and model in self.routes else "timeweb"

    def chat(self, model: str, messages: list[dict], schema: dict | None, max_tokens: int,
             params: dict | None = None, timeout_s: float | None = None) -> Chat:
        if self.route(model) == "anthropic":
            try:
                chat = self.anthropic.chat(self.routes[model], messages, schema, max_tokens, params, timeout_s)
                log.info("chat model=%s route=anthropic", model)
                return chat
            except GatewayError as e:
                log.warning("chat model=%s route=timeweb: anthropic failed (%s)", model, e.detail)
                if e.maybe_billed:
                    chat = self.timeweb.chat(model, messages, schema, max_tokens, params, timeout_s)
                    chat.cost_rub += anthropic_worst_rub(self.routes[model], prompt_chars(messages), max_tokens,
                                                         self.anthropic.usd_rub)
                    return chat
        return self.timeweb.chat(model, messages, schema, max_tokens, params, timeout_s)

    def transcribe(self, model: str, audio: bytes, content_type: str,
                   timeout_s: float | None = None) -> Transcript:
        return self.timeweb.transcribe(model, audio, content_type, timeout_s)
