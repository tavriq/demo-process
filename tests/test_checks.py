import pytest

from app import checks
from app.checks import check_improve, check_process, number_in_quote, quote_in_text
from tests.fakes import extract_answer, fixture, improve_answer, step_of


def process_of(name="sales", answer=None):
    process, errors = check_process(answer or extract_answer(name), fixture(name)["text"])
    assert errors == []
    return process


def errors_of(answer, name="sales"):
    return check_process(answer, fixture(name)["text"])[1]


@pytest.mark.parametrize("name", ["sales", "invoice"])
def test_fixtures_pass_clean(name):
    process = process_of(name)
    assert process["flags"] == {"unquoted": [], "numbers_dropped": []}
    improve, errors = check_improve(improve_answer(name), process)
    assert errors == [] and improve["flags"] == {"numbers_in_text": [], "external_kept": []}


def test_contract_shape():
    steps = {s["id"]: s for s in process_of()["steps"]}
    assert set(steps["s1"]) == {"id", "kind", "name", "role", "next", "quote"}
    assert set(steps["s2"]) == {"id", "kind", "name", "role", "next", "quote", "tool", "minutes", "wait_hours"}
    assert set(steps["s7"]) == {"id", "kind", "name", "role", "branches", "quote", "tool", "minutes", "wait_hours"}
    assert steps["s7"]["branches"] == [{"label": "согласен", "to": "s9"}, {"label": "просит скидку", "to": "s8"},
                                       {"label": "отказался", "to": "s12"}]
    assert steps["s11"]["next"] == [] and steps["s2"]["minutes"] == 10 and steps["s5"]["wait_hours"] == 16


# ---------- quotes ----------

@pytest.mark.parametrize("quote", [
    "МЕНЕДЖЕР вручную   переносит её в таблицу",          # case and spaces
    "менеджер вручную переносит ее в таблицу",            # ё/е
    "Договор подписывает директор, подписи договор ждёт 1-2 дня",  # en dash in the text, hyphen here
    "«Заявка приходит на общую почту…»",                  # quote marks and an ellipsis at the edge
    "Менеджер вручную переносит… уходит минут 10",       # parts around an ellipsis, in order
])
def test_quote_found_after_normalization(quote):
    assert quote_in_text(quote, checks.normalize(fixture()["text"]))


@pytest.mark.parametrize("quote", ["менеджер сам считает смету", "уходит минут 10… Менеджер вручную переносит",
                                   "КП", ""])
def test_quote_not_in_text(quote):
    assert not quote_in_text(quote, checks.normalize(fixture()["text"]))


def test_invented_quote_is_nulled_and_flagged():
    answer = extract_answer()
    step_of(answer, "s3")["quote"] = "менеджер созванивается с клиентом в течение часа"
    step_of(answer, "s6")["quote"] = None
    process = process_of(answer=answer)
    steps = {s["id"]: s for s in process["steps"]}
    assert steps["s3"]["quote"] is None and steps["s6"]["quote"] is None
    assert process["flags"]["unquoted"] == ["s3", "s6"]


def test_long_quote_is_cut_and_still_found():
    answer = extract_answer()
    long = fixture()["text"][:300]
    step_of(answer, "s1")["quote"] = long
    step = process_of(answer=answer)["steps"][0]
    assert len(step["quote"]) <= 120 and long.startswith(step["quote"]) and not process_of(answer=answer)[
        "flags"]["unquoted"]


# ---------- numbers ----------

def test_number_without_support_in_quote_is_dropped():
    answer = extract_answer()
    step_of(answer, "s3")["minutes"] = 20            # the quote has no number
    step_of(answer, "s6")["wait_hours"] = 4          # nor this one
    step_of(answer, "s2")["minutes"] = 15            # the quote says 10
    process = process_of(answer=answer)
    steps = {s["id"]: s for s in process["steps"]}
    assert steps["s3"]["minutes"] is None and steps["s6"]["wait_hours"] is None and steps["s2"]["minutes"] is None
    assert process["flags"]["numbers_dropped"] == [{"step": "s2", "field": "minutes", "value": 15},
                                                   {"step": "s3", "field": "minutes", "value": 20},
                                                   {"step": "s6", "field": "wait_hours", "value": 4}]


