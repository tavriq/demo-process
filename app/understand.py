"""What the code takes from an answer (docs/SPEC-analyst.md, «Кто что решает»): numbers for the
assumptions, «не знаю», «ничего, сразу передают» to a «between» question, and the draft model's
``answers`` checked against the answer.

Numbers come by regular expressions with units, as in app.checks: digits («120», «1,5», «60 000»),
words («сто двадцать», «полторы тысячи», «двое»), ranges («10–15», «от двух до трёх» -> the middle).
What the answer to each gap becomes:

- ``volume`` — times a month: «в день» ×21 working days, «в неделю» ×4.3, «в год» /12, a bare number
  is a month;
- ``rate`` — rubles an hour: «в час» as is, a monthly salary («в месяц», «зарплата», or a bare number
  of 5 000 and more) /168 hours of a full-time month, «в день» /8;
- ``minutes`` — minutes of work: an hour is 60, a working day 480, a week 2 400, «полчаса» 30; a
  bare number is minutes;
- ``wait_hours`` — hours of waiting: a day (and «сутки») is 8 working hours, a week 40, minutes /60;
  a bare number is hours;
- ``people`` — people: «двое», «трое», «сам», «один».

A number the code did not find may come from the draft model (``answers``): it stays only if its
quote is in the last answer and holds that number (app.checks.number_in_quote for time, the same
parsing for the rest), the target is a step or role of the draft and the value is in its limits.
"""

from __future__ import annotations

import math
import re

from app.checks import _WORDS as CHECK_WORDS
from app.checks import normalize, number_in_quote, quote_in_text

UNITS = {"volume": "заявок/мес", "rate": "₽/ч", "minutes": "мин", "wait_hours": "ч", "people": "чел"}
# What an assumption may be; outside it is not an answer to the question.
LIMITS = {"volume": (1, 100_000), "rate": (50, 20_000), "minutes": (0.5, 10_000), "wait_hours": (0, 2_000),
          "people": (1, 500)}
WORK_DAYS_PER_MONTH = 21
WEEKS_PER_MONTH = 4.3
HOURS_PER_MONTH = 168   # a full-time month: a monthly salary -> the price of an hour
DAY_H, WEEK_H = 8, 40   # working hours, as the v1 prompt and web/engine.js (dayH 8)
SALARY_FROM = 5_000     # a bare rate this big is a month's pay, not an hour's

_EXTRA_WORDS = {
    "ноль": 0, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19, "пятьдесят": 50, "шестьдесят": 60,
    "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100, "ста": 100, "двести": 200, "триста": 300,
    "четыреста": 400, "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
    "полсотни": 50, "сотня": 100, "сотни": 100, "сотню": 100, "десяток": 10, "десятка": 10, "дюжина": 12,
    "двое": 2, "трое": 3, "четверо": 4, "пятеро": 5, "шестеро": 6, "семеро": 7, "трем": 3, "четырем": 4,
    "пятнадцать": 15, "тысяча": 1000, "тысячу": 1000,
}
WORDS = {**CHECK_WORDS, **_EXTRA_WORDS}
_MULT = {"тыс": 1000, "тысяч": 1000, "тысячи": 1000, "тысяча": 1000, "тысячу": 1000, "к": 1000, "k": 1000,
         "млн": 1_000_000}

_WORD = "|".join(sorted(WORDS, key=len, reverse=True))
# a multiplier right after the number; «к» only glued to digits («50к»): «10 к каждой заявке» is a preposition
_M = r"тыс\w*|млн(?!\w)|(?<=\d)[кk](?!\w)"
# one number: digits with a decimal part or thousands in groups, or a run of number words
_ONE = rf"(?:\d{{1,3}}(?:[ \u00a0]\d{{3}})+(?!\d)|\d+(?:[.,]\d+)?|(?:(?:{_WORD})(?!\w)\s*)+)"
_QTY = re.compile(
    rf"(?<![\w.,])(?:от\s+)?({_ONE})\s*({_M})?(?:\s*(?:-|–|—|до|или)\s*({_ONE})\s*({_M})?)?",
    re.IGNORECASE)

