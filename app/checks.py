"""Code checks of the model answers (docs/SPEC.md, «Контракт данных», «Кто что решает»).

``check_process``: an Extract answer -> Process in the contract's shape and a list of errors. An
error (ids, kinds, a broken or unreachable graph, too many steps or roles) goes back to the model
for one retry. What is not an error is fixed here and reported in ``flags``:

- a quote that is not in the text (after normalization: case, whitespace, ё/е, dashes, quote
  marks) becomes ``null`` and the step goes to ``flags.unquoted``;
- a number the quote does not contain becomes ``null`` and goes to ``flags.numbers_dropped``.
  «Contains» allows the unit conversions the prompt asks for: an hour is 60 minutes, a day is 8
  working hours (or 24), a week 40 hours (or 168), the middle of a range «2–3 дня»;
- texts longer than their limit are cut.

``check_draft``: an interview draft (docs/SPEC-analyst.md) -> Process of what is said so far, or
None when it cannot be drawn. The checks of quotes and numbers are the same; what a draft may still
lack is not an error but repaired, and app.gaps asks about it: no start or end yet, a decision with
fewer than two branches (a branch or a next step that is not in the draft is dropped), a step with
no role (it goes to the role «Кто-то»), a task whose next step is not known yet (``next: []``),
steps not reachable yet. Ids, kinds, more than one start and the limits stay errors.

``check_improve``: an Improve answer -> Improve and errors (unknown steps, an action the step's
kind does not allow, a merge that is not into a neighbouring task of the same role). A digit in
``how``, ``risk`` or ``summary`` empties the field and goes to ``flags.numbers_in_text`` of the
Improve (``{"step", "field"}``, step ``null`` for the summary); «1С» is a name and does not count.
A step of an external role (``roles[].external``: a client, a supplier, a bank…) may only be kept:
any other action becomes ``keep`` with empty ``how``/``risk`` and goes to ``flags.external_kept``
(``{"step", "action"}``) — not an error, no retry.
"""

from __future__ import annotations

import math
import re

from app.schema import ACTIONS, ALLOWED_ACTIONS, KINDS, LIMITS, ROLE_ID, STEP_ID

MIN_QUOTE_CHARS = 6  # «и», «КП» are found anywhere and prove nothing
MAX_ERRORS = 12

# ---------- texts ----------


