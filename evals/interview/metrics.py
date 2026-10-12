"""Metrics of one interview and the table over runs (docs/SPEC-analyst.md, «Замер»).

    python -m evals.interview.metrics evals/interview/runs/*.jsonl

Per interview:

- reference steps found — ``evals/score.py`` on the final ``process``, the whole score is kept;
- questions asked — every question the server sent, the one left unanswered at the limit included;
- repeats — a question whose ``gap`` was asked before or whose text (normalized as in score.py) was;
- questions with digits — a digit anywhere in the text, «1С» aside (a product name, as in app/checks.py);
- minutes from the answers against assumptions — of the work steps (task, decision: web/engine.js
  gives the others no minutes) of the final ``process``, those whose minutes came from the answers:
  on the step itself (the interview's text is its transcript, so a number left there by the check is
  an answer's) or in ``assumptions["minutes"]`` (step id → minutes, like ``people`` of ``E.assume()``).
  The rest run on the default. v1 had minutes in 5 of 54 steps of all kinds of the four recorded
  examples, so the share is given over all steps too. ``minutes_grounded`` — how many of these numbers
  are in the description (``score.grounded``): the simulator must not invent them. ``minutes_on_step`` —
  how many stand on their own step: the step matched to a reference step (``score.match_steps``) whose
  number it is. The замер 12.10 had 11 grounded of 11, but only 4 on their step: a step's minutes went
  to the asked one.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.score import grounded, match_steps, normalize, score  # noqa: E402
from evals.summarize import fmt  # noqa: E402

V1_MINUTES = (5, 54)  # docs/SPEC-analyst.md: time from the text in 5 of 54 steps

_DIGIT = re.compile(r"\d")
_1C = re.compile(r"(?<!\w)1[сСcC](?!\w)")


def has_digits(text: str | None) -> bool:
    return bool(_DIGIT.search(_1C.sub("", text or "")))


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def question_counts(transcript: list[dict]) -> dict:
    """``transcript``: ``{"n", "gap", "q", "a"}`` per question as sent; ``a`` is None if unanswered."""
    gaps, texts = set(), set()
    repeats = 0
    for t in transcript:
        gap, text = t.get("gap"), normalize(t.get("q"))
        if (gap and gap in gaps) or (text and text in texts):
            repeats += 1
        gaps.add(gap)
        texts.add(text)
    return {
        "questions": len(transcript),
        "answers": sum(1 for t in transcript if t.get("a") is not None),
        "repeats": repeats,
        "questions_with_digits": sum(1 for t in transcript if has_digits(t.get("q"))),
    }


def answered_minutes(process: dict, assumptions: dict | None) -> dict[str, float]:
    """Work step id → minutes from the answers; the assumptions' value wins, as on the page."""
    extra = (assumptions or {}).get("minutes") if isinstance(assumptions, dict) else None
    extra = extra if isinstance(extra, dict) else {}
    out = {}
    for s in process.get("steps", []):
        if s.get("kind") not in ("task", "decision"):
            continue
        value = extra.get(s.get("id"))
        value = value if _number(value) else s.get("minutes")
        if _number(value):
            out[s["id"]] = value
    return out


def on_step(ref_id: str | None, value, ref: dict) -> bool:
    """The value is the reference's minutes of the matched step ``ref_id``."""
    return bool(ref_id) and any(n.get("step") == ref_id and n.get("field") == "minutes"
                                for n in grounded("minutes", value, ref))


def minutes_counts(process: dict, assumptions: dict | None, ref: dict) -> dict:
    work = [s for s in process.get("steps", []) if s.get("kind") in ("task", "decision")]
    got = answered_minutes(process, assumptions)
    ref_of = {pid: rid for rid, (pid, _) in match_steps(process, ref).items()} if ref.get("steps") else {}
    return {
        "steps_total": len(process.get("steps", [])),
        "minutes_answered": len(got),
        "minutes_assumed": len(work) - len(got),
        "minutes_grounded": sum(1 for v in got.values() if grounded("minutes", v, ref)),
        "minutes_on_step": sum(1 for sid, v in got.items() if on_step(ref_of.get(sid), v, ref)),
    }


