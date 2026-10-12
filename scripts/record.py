"""Record one live run for the page's examples: app/recorded/<id>.json (docs/SPEC.md, «Записанные
примеры»): ``{"name", "title", "text", "process", "improve", "steps", "totals"}``.

The text is taken from evals/processes.jsonl by its id (a line ``{"id", "text", …}``). Runs the
pipeline in-process against the real gateway (LLM_API_KEY, LLM_BASE_URL from the environment,
never printed) with a throwaway store capped by --max-rub. The image has no scripts/ and no
evals/, so mount them where the key lives:

    docker run --rm --user "$(id -u)" --env-file ../llm.env -v "$PWD/scripts:/app/scripts:ro" \\
        -v "$PWD/evals:/app/evals:ro" -v "$PWD/app/recorded:/app/app/recorded" \\
        --entrypoint python demo-process:local scripts/record.py --id sales

Handwritten fixtures (tests/fixtures/) never go to app/recorded: the image serves that folder.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.gateway import ChatClient, Gateway  # noqa: E402
from app.main import EXAMPLE_NAME  # noqa: E402
from app.pii import mask_pii  # noqa: E402
from app.pipeline import Deps, run  # noqa: E402
from app.pricing import run_worst_rub  # noqa: E402
from app.store import CapStore  # noqa: E402

SOURCE = ROOT / "evals" / "processes.jsonl"
# The page's buttons (docs/SPEC.md, «Что видит посетитель»).
TITLES = {"sales": "Заявка → КП → договор", "invoice": "Счёт поставщика", "returns": "Возврат товара",
          "onboarding": "Выход сотрудника"}


class RecordError(Exception):
    pass


def read_text(source: Path, item_id: str) -> dict:
    """The line of ``source`` with this id."""
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise RecordError(f"нет файла {source}") from None
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            raise RecordError(f"{source.name}:{n}: не JSON") from None
        if isinstance(item, dict) and item.get("id") == item_id:
            if not isinstance(item.get("text"), str) or not item["text"].strip():
                raise RecordError(f"{source.name}:{n}: нет text")
            return item
    raise RecordError(f"в {source.name} нет id «{item_id}»")


def record(name: str, title: str, text: str, gateway: ChatClient, settings: Settings, out_dir: Path,
           clock: Callable[[], float] | None = None, force: bool = False) -> Path:
    """Run the text once and write ``out_dir/<name>.json``. Only a finished run is written."""
    if not EXAMPLE_NAME.fullmatch(name):
        raise RecordError("id: латиница в нижнем регистре, цифры, «-» и «_», до 40 знаков")
    path = out_dir / f"{name}.json"
    if path.exists() and not force:
        raise RecordError(f"{path.name} уже есть: --force, чтобы перезаписать")
    masked = mask_pii(text.strip())[0]
    if not masked or len(masked) > settings.max_text_chars:
        raise RecordError(f"текст пустой или длиннее {settings.max_text_chars} знаков")
    with tempfile.TemporaryDirectory(prefix="demo-process-record-") as tmp:
        store = CapStore(Path(tmp) / "record.sqlite3", settings.daily_budget_rub, runs_per_hour=1, retention_days=1)
        deps = Deps(gateway, store, settings) if clock is None else Deps(gateway, store, settings, clock)
        state = run(secrets.token_urlsafe(16), masked, deps)
    if state["status"] != "done":
        raise RecordError(f"прогон не дошёл до конца: {state['error']}")
    data = {"name": name, "title": title, "text": masked, "process": state["process"], "improve": state["improve"],
            "steps": state["steps"], "totals": state["totals"]}
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None, gateway: ChatClient | None = None) -> int:
    """``gateway``: for tests; the command line always records live."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--id", required=True, help="id in --source, also the file name")
    ap.add_argument("--title", help=f"button name; default for {', '.join(TITLES)} is the page's")
    ap.add_argument("--source", type=Path, default=SOURCE)
    ap.add_argument("--out", type=Path, default=ROOT / "app" / "recorded")
    ap.add_argument("--max-rub", type=float, default=3.0, help="cap of this recording's spend")
    ap.add_argument("--force", action="store_true", help="overwrite an existing file")
    args = ap.parse_args(argv)

    settings = replace(Settings.from_env(), daily_budget_rub=args.max_rub)
    if gateway is None:
        if not settings.live:
            print("LLM_API_KEY и LLM_BASE_URL не заданы: живая запись невозможна", file=sys.stderr)
            return 2
        gateway = Gateway(settings.base_url, settings.api_key)
    worst = run_worst_rub(settings)
    if worst > args.max_rub:
        print(f"прогон стоит до {worst:.2f} ₽, больше --max-rub {args.max_rub}", file=sys.stderr)
        return 2
    try:
        item = read_text(args.source, args.id)
        title = args.title or item.get("title") or TITLES.get(args.id) or args.id
        path = record(args.id, str(title), item["text"], gateway, settings, args.out, force=args.force)
    except RecordError as e:
        print(str(e), file=sys.stderr)
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    flags, t = data["process"]["flags"], data["totals"]
    print(f"{path}: шагов {len(data['process']['steps'])}, без цитаты {len(flags['unquoted'])}, "
          f"чисел отсечено {len(flags['numbers_dropped'])}, изменений {len(data['improve']['changes'])}, "
          f"{t['ms'] / 1000:.1f} с, {t['rub']:.2f} ₽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
