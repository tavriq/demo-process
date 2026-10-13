"""Anthropic directly and the router over both gateways (docs/SPEC.md, «Шлюзы»). Fakes only: the
HTTP layer is a scripted urlopen, Timeweb is tests.fakes.FakeGateway."""

import io
import json
import logging
import math
import secrets
import urllib.error
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import gateway, schema
from app.config import Settings, parse_anthropic_models
from app.gateway import AnthropicGateway, GatewayError, Router
from app.main import create_app
from app.pipeline import Deps, parse_answer, run
from app.pricing import (ANTHROPIC_TOOL_TOKENS, anthropic_cost_rub, anthropic_worst_rub, call_cost_rub,
                         call_worst_rub, interview_step_worst_rub, interview_worst_rub, messages_worst_rub,
                         prompt_chars, run_worst_rub)
from app.store import Store
from tests.fakes import FakeGateway, Reply, extract_answer, fixture, happy, improve_answer

KEY = "sk-ant-test-" + "x" * 24
HAIKU = "anthropic/claude-haiku-5-5"
ON = {"LLM_API_KEY": "tw-key", "LLM_BASE_URL": "http://tw.example/v1", "ANTHROPIC_API_KEY": KEY,
      "ANTHROPIC_BASE_URL": "http://an.example/", "ANTHROPIC_MODELS": f"{HAIKU}=claude-haiku-5-5", "USD_RUB": "90"}
MESSAGES = [{"role": "system", "content": "Ты аналитик."}, {"role": "user", "content": "Опиши процесс."}]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def message(blocks, usage=None, stop="tool_use"):
    body = {"type": "message", "role": "assistant", "content": blocks, "stop_reason": stop,
            "usage": {"input_tokens": 2000, "output_tokens": 1000} if usage is None else usage}
    return FakeResponse(json.dumps(body).encode())


def tool_use(value, name="process"):
    return {"type": "tool_use", "id": "toolu_1", "name": name, "input": value}


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("http://an.example/v1/messages", code, "x", {}, io.BytesIO(body))


@pytest.fixture
def sent(monkeypatch):
    """Scripted urlopen: every request is kept; the replies are responses or exceptions."""
    calls, replies = [], []

    def urlopen(req, timeout):
        calls.append({"url": req.full_url, "body": json.loads(req.data), "timeout": timeout,
                      "headers": {k.lower(): v for k, v in req.header_items()}})
        reply = replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gateway.time, "sleep", lambda s: None)
    return calls, replies


def anthropic(timeout_s=60.0):
    return AnthropicGateway(KEY, 90.0, "http://an.example/", timeout_s)


def router(timeweb_replies=()):
    tw = FakeGateway(timeweb_replies)
    return Router(tw, anthropic(), {HAIKU: "claude-haiku-5-5"}), tw


# ---------- AnthropicGateway ----------

def test_request_shape(sent):
    calls, replies = sent
    replies.append(message([tool_use({})]))
    messages = [*MESSAGES, {"role": "assistant", "content": "{}"}, {"role": "user", "content": "Исправь."}]
    anthropic().chat("claude-haiku-5-5", messages, schema.PROCESS, 6000, {"reasoning_effort": "low"}, timeout_s=40)
    call = calls[0]
    assert call["url"] == "http://an.example/v1/messages" and call["timeout"] == 40
    assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["headers"]["content-type"] == "application/json" and "authorization" not in call["headers"]
    assert call["body"] == {
        "model": "claude-haiku-5-5", "max_tokens": 6000, "system": "Ты аналитик.",
        "messages": [{"role": "user", "content": "Опиши процесс."}, {"role": "assistant", "content": "{}"},
                     {"role": "user", "content": "Исправь."}],
        "tools": [{"name": "process", "input_schema": schema.PROCESS["schema"]}],
        "tool_choice": {"type": "tool", "name": "process"}}


