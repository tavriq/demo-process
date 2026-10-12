import json
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, load_examples
from app.pricing import interview_worst_rub, run_worst_rub
from tests.fakes import FakeGateway, extract_answer, fixture, happy

TEXT = "Заявка приходит на почту sales@corp.example, менеджер переносит её в таблицу. Звоните 8 912 345 67 89."


@pytest.fixture
def settings(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    return replace(Settings.from_env({}), api_key="k", base_url="http://gw.example", var_dir=tmp_path / "var",
                   recorded_dir=recorded)


def make(settings, replies=(), **overrides):
    gw = FakeGateway(replies)
    return TestClient(create_app(replace(settings, **overrides), client=gw)), gw


def start(client, text=TEXT):
    r = client.post("/api/process/runs", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def wait_done(client, run_id, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = client.get(f"/api/process/runs/{run_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("the run did not finish")


def recorded(directory, name="sales", **changes):
    f = fixture("sales")
    data = {"name": name, "title": f["title"] if name == "sales" else name, "text": f["text"],
            "process": {"title": "п", "steps": []},
            "improve": {"changes": [], "summary": ""}, "steps": [], "totals": {"rub": 0.1}, **changes}
    (directory / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_health_and_status(settings):
    client, _ = make(settings)
    r = client.get("/api/process/health")
    assert r.json() == {"ok": True} and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    status = client.get("/api/process/status").json()
    assert status == {"live": True, "budget_rub": 15.0, "spent_rub": 0.0,
                      "run_worst_rub": round(run_worst_rub(settings), 2), "runs_running": 0,
                      "interview_worst_rub": round(interview_worst_rub(settings), 2), "interviews_running": 0,
                      "models": {"extract": "anthropic/claude-haiku-5-5", "improve": "anthropic/claude-haiku-5-5",
                                 "draft": "gemini/gemini-3.1-flash-lite", "ask": "gemini/gemini-3.1-flash-lite",
                                 "stt": "openai/gpt-4o-transcribe"}}


def test_run_end_to_end_masks_contacts(settings):
    answer = extract_answer()
    client, gw = make(settings, [json.dumps(answer, ensure_ascii=False), happy()[1]])
    run_id = start(client)
    state = wait_done(client, run_id)
    assert state["status"] == "done" and state["stage"] == "done"
    assert "sales@corp.example" not in state["text"] and "[email скрыт]" in state["text"]
    assert "[телефон скрыт]" in state["text"] and "sales@corp.example" not in gw.user(0)
    assert set(state) >= {"status", "stage", "text", "process", "improve", "steps", "totals", "error"}
    # the fixture's quotes are from another text: all of them flagged, the numbers dropped
    assert len(state["process"]["flags"]["unquoted"]) == 12 and state["process"]["flags"]["numbers_dropped"]
    r = client.get(f"/api/process/runs/{run_id}")
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/process/status").json()["spent_rub"] == round(state["totals"]["rub"], 2)


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "   "}, {"text": "а" * 1501}, {"brief": "x"}, {},
                                  {"text": 5}])
def test_bad_text_is_422(settings, body):
    client, gw = make(settings)
    r = client.post("/api/process/runs", json=body)
    assert r.status_code == 422 and r.json()["code"] == "bad_request" and "1500" in r.json()["message"]
    assert gw.calls == []


def test_1500_chars_are_fine(settings):
    client, _ = make(settings, ["{}", "{}"])
    assert client.post("/api/process/runs", json={"text": "а" * 1500}).status_code == 200


def test_offline_without_key(settings):
    client = TestClient(create_app(replace(settings, api_key="")))
    r = client.post("/api/process/runs", json={"text": TEXT})
    assert r.status_code == 503 and r.json()["code"] == "offline"
    assert client.get("/api/process/status").json()["live"] is False


def test_budget_exhausted(settings):
    client, gw = make(settings, happy())
    client.app.state.store.reserve(15 - run_worst_rub(settings) / 2)
    r = client.post("/api/process/runs", json={"text": TEXT})
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted" and gw.calls == []
    assert client.get("/api/process/status").json()["live"] is False


def test_rate_limited(settings):
    client, _ = make(settings, happy() * 3)
    for _ in range(3):
        wait_done(client, start(client))
    r = client.post("/api/process/runs", json={"text": TEXT})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and r.json()["retry_after_s"] > 0


def test_busy_when_two_runs_go(settings):
    release = threading.Event()

    def blocked():
        assert release.wait(5)
        return happy()[0]

    client, _ = make(settings, [blocked, blocked, happy()[1], happy()[1]])
    first, second = start(client), start(client)
    r = client.post("/api/process/runs", json={"text": TEXT})
    assert r.status_code == 429 and r.json()["code"] == "busy"
    assert client.get("/api/process/status").json()["runs_running"] == 2
    running = client.get(f"/api/process/runs/{first}").json()
    assert running["status"] == "running" and running["stage"] == "extract"
    release.set()
    assert wait_done(client, first)["status"] == wait_done(client, second)["status"] == "done"
    # the busy refusal gave its hold back
    assert client.app.state.store.left_today() > 15 - 1


def test_unknown_run(settings):
    client, _ = make(settings)
    assert client.get("/api/process/runs/nope").status_code == 404
    r = client.get("/api/process/runs/" + "a" * 22)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_examples(settings):
    for name in ("onboarding", "invoice", "sales", "extra"):
        recorded(settings.recorded_dir, name)
    recorded(settings.recorded_dir, "broken", process=None)
    (settings.recorded_dir / "Bad Name.json").write_text("{}", encoding="utf-8")
    (settings.recorded_dir / "junk.json").write_text("not json", encoding="utf-8")
    client, gw = make(settings)
    r = client.get("/api/process/examples")
    assert r.headers["cache-control"] == "public, max-age=300"
    assert [e["name"] for e in r.json()] == ["sales", "invoice", "onboarding", "extra"]
    assert r.json()[0] == {"name": "sales", "title": "Заявка → КП → договор"}
    r = client.get("/api/process/examples/sales")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
    assert set(r.json()) == {"name", "title", "text", "process", "improve", "steps", "totals", "status", "stage",
                             "error"}
    assert r.json()["status"] == "done" and r.json()["text"] == fixture()["text"]
    assert client.get("/api/process/examples/broken").status_code == 404
    assert client.get("/api/process/examples/..%2Fx").status_code == 404
    assert gw.calls == []


def test_no_examples_dir(settings, tmp_path):
    assert load_examples(tmp_path / "missing") == {}


def test_big_body_is_413(settings):
    client, _ = make(settings)
    r = client.post("/api/process/runs", content=b'{"text": "' + b"a" * 20000 + b'"}',
                    headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_stale_run_fails_on_restart(settings):
    client, _ = make(settings)
    store = client.app.state.store
    from app.pipeline import initial_state
    store.create_run("x" * 22, initial_state("x" * 22, "т", store.clock()))
    client2, _ = make(settings)
    state = client2.get("/api/process/runs/" + "x" * 22).json()
    assert state["status"] == "failed" and state["error"]["code"] == "restart"