def interview(transcript: list[dict], final: dict, ref: dict) -> dict:
    """All counts of one interview; ``score`` and the minutes only when there is a final process."""
    out = question_counts(transcript)
    process = final.get("process")
    if isinstance(process, dict) and isinstance(process.get("steps"), list):
        out.update(minutes_counts(process, final.get("assumptions"), ref))
        out["score"] = score(process, ref)
    return out


# --- table -----------------------------------------------------------------------------------------

def last_by_id(lines: list[dict]) -> list[dict]:
    """A process run several times: its last line counts."""
    out: dict[str, dict] = {}
    for line in lines:
        out.pop(line["id"], None)
        out[line["id"]] = line
    return list(out.values())


def _cell(line: dict, key: str) -> str:
    return str(line[key]) if key in line else "—"


def table(lines: list[dict]) -> str:
    lines = last_by_id(lines)
    rows = ["| процесс | вопросов | повторов | с цифрами | шаги эталона | минуты из ответов / допущения | ₽ | ошибка |",
            "|---|---|---|---|---|---|---|---|"]
    per_line = ("questions", "repeats", "questions_with_digits", "steps_total", "minutes_answered", "minutes_assumed",
                "minutes_grounded", "minutes_on_step")
    tot = dict.fromkeys(per_line + ("steps_found", "steps_ref"), 0)
    rub = 0.0
    for line in lines:
        s = line.get("score") or {}
        found = f"{s['steps_found']} из {s['steps_ref']}" if s else "—"
        minutes = f"{line['minutes_answered']} / {line['minutes_assumed']}" if "minutes_answered" in line else "—"
        error = (line.get("error") or "").replace("|", "/")
        rows.append(f"| {line['id']} | {_cell(line, 'questions')} | {_cell(line, 'repeats')} | "
                    f"{_cell(line, 'questions_with_digits')} | {found} | {minutes} | {line.get('rub') or 0:.2f} | "
                    f"{error} |")
        rub += line.get("rub") or 0.0
        for key in per_line:
            tot[key] += line.get(key) or 0
        for key in ("steps_found", "steps_ref"):
            tot[key] += s.get(key) or 0
    rows.append(f"| всего | {tot['questions']} | {tot['repeats']} | {tot['questions_with_digits']} | "
                f"{fmt(tot['steps_found'], tot['steps_ref'])} | {tot['minutes_answered']} / {tot['minutes_assumed']} | "
                f"{rub:.2f} | не дошли {sum(1 for x in lines if not x.get('ok'))} из {len(lines)} |")
    rows.append("")
    rows.append(f"Минуты из ответов — у {tot['minutes_answered']} из {tot['steps_total']} шагов "
                f"(в v1 из текста — {V1_MINUTES[0]} из {V1_MINUTES[1]}), есть в описании — "
                f"{tot['minutes_grounded']} из {tot['minutes_answered']}, у своего шага — "
                f"{tot['minutes_on_step']} из {tot['minutes_answered']}.")
    return "\n".join(rows)


def read_lines(paths) -> list[dict]:
    return [json.loads(raw) for path in paths for raw in Path(path).read_text(encoding="utf-8").splitlines()
            if raw.strip()]


def refresh(line: dict) -> dict:
    """The minutes counts of a line by today's rules: its process and assumptions are stored."""
    ref = ROOT / "evals" / "reference" / f"{line.get('id')}.json"
    if isinstance(line.get("process"), dict) and ref.is_file():
        line.update(minutes_counts(line["process"], line.get("assumptions"), json.loads(ref.read_text(encoding="utf-8"))))
    return line


def main(argv=None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        paths = sorted(str(p) for p in (ROOT / "evals" / "interview" / "runs").glob("*.jsonl"))
    if not paths:
        raise SystemExit("нет прогонов: evals/interview/runs/*.jsonl пуст")
    print(table([refresh(line) for line in read_lines(paths)]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