def test_plain_text_has_no_tools_and_empty_turns_are_dropped(sent):
    calls, replies = sent
    replies.append(message([{"type": "thinking", "thinking": "", "signature": "s"},
                            {"type": "text", "text": "Готово"}, {"type": "text", "text": "."}], stop="max_tokens"))
    # a retry after an empty answer carries an empty assistant turn: Anthropic answers it with 400
    messages = [{"role": "system", "content": "a"}, {"role": "system", "content": "b"},
                {"role": "user", "content": "в"}, {"role": "assistant", "content": ""}, {"role": "user", "content": "г"}]
    chat = anthropic(timeout_s=7).chat("claude-haiku-5-5", messages, None, 300)
    body = calls[0]["body"]
    assert "tools" not in body and "tool_choice" not in body and "temperature" not in body
    assert body["system"] == "a\n\nb" and calls[0]["timeout"] == 7
    assert body["messages"] == [{"role": "user", "content": "в"}, {"role": "user", "content": "г"}]
    assert chat.content == "Готово." and chat.finish_reason == "length" and chat.route == "anthropic"


def test_tool_input_goes_on_as_the_json_the_pipeline_reads(sent):
    _, replies = sent
    answer = extract_answer()
    replies.append(message([{"type": "text", "text": "Вот схема."}, tool_use(answer)],
                           {"input_tokens": 1800, "output_tokens": 900, "cache_read_input_tokens": 200}))
    chat = anthropic().chat("claude-haiku-5-5", MESSAGES, schema.PROCESS, 6000)
    assert parse_answer(chat.content, schema.PROCESS) == answer and answer["title"] in chat.content
    assert (chat.tokens_in, chat.tokens_out, chat.has_usage) == (2000, 900, True)
    assert chat.cost_rub == pytest.approx((2000 * 0.10 + 900 * 0.50) * 90 / 1e6)
    assert chat.finish_reason == "stop" and chat.route == "anthropic"


def test_no_tool_call_and_no_usage(sent):
    """A refusal is HTTP 200 without the tool call: off format for the pipeline, no fallback."""
    _, replies = sent
    replies.append(message([], usage={}, stop="refusal"))
    chat = anthropic().chat("claude-haiku-5-5", MESSAGES, schema.PROCESS, 6000)
    assert chat.content == "" and chat.finish_reason == "content_filter" and not chat.has_usage
    assert parse_answer(chat.content, schema.PROCESS) is None


def test_errors_carry_no_key(sent, caplog):
    _, replies = sent
    caplog.set_level(logging.INFO, logger="demo_process")
    replies.append(http_error(401, f'{{"error": {{"message": "bad x-api-key {KEY}"}}}}'.encode()))
    with pytest.raises(GatewayError) as e:
        anthropic().chat("claude-haiku-5-5", MESSAGES, schema.PROCESS, 6000)
    assert e.value.detail == "anthropic HTTP 401" and KEY not in str(e.value)
    assert "anthropic HTTP 401" in caplog.text and "bad x-api-key ***" in caplog.text
    for error, detail in ((TimeoutError(), "anthropic TimeoutError"),
                          (urllib.error.URLError("an.example refused"), "anthropic URLError"),
                          (FakeResponse(b"not json"), "anthropic: unexpected response"),
                          (FakeResponse(b'{"content": [{"type": "tool_use"}]}'), "anthropic: unexpected response")):
        replies.append(error)
        with pytest.raises(GatewayError) as e:
            anthropic().chat("claude-haiku-5-5", MESSAGES, schema.PROCESS, 6000)
        assert e.value.detail == detail and "an.example" not in str(e.value)
    assert KEY not in caplog.text
    assert KEY not in repr(anthropic()) and KEY not in repr(Settings.from_env(ON))
    assert KEY not in repr(Router.from_settings(Settings.from_env(ON)))


# ---------- the router ----------

def test_router_sends_the_mapped_model_to_anthropic(sent, caplog):
    calls, replies = sent
    caplog.set_level(logging.INFO, logger="demo_process")
    replies.append(message([tool_use(improve_answer(), "improve")]))
    r, tw = router()
    chat = r.chat(HAIKU, MESSAGES, schema.IMPROVE, 3000, None, 45)
    assert chat.route == "anthropic" and tw.calls == [] and calls[0]["body"]["model"] == "claude-haiku-5-5"
    assert calls[0]["timeout"] == 45 and f"model={HAIKU} route=anthropic" in caplog.text
    assert chat.cost_rub == pytest.approx(anthropic_cost_rub("claude-haiku-5-5", 2000, 1000, 90))