def test_number_of_an_unquoted_step_is_dropped():
    answer = extract_answer()
    step_of(answer, "s2")["quote"] = "менеджер тратит на заявку 10 минут"  # not in the text
    process = process_of(answer=answer)
    assert process["steps"][1]["minutes"] is None
    assert process["flags"] == {"unquoted": ["s2"], "numbers_dropped": [{"step": "s2", "field": "minutes",
                                                                         "value": 10}]}


@pytest.mark.parametrize("value, field, quote", [
    (10, "minutes", "на это уходит минут 10"),
    (60, "minutes", "это занимает час"),
    (90, "minutes", "полтора часа"),
    (30, "minutes", "полчаса на звонок"),
    (1.5, "wait_hours", "ждёт 1,5 часа"),
    (20, "wait_hours", "ждёт подписи 2–3 дня"),        # middle of a range, 8-hour days
    (48, "wait_hours", "ждёт 2 дня"),                  # calendar days
    (16, "wait_hours", "ждёт два рабочих дня"),
    (40, "wait_hours", "отвечают через неделю"),
    (3, "wait_hours", "ждёт 3 дня"),                   # the bare number is in the quote
])
def test_number_supported(value, field, quote):
    assert number_in_quote(value, field, quote)


@pytest.mark.parametrize("value, field, quote", [
    (10, "minutes", "это занимает 15 минут"),
    (600, "minutes", "это занимает 15 минут"),
    (5, "minutes", "ведём в 1С"),                      # «1С» is not a number
    (30, "wait_hours", "ждёт 2–3 дня"),
    (8, "wait_hours", "отвечаем быстро"),
    (2, "minutes", None),
])
def test_number_not_supported(value, field, quote):
    assert not number_in_quote(value, field, quote)


def test_non_numbers_and_negatives_are_dropped():
    answer = extract_answer()
    step_of(answer, "s2")["minutes"] = "10"
    step_of(answer, "s5")["minutes"] = -40
    process = process_of(answer=answer)
    assert [(d["step"], d["field"]) for d in process["flags"]["numbers_dropped"]] == [("s2", "minutes"),
                                                                                      ("s5", "minutes")]


# ---------- graph and limits ----------

def test_unreachable_step():
    answer = extract_answer()
    step_of(answer, "s7")["branches"] = [{"label": "согласен", "to": "s9"}, {"label": "просит скидку", "to": "s8"}]
    assert errors_of(answer) == ["из start не дойти до s12"]


def test_unknown_target_and_role():
    answer = extract_answer()
    step_of(answer, "s3")["next"] = ["s99"]
    step_of(answer, "s5")["role"] = "r9"
    errors = errors_of(answer)
    assert "s5: роль «r9» не из roles" in errors and "s3 ведёт на несуществующий шаг «s99»" in errors


def test_one_start_and_an_end():
    answer = extract_answer()
    step_of(answer, "s2")["kind"] = "start"
    for s in answer["steps"]:
        if s["kind"] == "end":
            s["kind"] = "task"
            s["next"] = ["s1"]
    errors = errors_of(answer)
    assert "start должен быть ровно один, а их 2" in errors and "нет ни одного end" in errors
    assert "s11 ведёт в start, а в start ничего не входит" in errors


def test_dead_loop_has_no_way_to_the_end():
    answer = extract_answer()
    step_of(answer, "s4")["branches"] = [{"label": "да", "to": "s3"}, {"label": "нет", "to": "s3"}]
    errors = errors_of(answer)
    assert errors == ["из start не дойти до s5, s6, s7, s8, s9, s10, s11, s12",
                      "из s1, s2, s3, s4 не дойти до end: процесс там зацикливается"]
    answer = extract_answer()
    step_of(answer, "s8")["next"] = ["s8"]
    assert errors_of(answer) == ["из s8 не дойти до end: процесс там зацикливается"]