_DONT_KNOW = re.compile(
    r"(?<!\w)(?:не\s+знаю|не\s+помню|не\s+скажу|без\s+понятия|понятия\s+не\s+имею|сложно\s+сказать|трудно\s+сказать|"
    r"затрудняюсь|не\s+считал\w*|не\s+в\s+курсе|не\s+уверен\w*|хз|никто\s+не\s+считал|по-разному|по\s+разному)(?!\w)",
    re.IGNORECASE)
_ALONE = re.compile(r"(?<!\w)(?:я\s+сам|сам|сама|только\s+я|один|одна|в\s+одиночку)(?!\w)", re.IGNORECASE)

_PER = (("day", r"в\s+день|за\s+день|в\s+сутки|ежедневно|каждый\s+день|в\s+смену|за\s+смену"),
        ("week", r"в\s+неделю|за\s+неделю|еженедельно|каждую\s+неделю"),
        ("month", r"в\s+месяц|за\s+месяц|ежемесячно|каждый\s+месяц|в\s+мес\b"),
        ("quarter", r"в\s+квартал|за\s+квартал"),
        ("year", r"в\s+год|за\s+год|ежегодно"),
        ("hour", r"в\s+час|за\s+час|/\s*ч\b|/\s*час|час\s+работы|почасов"))
_TIME = re.compile(r"^\s*(минут\w*|мин\.?|полчаса|час\w*|ч\.?(?!\w)|дн\w*|день|сут\w*|недел\w*|месяц\w*)",
                   re.IGNORECASE)
_TIME_BEFORE = re.compile(r"(минут\w*|час\w*|дн\w*|день|сутки|недел\w*)\s*-?\s*$", re.IGNORECASE)
# «день-два», «час-полтора»: a range from one unit
_UNIT_RANGE = re.compile(r"(?<!\w)(минуту|час|день|сутки|неделю)\s*-\s*$", re.IGNORECASE)
_MONEY = re.compile(r"^\s*(?:тыс\w*\s*)?(?:руб\w*|р\.|р(?!\w)|₽)", re.IGNORECASE)
_SALARY = re.compile(r"зарплат|оклад|получа|в\s+месяц|за\s+месяц|в\s+мес\b", re.IGNORECASE)
_PAY = re.compile(r"зарплат|оклад|получа|налог|ставк|руб|₽|стоит|обходит", re.IGNORECASE)
# what is counted in a volume said in passing: «120 заявок в месяц», «пять новых сотрудников в месяц»
_NOUNS = r"(?:заяв|заказ|клиент|обращен|сч[её]т|возврат|сделк|штук|случа|сотрудник|договор|звонк|лид|поставк|отгрузк)"
_COUNTED_AFTER = re.compile(rf"^\s*(?:[а-яё]+\s+){{0,2}}?{_NOUNS}", re.IGNORECASE)
_COUNTED_BEFORE = re.compile(rf"{_NOUNS}[а-яё]*\s*$", re.IGNORECASE)
# «за час», «час» — a duration; «в час», «в день» — a rate or a volume, not this
_BARE_TIME = re.compile(r"(?<!\w)(?<!в\s)(полчаса|полтора\s+часа|час|минуту|день|сутки|неделю|полдня)(?!\w)",
                        re.IGNORECASE)

# «через два дня», «смета ждёт ещё день»: a wait before a step, not the time of the work (live 12.10: an
# off-topic answer about waiting gave a manager 960 minutes per request)
_DELAY = re.compile(r"(?<!\w)(?:через|спустя|жд[её]т|жд[её]м|ждут|ждать|ждали|ожида\w*)(?:\s+(?:ещ[её]|обычно|примерно|около|где-то|почти))*\s*$",
                    re.IGNORECASE)

def _num(token: str) -> float | None:
    token = token.strip().lower().replace("ё", "е")
    if not token:
        return None
    if token[0].isdigit():
        return float(re.sub(r"[ \u00a0]", "", token).replace(",", "."))
    total, parts = 0.0, token.split()
    for w in parts:
        if w not in WORDS:
            return None
        total += WORDS[w]
    return total


def _mult(word: str | None) -> float:
    if not word:
        return 1.0
    w = word.lower()
    return next((m for k, m in sorted(_MULT.items(), key=lambda x: -len(x[0])) if w.startswith(k)), 1.0)


