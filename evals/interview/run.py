"""Live interview eval: the owner simulator answers the interview over HTTP, the result is scored.

docs/SPEC-analyst.md, «Замер» and «Контракт API».

    python -m evals.interview.run --max-rub 15 --dry-run    # only the worst case, no calls
    python -m evals.interview.run --max-rub 15

For each process (by default the four examples) an interview starts with the page's topic button, or
with the title as the visitor's own phrase for the other niches. Each question goes to the simulator
(``owner.py``, through the gateway with ``LLM_API_KEY``/``LLM_BASE_URL``), its answer goes back as
text. When the server has no more questions (``done``) the interview builds; if it still waits for
answers, «Хватит» (``finish``) is pressed. The final ``GET`` is polled once a second like the page
does. Each line of ``evals/interview/runs/<date>.jsonl``: the process id, the transcript, the final
process and the counts of ``metrics.py``.

``--max-rub`` caps the whole run: the simulator and the interviews. An interview starts only if its
worst case (``app.pricing.interview_worst_rub``, the server's own bound) plus the simulator's for 8
answers fits into what is left; every simulator call is checked once more before it goes out
(``evals.run.BudgetClient``). After the interview its real cost is taken from the server's
``totals.rub`` (or the sum of ``steps``); the worst case when it did not report one or the interview
broke off. The worst case is computed from this process's env: run it with the server's models.

The server lets 3 interviews an hour from one address: for four in a row raise its limit for the
run or go in two parts with ``--only``; a ``rate_limited`` answer stops the run, the lines so far stay.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:  # python evals/interview/run.py works as well as python -m
    sys.path.insert(0, str(ROOT))

from evals.interview import metrics, owner  # noqa: E402
from evals.interview.api import DEFAULT_BASE_URL, Api, ApiError  # noqa: E402
from evals.run import BudgetClient, BudgetStop  # noqa: E402
from evals.score import load_processes, load_reference  # noqa: E402

RUNS_DIR = ROOT / "evals" / "interview" / "runs"
# The page's buttons (docs/SPEC-analyst.md, «Что видит посетитель»).
TOPICS = {"sales": "заявки", "invoice": "счета", "returns": "возвраты", "onboarding": "выход"}
DEFAULT_ONLY = tuple(TOPICS)
# After these the next interview would fail the same way: stop instead of burning the simulator.
STOP_CODES = {"budget", "budget_exhausted", "rate_limited", "offline", "unreachable"}
POLL_S = 1.0
WAIT_S = 180.0  # the final build: extract and improve with retries, as a v1 run (120 s) and some
_EPS = 1e-9


def load_interview_worst():
    """``app.pricing.interview_worst_rub`` is written in parallel: import it only when a run starts."""
    try:
        from app.pricing import interview_worst_rub
    except ImportError as e:
        raise SystemExit(f"нет app.pricing.interview_worst_rub (худший случай интервью ещё не готов): {e}") from None
    return interview_worst_rub


def start_body(p: dict) -> dict:
    if p["id"] in TOPICS:
        return {"topic": TOPICS[p["id"]], "text": None}
    return {"topic": None, "text": p["title"]}


def worst_rub(p: dict, interview_worst: float, owner_model: str) -> float:
    return interview_worst + owner.worst_rub(owner_model, p["text"])


def worst_case(processes: list[dict], interview_worst: float, owner_model: str, max_rub: float) -> tuple[str, int]:
    """The line printed before the run and how many processes fit into ``max_rub`` in the worst case."""
    per = [worst_rub(p, interview_worst, owner_model) for p in processes]
    total, fit, acc = sum(per), 0, 0.0
    for w in per:
        acc += w
        if acc > max_rub + _EPS:
            break
        fit += 1
    owners = total - len(per) * interview_worst
    msg = (f"худший случай: {len(per)} × интервью {interview_worst:.2f} ₽ + владелец {owners:.2f} ₽ "
           f"= {total:.2f} ₽; потолок --max-rub {max_rub:.2f} ₽")
    if fit < len(per):
        msg += f" — влезут {fit} из {len(per)}"
    return msg, fit


def interview_rub(final: dict) -> float | None:
    """What the server says the interview cost: ``totals.rub``, else the sum of ``steps``."""
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    totals = final.get("totals")
    if isinstance(totals, dict) and num(totals.get("rub")):
        return float(totals["rub"])
    steps = final.get("steps")
    if isinstance(steps, list) and steps and all(isinstance(s, dict) and num(s.get("rub")) for s in steps):
        return float(sum(s["rub"] for s in steps))
    return None


def error_text(error) -> str:
    if isinstance(error, dict):
        return ": ".join(str(error[k]) for k in ("code", "message") if error.get(k))
    return str(error or "error")


def converse(api: Api, client, model: str, p: dict, line: dict, poll_s: float, wait_s: float, sleep, clock) -> dict:
    """One interview to its end; fills ``line["interview"]`` and ``line["transcript"]`` as it goes and
    returns the final ``GET``."""
    started = api.start(**start_body(p))
    iid = line["interview"] = str(started["id"])
    question = started.get("question")
    while isinstance(question, dict):
        turn = {"n": question.get("n"), "gap": question.get("gap"), "q": str(question.get("text") or ""), "a": None}
        line["transcript"].append(turn)
        if len(line["transcript"]) > owner.MAX_QUESTIONS:  # the SPEC limit: no answer past it
            break
        turn["a"] = owner.answer(client, model, p["text"], turn["q"])
        reply = api.answer(iid, turn["a"])
        question = None if reply.get("done") else reply.get("question")

    if api.get(iid).get("status") == "asking":
        api.finish(iid)
    deadline = clock() + wait_s
    while True:
        final = api.get(iid)
        if final.get("status") in ("done", "error"):
            return final
        if clock() >= deadline:
            raise TimeoutError(f"интервью не достроилось за {wait_s:.0f} с")
        sleep(poll_s)


def run(processes: list[dict], api: Api, client, owner_model: str, max_rub: float, interview_worst: float, out: Path,
        refs: dict | None = None, poll_s: float = POLL_S, wait_s: float = WAIT_S, sleep=time.sleep,
        clock=time.monotonic) -> list[dict]:
    """Interview and score each process, appending a line to ``out`` as soon as it is done."""
    spent, lines = 0.0, []
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for p in processes:
            ref = (refs or {}).get(p["id"]) or load_reference(p["id"])
            line = {"id": p["id"], "owner_model": owner_model, "start": start_body(p), "ok": False, "error": None,
                    "interview": None, "transcript": []}
            started, stop = time.perf_counter(), False
            need = worst_rub(p, interview_worst, owner_model)
            rub_owner = rub_interview = 0.0
            if spent + need > max_rub + _EPS:
                line["error"] = f"budget: худший случай {need:.2f} ₽, осталось {max_rub - spent:.2f} ₽"
                stop = True
            else:
                budget = BudgetClient(client, max_rub - spent - interview_worst)
                final = None
                try:
                    final = converse(api, budget, owner_model, p, line, poll_s, wait_s, sleep, clock)
                except BudgetStop as e:
                    line["error"], stop = f"budget: {e}", True
                except ApiError as e:
                    line["error"], stop = str(e), e.code in STOP_CODES
                except Exception as e:  # one failed interview must not lose the others
                    line["error"] = f"{type(e).__name__}: {e}"
                rub_owner = budget.spent
                if line["interview"] is not None:  # started: the server spent, at most its worst case
                    reported = interview_rub(final) if final is not None else None
                    rub_interview = interview_worst if reported is None else reported
                    line["rub_interview_reported"] = reported is not None
                if final is not None:
                    line.update({k: final.get(k) for k in ("status", "process", "improve", "assumptions", "answered",
                                                           "steps", "totals")})
                    if final.get("status") == "error":
                        line["error"] = error_text(final.get("error"))
                line.update(metrics.interview(line["transcript"], final or {}, ref))
                line["ok"] = line["error"] is None and "score" in line
            spent += rub_owner + rub_interview
            line["ms"] = int((time.perf_counter() - started) * 1000)
            line["rub_owner"] = round(rub_owner, 4)
            line["rub_interview"] = round(rub_interview, 4)
            line["rub"] = round(rub_owner + rub_interview, 4)
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
            lines.append(line)
            if stop:
                break
    return lines


def select(processes: list[dict], only: str) -> list[dict]:
    wanted = [x.strip() for x in only.split(",") if x.strip()]
    unknown = sorted(set(wanted) - {p["id"] for p in processes})
    if unknown:
        raise SystemExit(f"нет таких id: {', '.join(unknown)}")
    return [p for p in processes if p["id"] in wanted]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-rub", type=float, required=True, help="потолок всего прогона (владелец и интервью), ₽")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API интервью, по умолчанию {DEFAULT_BASE_URL}")
    ap.add_argument("--only", default=",".join(DEFAULT_ONLY), help="id через запятую; по умолчанию четыре примера")
    ap.add_argument("--owner-model", default=owner.DEFAULT_MODEL, help="модель владельца-симулятора")
    ap.add_argument("--dry-run", action="store_true", help="только напечатать худший случай")
    args = ap.parse_args(argv)

    processes = select(load_processes(), args.only)
    interview_worst_rub = load_interview_worst()
    from app.config import Settings

    settings = Settings.from_env()
    interview_worst = float(interview_worst_rub(settings))
    msg, fit = worst_case(processes, interview_worst, args.owner_model, args.max_rub)
    print(msg, flush=True)
    if args.dry_run:
        return 0
    if fit == 0:
        raise SystemExit("потолок меньше худшего случая одного интервью")
    if not settings.live:
        raise SystemExit("живой режим выключен: задайте LLM_API_KEY и LLM_BASE_URL (на них отвечает владелец)")
    from app.gateway import Gateway

    client = Gateway(settings.base_url, settings.api_key)
    out = RUNS_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M')}.jsonl"
    lines = run(processes, Api(args.base_url), client, args.owner_model, args.max_rub, interview_worst, out)
    print(metrics.table(lines))
    ok = sum(1 for x in lines if x["ok"])
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"{shown}: {ok} из {len(processes)} интервью, {sum(x['rub'] for x in lines):.2f} ₽ "
          f"из {args.max_rub:.2f}")
    return 0 if ok == len(processes) else 1


if __name__ == "__main__":
    sys.exit(main())
