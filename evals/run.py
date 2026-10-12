"""Live eval run: extract every process of processes.jsonl with one model and score it.

    python -m evals.run --model gemini/gemini-3.1-flash-lite --only sales,invoice --max-rub 2

Each line of ``evals/runs/<date>-<model>.jsonl``: the process id, the model, ``ok``, the error, time,
rubles, the extracted Process and its score (``evals/score.py``). The model is passed to the
pipeline through ``EXTRACT_MODEL`` (docs/SPEC.md: models come from env). ``--max-rub`` caps the
spend of this run: before every gateway call its worst case (``app/pricing.py``) must fit into what
is left, otherwise the run stops and the lines written so far stay.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # python evals/run.py works as well as python -m evals.run
    sys.path.insert(0, str(ROOT))

from evals.score import load_processes, load_reference, score  # noqa: E402

RUNS_DIR = ROOT / "evals" / "runs"


class BudgetStop(Exception):
    """The next call could exceed ``--max-rub``."""


class BudgetClient:
    """Wraps the gateway client: refuses a call whose worst case does not fit, counts real rubles
    (the worst case when the gateway returned no usage)."""

    def __init__(self, client, max_rub: float):
        self.client, self.max_rub, self.spent = client, max_rub, 0.0
        self.stopped = False

    def chat(self, model, messages, schema, max_tokens, params=None, timeout_s=None):
        from app.pricing import messages_worst_rub

        worst = messages_worst_rub(model, messages, max_tokens)
        if self.spent + worst > self.max_rub:
            self.stopped = True
            raise BudgetStop(f"следующий вызов до {worst:.2f} ₽, осталось {self.max_rub - self.spent:.2f} ₽")
        chat = self.client.chat(model, messages, schema, max_tokens, params=params, timeout_s=timeout_s)
        self.spent += chat.cost_rub if getattr(chat, "has_usage", True) else worst
        return chat


def load_extract():
    """``app.pipeline.extract_process`` is written in parallel: import it only when a run starts."""
    try:
        from app.pipeline import extract_process
    except ImportError as e:
        raise SystemExit(f"нет app.pipeline.extract_process (конвейер ещё не готов): {e}") from None
    return extract_process


def as_process(result):
    """The Process from what ``extract_process`` returns: the Process itself, a dict with
    ``process`` or a tuple whose first item is the Process."""
    if isinstance(result, tuple):
        result = result[0]
    if isinstance(result, dict) and isinstance(result.get("process"), dict):  # a run record, as GET runs/{id}
        result = result["process"]
    if not isinstance(result, dict) or "steps" not in result:
        raise ValueError("extract_process вернула не Process")
    return result


def run(processes, extract, settings, client, model: str, max_rub: float, out: Path,
        refs: dict | None = None) -> list[dict]:
    """Extract and score each process, appending a line to ``out`` as soon as it is done."""
    budget = BudgetClient(client, max_rub)
    lines = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for p in processes:
            ref = (refs or {}).get(p["id"]) or load_reference(p["id"])
            spent_before, started = budget.spent, time.perf_counter()
            line = {"id": p["id"], "model": model, "ok": False, "error": None}
            try:
                process = as_process(extract(p["text"], settings, budget))
                line.update(ok=True, process=process, score=score(process, ref))
            except BudgetStop as e:
                line["error"] = f"budget: {e}"
            except Exception as e:  # one failed process must not lose the others
                line["error"] = f"{type(e).__name__}: {getattr(e, 'detail', e)}"
            line["ms"] = int((time.perf_counter() - started) * 1000)
            line["rub"] = round(budget.spent - spent_before, 4)
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
            f.flush()
            lines.append(line)
            if budget.stopped:
                break
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", required=True, help="модель шага extract, например anthropic/claude-haiku-5-5")
    ap.add_argument("--only", default="", help="id через запятую; по умолчанию все 12")
    ap.add_argument("--max-rub", type=float, default=3.0, help="потолок трат на этот прогон, ₽")
    args = ap.parse_args(argv)

    processes = load_processes()
    if args.only:
        wanted = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = sorted(set(wanted) - {p["id"] for p in processes})
        if unknown:
            raise SystemExit(f"нет таких id: {', '.join(unknown)}")
        processes = [p for p in processes if p["id"] in wanted]

    extract = load_extract()
    os.environ["EXTRACT_MODEL"] = args.model
    from app.config import Settings
    from app.gateway import Gateway

    settings = Settings.from_env()
    if not settings.live:
        raise SystemExit("живой режим выключен: задайте LLM_API_KEY и LLM_BASE_URL")
    client = Gateway(settings.base_url, settings.api_key)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    out = RUNS_DIR / f"{stamp}-{re.sub(r'[^A-Za-z0-9.-]+', '_', args.model)}.jsonl"
    lines = run(processes, extract, settings, client, args.model, args.max_rub, out)
    ok = sum(1 for x in lines if x["ok"])
    print(f"{out.relative_to(ROOT)}: {ok} из {len(lines)} разобраны, {sum(x['rub'] for x in lines):.2f} ₽")
    return 0 if ok == len(processes) else 1


if __name__ == "__main__":
    sys.exit(main())