def quantities(text: str) -> list[dict]:
    """Every number of the text: its value (the middle of a range), where it ends, what follows it
    (the next 40 characters) and what is before it (the previous 20)."""
    out = []
    for m in _QTY.finditer(text):
        a = _num(m[1])
        if a is None:
            continue
        a *= _mult(m[2])
        b = _num(m[3]) if m[3] else None
        if b is not None:
            b *= _mult(m[4] or m[2])
            if m[2] is None and m[4]:
                a *= _mult(m[4])
        value = a if b is None else (a + b) / 2
        out.append({"value": value, "start": m.start(), "end": m.end(), "after": text[m.end():m.end() + 40],
                    "before": text[max(0, m.start() - 20):m.start()], "text": m[0].strip()})
    return out


def _period(text: str) -> str | None:
    """The period said first in ``text``: «в день», «в месяц»…"""
    found = [(m.start(), name) for name, pattern in _PER if (m := re.search(pattern, text, re.IGNORECASE))]
    return min(found)[1] if found else None


def _clause(after: str) -> str:
    """What follows a number up to the next punctuation or number: its own period and unit only
    («120 заявок в месяц, то есть 6 в день» -> « заявок в месяц»)."""
    return re.split(r"[,.;:!?()]|\d", after, maxsplit=1)[0]


def _time_minutes(unit: str) -> float | None:
    """Minutes in one unit: work time, a working day is 8 hours."""
    u = unit.lower().rstrip(".")
    if u.startswith("полчас"):
        return 30
    if u.startswith("мин"):
        return 1
    if u.startswith("час") or u == "ч":
        return 60
    if u.startswith("дн") or u == "день" or u.startswith("сут"):
        return DAY_H * 60
    if u.startswith("недел"):
        return WEEK_H * 60
    if u.startswith("месяц"):
        return 4 * WEEK_H * 60
    return None


def _duration(q: dict, default_minutes: float) -> float:
    """A quantity as minutes: by its unit after it («20 минут») or before it («минут двадцать»,
    «день-два» — from one to two days)."""
    one = _UNIT_RANGE.search(q["before"])
    if one:
        per = _time_minutes(one[1])
        if per is not None:
            return (1 + q["value"]) / 2 * per
    unit = _TIME.match(q["after"])
    if unit:
        per = _time_minutes(unit[1])
        if per is not None:
            return q["value"] * per
    before = _TIME_BEFORE.search(q["before"])
    if before:
        per = _time_minutes(before[1])
        if per is not None:
            return q["value"] * per
    return q["value"] * default_minutes


def _is_time(q: dict) -> bool:
    """A duration: «20 минут» or «минут 20»."""
    return bool(_TIME.match(q["after"]) or _TIME_BEFORE.search(q["before"]))


def _counted(q: dict) -> bool:
    """«120 заявок», «заявок 120»."""
    return bool(_COUNTED_AFTER.match(q["after"]) or _COUNTED_BEFORE.search(q["before"]))


def _per_month(q: dict) -> float | None:
    """A count as times a month by the period said next to it (none: a month); None for «в час»."""
    per = _period(_clause(q["after"]))
    if per == "hour":
        return None
    return q["value"] * {"day": WORK_DAYS_PER_MONTH, "week": WEEKS_PER_MONTH, "quarter": 1 / 3,
                         "year": 1 / 12}.get(per, 1)


def _is_money(q: dict) -> bool:
    return bool(_MONEY.match(q["after"]))


def _in_limits(field: str, value: float) -> bool:
    lo, hi = LIMITS[field]
    return math.isfinite(value) and lo <= value <= hi


def _round(field: str, value: float) -> float:
    if field in ("volume", "rate"):
        return float(round(value))
    return round(value, 1) if value != int(value) else float(value)