@pytest.mark.parametrize("error, reason, billed", [
    (http_error(529, b'{"type": "overloaded_error"}'), "anthropic HTTP 529", False),
    (TimeoutError(), "anthropic TimeoutError", True),
    (http_error(400), "anthropic HTTP 400", False),
    (http_error(500), "anthropic HTTP 500", False),
    (urllib.error.URLError("down"), "anthropic URLError", True),
    (FakeResponse(b"<html>"), "anthropic: unexpected response", True),
])
def test_any_anthropic_error_repeats_the_call_once_through_timeweb(sent, caplog, error, reason, billed):
    """An error status is not billed; a timeout, a broken connection or an unreadable answer may be:
    the fallback then also pays Anthropic's worst case."""
    calls, replies = sent
    caplog.set_level(logging.INFO, logger="demo_process")
    replies.append(error)
    r, tw = router([Reply("{}", tokens_in=1000, tokens_out=500)])
    chat = r.chat(HAIKU, MESSAGES, schema.PROCESS, 6000, None, 40)
    assert len(calls) == 1 and replies == []  # Anthropic once, no retry there
    assert tw.calls == [{"model": HAIKU, "messages": MESSAGES, "schema": schema.PROCESS, "max_tokens": 6000,
                         "timeout_s": 40}]
    extra = anthropic_worst_rub("claude-haiku-5-5", prompt_chars(MESSAGES), 6000, 90) if billed else 0
    assert chat.route == "timeweb" and chat.cost_rub == pytest.approx(call_cost_rub(HAIKU, 1000, 500) + extra)
    assert chat.cost_rub <= call_worst_rub(HAIKU, prompt_chars(MESSAGES), 6000, Settings.from_env(ON))
    assert f"model={HAIKU} route=timeweb: anthropic failed ({reason})" in caplog.text and KEY not in caplog.text


def test_a_broken_connection_or_base_url_falls_back_too(monkeypatch):
    def urlopen(req, timeout):
        if not req.full_url.startswith("http"):
            raise ValueError("unknown url type")
        raise gateway.http.client.IncompleteRead(b"")

    monkeypatch.setattr(gateway.urllib.request, "urlopen", urlopen)
    for base_url, billed in (("http://an.example", True), ("an.example", False)):  # a bad URL is never sent
        r, tw = router(["{}"])
        r.anthropic.base_url = base_url
        chat = r.chat(HAIKU, MESSAGES, schema.PROCESS, 6000)
        assert chat.route == "timeweb" and len(tw.calls) == 1
        assert (chat.cost_rub > call_cost_rub(HAIKU, 1000, 500)) is billed  # FakeGateway's default usage


def test_fallback_that_fails_too_is_the_timeweb_error(sent):
    _, replies = sent
    replies.append(http_error(529))
    r, tw = router([GatewayError("chat HTTP 500")])
    with pytest.raises(GatewayError, match="chat HTTP 500"):
        r.chat(HAIKU, MESSAGES, schema.PROCESS, 6000)
    assert len(tw.calls) == 1


def test_other_models_and_speech_go_to_timeweb(sent):
    calls, _ = sent
    r, tw = router(["{}"])
    tw.stt.append("Сто заявок в месяц")
    assert r.chat("gemini/gemini-3.1-flash-lite", MESSAGES, schema.QUESTION, 300).route == "timeweb"
    assert r.transcribe(HAIKU, b"x", "audio/webm").text == "Сто заявок в месяц"
    assert calls == [] and len(tw.calls) == 1 and len(tw.heard) == 1
    assert (r.route(HAIKU), r.route("gemini/gemini-3.1-flash-lite")) == ("anthropic", "timeweb")


# ---------- settings: when the route is on ----------

def test_settings_turn_the_route_on():
    s = Settings.from_env(ON)
    assert s.anthropic_on and s.anthropic_id(HAIKU) == "claude-haiku-5-5" and s.usd_rub == 90
    assert s.anthropic_base_url == "http://an.example/" and s.anthropic_id("gemini/gemini-3.1-flash-lite") is None
    r = Router.from_settings(s)
    assert r.routes == {HAIKU: "claude-haiku-5-5"} and r.anthropic.base_url == "http://an.example/"
    assert r.anthropic.usd_rub == 90 and r.timeweb.base_url == "http://tw.example/v1"
    assert s.routes() == {HAIKU: "anthropic", "gemini/gemini-3.1-flash-lite": "timeweb",
                          "openai/gpt-4o-transcribe": "timeweb"}