def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def cut(text: str, limit: int) -> str:
    """At most ``limit`` characters, on a word boundary when there is one in the last third."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    space = head.rfind(" ")
    return (head[:space] if space > limit * 2 // 3 else head).rstrip(" ,;:—-")


_DASHES = re.compile(r"[‐-―−]")
_QUOTES = re.compile(r"[«»“”„\"']")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = _DASHES.sub("-", text.lower().replace("ё", "е"))
    text = _QUOTES.sub('"', text)
    return _SPACE.sub(" ", text).strip()


_ELLIPSIS = re.compile(r"\s*(?:…|\.\.\.)\s*")
_EDGE = " .,;:!?\"-"


def quote_in_text(quote: str, text_norm: str) -> bool:
    """The quote (or its parts around «…», in order) is in the normalized text."""
    pos = 0
    parts = [p.strip(_EDGE) for p in _ELLIPSIS.split(normalize(quote))]
    parts = [p for p in parts if p]
    if not parts or sum(len(p) for p in parts) < MIN_QUOTE_CHARS:
        return False
    for part in parts:
        found = text_norm.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


# ---------- numbers in a quote ----------

_WORDS = {
    "один": 1, "одна": 1, "одну": 1, "одного": 1, "одной": 1, "полтора": 1.5, "полторы": 1.5,
    "два": 2, "две": 2, "двух": 2, "пару": 2, "пара": 2, "пары": 2, "три": 3, "трех": 3, "четыре": 4,
    "четырех": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8,
    "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10, "пятнадцать": 15, "пятнадцати": 15,
    "двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30, "сорок": 40, "сорока": 40,
}
_N = r"\d+(?:[.,]\d+)?|" + "|".join(sorted(_WORDS, key=len, reverse=True))
_QTY = re.compile(rf"(?<!\w)({_N})(?!\w)(?:\s*(?:-|до|или)\s*({_N})(?!\w))?(?:\s+([а-яa-z]+))?(?:\s+([а-яa-z]+))?")
_BARE = re.compile(r"(?<!\w)(минуту|час|день|сутки|неделю|неделя|полчаса|полдня)(?!\w)")
_PREFIXES = (("мин", (1,)), ("полчас", (30,)), ("час", (60,)), ("ч", (60,)), ("полдн", (240, 720)),
             ("дн", (480, 1440)), ("ден", (480, 1440)), ("сут", (1440,)), ("недел", (2400, 10080)))


def _num(token: str) -> float:
    return float(_WORDS[token]) if token in _WORDS else float(token.replace(",", "."))


def _unit_minutes(word: str | None) -> tuple[float, ...]:
    """Minutes in one unit named by ``word``; () when it is not a unit of time."""
    if not word:
        return ()
    if word == "ч":
        return (60,)
    for prefix, minutes in _PREFIXES:
        if prefix != "ч" and word.startswith(prefix):
            return minutes
    return ()


def quote_numbers(quote: str) -> tuple[set[float], set[float]]:
    """Values a quote supports: (bare numbers, durations in minutes)."""
    text = normalize(quote)
    bare, minutes = set(), set()
    for m in _QTY.finditer(text):
        a = _num(m[1])
        values = {a} if m[2] is None else {a, _num(m[2]), (a + _num(m[2])) / 2}
        bare |= values
        unit = m[3]
        if unit and (unit.startswith("рабоч") or unit.startswith("календар")):
            unit = m[4]
        minutes |= {v * k for v in values for k in _unit_minutes(unit)}
    for m in _BARE.finditer(text):
        if not re.search(rf"(?:{_N})\s+$", text[:m.start()]):
            minutes |= set(_unit_minutes(m[1]))
    return bare, minutes


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 + 0.005 * abs(b)


def number_in_quote(value: float, field: str, quote: str | None) -> bool:
    if not quote:
        return False
    bare, minutes = quote_numbers(quote)
    scale = 1 if field == "minutes" else 60
    return any(_close(value, b) for b in bare) or any(_close(value, m / scale) for m in minutes)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


# ---------- Process ----------


def _targets(step: dict) -> list[str]:
    return [b["to"] for b in step["branches"]] if step["kind"] == "decision" else list(step["next"])


def _reach(start: str, edges: dict[str, list[str]]) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        for nxt in edges.get(todo.pop(), []):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


VAGUE_ROLE = "Кто-то"  # a draft's step whose doer is not named yet (app.gaps asks who)


def check_process(raw: dict, text: str) -> tuple[dict, list[str]]:
    """``raw``: the model's answer (top-level keys and their types already checked by
    app.pipeline.parse_answer). ``text``: what the model got (contacts masked)."""
    return _check_process(raw, text, draft=False)


def check_draft(raw: dict, text: str) -> tuple[dict | None, list[str]]:
    """An interview draft: (Process, []) or (None, errors). ``text``: the visitor's words only (the
    phrase and the answers, contacts masked), so a quote from a question is not found."""
    process, errors = _check_process(raw, text, draft=True)
    return (None, errors) if errors else (process, [])


def _free_role_id(role_ids: set[str]) -> str | None:
    return next((f"r{n}" for n in range(1, LIMITS["roles"] + 1) if f"r{n}" not in role_ids), None)


def _check_process(raw: dict, text: str, draft: bool) -> tuple[dict, list[str]]:
    errors: list[str] = []
    text_norm = normalize(text)
    title = cut(_str(raw.get("title")), LIMITS["title"]) or "Процесс"

    roles, role_ids = [], set()
    for i, r in enumerate(raw.get("roles") or []):
        if not isinstance(r, dict):
            errors.append(f"roles[{i}] — не объект")
            continue
        rid, name = _str(r.get("id")), cut(_str(r.get("name")), LIMITS["role_name"])
        if not ROLE_ID.fullmatch(rid):
            errors.append(f"роль «{name}»: id «{rid}», нужен r1…r6")
        elif rid in role_ids:
            errors.append(f"id роли {rid} повторяется")
        if not name:
            if not draft:
                errors.append(f"роль {rid}: пустое имя")
            name = VAGUE_ROLE
        external = r.get("external")
        if not isinstance(external, bool) and not draft:
            errors.append(f"роль {rid}: external должен быть true или false")
        role_ids.add(rid)
        roles.append({"id": rid, "name": name, "external": external is True})
    if not roles and not draft:
        errors.append("нет ни одной роли")
    elif len(roles) > LIMITS["roles"]:
        errors.append(f"ролей {len(roles)}, можно не больше {LIMITS['roles']}")

    def vague_role() -> str:
        """A draft's role for a step without one: the existing «Кто-то» or a new one."""
        for r in roles:
            if normalize(r["name"]) == normalize(VAGUE_ROLE) and not r["external"]:
                return r["id"]
        rid = _free_role_id(role_ids)
        if rid is None:
            errors.append(f"ролей уже {LIMITS['roles']}, а у шага нет роли")
            return ""
        role_ids.add(rid)
        roles.append({"id": rid, "name": VAGUE_ROLE, "external": False})
        return rid

    steps, ids = [], []
    unquoted, dropped = [], []
    raw_steps = raw.get("steps") or []
    if len(raw_steps) > LIMITS["steps"]:
        errors.append(f"шагов {len(raw_steps)}, можно не больше {LIMITS['steps']}: объедини мелкие")
    if draft and not raw_steps:
        errors.append("нет ни одного шага")
    # a draft keeps only the edges that lead to a step it has, and none into the start
    known = {_str(s.get("id")) for s in raw_steps if isinstance(s, dict)}
    first_start = next((_str(s.get("id")) for s in raw_steps if isinstance(s, dict) and s.get("kind") == "start"), None)

    def keep(target: str) -> bool:
        return not draft or (target in known and target != first_start)

    for i, s in enumerate(raw_steps):
        if not isinstance(s, dict):
            errors.append(f"steps[{i}] — не объект")
            continue
        sid, kind = _str(s.get("id")), s.get("kind")
        name = cut(_str(s.get("name")), LIMITS["step_name"])
        if not STEP_ID.fullmatch(sid):
            errors.append(f"шаг «{name}»: id «{sid}», нужен s1…s15")
        elif sid in ids:
            errors.append(f"id шага {sid} повторяется")
        ids.append(sid)
        if kind not in KINDS:
            errors.append(f"{sid}: kind «{kind}», нужен один из {', '.join(KINDS)}")
            continue
        if not name:
            errors.append(f"{sid}: пустое имя")
        role = _str(s.get("role"))
        if role not in role_ids:
            if draft:
                role = vague_role()
            else:
                errors.append(f"{sid}: роль «{role}» не из roles")
        step = {"id": sid, "kind": kind, "name": name, "role": role}

        if kind == "decision":
            branches = []
            for b in s.get("branches") if isinstance(s.get("branches"), list) else []:
                if isinstance(b, dict) and keep(_str(b.get("to"))):
                    branches.append({"label": cut(_str(b.get("label")), LIMITS["label"]), "to": _str(b.get("to"))})
            if draft:
                branches = branches[:LIMITS["branches_max"]]
            else:
                if not LIMITS["branches_min"] <= len(branches) <= LIMITS["branches_max"]:
                    errors.append(f"{sid}: у решения {len(branches)} веток, нужно 2–3")
                if any(not b["label"] for b in branches):
                    errors.append(f"{sid}: у ветки решения нет подписи")
            step["branches"] = branches
        else:
            nxt = [_str(n) for n in s.get("next")] if isinstance(s.get("next"), list) else []
            if draft:
                nxt = [] if kind == "end" else [n for n in nxt if keep(n)][:1]
            if kind == "end" and nxt:
                errors.append(f"{sid}: у конца next должен быть пустым")
            if kind != "end" and len(nxt) != 1 and not draft:
                errors.append(f"{sid}: у {kind} в next ровно один шаг, а не {len(nxt)}")
            step["next"] = nxt

        quote = cut(_str(s.get("quote")), LIMITS["quote"]) or None
        if quote is not None and not quote_in_text(quote, text_norm):
            quote = None
        if quote is None:
            unquoted.append(sid)
        step["quote"] = quote

        if kind in ("task", "decision"):
            step["tool"] = cut(_str(s.get("tool")), LIMITS["tool"]) or None
            for field in ("minutes", "wait_hours"):
                value, given = _number(s.get(field)), s.get(field)
                if given is not None and (value is None or value < 0 or value > LIMITS[field]
                                          or not number_in_quote(value, field, quote)):
                    dropped.append({"step": sid, "field": field, "value": given})
                    value = None
                step[field] = value
        steps.append(step)

    by_id = {s["id"]: s for s in steps}
    starts = [s["id"] for s in steps if s["kind"] == "start"]
    if len(starts) > 1 or (len(starts) != 1 and not draft):
        errors.append(f"start должен быть ровно один, а их {len(starts)}")
    if not any(s["kind"] == "end" for s in steps) and not draft:
        errors.append("нет ни одного end")
    edges = {s["id"]: _targets(s) for s in steps}
    for sid, targets in edges.items():
        for t in targets:
            if t not in by_id:
                errors.append(f"{sid} ведёт на несуществующий шаг «{t}»")
            elif starts and t == starts[0]:
                errors.append(f"{sid} ведёт в start, а в start ничего не входит")
    if len(starts) == 1 and not errors and not draft:
        reached = _reach(starts[0], edges)
        lost = [s for s in by_id if s not in reached]
        if lost:
            errors.append(f"из start не дойти до {', '.join(lost)}")
        ends = {s["id"] for s in steps if s["kind"] == "end"}
        reverse: dict[str, list[str]] = {}
        for sid, targets in edges.items():
            for t in targets:
                reverse.setdefault(t, []).append(sid)
        to_end = set().union(*(_reach(e, reverse) for e in ends)) if ends else set()
        stuck = [s for s in by_id if s not in to_end]
        if stuck:
            errors.append(f"из {', '.join(stuck)} не дойти до end: процесс там зацикливается")

    process = {"title": title, "roles": roles, "steps": steps,
               "flags": {"unquoted": unquoted, "numbers_dropped": dropped}}
    return process, errors[:MAX_ERRORS]


