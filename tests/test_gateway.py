import io
import json
import urllib.error

import pytest

from app import gateway
from app.gateway import Gateway, GatewayError
from app.schema import PROCESS


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answer(content="{}", usage=None, finish="stop"):
    body = {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return FakeResponse(json.dumps(body).encode())


@pytest.fixture
def sent(monkeypatch):
    calls, replies = [], []

    def urlopen(req, timeout):
        calls.append({"url": req.full_url, "body": json.loads(req.data), "timeout": timeout,
                      "auth": req.get_header("Authorization")})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    return calls, replies


def test_json_schema_and_usage(sent):
    calls, replies = sent
    replies.append(answer('{"niche": "x"}', {"prompt_tokens": 1000, "completion_tokens": 100}))
    chat = Gateway("http://gw.example/v1/", "secret").chat("gemini/gemini-3.1-flash-lite",
                                                          [{"role": "user", "content": "б"}], PROCESS, 1200,
                                                          timeout_s=40)
    body = calls[0]["body"]
    assert calls[0]["url"] == "http://gw.example/v1/chat/completions" and calls[0]["timeout"] == 40
    assert body["response_format"] == {"type": "json_schema", "json_schema": PROCESS}
    assert body["max_completion_tokens"] == 1200 and body["temperature"] == 0
    assert chat.content == '{"niche": "x"}' and chat.finish_reason == "stop" and chat.has_usage
    assert chat.cost_rub == pytest.approx((1000 * 34 + 100 * 203) / 1e6)


def test_plain_text_has_no_response_format_and_no_usage(sent):
    calls, replies = sent
    replies.append(answer("<html></html>", finish="length"))
    chat = Gateway("http://gw.example", "k", timeout_s=7).chat("m", [], None, 14000)
    assert "response_format" not in calls[0]["body"] and calls[0]["timeout"] == 7
    assert chat.finish_reason == "length" and not chat.has_usage and chat.cost_rub == 0


def test_one_wait_after_429_then_errors_without_secrets(sent):
    calls, replies = sent
    busy = urllib.error.HTTPError("http://gw.example", 429, "busy", {}, io.BytesIO(b"slow down"))
    replies += [busy, answer()]
    Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 2
    failed = len(calls)
    replies += [urllib.error.HTTPError("http://gw.example", 500, "x", {}, io.BytesIO(b"secret detail"))] * 2
    with pytest.raises(GatewayError) as e:
        Gateway("http://gw.example", "secret").chat("m", [], None, 10)
    assert e.value.detail == "chat HTTP 500" and "secret" not in str(e.value) and len(calls) == failed + 2
    replies.append(TimeoutError())
    with pytest.raises(GatewayError, match="TimeoutError"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)
    replies.append(FakeResponse(b"not json"))
    with pytest.raises(GatewayError, match="unexpected"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)


def test_transient_400_is_repeated_once(sent):
    # 08.10: Haiku got HTTP 400 on 2 of 4 identical calls, the same request passed a minute later
    calls, replies = sent
    replies += [urllib.error.HTTPError("http://gw.example", 400, "bad", {}, io.BytesIO(b"upstream")), answer()]
    Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 2
    replies += [urllib.error.HTTPError("http://gw.example", 401, "no", {}, io.BytesIO(b"key"))]
    with pytest.raises(GatewayError, match="HTTP 401"):
        Gateway("http://gw.example", "k").chat("m", [], None, 10)
    assert len(calls) == 3


def test_transcribe_sends_multipart_and_reads_usage(monkeypatch):
    sent = []

    def urlopen(req, timeout):
        sent.append({"url": req.full_url, "body": req.data, "timeout": timeout,
                     "type": req.get_header("Content-type"), "auth": req.get_header("Authorization")})
        return FakeResponse(json.dumps({"text": " Где-то 120 заявок. ", "usage": {
            "input_tokens": 240, "output_tokens": 30, "input_token_details": {"audio_tokens": 200}}}).encode())

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    audio = b"\x1aE\xdf\xa3" + bytes(range(256))
    heard = Gateway("http://gw.example/v1", "secret").transcribe("openai/gpt-4o-transcribe", audio,
                                                                 "audio/webm;codecs=opus", timeout_s=30)
    call = sent[0]
    assert call["url"] == "http://gw.example/v1/audio/transcriptions" and call["timeout"] == 30
    assert call["type"].startswith("multipart/form-data; boundary=") and call["auth"] == "Bearer secret"
    body = call["body"]
    assert b'name="model"\r\n\r\nopenai/gpt-4o-transcribe\r\n' in body and b'name="language"\r\n\r\nru\r\n' in body
    assert gateway.STT_PROMPT.encode() in body and b'filename="answer.webm"' in body
    assert b"Content-Type: audio/webm\r\n\r\n" + audio + b"\r\n--" in body
    assert heard.text == "Где-то 120 заявок." and heard.audio_tokens == 200 and heard.has_usage
    assert heard.cost_rub == pytest.approx((240 * 810 + 30 * 1350) / 1e6)


def test_transcribe_errors_and_no_usage(monkeypatch):
    replies = []

    def urlopen(req, timeout):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    replies.append(FakeResponse(json.dumps({"text": "да"}).encode()))
    heard = Gateway("http://gw.example", "k").transcribe("openai/gpt-4o-transcribe", b"x", "audio/ogg")
    assert heard.text == "да" and not heard.has_usage and heard.audio_tokens == 0
    replies += [urllib.error.HTTPError("http://gw.example", 500, "x", {}, io.BytesIO(b"secret detail"))] * 2
    with pytest.raises(GatewayError) as e:
        Gateway("http://gw.example", "secret").transcribe("m", b"x", "audio/ogg")
    assert e.value.detail == "stt HTTP 500" and "secret" not in str(e.value) and replies == []
    replies.append(FakeResponse(b"not json"))
    with pytest.raises(GatewayError, match="unexpected"):
        Gateway("http://gw.example", "k").transcribe("m", b"x", "audio/ogg")


@pytest.mark.parametrize("text, noise", [
    ("Где-то сто заявок в месяц", False), ("120", False), ("Thank you.", True), ("Продолжение следует...", True),
    ("Владелец бизнеса по-русски рассказывает", True), ("Субтитры подготовил редактор", True), ("", True),
    ("Владелец", False), ("Владелец бизнеса", False), ("Процесс", False)])
def test_is_noise(text, noise):
    assert gateway.is_noise(text) is noise
