"""app.gaps (which question is next), app.understand (numbers from an answer) and
app.checks.check_draft (a draft that may still be incomplete)."""

import copy

import pytest

from app import gaps, understand
from app.checks import check_draft, check_process
from tests.fakes import extract_answer, fixture, step_of

SALES = fixture("sales")


def sales_process():
    process, errors = check_process(extract_answer("sales"), SALES["text"])
    assert errors == []
    return process


def keys(gap_list, kind=None):
    return [g.key for g in gap_list if kind is None or g.kind == kind]


# ---------- the order of the gaps ----------

def test_no_draft_asks_how_it_goes_then_volume_then_rate():
    found = gaps.find_gaps(None, {}, "заявки")
    assert keys(found) == ["start_end", "volume", "rate"]
    assert found[0].template.startswith("Расскажите по шагам, как у вас проходит заявка клиента")
    own = gaps.find_gaps(None, {}, None, "у нас ремонт техники")
    assert own[0].key == "start_end" and "этот процесс" in own[0].template
    assert keys(gaps.find_gaps(None, {"perMonth": 120, "rate": 800}, "заявки")) == ["start_end"]


@pytest.mark.parametrize("drop, wording", [("end", "Чем заканчивается"), ("start", "С чего начинается")])
def test_a_draft_without_its_start_or_end(drop, wording):
    process = sales_process()
    process["steps"] = [s for s in process["steps"] if s["kind"] != drop]
    first = gaps.find_gaps(process, {})[0]
    assert first.key == "start_end" and first.template.startswith(wording)


def test_priorities_on_one_draft():
    """1 start/end, 2 volume, 3 role, 4 branches, 5 between, 6 rate, 7 minutes, 8 waiting, 9 people."""
    process = sales_process()
    process["steps"] = [s for s in process["steps"] if s["id"] != "s11"]  # no «Договор у клиента»
    process["steps"] = [s for s in process["steps"] if s["id"] != "s12"]  # and no end at all
    process["roles"].append({"id": "r7", "name": "Кто-то", "external": False})
    step_of(process, "s8")["role"] = "r7"
    step_of(process, "s4")["branches"] = step_of(process, "s4")["branches"][:1]
    kinds = []
    for g in gaps.find_gaps(process, {}):
        if g.kind not in kinds:
            kinds.append(g.kind)
    assert kinds == ["start_end", "volume", "role", "branches", "between", "rate", "minutes", "wait", "people"]
    found = gaps.find_gaps(process, {})
    assert keys(found, "role") == ["s8.role"] and keys(found, "branches") == ["s4.branches"]


def test_minutes_by_share_of_hours():
    """Hand count on sales: the loops s4 -> s3 and s8 -> s6 take 0.2, so v(s3) = v(s4) = v(s6) = v(s7) = 1.25,
    v(s8) = 0.25, v(s9) = v(s10) = 0.5. Minutes from the text: s2 10, s5 40, s9 60. Unknown, × the default:
    s3 15 × 1.25 = 18.75, s6 18.75, s10 15 × 0.5 = 7.5, s4 (decision) 5 × 1.25 = 6.25, s8 15 × 0.25 = 3.75;
    s7 is the client's: not asked."""
    process = sales_process()
    v = gaps.visits(process)
    assert v["s3"] == pytest.approx(1.25) and v["s6"] == pytest.approx(1.25) and v["s8"] == pytest.approx(0.25)
    assert v["s9"] == pytest.approx(0.5) and v["s11"] == pytest.approx(0.5) and v["s12"] == pytest.approx(0.4 * 1.25)
    found = gaps.find_gaps(process, {})
    assert keys(found, "minutes") == ["s3.minutes", "s6.minutes", "s10.minutes", "s4.minutes", "s8.minutes"]
    # minutes from an answer: the step is known, its share moves the rest
    found = gaps.find_gaps(process, {"minutes": {"s3": 5}})
    assert keys(found, "minutes") == ["s6.minutes", "s10.minutes", "s4.minutes", "s8.minutes"]


def test_waiting_by_share_of_days():
    """Handoffs between roles into a step with no waiting from the text, × 4 h: s6 gets s5 -> s6 (1.0) and
    s8 -> s6 (0.25) = 5, s7 gets s6 -> s7 (1.25) = 5, s9 0.5 × 4 = 2, s8 0.25 × 4 = 1; s5 and s10 have theirs."""
    found = gaps.find_gaps(sales_process(), {})
    assert keys(found, "wait") == ["s6.wait_hours", "s7.wait_hours", "s9.wait_hours", "s8.wait_hours"]
    assert "«Сметчик» к «Менеджер»" in found[[g.key for g in found].index("s6.wait_hours")].about
    assert keys(gaps.find_gaps(sales_process(), {"wait_hours": {"s6": 2}}), "wait")[0] == "s7.wait_hours"


