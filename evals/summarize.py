"""Shares with the 95% Wilson interval from eval runs, one column per model.

    python -m evals.summarize evals/runs/*.jsonl

Counts are pooled over processes (a share of all reference steps, not a mean of per-process
shares). Steps of one process are not independent, so the interval is a lower bound of the real
uncertainty; with 12 processes it is a guide, not a test. When a process was run several times with
one model, the last line counts.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

Z95 = 1.959964

# (label, numerator, denominator) over the counts of evals/score.py.
METRICS = [
    ("найдены шаги эталона", "steps_found", "steps_ref"),
    ("лишние шаги модели", "steps_extra", "steps_pred"),
    ("роль верна среди найденных", "role_ok", "steps_found"),
    ("сохранены переходы", "edges_kept", "edges_ref"),
    ("найдены решения", "decisions_found", "decisions_ref"),
    ("выдуманные числа до проверки", "invented_before", "numbers_before"),
    ("выдуманные числа после проверки", "invented_after", "numbers_after"),
    ("число на своём шаге", "numbers_on_step", "numbers_after"),
]


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float, float] | None:
    """(share, low, high); None when there is nothing to divide."""
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


def read_lines(paths) -> list[dict]:
    out = []
    for path in paths:
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            if raw.strip():
                out.append(json.loads(raw))
    return out


def totals(lines: list[dict]) -> dict[str, dict]:
    """Model → summed counts, plus ``processes``, ``failed`` and ``rub``."""
    last: dict[tuple[str, str], dict] = {}
    for line in lines:
        last[(line["model"], line["id"])] = line
    out: dict[str, dict] = {}
    for (model, _), line in last.items():
        t = out.setdefault(model, {"processes": 0, "failed": 0, "rub": 0.0})
        t["processes"] += 1
        t["rub"] += line.get("rub") or 0.0
        if not line.get("ok"):
            t["failed"] += 1
            continue
        for key, value in line["score"].items():
            if isinstance(value, (int, float)):
                t[key] = t.get(key, 0) + value
    return out


def fmt(k: int, n: int) -> str:
    w = wilson(k, n)
    if w is None:
        return "—"
    p, lo, hi = w
    return f"{p:.0%} [{lo:.0%}–{hi:.0%}] ({k}/{n})"


def table(by_model: dict[str, dict]) -> str:
    models = sorted(by_model)
    rows = ["| метрика | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for label, num, den in METRICS:
        cells = [fmt(int(by_model[m].get(num, 0)), int(by_model[m].get(den, 0))) for m in models]
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    rows.append("| не разобраны | " + " | ".join(f"{by_model[m]['failed']} из {by_model[m]['processes']}"
                                                 for m in models) + " |")
    rows.append("| потрачено, ₽ | " + " | ".join(f"{by_model[m]['rub']:.2f}" for m in models) + " |")
    return "\n".join(rows)


def main(argv=None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths:
        paths = sorted(str(p) for p in (ROOT / "evals" / "runs").glob("*.jsonl"))
    if not paths:
        raise SystemExit("нет прогонов: evals/runs/*.jsonl пуст")
    print(table(totals(read_lines(paths))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
