"""evals/: the data is well formed, score.py gives the hand-counted numbers, the threshold holds on
the hand-written answers it was picked on, summarize and run count right (docs/SPEC.md, «Замер»)."""

import json
import re
import sys
from collections import deque
from pathlib import Path

import pytest

from evals import run as evrun
from evals import score as sc
from evals import summarize as sm

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "evals"
EXAMPLES = ["sales", "invoice", "returns", "onboarding"]
NICHES = ["warehouse", "autoservice", "accounting", "legal", "travel", "furniture", "rental", "online_school"]
REVIEWED = ["sales", "invoice", "returns", "onboarding", "autoservice"]


# --- data -----------------------------------------------------------------------------------------

def test_twelve_processes_with_references():
    procs = sc.load_processes()
    assert [p["id"] for p in procs] == EXAMPLES + NICHES
    assert all(set(p) == {"id", "title", "text"} for p in procs)
    assert sorted(f.stem for f in sc.REFERENCE_DIR.glob("*.json")) == sorted(EXAMPLES + NICHES)


@pytest.mark.parametrize("p", sc.load_processes(), ids=lambda p: p["id"])
def test_text_length_and_numbers(p):
    lo, hi = (600, 1200) if p["id"] in EXAMPLES else (400, 1500)
    assert lo <= len(p["text"]) <= hi
    assert len(re.findall(r"\d+", p["text"])) >= 2
    assert not re.search(r"@|https?://|\+7|\b8 ?\(?9\d\d", p["text"])  # no contacts in the texts


def _out(ref):
    out = {s["id"]: [] for s in ref["steps"]}
    for a, b in ref["edges"]:
        out[a].append(b)
    return out


@pytest.mark.parametrize("pid", EXAMPLES + NICHES)
def test_reference_is_a_valid_process(pid):
    ref = sc.load_reference(pid)
    text = next(p["text"] for p in sc.load_processes() if p["id"] == pid)
    assert ref["id"] == pid
    role_ids = [r["id"] for r in ref["roles"]]
    step_ids = [s["id"] for s in ref["steps"]]
    assert role_ids == [f"r{i}" for i in range(1, len(role_ids) + 1)] and len(role_ids) <= 6
    assert step_ids == [f"s{i}" for i in range(1, len(step_ids) + 1)] and len(step_ids) <= 15
    assert all(isinstance(x["aliases"], list) for x in ref["roles"] + ref["steps"])
    assert all(s["role"] in role_ids for s in ref["steps"])
    kinds = [s["kind"] for s in ref["steps"]]
    assert kinds.count("start") == 1 and kinds.count("end") >= 1
    out = _out(ref)
    assert all(a in out and b in out for a, b in ref["edges"])
    assert len({tuple(e) for e in ref["edges"]}) == len(ref["edges"])
    for s in ref["steps"]:
        n = len(out[s["id"]])
        assert {"decision": 2 <= n <= 3, "end": n == 0}.get(s["kind"], n == 1), s["id"]
    start = next(s["id"] for s in ref["steps"] if s["kind"] == "start")
    seen, todo = {start}, deque([start])
    while todo:
        for b in out[todo.popleft()]:
            if b not in seen:
                seen.add(b)
                todo.append(b)
    assert seen == set(step_ids)
    for n in ref["numbers"]:
        assert n["step"] is None or n["step"] in step_ids
        assert n["field"] in ("minutes", "wait_hours") and n["unit"] in ("мин", "ч", "дн", "нед")
        assert sc.normalize(n["quote"]) in sc.normalize(text)


def test_an_example_has_a_rework_loop():
    def has_back_edge(ref):
        order = {s["id"]: i for i, s in enumerate(ref["steps"])}
        return any(order[b] < order[a] for a, b in ref["edges"])
    assert sum(has_back_edge(sc.load_reference(pid)) for pid in EXAMPLES) >= 1