def test_people_by_load():
    """Minutes a request gives each role: r1 10 + 18.75 + 6.25 + 18.75 = 53.75, r2 40, r4 60 × 0.5 = 30,
    r5 7.5, r3 3.75; the client (r6) is not asked."""
    found = gaps.find_gaps(sales_process(), {})
    assert keys(found, "people") == ["r1.people", "r2.people", "r4.people", "r5.people", "r3.people"]
    assert keys(gaps.find_gaps(sales_process(), {"people": {"r1": 3}}), "people")[0] == "r2.people"


def test_system_and_external_roles_are_not_asked_about_minutes_or_people():
    process = sales_process()
    process["roles"].append({"id": "r7", "name": "Система", "external": False})
    step_of(process, "s2")["role"] = "r7"
    step_of(process, "s2")["minutes"] = None
    found = gaps.find_gaps(process, {})
    assert "s2.minutes" not in keys(found) and "r7.people" not in keys(found) and "r6.people" not in keys(found)
    assert "s7.minutes" not in keys(found)


def test_next_gap_never_asks_twice():
    process = sales_process()
    first = gaps.next_gap(process, {}, [])
    assert first.key == "volume"
    assert gaps.next_gap(process, {}, ["volume"]).key == "between.1"
    assert gaps.next_gap(process, {}, ["volume", "between.1", "between.2"]).key == "rate"
    assert gaps.next_gap(process, {}, ["volume", "between.1", "between.2", "rate"]).key == "s3.minutes"
    everything = keys(gaps.find_gaps(process, {}))
    assert gaps.next_gap(process, {}, everything) is None


# ---------- the wording ----------

@pytest.mark.parametrize("text, expected, removed, template", [
    ("Сколько минут уходит на «Посчитать смету»?", "Сколько минут уходит на «Посчитать смету»?", 0, False),
    ("Сколько минут (например, 10–15) уходит на ввод в 1С?", "Сколько минут уходит на ввод в 1С?", 2, False),
    ("Больше 10 минут или меньше?", "Шаблон «Посчитать смету»?", 1, True),
    ("", "Шаблон «Посчитать смету»?", 0, True),
    ("Ok?", "Шаблон «Посчитать смету»?", 0, True),
    ("Сколько? " * 30, "Шаблон «Посчитать смету»?", 0, True),
])
def test_digits_never_reach_the_question(text, expected, removed, template):
    question, n, used = gaps.clean_question(text, "Шаблон «Посчитать смету»?")
    assert (question, n, used) == (expected, removed, template)
    assert gaps.digits_in(question) == 0


def test_template_loses_the_digits_of_a_step_name():
    question, _, used = gaps.clean_question("за 5", "Сколько времени уходит на «Провести 2 счёта в 1С»?")
    assert used and question == "Сколько времени уходит на «Провести счёта в 1С»?"


def test_every_template_is_short_and_has_no_digits():
    process = sales_process()
    step_of(process, "s6")["name"] = "Собрать КП на 3 позиции и отправить клиенту по почте с приложениями к нему"
    process["roles"].append({"id": "r7", "name": "Кто-то", "external": False})
    step_of(process, "s8")["role"] = "r7"
    for g in gaps.find_gaps(process, {}) + gaps.find_gaps(None, {}, "выход"):
        question, _, _ = gaps.clean_question("", g.template)
        assert len(question) <= 160 and gaps.digits_in(question) == 0 and g.about


# ---------- numbers from an answer ----------

