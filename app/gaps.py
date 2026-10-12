"""Which question is next (docs/SPEC-analyst.md, «Пробелы и их приоритет»): the code picks the gap,
a model only words it.

Gaps, by priority:

1. ``start_end`` — no start or no end of the process (or no draft yet);
2. ``volume`` — how many times a month the process runs (assumption 100);
3. ``<step>.role`` — a step with no role or the role «Кто-то»;
4. ``<step>.branches`` — a decision with fewer than two branches (app.checks.check_draft drops a
   branch that leads nowhere);
5. ``rate`` — the price of an hour (assumption 700 ₽);
6. ``<step>.minutes`` — unknown minutes of the step with the largest share of hours;
7. ``<step>.wait_hours`` — unknown waiting before the step after a handoff between roles with the
   largest share of days;
8. ``<role>.people`` — how many people in the role with the largest load.

The key of a gap is also the key of the number it asks for (``answered`` in the API). One gap — one
question: a key once asked is never asked again, whatever the answer («не знаю» leaves the
assumption).

«The current calculation» is web/engine.js on the defaults (docs/SPEC.md, «Расчёт»), ported here as
far as the choice needs it: the shares of branches (a loop back gets 0.2), the visits of each step
from the absorbing Markov chain, minutes from the draft or the answers, else 15 for a task and 5 for
a decision. Steps of an external role and of the role «Система» are not asked about minutes or people.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.checks import VAGUE_ROLE, cut, normalize

# web/engine.js DEFAULTS
DEFAULTS = {"perMonth": 100, "rate": 700, "hoursPerPerson": 120, "taskMin": 15, "decisionMin": 5, "handoffH": 4,
            "loopShare": 0.2}
ORDER = ("start_end", "volume", "role", "branches", "rate", "minutes", "wait", "people")
# what each kind asks for (app.understand), None — not a number
FIELD = {"start_end": None, "volume": "volume", "role": None, "branches": None, "rate": "rate",
         "minutes": "minutes", "wait": "wait_hours", "people": "people"}

# The four hints of the page (docs/SPEC-analyst.md, «Что видит посетитель») and what the first
# question calls the process.
TOPICS = {"заявки": "заявка клиента", "счета": "оплата счёта поставщика", "возвраты": "возврат товара",
          "выход": "выход нового сотрудника"}

_VAGUE = re.compile(r"^(?:кто[- ]?то|некто|неизвестн\w*|не указан\w*|не ясно|неясно|без роли|\?+|-+|—)$")
_SYSTEM = re.compile(r"^(?:система|программа|crm|срм|бот|робот)$")
NAME_IN_QUESTION = 60


@dataclass(frozen=True)
class Gap:
    key: str            # "volume", "s3.minutes", "r2.people"…
    kind: str           # one of ORDER
    step: str | None = None
    role: str | None = None
    about: str = ""     # what to find out, by name: what the ask model gets
    template: str = ""  # the question when the model gives none

    @property
    def field(self) -> str | None:
        return FIELD[self.kind]


def is_vague_role(name: str) -> bool:
    return bool(_VAGUE.match(normalize(name or ""))) or normalize(name or "") == normalize(VAGUE_ROLE)


def is_system_role(name: str) -> bool:
    return bool(_SYSTEM.match(normalize(name or "")))


# ---------- web/engine.js, the part the choice needs ----------

def _outs(step: dict) -> list[str]:
    if step["kind"] == "decision":
        return [b["to"] for b in step.get("branches") or []]
    return list(step.get("next") or [])


def _reach(by: dict, start: str) -> set[str]:
    seen, todo = {start}, [start]
    while todo:
        s = by.get(todo.pop())
        for t in _outs(s) if s else []:
            if t in by and t not in seen:
                seen.add(t)
                todo.append(t)
    return seen


def edges(process: dict) -> list[tuple[str, str, float]]:
    """(from, to, share): a branch that loops back gets loopShare, the rest share what is left."""
    by = {s["id"]: s for s in process["steps"]}
    out = []
    for s in process["steps"]:
        targets = [t for t in _outs(s) if t in by]
        if not targets or s["kind"] == "end":
            continue
        loop = [s["kind"] == "decision" and s["id"] in _reach(by, t) for t in targets]
        n_loop = sum(loop)
        n_fwd = len(targets) - n_loop
        split = n_loop and n_fwd and n_loop * DEFAULTS["loopShare"] < 1
        for t, is_loop in zip(targets, loop):
            if split:
                p = DEFAULTS["loopShare"] if is_loop else (1 - n_loop * DEFAULTS["loopShare"]) / n_fwd
            else:
                p = 1 / len(targets)
            out.append((s["id"], t, p))
    return out


def visits(process: dict, edge_list: list | None = None) -> dict[str, float]:
    """Expected passes of a request through each step (at most 200 iterations, as engine.js)."""
    steps = process["steps"]
    if not steps:
        return {}
    edge_list = edges(process) if edge_list is None else edge_list
    start = next((s["id"] for s in steps if s["kind"] == "start"), steps[0]["id"])
    v = {s["id"]: 0.0 for s in steps}
    for _ in range(200):
        nv = {sid: (1.0 if sid == start else 0.0) for sid in v}
        for a, b, p in edge_list:
            nv[b] += v[a] * p
        diff = max(abs(nv[k] - v[k]) for k in v)
        v = nv
        if diff < 1e-12:
            break
    return v


def minutes_of(step: dict, assumptions: dict) -> float:
    if step["kind"] not in ("task", "decision"):
        return 0.0
    known = (assumptions.get("minutes") or {}).get(step["id"])
    if known is not None:
        return float(known)
    if step.get("minutes") is not None:
        return float(step["minutes"])
    return float(DEFAULTS["decisionMin"] if step["kind"] == "decision" else DEFAULTS["taskMin"])


# ---------- the gaps ----------

def _name(text: str) -> str:
    """A step or role name inside a question: short and without digits."""
    return cut(strip_digits(text), NAME_IN_QUESTION) or "этот шаг"


def _lower(name: str) -> str:
    """«Сметчик» -> «сметчик» inside a sentence; «ОТК» stays."""
    return name if len(name) > 1 and name[:2].isupper() else name[:1].lower() + name[1:]


def _start_end(process: dict | None, topic: str | None, phrase: str | None) -> Gap | None:
    if not process or not process.get("steps"):
        what = TOPICS.get(topic or "")
        about = "Как проходит процесс по шагам: с чего всё начинается, кто что делает и чем заканчивается."
        if what:
            about += f" Тема: {what}."
        template = (f"Расскажите по шагам, как у вас проходит {what}: с чего всё начинается и чем заканчивается?"
                    if what else "Расскажите по шагам, как устроен этот процесс: с чего начинается и чем заканчивается?")
        return Gap("start_end", "start_end", about=about, template=template)
    kinds = {s["kind"] for s in process["steps"]}
    if "start" in kinds and "end" in kinds:
        return None
    if "start" not in kinds and "end" not in kinds:
        return Gap("start_end", "start_end", about="С чего начинается процесс и чем он заканчивается.",
                   template="С чего начинается этот процесс и чем он заканчивается?")
    if "start" not in kinds:
        return Gap("start_end", "start_end", about="С чего начинается процесс: какое событие запускает работу.",
                   template="С чего начинается этот процесс — что запускает работу?")
    return Gap("start_end", "start_end", about="Чем заканчивается процесс: что считается результатом.",
               template="Чем заканчивается этот процесс — что считается результатом?")


def find_gaps(process: dict | None, assumptions: dict | None = None, topic: str | None = None,
              phrase: str | None = None) -> list[Gap]:
    """Every open gap of the draft in priority order (within a kind: the largest share first)."""
    a = assumptions or {}
    gaps: list[Gap] = []
    first = _start_end(process, topic, phrase)
    if first:
        gaps.append(first)
    if a.get("perMonth") is None:
        gaps.append(Gap("volume", "volume", about="Сколько раз в месяц проходит этот процесс: сколько заявок или "
                                                 "случаев бывает за месяц.",
                        template="Сколько примерно таких случаев у вас бывает за месяц?"))
    if not process or not process.get("steps"):
        if a.get("rate") is None:
            gaps.append(_rate())
        return gaps

    steps = process["steps"]
    roles = {r["id"]: r for r in process.get("roles") or []}
    external = {rid for rid, r in roles.items() if r.get("external")}
    system = {rid for rid, r in roles.items() if is_system_role(r["name"])}
    work = [s for s in steps if s["kind"] in ("task", "decision")]

    for s in work:
        role = roles.get(s["role"])
        if role is None or is_vague_role(role["name"]):
            name = _name(s["name"])
            gaps.append(Gap(f"{s['id']}.role", "role", step=s["id"], role=s["role"] or None,
                            about=f"Кто делает шаг «{name}»: какая роль или должность.",
                            template=f"Кто у вас делает «{name}»?"))
    for s in steps:
        if s["kind"] == "decision" and len(s.get("branches") or []) < 2:
            name = _name(s["name"])
            gaps.append(Gap(f"{s['id']}.branches", "branches", step=s["id"],
                            about=f"Какие варианты бывают на развилке «{name}» и куда идёт процесс в каждом.",
                            template=f"Какие варианты бывают на шаге «{name}» и что происходит дальше в каждом?"))
    if a.get("rate") is None:
        gaps.append(_rate())

    edge_list = edges(process)
    v = visits(process, edge_list)
    by = {s["id"]: s for s in steps}
    known_minutes = a.get("minutes") or {}
    shares = []
    for i, s in enumerate(work):
        if s["role"] in external or s["role"] in system:
            continue
        if s.get("minutes") is None and s["id"] not in known_minutes:
            shares.append((-v.get(s["id"], 0) * minutes_of(s, a), i, s))
    for _, _, s in sorted(shares, key=lambda x: x[:2]):
        name = _name(s["name"])
        role = roles.get(s["role"])
        who = f" (делает {role['name']})" if role and not is_vague_role(role["name"]) else ""
        gaps.append(Gap(f"{s['id']}.minutes", "minutes", step=s["id"], role=s["role"],
                        about=f"Сколько минут работы занимает шаг «{name}»{who} за один раз.",
                        template=f"Сколько времени работы уходит на «{name}» за один раз?"))

    known_wait = a.get("wait_hours") or {}
    waits: dict[str, float] = {}
    sources: dict[str, str] = {}
    for x, y, p in edge_list:
        src, dst = by[x], by[y]
        if dst["kind"] not in ("task", "decision") or src["role"] == dst["role"]:
            continue
        if dst.get("wait_hours") is not None or y in known_wait:
            continue
        waits[y] = waits.get(y, 0) + v.get(x, 0) * p * DEFAULTS["handoffH"]
        if y not in sources or v.get(x, 0) * p > v.get(sources[y], 0):
            sources[y] = x
    order = {s["id"]: i for i, s in enumerate(steps)}
    for y in sorted(waits, key=lambda k: (-waits[k], order[k])):
        dst, src = by[y], by[sources[y]]
        name = _name(dst["name"])
        role_a = roles.get(src["role"], {}).get("name") or "предыдущей роли"
        role_b = roles.get(dst["role"], {}).get("name") or "следующей роли"
        gaps.append(Gap(f"{y}.wait_hours", "wait", step=y, role=dst["role"],
                        about=f"Сколько обычно ждёт заявка перед шагом «{name}», когда её передают от «{_name(role_a)}» "
                              f"к «{_name(role_b)}»: часы или дни.",
                        template=f"Сколько обычно проходит времени, пока за «{name}» возьмутся после передачи от "
                                 f"«{_name(role_a)}»?"))

    per_month = a.get("perMonth") or DEFAULTS["perMonth"]
    known_people = a.get("people") or {}
    load: dict[str, float] = {}
    for s in work:
        r = s["role"]
        if r in external or r in system or r not in roles or is_vague_role(roles[r]["name"]):
            continue
        load[r] = load.get(r, 0) + per_month * v.get(s["id"], 0) * minutes_of(s, a) / 60 / DEFAULTS["hoursPerPerson"]
    role_order = {rid: i for i, rid in enumerate(roles)}
    for r in sorted(load, key=lambda k: (-load[k], role_order[k])):
        if r in known_people:
            continue
        name = _name(roles[r]["name"])
        gaps.append(Gap(f"{r}.people", "people", role=r, about=f"Сколько человек работает в роли «{name}».",
                        template=f"Сколько человек у вас в роли «{name}»?"))

    rank = {k: i for i, k in enumerate(ORDER)}
    return sorted(gaps, key=lambda g: rank[g.kind])  # stable: the order within a kind stays


def _rate() -> Gap:
    return Gap("rate", "rate", about="Во сколько компании обходится час работы сотрудника: зарплата с налогами в "
                                    "пересчёте на час.",
               template="Во сколько вам обходится час работы сотрудника — с зарплатой и налогами?")


def next_gap(process: dict | None, assumptions: dict | None, asked, topic: str | None = None,
             phrase: str | None = None) -> Gap | None:
    """The open gap with the highest priority that was not asked yet; None — nothing to ask."""
    asked = set(asked or ())
    return next((g for g in find_gaps(process, assumptions, topic, phrase) if g.key not in asked), None)


# ---------- the wording ----------

_1C = re.compile(r"(?<!\w)1[сСcC](?!\w)")  # «1С» is a product name, not a number
_1C_OR_DIGITS = re.compile(r"((?<!\w)1[сСcC](?!\w))|\d+")
_PAREN_DIGITS = re.compile(r"\s*\([^()]*\d[^()]*\)")
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
MAX_QUESTION_CHARS = 160
MIN_QUESTION_CHARS = 12


def digits_in(text: str) -> int:
    return len(re.findall(r"\d+", _1C.sub("", text or "")))


def strip_digits(text: str) -> str:
    """Digits out, «1С» stays."""
    return " ".join(_1C_OR_DIGITS.sub(lambda m: m[1] or " ", text or "").split())


def clean_question(text: str, template: str) -> tuple[str, int, bool]:
    """The model's wording without digits: a bracket with digits goes away whole; if digits are still
    left, or the question is empty, too long or not Russian, the template is used (its step names
    lose their digits). Returns (question, digit groups removed, template used)."""
    raw = " ".join(str(text or "").split()).strip(" \"'«»")
    found = digits_in(raw)
    clean = _PAREN_DIGITS.sub("", raw) if found else raw
    clean = " ".join(clean.split())
    if (digits_in(clean) or len(clean) < MIN_QUESTION_CHARS or len(clean) > MAX_QUESTION_CHARS
            or not _CYRILLIC.search(clean)):
        return cut(strip_digits(template), MAX_QUESTION_CHARS), found, True
    return clean, found, False
