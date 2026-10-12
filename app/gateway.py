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
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Protocol

from app.pricing import STT_PROMPT_TOKENS, call_cost_rub

# Per-model request settings, checked by probes in demo-arena and demo-voice (07–08.10).
MODEL_PARAMS: dict[str, dict] = {
    "timeweb/gpt-oss-120b": {"reasoning_effort": "low"},
    "timeweb/gemma4:31b": {"reasoning_effort": "none"},
}


class GatewayError(Exception):
    """The gateway failed. ``detail`` is safe to log. Whether a failed call was billed is
    unknown (a timeout may be), so callers settle the reserved worst case."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


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