@pytest.mark.parametrize("field, text, value", [
    ("volume", "Где-то 120 заявок в месяц", 120), ("volume", "штук 30 в неделю", 129), ("volume", "5 в день", 105),
    ("volume", "около сотни", 100), ("volume", "сто двадцать", 120), ("volume", "полторы тысячи в год", 125),
    ("volume", "120 клиентов", 120),
    ("rate", "700 рублей в час", 700), ("rate", "зарплата 60 тысяч", 357), ("rate", "60 000 в месяц", 357),
    ("rate", "3000 в день", 375), ("rate", "от 500 до 700 рублей в час", 600),
    ("minutes", "минут 20", 20), ("minutes", "полтора часа", 90), ("minutes", "за час управляюсь", 60),
    ("minutes", "10–15 минут", 12.5), ("minutes", "от двух до трёх часов", 150), ("minutes", "20", 20),
    ("wait_hours", "2–3 дня", 20), ("wait_hours", "сутки", 8), ("wait_hours", "минут 30", 0.5),
    ("wait_hours", "неделю", 40), ("wait_hours", "4", 4),
    ("people", "двое", 2), ("people", "два человека", 2), ("people", "я сам", 1), ("people", "трое-четверо", 3.5),
    # the unit decides, not the first number
    ("minutes", "На одну заявку уходит минут 20", 20), ("minutes", "Одна заявка — минут 10", 10),
    ("minutes", "полчаса на 1 заявку", 30), ("minutes", "у нас 3 менеджера, каждый тратит по 20 минут", 20),
    ("minutes", "от 10 до 20, иногда 3 заявки подряд", 15), ("minutes", "минут 10 к каждой заявке", 10),
    # the period next to the number, not any period of the answer
    ("volume", "120 заявок в месяц, то есть около 6 в день", 120), ("volume", "100 заявок в месяц, в среднем 5 в день", 100),
    ("rate", "зарплата 50 тысяч, работают 8 часов в день", 298), ("rate", "50к в месяц", 298),
    # a range from one unit
    ("wait_hours", "день-два", 12), ("minutes", "час-два", 90), ("minutes", "час-полтора", 75),
    # a wait said next to the work: only the work is minutes
    ("minutes", "переношу минут десять, а клиент ждёт ответа через два дня", 10),
    ("wait_hours", "замерщик выезжает обычно через два дня", 16),
])
def test_value_for(field, text, value):
    assert understand.value_for(field, text) == value


@pytest.mark.parametrize("field, text", [("volume", "не знаю"), ("minutes", "700 рублей в час"),
                                         ("volume", "20 минут"), ("people", "по-разному"), ("rate", "дорого"),
                                         # live 12.10: asked the minutes of a step, told about waiting
                                         ("minutes", "Замерщик выезжает обычно через два дня, потом смета ждёт ещё день у сметчика."),
                                         ("minutes", "ждут около часа"),
                                         # замер 12.10, invoice: подпись директора
                                         ("minutes", "Подписи ждём обычно 2–3 дня, директор часто в разъездах.")])
def test_no_value(field, text):
    assert understand.value_for(field, text) is None


def test_dont_know():
    assert understand.dont_know("Не знаю, никто не считал", "volume")
    assert understand.dont_know("Трудно сказать", None)
    assert not understand.dont_know("не знаю точно, наверное 100", "volume")
    assert not understand.dont_know("минут 20", "minutes")
    # a long answer says something else too: it goes to the draft
    assert not understand.dont_know("Заявка приходит на почту, менеджер переносит её в таблицу и звонит клиенту, "
                                    "а что дальше — не знаю", None)


def test_from_answer_takes_the_asked_field_and_a_volume_or_rate_said_outright():
    assert understand.from_answer("уходит 20 минут, а заявок 150 в месяц", "s3.minutes", "minutes") == [
        {"field": "s3.minutes", "value": 20, "unit": "мин"}, {"field": "volume", "value": 150, "unit": "заявок/мес"}]
    # a salary is not a volume; «в час» is not a duration
    assert understand.from_answer("Менеджер получает около 70 тысяч в месяц с налогами.", "rate", "rate") == [
        {"field": "rate", "value": 417, "unit": "₽/ч"}]
    assert understand.from_answer("ставка 700 рублей в час, а так не знаю", "s3.minutes", "minutes") == [
        {"field": "rate", "value": 700, "unit": "₽/ч"}]
    assert understand.from_answer("Минут 15, это 2 раза в день", "s3.minutes", "minutes") == [
        {"field": "s3.minutes", "value": 15, "unit": "мин"}]
    assert understand.from_answer("на одну заявку уходит 20 минут, а в месяц их 100", "s3.minutes", "minutes") == [
        {"field": "s3.minutes", "value": 20, "unit": "мин"}]