def test_branches_and_next_counts():
    answer = extract_answer()
    step_of(answer, "s4")["branches"] = [{"label": "да", "to": "s5"}]
    step_of(answer, "s2")["next"] = ["s3", "s4"]
    step_of(answer, "s11")["next"] = ["s1"]
    errors = errors_of(answer)
    assert "s4: у решения 1 веток, нужно 2–3" in errors
    assert "s2: у task в next ровно один шаг, а не 2" in errors
    assert "s11: у конца next должен быть пустым" in errors


def test_ids_and_limits():
    answer = extract_answer()
    answer["steps"][1]["id"] = "step2"
    answer["roles"].append({"id": "r1", "name": "Ещё менеджер"})
    errors = errors_of(answer)
    assert "шаг «Перенести заявку в таблицу»: id «step2», нужен s1…s15" in errors and "id роли r1 повторяется" in errors
    answer = extract_answer()
    for i in range(13, 17):
        answer["steps"].append({**step_of(answer, "s12"), "id": f"s{i}"})
    errors = errors_of(answer)
    assert errors[0] == "шагов 16, можно не больше 15: объедини мелкие" and "шаг «Заявка закрыта»: id «s16», нужен s1…s15" \
        in errors


def test_long_texts_are_cut():
    answer = extract_answer()
    answer["title"] = "Очень длинное название процесса " * 5
    step_of(answer, "s2")["name"] = "слово " * 30
    process = process_of(answer=answer)
    assert len(process["title"]) <= 80 and len(process["steps"][1]["name"]) <= 80
    assert not process["title"].endswith(" ")


def test_not_objects_are_errors_not_crashes():
    assert errors_of({"title": 1, "roles": ["r1"], "steps": [None, 5]})[:3] == [
        "roles[0] — не объект", "нет ни одной роли", "steps[0] — не объект"]


# ---------- improve ----------

def test_digits_in_how_risk_summary_are_cleared():
    process = process_of()
    answer = improve_answer()
    answer["changes"][0]["how"] = "заявка за 5 минут попадает в CRM"
    answer["changes"][1]["risk"] = "потеря 10% заявок"
    answer["summary"] = "Экономия 34 ч в месяц"
    answer["changes"][2]["how"] = "договор из шаблона в 1С"   # a name, not a number
    improve, errors = check_improve(answer, process)
    assert errors == []
    assert improve["changes"][0]["how"] == "" and improve["changes"][1]["risk"] == "" and improve["summary"] == ""
    assert improve["changes"][2]["how"] == "договор из шаблона в 1С"
    assert improve["flags"]["numbers_in_text"] == [{"step": "s2", "field": "how"}, {"step": "s4", "field": "risk"},
                                                   {"step": None, "field": "summary"}]


def test_improve_rules_by_kind():
    process = process_of()
    answer = improve_answer()
    answer["changes"] = [c for c in answer["changes"] if c["step"] != "s4"]
    answer["changes"] += [{"step": "s1", "action": "remove", "merge_into": None, "how": "", "risk": ""},
                          {"step": "s4", "action": "merge", "merge_into": "s3", "how": "", "risk": ""},
                          {"step": "s42", "action": "keep", "merge_into": None, "how": "", "risk": ""},
                          {"step": "s2", "action": "keep", "merge_into": None, "how": "", "risk": ""}]
    errors = check_improve(answer, process)[1]
    assert errors == ["s1: у start нельзя remove, можно keep",
                      "s4: у decision нельзя merge, можно keep, automate",
                      "changes[5]: шага «s42» нет в процессе", "s2: больше одной записи"]