def value_for(field: str, text: str) -> float | None:
    """The number the answer gives for ``field`` (converted to its unit), or None."""
    text = normalize(text)
    qs = quantities(text)
    value = None
    if field == "volume":
        counts = [q for q in qs if not _is_time(q) and not _is_money(q)]
        for q in sorted(counts, key=lambda q: not _counted(q)):  # «заявок 120» before any other number
            value = _per_month(q)
            if value is not None:
                break
    elif field == "rate":
        for q in qs:
            if _is_time(q) and not _is_money(q):
                continue
            per = _period(_clause(q["after"]))
            v = q["value"]
            if per == "hour":
                value = v
            elif per == "day":
                value = v / DAY_H
            elif per == "month" or _SALARY.search(text) or v >= SALARY_FROM:
                value = v / HOURS_PER_MONTH
            else:
                value = v
            break
    elif field in ("minutes", "wait_hours"):
        # a number with a time unit; else «полчаса», «час»; else the only plain number of the answer
        default = 1 if field == "minutes" else 60
        work = (lambda start: not _DELAY.search(text[:start])) if field == "minutes" else (lambda start: True)
        timed = [q for q in qs if _is_time(q) and not _is_money(q) and work(q["start"])]
        plain = [q for q in qs if not _is_time(q) and not _is_money(q) and not _counted(q) and work(q["start"])]
        bare = next((m for m in _BARE_TIME.finditer(text) if work(m.start())), None)
        minutes = None
        if timed:
            minutes = _duration(timed[0], default)
        elif bare:
            word = bare[1].lower()
            minutes = {"полчаса": 30, "час": 60, "минуту": 1, "день": DAY_H * 60, "сутки": DAY_H * 60,
                       "неделю": WEEK_H * 60, "полдня": DAY_H * 30}.get(word, 90 if word.startswith("полтора") else None)
        elif len(plain) == 1:
            minutes = plain[0]["value"] * default
        if minutes is not None:
            value = minutes if field == "minutes" else minutes / 60
    elif field == "people":
        for q in qs:
            if _is_time(q) or _is_money(q):
                continue
            value = q["value"]
            break
        if value is None and _ALONE.search(text):
            value = 1.0
    if value is None or not _in_limits(field, value):
        return None
    return _round(field, value)


DONT_KNOW_CHARS = 80  # a longer answer tells something else too: it goes to the draft


def dont_know(text: str, field: str | None) -> bool:
    """A short «не знаю» and the like, with no number for the asked field."""
    if len(normalize(text)) > DONT_KNOW_CHARS or not _DONT_KNOW.search(normalize(text)):
        return False
    return field is None or value_for(field, text) is None


# «Ничего, сразу передаём сметчику» to «what happens between X and Y»: no step between them. Any word
# but these, the steps' and the roles' may be a step: such an answer goes to the draft.
_NOTHING = re.compile(r"(?<!\w)(?:ничего|никаких|нет|сразу|напрямую|тут\s+же)(?!\w)", re.IGNORECASE)
_NOTHING_WORDS = re.compile(
    r"^(?:ничего|никаких|никак\w*|нет|не|сразу|напрямую|тут|там|же|так|все|просто|особ\w*|происход\w*|бывает|"
    r"между|ними|этими|шаг\w*|друг\w*|лишн\w*|больше|дальше|следующ\w*|переда\w*|отда\w*|отправл\w*|"
    r"пересыла\w*|уход\w*|идет|попада\w*|переход\w*|нас|мы|он|она|они|его|ее|их|это|у|в|на|к|и|а|с|по|от|до|за|"
    r"из|да)$")
NOTHING_CHARS = 80


def nothing_between(text: str, names=()) -> bool:
    """A short answer that nothing happens between the two steps of a «between» question: «ничего»,
    «сразу», «нет» and only words of handing over and of ``names`` (the two steps, the roles)."""
    t = normalize(text)
    if len(t) > NOTHING_CHARS or not _NOTHING.search(t):
        return False
    named = " ".join(names)
    words, stems = set(re.findall(r"[а-яa-z]+", normalize(named))), _marks(named)
    return all(_NOTHING_WORDS.match(w) or w in words or (len(w) >= 4 and w[:4] in stems)
               for w in re.findall(r"[а-яa-z]+", t))


def item(key: str, field: str, value: float) -> dict:
    """An ``understood`` row: the key of the gap (``volume``, ``s3.minutes``…), the value, its unit."""
    return {"field": key, "value": int(value) if value == int(value) else value, "unit": UNITS[field]}