def test_from_model_keeps_only_what_the_answer_holds():
    process = sales_process()
    answer = "Смета в Excel занимает около 40 минут, а заявка ждёт сметчика обычно 2 дня. Сметчиков у нас двое."
    items = [
        {"field": "wait_hours", "target": "s5", "value": 16, "quote": "заявка ждёт сметчика обычно 2 дня"},
        {"field": "people", "target": "r2", "value": 2, "quote": "Сметчиков у нас двое"},
        {"field": "minutes", "target": "s5", "value": 45, "quote": "занимает около 40 минут"},     # not in the quote
        {"field": "minutes", "target": "s6", "value": 40, "quote": "около 40 минут в Word"},       # not in the answer
        {"field": "minutes", "target": "s99", "value": 40, "quote": "занимает около 40 минут"},    # no such step
        {"field": "people", "target": "r6", "value": 2, "quote": "Сметчиков у нас двое"},          # the client's
        {"field": "volume", "target": None, "value": 2, "quote": "ждёт сметчика обычно 2 дня"},   # 2 days, not a volume
        {"field": "minutes", "target": "s5", "value": 40, "quote": "занимает около 40 минут"},     # the code has it
        "junk", {"field": ["minutes"], "target": "s5", "value": 40, "quote": "40 минут"},
        {"field": "people", "target": ["r2"], "value": 2, "quote": "Сметчиков у нас двое"},
    ]
    got = understand.from_model(items, answer, process, {"s5.minutes"})
    assert got == [{"field": "s5.wait_hours", "value": 16, "unit": "ч"}, {"field": "r2.people", "value": 2, "unit": "чел"}]


def test_from_model_converts_a_volume_by_its_period():
    got = understand.from_model([{"field": "volume", "target": None, "value": 30, "quote": "30 заявок в неделю"}],
                                "Бывает 30 заявок в неделю", None, set())
    assert got == [{"field": "volume", "value": 129, "unit": "заявок/мес"}]


def test_apply_writes_the_engine_keys():
    a = understand.apply({}, [{"field": "volume", "value": 120, "unit": ""}, {"field": "rate", "value": 800, "unit": ""},
                              {"field": "s3.minutes", "value": 20, "unit": ""},
                              {"field": "s5.wait_hours", "value": 16, "unit": ""},
                              {"field": "r2.people", "value": 2, "unit": ""}])
    assert a == {"perMonth": 120, "rate": 800, "people": {"r2": 2}, "minutes": {"s3": 20}, "wait_hours": {"s5": 16}}
    assert understand.apply(a, [{"field": "volume", "value": 90, "unit": ""}])["perMonth"] == 90


# ---------- the draft ----------

TEXT = SALES["text"]


def draft_of(steps, roles=None):
    return {"title": "Черновик", "roles": roles if roles is not None else copy.deepcopy(extract_answer()["roles"]),
            "steps": steps, "answers": []}


def test_a_draft_may_lack_its_end_and_next_steps():
    answer = extract_answer()
    steps = [s for s in answer["steps"] if s["id"] in ("s1", "s2", "s3")]
    step_of({"steps": steps}, "s3")["next"] = ["s4"]  # s4 is not in the draft yet
    draft, errors = check_draft(draft_of(steps), TEXT)
    assert errors == [] and [s["kind"] for s in draft["steps"]] == ["start", "task", "task"]
    assert step_of(draft, "s3")["next"] == [] and step_of(draft, "s2")["minutes"] == 10
    assert check_process(draft_of(steps), TEXT)[1]  # v1 would send it back


def test_a_draft_drops_branches_to_nowhere_and_gives_a_role_to_a_step_without_one():
    answer = extract_answer()
    steps = [s for s in answer["steps"] if s["id"] in ("s1", "s2", "s3", "s4", "s5")]
    step_of({"steps": steps}, "s4")["branches"].append({"label": "позже", "to": "s15"})
    step_of({"steps": steps}, "s4")["branches"].append({"label": "в начало", "to": "s1"})
    step_of({"steps": steps}, "s5")["role"] = ""
    draft, errors = check_draft(draft_of(steps, extract_answer()["roles"][:1]), TEXT)
    assert errors == []
    assert step_of(draft, "s4")["branches"] == [{"label": "да", "to": "s5"}, {"label": "нет", "to": "s3"}]
    assert draft["roles"][-1] == {"id": "r2", "name": "Кто-то", "external": False}
    assert step_of(draft, "s5")["role"] == "r2"
    assert gaps.next_gap(draft, {"perMonth": 100}, []).key == "start_end"  # no end yet: that first
    assert gaps.next_gap(draft, {"perMonth": 100}, ["start_end"]).key == "s5.role"


def test_a_draft_with_six_roles_has_no_room_for_one_more():
    answer = extract_answer()
    steps = [s for s in answer["steps"] if s["id"] in ("s1", "s2")]
    step_of({"steps": steps}, "s2")["role"] = "r9"
    draft, errors = check_draft(draft_of(steps), TEXT)
    assert draft is None and "ролей уже 6" in errors[0]


