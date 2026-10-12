"""Client of the interview API (docs/SPEC-analyst.md, «Контракт API»). Stdlib only: the server is
reached over HTTP like the page does, nothing from app/ is imported."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:18106/api/process"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ApiError(Exception):
    """A non-2xx answer (``code`` from the error body, as in v1) or no answer at all (``unreachable``)."""

    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.status, self.code, self.message = status, code, message


class Api:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout_s: float = 120.0):
        self.base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # A local server is reached directly, not through a proxy from the environment.
        local = urllib.parse.urlsplit(self.base).hostname in LOOPBACK
        self.opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout_s) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(0, "unreachable", str(getattr(e, "reason", "") or type(e).__name__)) from None
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except ValueError:
            payload = None
        if not 200 <= status < 300:
            err = payload if isinstance(payload, dict) else {}
            raise ApiError(status, str(err.get("code") or f"http_{status}"), str(err.get("message") or ""))
        if not isinstance(payload, dict):
            raise ApiError(status, "bad_response", f"{method} {path}: не JSON-объект")
        return payload

    @staticmethod
    def _id(interview_id: str) -> str:
        return urllib.parse.quote(str(interview_id), safe="")

    def start(self, topic: str | None = None, text: str | None = None) -> dict:
        return self._call("POST", "/interviews", {"topic": topic, "text": text})

    def answer(self, interview_id: str, text: str) -> dict:
        return self._call("POST", f"/interviews/{self._id(interview_id)}/answers", {"text": text})

    def finish(self, interview_id: str) -> None:
        self._call("POST", f"/interviews/{self._id(interview_id)}/finish")

    def get(self, interview_id: str) -> dict:
        return self._call("GET", f"/interviews/{self._id(interview_id)}")