def test_defaults_keep_the_route_off():
    d = Settings.from_env({})
    assert not d.anthropic_on and d.anthropic_base_url == "https://api.anthropic.com"
    assert (d.anthropic_api_key, d.anthropic_models, d.usd_rub) == ("", (), 0)
    r = Router.from_settings(d)
    assert r.anthropic is None and r.routes == {} and r.route(HAIKU) == "timeweb"


@pytest.mark.parametrize("change", [
    {"ANTHROPIC_API_KEY": ""}, {"ANTHROPIC_MODELS": ""}, {"USD_RUB": ""}, {"USD_RUB": "0"},
    {"ANTHROPIC_MODELS": f"{HAIKU}=claude-opus-5-5"},  # an id without a price
])
def test_route_is_off_without_key_mapping_rate_or_price(sent, change):
    calls, replies = sent
    s = Settings.from_env({**ON, **change})
    assert s.anthropic_id(HAIKU) is None and s.routes()[HAIKU] == "timeweb"
    r = Router.from_settings(s)
    assert r.anthropic is None and r.routes == {}
    replies.append(FakeResponse(json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()))
    chat = r.chat(HAIKU, MESSAGES, schema.PROCESS, 6000)
    assert calls[0]["url"] == "http://tw.example/v1/chat/completions" and chat.route == "timeweb"
    assert calls[0]["headers"]["authorization"] == "Bearer tw-key" and "x-api-key" not in calls[0]["headers"]
    assert run_worst_rub(s) == run_worst_rub(Settings.from_env({}))


def test_mapping_is_parsed_strictly():
    assert parse_anthropic_models(f" {HAIKU} = claude-haiku-5-5 , anthropic/claude-haiku-4-5=claude-haiku-4-5,") == (
        (HAIKU, "claude-haiku-5-5"), ("anthropic/claude-haiku-4-5", "claude-haiku-4-5"))
    assert parse_anthropic_models("") == ()
    for bad in (HAIKU, f"{HAIKU}=", "gemini/gemini-3.1-flash-lite=claude-haiku-5-5", "claude-haiku-5-5=x"):
        with pytest.raises(ValueError, match="ANTHROPIC_MODELS"):
            parse_anthropic_models(bad)


# ---------- money ----------

def test_worst_case_is_both_routes():
    """A timeout at Anthropic may be billed and the call then goes to Timeweb: the reserve holds both."""
    off = Settings.from_env({})
    on = Settings.from_env(ON)
    tokens_in = math.ceil(3001 / 2)
    assert call_worst_rub(HAIKU, 3001, 6000, on) == pytest.approx(
        call_cost_rub(HAIKU, tokens_in, 6000)
        + anthropic_cost_rub("claude-haiku-5-5", tokens_in + ANTHROPIC_TOOL_TOKENS, 6000, 90))
    assert anthropic_worst_rub("claude-haiku-5-5", 3001, 6000, 90) == pytest.approx(
        anthropic_cost_rub("claude-haiku-5-5", tokens_in + ANTHROPIC_TOOL_TOKENS, 6000, 90))
    assert call_worst_rub(HAIKU, 3001, 6000) == call_cost_rub(HAIKU, tokens_in, 6000)
    assert messages_worst_rub(HAIKU, MESSAGES, 6000, on) > messages_worst_rub(HAIKU, MESSAGES, 6000, off)
    assert run_worst_rub(on) > run_worst_rub(off) and interview_worst_rub(on) > interview_worst_rub(off)
    # draft and ask run on gemini: always Timeweb
    assert interview_step_worst_rub(on, "draft") == interview_step_worst_rub(off, "draft")


def test_tool_overhead_covers_the_largest_schema():
    largest = max(len(json.dumps({"name": s["name"], "input_schema": s["schema"]}))
                  for s in (schema.PROCESS, schema.IMPROVE, schema.DRAFT, schema.QUESTION))
    assert ANTHROPIC_TOOL_TOKENS >= 588 + math.ceil(largest / 2)  # Haiku 4.5's forced-tool prompt