def test_merge_only_into_a_neighbouring_task_of_the_same_role():
    invoice = process_of("invoice")
    assert check_improve(improve_answer("invoice"), invoice)[0]["changes"][2] == {
        "step": "s7", "action": "merge", "merge_into": "s8", "how": "скан не нужен: файл счёта прикладывается к платёжке",
        "risk": "в общей папке не будет копий"}

    def merge(step, into):
        return check_improve({"changes": [{"step": step, "action": "merge", "merge_into": into, "how": "", "risk": ""}],
                              "summary": ""}, invoice)[1]

    assert merge("s8", "s7") == []                                    # the step before is a neighbour too
    assert merge("s2", "s8") == ["s2: s8 не соседний шаг"]
    assert merge("s5", "s6") == ["s5: сливать можно только с задачей той же роли, s6 не подходит"]
    assert merge("s2", "s2") == ["s2: merge_into «s2» — не другой шаг процесса"]
    assert merge("s2", None) == ["s2: merge_into «» — не другой шаг процесса"]


def test_merge_into_is_null_for_other_actions_and_texts_are_cut():
    answer = improve_answer()
    answer["changes"][0].update(merge_into="s3", how="очень " * 40)
    change = check_improve(answer, process_of())[0]["changes"][0]
    assert change["merge_into"] is None and len(change["how"]) <= 140


# ---------- external roles ----------

def test_external_roles_come_through():
    roles = {r["id"]: r for r in process_of()["roles"]}
    assert roles["r6"] == {"id": "r6", "name": "Клиент", "external": True}
    assert [r["external"] for r in roles.values()].count(True) == 1
    assert {r["name"]: r["external"] for r in process_of("invoice")["roles"]}["Поставщик"] is True


@pytest.mark.parametrize("value", [None, "true", 1])
def test_external_must_be_a_bool(value):
    answer = extract_answer()
    answer["roles"][0]["external"] = value
    assert errors_of(answer) == ["роль r1: external должен быть true или false"]
    del answer["roles"][0]["external"]
    assert errors_of(answer) == ["роль r1: external должен быть true или false"]


def test_external_steps_are_only_kept():
    process = process_of()
    answer = improve_answer()
    answer["changes"].append({"step": "s7", "action": "automate", "merge_into": None,
                              "how": "клиент отвечает кнопкой в письме", "risk": "не нажмёт"})
    improve, errors = check_improve(answer, process)
    assert errors == []
    assert improve["changes"][-1] == {"step": "s7", "action": "keep", "merge_into": None, "how": "", "risk": ""}
    assert improve["flags"]["external_kept"] == [{"step": "s7", "action": "automate"}]


def test_external_rule_wins_over_the_kind_rule_and_merge_rules():
    invoice = process_of("invoice")
    answer = {"changes": [{"step": "s1", "action": "remove", "merge_into": None, "how": "", "risk": ""}],
              "summary": ""}
    improve, errors = check_improve(answer, invoice)  # s1 is the supplier's start: kept, not an error
    assert errors == [] and improve["flags"]["external_kept"] == [{"step": "s1", "action": "remove"}]
    answer = extract_answer()
    for s in answer["steps"]:
        if s["id"] in ("s2", "s3"):
            s["role"] = "r6"
    process = process_of(answer=answer)
    merge = {"changes": [{"step": "s2", "action": "merge", "merge_into": "s3", "how": "в одно окно", "risk": ""}],
             "summary": ""}
    improve, errors = check_improve(merge, process)
    assert errors == [] and improve["changes"][0]["action"] == "keep" and improve["changes"][0]["merge_into"] is None
    # a keep of an external step is not reported, an unknown action is still an error
    keep = {"changes": [{"step": "s2", "action": "keep", "merge_into": None, "how": "", "risk": ""},
                        {"step": "s3", "action": "fly", "merge_into": None, "how": "", "risk": ""}], "summary": ""}
    improve, errors = check_improve(keep, process)
    assert improve["flags"]["external_kept"] == [] and errors == ["s3: у task нельзя fly, можно keep, automate, remove, merge"]