def test_review_blank_lists_every_reference_step():
    blank = (ROOT / "evals" / "review-blank.md").read_text(encoding="utf-8")
    assert re.findall(r"^## (\w+) — ", blank, re.M) == REVIEWED
    for pid in REVIEWED:
        section = blank.split(f"## {pid} — ")[1].split("\n## ")[0]
        for s in sc.load_reference(pid)["steps"]:
            assert f"| {s['id']} | {s['name']} |" in section
        assert "| согласен | поправка |" in section


# --- similarity and threshold ---------------------------------------------------------------------

def test_trigrams_and_dice():
    assert sc.trigrams("Акт") == {" ак", "акт", "кт "}
    assert sc.trigrams("в и") == set()  # stop words only
    # «оплатить»+«счет»: 12 trigrams, «оплата»+«счета»: 10 («та » twice), 7 shared.
    assert sc.dice("Оплатить счёт", "Оплата счёта") == pytest.approx(14 / 22)
    assert sc.dice("", "счёт") == 0.0


def _fixture(pid):
    return json.loads((FIXTURES / f"{pid}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("pid", ["sales", "invoice", "returns"])
def test_threshold_gives_the_true_pairs(pid):
    f = _fixture(pid)
    got = {r: m[0] for r, m in sc.match_steps(f["process"], sc.load_reference(pid)).items()}
    assert got == f["truth"]


def test_threshold_bounds_from_the_comment():
    f = _fixture("sales")
    ref = sc.load_reference("sales")
    assert "s6" in sc.match_steps(f["process"], ref, 0.35)
    assert "s6" not in sc.match_steps(f["process"], ref, 0.40)  # «Отправка КП клиенту», 0.39
    f = _fixture("invoice")
    extra = next(s for s in f["process"]["steps"] if s["id"] == "s2")
    missed = next(s for s in sc.load_reference("invoice")["steps"] if s["id"] == "s5")
    assert sc.similarity(extra, missed) == pytest.approx(0.18, abs=0.005)


# --- score on a hand-written prediction ------------------------------------------------------------

REF = {
    "id": "toy", "title": "Заявка → счёт",
    "roles": [{"id": "r1", "name": "Менеджер", "aliases": ["продажник"]},
              {"id": "r2", "name": "Бухгалтер", "aliases": []}],
    "steps": [
        {"id": "s1", "kind": "start", "name": "Заявка пришла", "role": "r1", "aliases": []},
        {"id": "s2", "kind": "task", "name": "Проверить заявку", "role": "r1", "aliases": []},
        {"id": "s3", "kind": "decision", "name": "Данных хватает?", "role": "r1", "aliases": []},
        {"id": "s4", "kind": "task", "name": "Запросить данные у клиента", "role": "r1", "aliases": []},
        {"id": "s5", "kind": "task", "name": "Выставить счёт", "role": "r2", "aliases": ["Счёт клиенту"]},
        {"id": "s6", "kind": "task", "name": "Отправить договор", "role": "r1", "aliases": []},
        {"id": "s7", "kind": "end", "name": "Сделка закрыта", "role": "r1", "aliases": []},
    ],
    "edges": [["s1", "s2"], ["s2", "s3"], ["s3", "s5"], ["s3", "s4"], ["s4", "s2"], ["s5", "s6"], ["s6", "s7"]],
    "numbers": [
        {"step": "s2", "field": "minutes", "values": [10], "unit": "мин", "quote": "минут 10"},
        {"step": "s5", "field": "wait_hours", "values": [2], "unit": "дн", "quote": "2 дня"},
        {"step": None, "field": "minutes", "values": [1], "unit": "ч", "quote": "всего 1 час"},
    ],
}

# Found s1, s2, s3, s5, s6, s7; missed s4 (the loop goes straight back to a2); a6 is extra; a5 has the
# client's role; a6 has an invented 30, the code dropped an invented 45 and a grounded 48; a7's 60 is
# the whole-process «1 час», grounded but not on a step.
PRED = {
    "title": "Заявка",
    "roles": [{"id": "r1", "name": "Менеджер по продажам"}, {"id": "r2", "name": "Бухгалтерия"},
              {"id": "r3", "name": "Клиент"}],
    "steps": [
        {"id": "a1", "kind": "start", "name": "Заявка пришла", "role": "r1", "next": ["a2"], "quote": None},
        {"id": "a2", "kind": "task", "name": "Проверка заявки", "role": "r1", "next": ["a3"], "quote": "минут 10",
         "minutes": 10, "wait_hours": None},
        {"id": "a3", "kind": "decision", "name": "Данных хватает?", "role": "r1",
         "branches": [{"label": "да", "to": "a5"}, {"label": "нет", "to": "a2"}], "quote": None},
        {"id": "a5", "kind": "task", "name": "Выставить счёт", "role": "r3", "next": ["a6"], "quote": None,
         "minutes": None, "wait_hours": 16},
        {"id": "a6", "kind": "task", "name": "Внести в CRM", "role": "r1", "next": ["a7"], "quote": None,
         "minutes": 30, "wait_hours": None},
        {"id": "a7", "kind": "task", "name": "Отправка договора", "role": "r1", "next": ["a8"], "quote": None,
         "minutes": 60, "wait_hours": None},
        {"id": "a8", "kind": "end", "name": "Сделка закрыта", "role": "r1", "next": [], "quote": None},
    ],
    "flags": {"unquoted": [], "numbers_dropped": [{"step": "a7", "field": "minutes", "value": 45},
                                                  {"step": "a2", "field": "wait_hours", "value": 48}]},
}


def test_score_counts():
    s = sc.score(PRED, REF)
    assert {r: m[0] for r, m in s.pop("matches").items()} == {
        "s1": "a1", "s2": "a2", "s3": "a3", "s5": "a5", "s6": "a7", "s7": "a8"}
    assert s == {
        "steps_ref": 6, "steps_found": 5, "steps_pred": 6, "steps_extra": 1, "role_ok": 4,
        "edges_ref": 7, "edges_kept": 5,
        "decisions_ref": 1, "decisions_found": 1, "decisions_pred": 1, "decisions_extra": 0,
        "numbers_before": 6, "numbers_after": 4, "numbers_dropped": 2,
        "invented_before": 2, "invented_after": 1, "numbers_on_step": 2,
    }
    assert sc.headline(sc.score(PRED, REF)) == (
        "нашла 5 из 6 шагов эталона, роль верна в 4 из 5, порядок — 5 из 7 переходов, "
        "выдуманных чисел 1 (код отсёк 1)")


def test_score_of_the_reference_itself_is_perfect():
    as_pred = {"roles": REF["roles"], "steps": [], "flags": {}}
    nxt = {}
    for a, b in REF["edges"]:
        nxt.setdefault(a, []).append(b)
    for s in REF["steps"]:
        step = dict(s)
        if s["kind"] == "decision":
            step["branches"] = [{"label": "", "to": b} for b in nxt[s["id"]]]
        else:
            step["next"] = nxt.get(s["id"], [])
        as_pred["steps"].append(step)
    s = sc.score(as_pred, REF)
    assert (s["steps_found"], s["role_ok"], s["edges_kept"], s["decisions_found"], s["steps_extra"]) == (6, 6, 7, 1, 0)


def test_an_edge_through_a_matched_step_is_not_kept():
    pred = {"steps": [{"id": "x", "next": ["y"]}, {"id": "y", "next": ["z"]}, {"id": "z", "next": []}]}
    assert sc.edge_kept(pred, "x", "z", matched={"x", "z"})
    assert not sc.edge_kept(pred, "x", "z", matched={"x", "y", "z"})


def test_grounded_ranges():
    two_days = {"numbers": [{"step": "s1", "field": "wait_hours", "values": [2, 3], "unit": "дн", "quote": ""}]}
    assert sc.grounded("wait_hours", 16, two_days) and sc.grounded("wait_hours", 72, two_days)
    assert not sc.grounded("wait_hours", 15, two_days) and not sc.grounded("wait_hours", 73, two_days)
    assert sc.grounded("minutes", 2 * 480, two_days)
    assert not sc.grounded("tool", 16, two_days) and not sc.grounded("wait_hours", "16", two_days)


# --- summarize -------------------------------------------------------------------------------------

def test_wilson():
    p, lo, hi = sm.wilson(8, 10)
    assert (p, round(lo, 4), round(hi, 4)) == (0.8, 0.4902, 0.9433)
    p, lo, hi = sm.wilson(0, 5)
    assert (p, lo, round(hi, 4)) == (0.0, 0.0, 0.4345)
    assert sm.wilson(0, 0) is None


def test_totals_pool_counts_and_keep_the_last_line():
    lines = [
        {"id": "a", "model": "m1", "ok": True, "rub": 0.1, "score": {"steps_found": 1, "steps_ref": 5, "matches": {}}},
        {"id": "a", "model": "m1", "ok": True, "rub": 0.2, "score": {"steps_found": 4, "steps_ref": 5, "matches": {}}},
        {"id": "b", "model": "m1", "ok": True, "rub": 0.2, "score": {"steps_found": 4, "steps_ref": 5}},
        {"id": "c", "model": "m1", "ok": False, "rub": 0.05, "error": "x"},
        {"id": "a", "model": "m2", "ok": True, "rub": 0.1, "score": {"steps_found": 0, "steps_ref": 5}},
    ]
    t = sm.totals(lines)
    assert t["m1"] == {"processes": 3, "failed": 1, "rub": pytest.approx(0.45), "steps_found": 8, "steps_ref": 10}
    table = sm.table(t)
    assert "| найдены шаги эталона | 80% [49%–94%] (8/10) | 0% [0%–43%] (0/5) |" in table
    assert "| не разобраны | 1 из 3 | 0 из 1 |" in table
    assert "| найдены решения | — | — |" in table


# --- run -------------------------------------------------------------------------------------------

class _Chat:
    def __init__(self, cost):
        self.content, self.cost_rub, self.has_usage = "{}", cost, True


class _Client:
    calls = 0

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        self.calls += 1
        return _Chat(0.1)


MODEL = "anthropic/claude-haiku-5-5"
MESSAGES = [{"role": "user", "content": "x" * 1000}]


def _extract(text, settings, client):
    client.chat(MODEL, MESSAGES, None, 2500)
    return {"process": _fixture("sales")["process"]}


def test_run_scores_and_stops_at_the_budget(tmp_path):
    from app.pricing import messages_worst_rub

    worst = messages_worst_rub(MODEL, MESSAGES, 2500)
    procs = [p for p in sc.load_processes() if p["id"] in ("sales", "invoice", "returns")]
    client, out = _Client(), tmp_path / "run.jsonl"
    lines = evrun.run(procs, _extract, None, client, MODEL, 0.2 + worst - 0.01, out)
    assert client.calls == 2
    assert [(x["id"], x["ok"]) for x in lines] == [("sales", True), ("invoice", True), ("returns", False)]
    assert lines[2]["error"].startswith("budget:") and lines[2]["rub"] == 0
    assert lines[0]["rub"] == pytest.approx(0.1)
    assert lines[0]["score"]["steps_found"] == 9  # the sales fixture against the sales reference
    assert [json.loads(x)["id"] for x in out.read_text(encoding="utf-8").splitlines()] == ["sales", "invoice", "returns"]


def test_run_keeps_going_after_a_failed_process(tmp_path):
    def broken(text, settings, client):
        raise ValueError("bad json")
    procs = [p for p in sc.load_processes() if p["id"] in ("sales", "invoice")]
    lines = evrun.run(procs, broken, None, _Client(), MODEL, 1.0, tmp_path / "r.jsonl")
    assert [x["error"] for x in lines] == ["ValueError: bad json"] * 2


def test_as_process_shapes():
    p = _fixture("sales")["process"]
    assert evrun.as_process(p) is p
    assert evrun.as_process((p, [])) is p
    assert evrun.as_process({"process": p, "steps": []}) is p
    with pytest.raises(ValueError):
        evrun.as_process({"status": "failed"})


def test_missing_pipeline_is_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "app.pipeline", None)
    with pytest.raises(SystemExit, match="app.pipeline.extract_process"):
        evrun.load_extract()