def test_a_run_pays_the_route_each_call_took(sent, tmp_path):
    """Extract went to Anthropic, improve fell back to Timeweb: each step costs its own route."""
    calls, replies = sent
    settings = replace(Settings.from_env(ON), var_dir=tmp_path)
    replies += [message([tool_use(extract_answer())], {"input_tokens": 3000, "output_tokens": 1500}),
                http_error(529)]
    tw = FakeGateway([Reply(happy()[1], tokens_in=1200, tokens_out=700)])
    r = Router.from_settings(settings)
    r.timeweb = tw
    store = Store(tmp_path / "s.sqlite3", 15, 3)
    state = run(secrets.token_urlsafe(16), fixture()["text"], Deps(r, store, settings))
    assert state["status"] == "done", state["error"]
    assert [(s["step"], s["ok"]) for s in state["steps"]] == [("extract", True), ("improve", True)]
    extract_rub = anthropic_cost_rub("claude-haiku-5-5", 3000, 1500, 90)
    improve_rub = call_cost_rub(HAIKU, 1200, 700)
    assert [s["rub"] for s in state["steps"]] == [round(extract_rub, 4), round(improve_rub, 4)]
    assert store.spent_today() == pytest.approx(extract_rub + improve_rub)
    assert len(calls) == 2 and [c["model"] for c in tw.calls] == [HAIKU]



def test_a_timeout_at_anthropic_is_paid_within_the_reserve(sent, tmp_path):
    """Improve timed out at Anthropic and Timeweb answered: the step pays Timeweb plus Anthropic's
    worst case, and that fits the reserve."""
    calls, replies = sent
    settings = replace(Settings.from_env(ON), var_dir=tmp_path)
    replies += [message([tool_use(extract_answer())], {"input_tokens": 3000, "output_tokens": 1500}),
                TimeoutError()]
    tw = FakeGateway([Reply(happy()[1], tokens_in=1200, tokens_out=700)])
    r = Router.from_settings(settings)
    r.timeweb = tw
    store = Store(tmp_path / "s.sqlite3", 15, 3)
    state = run(secrets.token_urlsafe(16), fixture()["text"], Deps(r, store, settings))
    assert state["status"] == "done", state["error"]
    sent_improve = tw.calls[0]
    improve_rub = call_cost_rub(HAIKU, 1200, 700) + anthropic_worst_rub(
        "claude-haiku-5-5", prompt_chars(sent_improve["messages"]), sent_improve["max_tokens"], 90)
    assert state["steps"][1]["rub"] == round(improve_rub, 4)
    assert improve_rub <= messages_worst_rub(HAIKU, sent_improve["messages"], sent_improve["max_tokens"], settings)
    assert store.spent_today() == pytest.approx(anthropic_cost_rub("claude-haiku-5-5", 3000, 1500, 90) + improve_rub)

# ---------- the app ----------

def test_app_builds_the_router_and_status_shows_routes(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    on = replace(Settings.from_env(ON), var_dir=tmp_path / "on", recorded_dir=recorded)
    app = create_app(on)
    assert isinstance(app.state.client, Router) and app.state.client.routes == {HAIKU: "claude-haiku-5-5"}
    r = TestClient(app).get("/api/process/status")
    assert r.json()["routes"] == {HAIKU: "anthropic", "gemini/gemini-3.1-flash-lite": "timeweb",
                                  "openai/gpt-4o-transcribe": "timeweb"}
    assert KEY not in r.text and "tw-key" not in r.text
    off = replace(Settings.from_env({**ON, "ANTHROPIC_API_KEY": ""}), var_dir=tmp_path / "off", recorded_dir=recorded)
    app = create_app(off)
    assert isinstance(app.state.client, Router) and app.state.client.anthropic is None
    assert "routes" not in TestClient(app).get("/api/process/status").json()


def test_status_is_as_before_without_the_route(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    s = replace(Settings.from_env({}), var_dir=tmp_path / "var", recorded_dir=recorded)
    status = TestClient(create_app(s)).get("/api/process/status").json()
    assert "routes" not in status and status["live"] is False

