"""The repo is public: no private names, local paths, employer words or off-limits niches in any
file (docs/SPEC.md, «Страница»). The words live outside the repo, in ~/.config/tavriq/stopwords or
$TAVRIQ_STOPWORDS (one regex per line, # for comments); without that file only local paths are checked.
The path pattern is split so that this file does not match itself."""

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "var"}
CONTRACT = {"docs/SPEC.md", "docs/template.md", "docs/checklist.md"}
WORDS_FILE = Path(os.environ.get("TAVRIQ_STOPWORDS", "~/.config/tavriq/stopwords")).expanduser()


def _words() -> list[str]:
    if not WORDS_FILE.is_file():
        return []
    lines = (line.strip() for line in WORDS_FILE.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


FORBIDDEN = re.compile("|".join(["/Us" + "ers/", *_words()]), re.IGNORECASE)


def _text_files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if path.is_file() and not SKIP_DIRS.intersection(rel.parts):
            try:
                yield rel.as_posix(), path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


def _hits(only_contract: bool) -> list[str]:
    out = []
    for rel, text in _text_files():
        if (rel in CONTRACT) != only_contract:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            out += [f"{rel}:{n}: {m.group(0)}" for m in FORBIDDEN.finditer(line)]
    return out


def test_no_private_words_outside_the_contract():
    assert _hits(only_contract=False) == []


def test_no_private_words_in_the_contract():
    assert _hits(only_contract=True) == []


def test_private_words_are_checked():
    if not WORDS_FILE.is_file():
        pytest.skip(f"{WORDS_FILE} is absent: only local paths were checked")
    assert _words()