# A step's time said by its clause (замер 12.10: asked about one step, the owner gave another's time —
# «сверка занимает 20 минут», «5 минут относятся к бухгалтеру», «а клиент думает неделю»).
_SENTENCES = re.compile(r"[.!?;]+")
_CLAUSES = re.compile(r",\s+(?:а|но)\s+")
# «получаю за неделю до выхода, иногда за 1 день»: how early, neither the work nor a wait between steps
_LEAD = re.compile(r"(?<!\w)за\s+(?:\S+\s+){0,2}до(?!\w)", re.IGNORECASE)
# the time of the work, not of a wait: «тратит 10 минут», «уходит минут 10», «40 минут на подбор»
_WORK_SAID = re.compile(r"(?<!\w)(?:трат|уход|занима|делае|делаю)\w*|(?:минут\w*|час\w*)\s+на\s+\w", re.IGNORECASE)
_DELAY_SAID = re.compile(r"(?<!\w)(?:через|спустя|жд[её]т|жд[её]м|ждут|ждать|ждали|ожида\w*|дума\w*)", re.IGNORECASE)
ABOUT_ASKED = "asked"


def _marks(text: str) -> set[str]:
    """Four-letter stems of the words of five letters and more: «сверка», «сверяет» -> «свер»."""
    return {w[:4] for w in re.findall(r"[а-яa-z]{5,}", normalize(text))}


def _about(clause: str, process: dict, step_id: str) -> str | None:
    """Which step ``clause`` speaks about: ``ABOUT_ASKED``, another task step's id, or None when two
    or more are named. The action (the first word of the step's name) decides before the role: «сверка
    у бухгалтера» is about the check even when the asked step is the accountant's too."""
    from app.gaps import is_system_role  # app.gaps imports this module

    roles = {r.get("id"): r for r in process.get("roles") or [] if isinstance(r, dict)}
    words = _marks(clause)
    tasks = {}
    for s in process.get("steps") or []:
        if not isinstance(s, dict) or s.get("kind") != "task":
            continue
        role = roles.get(s.get("role")) or {}
        if s.get("id") != step_id and (role.get("external") or is_system_role(role.get("name") or "")):
            continue
        first = re.findall(r"[а-яa-z]+", normalize(s.get("name") or ""))
        tasks[s.get("id")] = ({first[0][:4]} if first and len(first[0]) >= 5 else set(),
                              _marks(role.get("name") or ""), s.get("role"))
    if step_id not in tasks:
        return ABOUT_ASKED
    action, role_marks, role_id = tasks[step_id]
    by_action = [k for k, (a, _, _) in tasks.items() if a and a & words and (k == step_id or a - action)]
    if step_id in by_action:
        return ABOUT_ASKED
    if by_action:
        return by_action[0] if len(by_action) == 1 else None
    named = {r for k, (_, m, r) in tasks.items() if m and m & words and (r == role_id or m - role_marks)}
    if not named or role_id in named:
        return ABOUT_ASKED
    of_role = [k for k, (_, _, r) in tasks.items() if r in named]
    return of_role[0] if len(of_role) == 1 else None


def _timed_answer(answer: str, gap_key: str, field: str, process: dict) -> list[dict]:
    """Minutes or a wait for the asked step, clause by clause: a clause about another step gives that
    step's minutes (one named step) or nothing; days for minutes need «занимает», «уходит», «тратит»;
    the time of the work is not a wait."""
    step_id = gap_key.split(".")[0]
    out = []
    clauses = [c for sentence in _SENTENCES.split(normalize(answer)) if not _LEAD.search(sentence)
               for c in _CLAUSES.split(sentence)]
    for clause in clauses:
        work, delay = _WORK_SAID.search(clause), _DELAY_SAID.search(clause)
        if field == "wait_hours" and work and not delay:
            continue
        value = value_for(field, clause)
        if value is None or (field == "minutes" and value >= DAY_H * 60 and not work):
            continue
        about = _about(clause, process, step_id)
        if about == ABOUT_ASKED:
            out.append(item(gap_key, field, value))
            break
        if about and field == "minutes" and all(u["field"] != f"{about}.minutes" for u in out):
            out.append(item(f"{about}.minutes", "minutes", value))
    return out


