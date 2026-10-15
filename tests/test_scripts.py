import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import load_examples
from scripts import dev_server, record
from tests.fakes import FakeGateway, fixture, happy


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "processes.jsonl"
    lines = [{"id": "sales", "text": fixture("sales")["text"]}, {"id": "other", "text": "т", "title": "Другой"}]
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n\n", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)


def test_record_writes_an_example(tmp_path, source, capsys):
    out = tmp_path / "recorded"
    args = ["--id", "sales", "--source", str(source), "--out", str(out)]
    assert record.main(args, gateway=FakeGateway(happy())) == 0
    data = json.loads((out / "sales.json").read_text(encoding="utf-8"))
    assert set(data) == {"name", "title", "text", "process", "improve", "steps", "totals"}
    assert data["title"] == "Заявка → КП → договор" and data["text"] == fixture("sales")["text"]
    assert data["process"]["flags"] == {"unquoted": [], "numbers_dropped": []} and len(data["steps"]) == 2
    assert "шагов 12" in capsys.readouterr().out
    assert load_examples(out)["sales"]["process"] == data["process"]
    # no overwrite without --force
    assert record.main(args, gateway=FakeGateway(happy())) == 1
    assert record.main([*args, "--force"], gateway=FakeGateway(happy())) == 0


def test_record_errors(tmp_path, source):
    out = tmp_path / "recorded"
    assert record.main(["--id", "missing", "--source", str(source), "--out", str(out)], FakeGateway()) == 1
    assert record.main(["--id", "sales", "--source", str(tmp_path / "no.jsonl"), "--out", str(out)],
                       FakeGateway()) == 1
    # a failed run writes nothing
    assert record.main(["--id", "other", "--source", str(source), "--out", str(out)], FakeGateway(["{}", "{}"])) == 1
    assert not out.exists()


def test_record_live_needs_the_key(tmp_path, source):
    assert record.main(["--id", "sales", "--source", str(source), "--out", str(tmp_path)]) == 2


def test_record_refuses_a_bad_id(tmp_path):
    with pytest.raises(record.RecordError):
        record.record("../evil", "т", "текст", FakeGateway(), dev_server.dev_settings(tmp_path), tmp_path)


def test_dev_server(tmp_path):
    client = TestClient(dev_server.build(tmp_path, speed=0, web=None))
    examples = client.get("/api/process/examples").json()
    assert [e["name"] for e in examples] == ["sales", "invoice"]
    invoice = client.get("/api/process/examples/invoice").json()
    assert invoice["process"]["title"] == "Оплата счёта поставщика" and invoice["process"]["flags"]["unquoted"] == []
    assert [c["action"] for c in invoice["improve"]["changes"]] == ["automate", "remove", "merge", "automate"]
    assert client.get("/api/process/status").json()["live"] is True
    # a live run on the fixture's text gets that fixture's answers
    run_id = client.post("/api/process/runs", json={"text": fixture("invoice")["text"]}).json()["id"]
    deadline = time.monotonic() + 5
    while (state := client.get(f"/api/process/runs/{run_id}").json())["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert state["status"] == "done" and state["process"] == invoice["process"]


def test_dev_server_serves_web(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "dev.html").write_text("<!doctype html><title>стенд</title>", encoding="utf-8")
    client = TestClient(dev_server.build(tmp_path / "run", speed=0, web=web))
    assert "стенд" in client.get("/dev.html").text
    assert client.get("/api/process/health").json() == {"ok": True}


def test_fixtures_are_what_the_spec_asks():
    for name in ("sales", "invoice"):
        f = fixture(name)
        assert 600 <= len(f["text"]) <= 1200 and len([c for c in f["text"] if c.isdigit()]) >= 2
    kinds = [(s["kind"], len(s["branches"])) for s in fixture("sales")["extract"]["steps"]]
    assert ("decision", 3) in kinds  # a decision with three branches; s4 and s8 loop back


def test_dev_server_interview_whole(tmp_path):
    """8 questions, the final, the report by its link; «Хватит»; a recording; a recording not heard."""
    client = TestClient(dev_server.build(tmp_path, speed=0, web=None))
    url = "/api/process/interviews"
    report = client.get(f"{url}/{dev_server.REPORT_ID}").json()
    assert report["status"] == "done" and len(report["transcript"]) == 8
    assert report["process"]["flags"] == {"unquoted": [], "numbers_dropped": []} and report["improve"]["changes"]
    assert report["answered"] == ["volume", "rate", "s6.minutes", "s5.wait_hours"]
    assert report["transcript"][3]["q"] == "Что происходит после «Собрать и отправить КП» и до «Что ответил клиент?» — " \
                                           "нет ли там ещё шагов?"
    # the volume wording has digits on purpose: the code replaced it
    assert report["transcript"][1]["q"] == "Сколько примерно таких случаев у вас бывает за месяц?"

    interview = dev_server.load_interview()
    started = client.post(url, json={"topic": "заявки"})
    assert started.status_code == 201 and started.json()["question"]["n"] == 1
    interview_id = started.json()["id"]
    turns = [client.post(f"{url}/{interview_id}/answers", json={"text": a}).json() for a in interview["answers"]]
    assert [t["question"]["n"] for t in turns[:-1]] == list(range(2, 9)) and turns[-1]["done"] is True
    deadline = time.monotonic() + 5
    while (state := client.get(f"{url}/{interview_id}").json())["status"] == "building":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert state["status"] == "done" and state["assumptions"] == report["assumptions"]

    second = client.post(url, json={"text": "Как у нас принимают возврат товара"}).json()["id"]
    for i in range(3):
        files = {"audio": ("a.webm", b"x" * 4000, "audio/webm")}
        assert client.post(f"{url}/{second}/answers", files=files).json()["answer_text"] == interview["answers"][i]
    short = client.post(f"{url}/{second}/answers", files={"audio": ("a.webm", b"x" * 10, "audio/webm")})
    assert short.status_code == 422 and short.json()["code"] == "stt_failed"
    assert client.post(f"{url}/{second}/finish").status_code == 202