@pytest.mark.parametrize("change, error", [
    (lambda steps: steps.append({**steps[0], "id": "s14"}), "start должен быть ровно один, а их 2"),
    (lambda steps: steps[1].update(id="x2"), "нужен s1…s15"),
    (lambda steps: steps[1].update(kind="gateway"), "kind «gateway»"),
    (lambda steps: steps.clear(), "нет ни одного шага"),
])
def test_what_a_draft_cannot_be(change, error):
    steps = [s for s in extract_answer()["steps"] if s["id"] in ("s1", "s2", "s3")]
    change(steps)
    draft, errors = check_draft(draft_of(steps), TEXT)
    assert draft is None and any(error in e for e in errors)


def test_a_draft_checks_quotes_and_numbers_as_v1():
    steps = [s for s in extract_answer()["steps"] if s["id"] in ("s1", "s2", "s3")]
    step_of({"steps": steps}, "s3")["minutes"] = 25
    step_of({"steps": steps}, "s1")["quote"] = "этого в тексте нет совсем"
    draft, _ = check_draft(draft_of(steps), TEXT)
    assert draft["flags"] == {"unquoted": ["s1"], "numbers_dropped": [{"step": "s3", "field": "minutes", "value": 25}]}
    assert step_of(draft, "s3")["minutes"] is None


# ---------- what is between two steps (замер 13.10: 8 questions of 8 went to numbers) ----------

def between_keys(found):
    return [(g.key, g.step, g.to) for g in found if g.kind == "between"]


def renumbered(process, shift=1):
    """The same draft after a redraw that moved every id: the model's ids are not the steps'."""
    ids = {s["id"]: f"s{int(s['id'][1:]) + shift}" for s in process["steps"]}
    out = copy.deepcopy(process)
    for s in out["steps"]:
        s["id"] = ids[s["id"]]
        s["next"] = [ids[t] for t in s.get("next") or []]
        for b in s.get("branches") or []:
            b["to"] = ids[b["to"]]
    return out


def asked(process, *found):
    return [gaps.between_record(g, process) for g in found]


def test_between_asks_about_a_handoff_between_roles():
    """Only an edge between two work steps where the role changes: not the start, not the end, not the
    same role."""
    draft = {"title": "Ч", "roles": [{"id": "r1", "name": "Менеджер", "external": False},
                                     {"id": "r2", "name": "Сметчик", "external": False}],
             "steps": [{"id": "s1", "kind": "start", "name": "Пришла заявка", "role": "r1", "next": ["s2"]},
                       {"id": "s2", "kind": "task", "name": "Принять заявку", "role": "r1", "next": ["s3"]},
                       {"id": "s3", "kind": "task", "name": "Записать в журнал", "role": "r1", "next": ["s4"]},
                       {"id": "s4", "kind": "task", "name": "Посчитать смету", "role": "r2", "next": ["s5"]},
                       {"id": "s5", "kind": "end", "name": "Смета готова", "role": "r2", "next": []}]}
    found = gaps.find_gaps(draft, {"perMonth": 100, "rate": 700})
    assert between_keys(found) == [("between.1", "s3", "s4")]
    g = next(g for g in found if g.kind == "between")
    assert g.field is None and g.pair == ("Записать в журнал", "Посчитать смету")
    assert g.template == "Что происходит между «Записать в журнал» и «Посчитать смету» — нет ли там ещё шагов?"
    assert "от «Менеджер» к «Сметчик»" in g.about
    # the same role all along: nothing to ask
    draft["steps"][3]["role"] = "r1"
    assert between_keys(gaps.find_gaps(draft, {})) == []


def test_between_by_passes_of_the_edge_then_by_the_order_of_steps(monkeypatch):
    """Hand count on sales (visits × share, the loops take 0.2): s6 -> s7 1.25 goes first though it comes
    later; s4 -> s5 and s5 -> s6 1.0 each, s7 -> s9 and s9 -> s10 0.5, s7 -> s8 and s8 -> s6 0.25: a tie
    by the order of the steps. s2 -> s3 is the same role, s7 -> s12 ends."""
    monkeypatch.setattr(gaps, "MAX_BETWEEN", 10)
    assert between_keys(gaps.find_gaps(sales_process(), {})) == [
        ("between.1", "s6", "s7"), ("between.2", "s4", "s5"), ("between.3", "s5", "s6"), ("between.4", "s7", "s9"),
        ("between.5", "s9", "s10"), ("between.6", "s7", "s8"), ("between.7", "s8", "s6")]