def from_answer(answer: str, gap_key: str | None, gap_field: str | None, process: dict | None = None) -> list[dict]:
    """What the code understood: the asked field, and a volume or a rate said outright («120 заявок в
    месяц», «700 рублей в час») whatever was asked. With the draft ``process``, minutes and waits of a
    step go to the step the clause speaks about (``_timed_answer``)."""
    out, seen = [], set()
    if gap_key and gap_field:
        if gap_field in ("minutes", "wait_hours") and process and "." in gap_key:
            out += _timed_answer(answer, gap_key, gap_field, process)
        else:
            value = value_for(gap_field, answer)
            if value is not None:
                out.append(item(gap_key, gap_field, value))
        if any(u["field"] == gap_key for u in out):
            seen.add(gap_field)
    text = normalize(answer)
    if "volume" not in seen and not _PAY.search(text):
        for q in quantities(text):
            one_word = q["value"] == 1 and not q["text"][:1].isdigit()  # «на одну заявку» is not a volume
            if _counted(q) and _period(_clause(q["after"])) in ("day", "week", "month", "year") and not one_word \
                    and not _is_time(q) and not _is_money(q):
                value = _per_month(q)
                if value is not None and _in_limits("volume", value):
                    out.append(item("volume", "volume", _round("volume", value)))
                break
    if "rate" not in seen and any(_is_money(q) and _period(_clause(q["after"])) == "hour" for q in quantities(text)):
        value = value_for("rate", answer)
        if value is not None:
            out.append(item("rate", "rate", value))
    return out


def from_model(answers: list, answer: str, process: dict | None, have: set[str]) -> list[dict]:
    """The draft model's ``answers`` that hold up: the quote is in the last answer and has the number,
    the target exists. ``have``: keys already understood by the code (the code wins)."""
    out = []
    answer_norm = normalize(answer)
    steps = {s["id"]: s for s in (process or {}).get("steps") or []}
    roles = {r["id"]: r for r in (process or {}).get("roles") or []}
    for a in answers if isinstance(answers, list) else []:
        if not isinstance(a, dict):
            continue
        field, target, quote, value = a.get("field"), a.get("target"), a.get("quote"), a.get("value")
        if not isinstance(field, str) or field not in UNITS or isinstance(value, bool) \
                or not isinstance(value, (int, float)) or not (target is None or isinstance(target, str)):
            continue
        if not isinstance(quote, str) or not quote_in_text(quote, answer_norm):
            continue
        if field in ("minutes", "wait_hours"):
            step = steps.get(target)
            if step is None or step["kind"] not in ("task", "decision") or not number_in_quote(value, field, quote):
                continue
            key = f"{target}.{field}"
        elif field == "people":
            if target not in roles or roles[target].get("external") or value_for("people", quote) != value:
                continue
            key = f"{target}.people"
        else:
            said = [q["value"] for q in quantities(normalize(quote))]
            if not any(abs(v - value) <= 1e-6 + 0.005 * abs(v) for v in said):
                continue
            value = value_for(field, quote)  # the code converts the period
            if value is None:
                continue
            key = field
        if key in have or not _in_limits(field, float(value)):
            continue
        have.add(key)
        out.append(item(key, field, _round(field, float(value))))
    return out


def apply(assumptions: dict, understood: list[dict]) -> dict:
    """Assumptions with the understood numbers: the keys of web/engine.js ``E.assume()``
    (``perMonth``, ``rate``, ``people`` by role) and ``minutes`` / ``wait_hours`` by step."""
    a = {"people": {}, "minutes": {}, "wait_hours": {}, **{k: v for k, v in assumptions.items()}}
    a["people"], a["minutes"], a["wait_hours"] = dict(a["people"]), dict(a["minutes"]), dict(a["wait_hours"])
    for u in understood:
        key, value = u["field"], u["value"]
        if key == "volume":
            a["perMonth"] = value
        elif key == "rate":
            a["rate"] = value
        else:
            target, field = key.split(".", 1)
            a[field][target] = value
    return a