# ---------- Improve ----------

_DIGIT = re.compile(r"\d")
_1C = re.compile(r"(?<!\w)1[сСcC](?!\w)")  # «1С» is a product name, not a number


def _clean_text(value, field: str, step: str | None, flagged: list[dict]) -> str:
    text = cut(_str(value), LIMITS[field])
    if _DIGIT.search(_1C.sub("", text)):
        flagged.append({"step": step, "field": field})
        return ""
    return text


def check_improve(raw: dict, process: dict) -> tuple[dict, list[str]]:
    errors: list[str] = []
    by_id = {s["id"]: s for s in process["steps"]}
    external = {r["id"] for r in process["roles"] if r.get("external")}
    changes, seen, flagged, kept = [], set(), [], []
    for i, c in enumerate(raw.get("changes") or []):
        if not isinstance(c, dict):
            errors.append(f"changes[{i}] — не объект")
            continue
        sid, action = _str(c.get("step")), c.get("action")
        step = by_id.get(sid)
        if step is None:
            errors.append(f"changes[{i}]: шага «{sid}» нет в процессе")
            continue
        if sid in seen:
            errors.append(f"{sid}: больше одной записи")
            continue
        seen.add(sid)
        if step["role"] in external and action in ACTIONS and action != "keep":
            # a step outside the company is not ours to change: kept, reported, no retry
            kept.append({"step": sid, "action": action})
            changes.append({"step": sid, "action": "keep", "merge_into": None, "how": "", "risk": ""})
            continue
        if action not in ALLOWED_ACTIONS[step["kind"]]:
            errors.append(f"{sid}: у {step['kind']} нельзя {action}, можно {', '.join(ALLOWED_ACTIONS[step['kind']])}")
            continue
        merge_into = None
        if action == "merge":
            merge_into = _str(c.get("merge_into"))
            other = by_id.get(merge_into)
            if other is None or merge_into == sid:
                errors.append(f"{sid}: merge_into «{merge_into}» — не другой шаг процесса")
                continue
            if other["kind"] != "task" or other["role"] != step["role"]:
                errors.append(f"{sid}: сливать можно только с задачей той же роли, {merge_into} не подходит")
                continue
            if merge_into not in _targets(step) and sid not in _targets(other):
                errors.append(f"{sid}: {merge_into} не соседний шаг")
                continue
        changes.append({"step": sid, "action": action, "merge_into": merge_into,
                        "how": _clean_text(c.get("how"), "how", sid, flagged),
                        "risk": _clean_text(c.get("risk"), "risk", sid, flagged)})
    summary = _clean_text(raw.get("summary"), "summary", None, flagged)
    flags = {"numbers_in_text": flagged, "external_kept": kept}
    return {"changes": changes, "summary": summary, "flags": flags}, errors[:MAX_ERRORS]