def test_between_at_most_two_an_interview():
    process = sales_process()
    found = gaps.find_gaps(process, {})
    assert gaps.MAX_BETWEEN == 2 and between_keys(found) == [("between.1", "s6", "s7"), ("between.2", "s4", "s5")]
    first, second = [g for g in found if g.kind == "between"]
    assert between_keys(gaps.find_gaps(process, {}, between=asked(process, first))) == [("between.2", "s4", "s5")]
    both = asked(process, first, second)
    assert between_keys(gaps.find_gaps(process, {}, between=both)) == []
    assert between_keys(gaps.find_gaps(renumbered(process), {}, between=both)) == []
    assert gaps.next_gap(process, {"perMonth": 100}, ["between.1", "between.2"], between=both).key == "rate"


def test_between_is_not_asked_again_when_the_ids_move():
    """The pair is kept by name: after a redraw that moved the ids the same steps are not asked again,
    and other steps under the ids asked before are."""
    process = sales_process()
    first = next(g for g in gaps.find_gaps(process, {}) if g.kind == "between")
    record = asked(process, first)
    moved = renumbered(process)  # «Собрать и отправить КП» -> «Что ответил клиент?» is s7 -> s8 now
    assert between_keys(gaps.find_gaps(moved, {}, between=record)) == [("between.2", "s5", "s6")]
    assert gaps.next_gap(moved, {"perMonth": 100}, ["volume", "between.1"], between=record).key == "between.2"
    other = sales_process()
    step_of(other, "s6")["name"], step_of(other, "s7")["name"] = "Выставить счёт", "Пришла ли оплата?"
    assert between_keys(gaps.find_gaps(other, {}, between=record))[0] == ("between.2", "s6", "s7")


def test_between_does_not_ask_about_the_pair_its_answer_split():
    """The answer put «Позвонить клиенту по КП» between the two: s13 -> s7 is the same handoff, asked."""
    process = sales_process()
    first = next(g for g in gaps.find_gaps(process, {}) if g.kind == "between")
    record = asked(process, first)
    step_of(process, "s6")["next"] = ["s13"]
    process["steps"].insert(6, {"id": "s13", "kind": "task", "name": "Позвонить клиенту по КП", "role": "r1",
                                "next": ["s7"], "branches": [], "quote": None, "tool": None, "minutes": None,
                                "wait_hours": None})
    assert between_keys(gaps.find_gaps(process, {}))[0] == ("between.1", "s13", "s7")  # not asked yet: it is new
    assert between_keys(gaps.find_gaps(process, {}, between=record)) == [("between.2", "s4", "s5")]


def test_between_question_has_no_digits_and_fits():
    process = sales_process()
    step_of(process, "s6")["name"] = "Собрать КП на 3 позиции и отправить клиенту по почте с приложениями к нему"
    step_of(process, "s7")["name"] = "Что ответил клиент на 2-е письмо с коммерческим предложением и приложениями?"
    g = next(g for g in gaps.find_gaps(process, {}) if g.kind == "between")
    question, _, used = gaps.clean_question("", g.template)
    assert used and question == g.template and question.endswith("— нет ли там ещё шагов?")
    assert gaps.digits_in(question) == 0 and len(question) <= gaps.MAX_QUESTION_CHARS
    assert gaps.digits_in(g.about) == 0 and len(g.about) <= 400  # app.pricing.GAP_CHARS


@pytest.mark.parametrize("text, nothing", [
    ("Ничего, сразу передаём сметчику.", True), ("Ничего не происходит", True), ("Нет, сразу", True),
    ("Никаких шагов нет, менеджер сразу отправляет сметчику", True), ("Нет, больше ничего, всё сразу", True),
    ("Ничего, КП сразу уходит клиенту", True),
    # a step, or maybe one: the draft decides
    ("Нет, ещё менеджер проверяет данные", False), ("Ничего особенного, менеджер звонит клиенту", False),
    ("Сначала звонит, потом сразу передаёт", False), ("Сразу, но иногда руководитель смотрит смету", False),
    ("Да", False), ("не знаю", False), ("Ничего, сразу передаём. " * 5, False),
])
def test_nothing_between(text, nothing):
    names = ["Уточнить объём у клиента", "Посчитать смету", "Собрать и отправить КП", "Менеджер", "Сметчик", "Клиент"]
    assert understand.nothing_between(text, names) is nothing
