"""Score one extracted Process against the hand-made reference (docs/SPEC.md, «Замер»).

Matching is 1-to-1. A model step and a reference step are a pair candidate when the step kinds are
compatible (a decision only with a decision; start, task and end with each other: whether «пришла
заявка» is a start event or a first task is the model's call, not an error) and the similarity is
at least ``THRESHOLD`` (how it was picked is next to it). Similarity is the Dice coefficient of
character trigrams between the model step's name or quote and the reference name or an alias, the
best of them. Candidates are taken greedily from
the most similar down; with a handful of steps per process this gives the same pairs as an optimal
assignment in practice and is easy to check by hand.

All metrics are counts, not shares: ``summarize.py`` pools them over processes and gives shares
with the Wilson interval.

Numbers. A model number (``minutes`` or ``wait_hours``) is grounded when its value falls in the
range of a reference number converted to the field's unit (a day is 8 working or 24 calendar hours,
a week 40 or 168). Otherwise it is invented. «Before the check» counts the numbers the code dropped
(``flags.numbers_dropped``) plus those left on the steps; «after» counts only those left.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

EVALS = Path(__file__).resolve().parent
REFERENCE_DIR = EVALS / "reference"
PROCESSES = EVALS / "processes.jsonl"

# Picked on hand-written model answers for sales, invoice and returns (tests/fixtures/evals/, each
# with the true pairs a person made; tests/test_evals.py keeps them). The weakest true pairs:
#   «Отправка КП клиенту»   ~ «Подготовить и отправить КП»   0.39
#   «Деньги вернули»        ~ «Деньги возвращены»            0.41
#   «Звонок клиенту: формат, размеры, мощность» ~ «Уточнить у клиента параметры заведения» 0.49
# The strongest pair left once the true ones are taken (an extra model step and a missed reference
# step): «Привезти товар со счётом» ~ «Распечатать счёт и отнести на подпись» 0.18. All 33 true pairs
# and no wrong one hold for thresholds 0.20–0.35, at 0.40 the КП pair is lost. 0.30 sits in the
# middle of the gap 0.18–0.39: live answers word steps more loosely than these.
# Strong wrong pairs exist («Согласовать и подписать договор» ~ end «Договор подписан» 0.84), the
# greedy 1-to-1 order resolves them while the true step is there; if the model drops that step, the
# neighbour is taken — a known limit, the review of the matches in runs/*.jsonl shows it.
THRESHOLD = 0.30
QUOTE_WEIGHT = 0.8

# Unit of a reference number → (low, high) factor to minutes and to hours.
_TO_MINUTES = {"мин": (1, 1), "ч": (60, 60), "дн": (480, 1440), "нед": (2400, 10080)}
_FIELD_FACTOR = {"minutes": 1, "wait_hours": 1 / 60}
_EPS = 1e-9


def normalize(text: str | None) -> str:
    text = (text or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


# Prepositions and conjunctions: in short step names they make a third of the trigrams.
STOPWORDS = {"а", "в", "во", "и", "к", "ко", "с", "со", "на", "по", "о", "об", "от", "до", "из", "за", "у",
             "не", "для", "или", "ли", "же", "то"}


def trigrams(text: str | None) -> set[str]:
    """Character trigrams of each word padded with spaces: «акт» → « ак», «акт», «кт »."""
    out: set[str] = set()
    for word in normalize(text).split():
        if word in STOPWORDS:
            continue
        padded = f" {word} "
        out.update(padded[i:i + 3] for i in range(len(padded) - 2))
    return out


def dice(a: str | None, b: str | None) -> float:
    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return 2 * len(ta & tb) / (len(ta) + len(tb))


def similarity(pred_step: dict, ref_step: dict) -> float:
    """Best Dice of the model name, or of the quote times ``QUOTE_WEIGHT``, to the reference name or
    an alias. A quote is a longer piece of the text and shares words with the neighbouring steps (in
    invoice the end event quoted «счёт подшить в папку» and outscored the true «подшить» step)."""
    theirs = [b for b in [ref_step.get("name"), *ref_step.get("aliases", [])] if b]
    by_name = max((dice(pred_step.get("name"), b) for b in theirs), default=0.0)
    by_quote = max((dice(pred_step.get("quote"), b) for b in theirs), default=0.0)
    return max(by_name, QUOTE_WEIGHT * by_quote)


def _is_decision(step: dict) -> bool:
    return step.get("kind") == "decision"


def match_steps(pred: dict, ref: dict, threshold: float = THRESHOLD) -> dict[str, tuple[str, float]]:
    """Reference step id → (model step id, similarity), 1-to-1."""
    candidates = []
    for ri, r in enumerate(ref["steps"]):
        for pi, p in enumerate(pred.get("steps", [])):
            if _is_decision(r) != _is_decision(p):
                continue
            sim = similarity(p, r)
            if sim >= threshold:
                candidates.append((-sim, ri, pi, r["id"], p["id"], sim))
    out: dict[str, tuple[str, float]] = {}
    used: set[str] = set()
    for *_, rid, pid, sim in sorted(candidates):
        if rid not in out and pid not in used:
            out[rid] = (pid, round(sim, 3))
            used.add(pid)
    return out


def map_roles(pred: dict, ref: dict, threshold: float = THRESHOLD) -> dict[str, str]:
    """Model role id → the most similar reference role id (several model roles may map to one)."""
    out = {}
    for p in pred.get("roles", []):
        best, best_sim = None, threshold
        for r in ref["roles"]:
            sim = max(dice(p.get("name"), n) for n in [r["name"], *r.get("aliases", [])])
            if sim >= best_sim:
                best, best_sim = r["id"], sim
        if best:
            out[p["id"]] = best
    return out


def successors(step: dict) -> list[str]:
    return list(step.get("next") or []) + [b.get("to") for b in step.get("branches") or [] if b.get("to")]


def edge_kept(pred: dict, a: str, b: str, matched: set[str]) -> bool:
    """A path a → … → b in the model graph whose inner steps are all unmatched (extra steps the
    model put in between do not break the order; a matched step in between does)."""
    nxt = {s["id"]: successors(s) for s in pred.get("steps", [])}
    seen, todo = {a}, list(nxt.get(a, []))
    while todo:
        cur = todo.pop()
        if cur == b:
            return True
        if cur in seen or cur in matched:
            continue
        seen.add(cur)
        todo.extend(nxt.get(cur, []))
    return False


def _range_in(field: str, number: dict) -> tuple[float, float]:
    lo_f, hi_f = _TO_MINUTES[number["unit"]]
    k = _FIELD_FACTOR[field]
    return min(number["values"]) * lo_f * k, max(number["values"]) * hi_f * k


def grounded(field: str, value, ref: dict) -> list[dict]:
    """Reference numbers that the value of this field is a reading of."""
    if field not in _FIELD_FACTOR or not isinstance(value, (int, float)):
        return []
    out = []
    for n in ref.get("numbers", []):
        lo, hi = _range_in(field, n)
        if lo - _EPS <= value <= hi + _EPS:
            out.append(n)
    return out


def model_numbers(pred: dict) -> tuple[list[tuple], list[tuple]]:
    """(numbers left on the steps, numbers the code dropped) as (step, field, value)."""
    after = [(s["id"], f, s[f]) for s in pred.get("steps", []) for f in ("minutes", "wait_hours")
             if s.get(f) is not None]
    dropped = [(d.get("step"), d.get("field"), d.get("value"))
               for d in (pred.get("flags") or {}).get("numbers_dropped", [])]
    return after, dropped


def score(pred: dict, ref: dict, threshold: float = THRESHOLD) -> dict:
    matches = match_steps(pred, ref, threshold)
    by_pred = {pid: rid for rid, (pid, _) in matches.items()}
    pred_steps = {s["id"]: s for s in pred.get("steps", [])}
    roles = map_roles(pred, ref, threshold)

    ref_tasks = [r for r in ref["steps"] if not _is_decision(r)]
    ref_decisions = [r for r in ref["steps"] if _is_decision(r)]
    pred_tasks = [p for p in pred_steps.values() if not _is_decision(p)]
    pred_decisions = [p for p in pred_steps.values() if _is_decision(p)]
    found_tasks = [r for r in ref_tasks if r["id"] in matches]
    role_ok = sum(1 for r in found_tasks if roles.get(pred_steps[matches[r["id"]][0]].get("role")) == r["role"])

    matched_pred = set(by_pred)
    edges_kept = sum(1 for a, b in ref["edges"] if a in matches and b in matches
                     and edge_kept(pred, matches[a][0], matches[b][0], matched_pred))

    after, dropped = model_numbers(pred)
    invented_after = sum(1 for _, f, v in after if not grounded(f, v, ref))
    invented_before = invented_after + sum(1 for _, f, v in dropped if not grounded(f, v, ref))
    on_step = sum(1 for sid, f, v in after if any(n.get("step") == by_pred.get(sid) for n in grounded(f, v, ref)))

    return {
        "steps_ref": len(ref_tasks),
        "steps_found": len(found_tasks),
        "steps_pred": len(pred_tasks),
        "steps_extra": sum(1 for p in pred_tasks if p["id"] not in by_pred),
        "role_ok": role_ok,
        "edges_ref": len(ref["edges"]),
        "edges_kept": edges_kept,
        "decisions_ref": len(ref_decisions),
        "decisions_found": sum(1 for r in ref_decisions if r["id"] in matches),
        "decisions_pred": len(pred_decisions),
        "decisions_extra": sum(1 for p in pred_decisions if p["id"] not in by_pred),
        "numbers_before": len(after) + len(dropped),
        "numbers_after": len(after),
        "numbers_dropped": len(dropped),
        "invented_before": invented_before,
        "invented_after": invented_after,
        "numbers_on_step": on_step,
        "matches": {rid: list(m) for rid, m in matches.items()},
    }


def headline(s: dict) -> str:
    """The line for the page (docs/SPEC.md, «Замер»)."""
    cut = s["invented_before"] - s["invented_after"]
    return (f"нашла {s['steps_found']} из {s['steps_ref']} шагов эталона, роль верна в {s['role_ok']} "
            f"из {s['steps_found']}, порядок — {s['edges_kept']} из {s['edges_ref']} переходов, "
            f"выдуманных чисел {s['invented_after']} (код отсёк {cut})")


def load_processes(path: Path = PROCESSES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_reference(pid: str, directory: Path = REFERENCE_DIR) -> dict:
    return json.loads((directory / f"{pid}.json").read_text(encoding="utf-8"))
