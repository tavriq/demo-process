"""Minutes and waits go to the step the clause speaks about (замер 12.10, docs/SPEC-analyst.md): the
owner answers about another step, a wait or how early, not about the asked one."""

from app import understand

SALES = {"roles": [{"id": "r1", "name": "Менеджер"}, {"id": "r2", "name": "Инженер"},
                   {"id": "r3", "name": "Клиент", "external": True}],
         "steps": [{"id": "s1", "name": "Заявка пришла", "kind": "start", "role": "r1"},
                   {"id": "s2", "name": "Перенос заявки в таблицу", "kind": "task", "role": "r1"},
                   {"id": "s3", "name": "Уточнение деталей у клиента", "kind": "task", "role": "r1"},
                   {"id": "s4", "name": "Подбор оборудования и расчёт сметы", "kind": "task", "role": "r2"},
                   {"id": "s5", "name": "Данных достаточно?", "kind": "decision", "role": "r2"},
                   {"id": "s6", "name": "Подготовка и отправка КП", "kind": "task", "role": "r1"}]}
INVOICE = {"roles": [{"id": "r1", "name": "Бухгалтер"}, {"id": "r2", "name": "Директор"}],
           "steps": [{"id": "s2", "name": "Получение счёта", "kind": "task", "role": "r1"},
                     {"id": "s3", "name": "Сверка счёта с заявкой и накладной", "kind": "task", "role": "r1"},
                     {"id": "s5", "name": "Подписание счёта директором", "kind": "task", "role": "r2"},
                     {"id": "s6", "name": "Внесение счёта в 1С и реестр платежей", "kind": "task", "role": "r1"},
                     {"id": "s8", "name": "Проведение платежа", "kind": "task", "role": "r1"}]}
RETURNS = {"roles": [{"id": "r2", "name": "Оператор"}, {"id": "r5", "name": "Бухгалтер"}],
           "steps": [{"id": "s5", "name": "Оформление возврата и выдача кода", "kind": "task", "role": "r2"},
                     {"id": "s8", "name": "Возврат денег клиенту", "kind": "task", "role": "r5"}]}
ONBOARDING = {"roles": [{"id": "r1", "name": "Кадровик"}],
              "steps": [{"id": "s2", "name": "Оформление документов", "kind": "task", "role": "r1"}]}


def got(answer, gap, process):
    field = gap.split(".")[1]
    return [(u["field"], u["value"]) for u in understand.from_answer(answer, gap, field, process)]


def test_another_steps_minutes_go_to_that_step():
    a = ("Сверка одного счёта с заявкой и накладной у бухгалтера занимает примерно 20 минут, если всё сходится. "
         "Сколько времени уходит именно на проведение платежа, я не знаю.")
    assert got(a, "s8.minutes", INVOICE) == [("s3.minutes", 20)]
    assert got(a, "s2.minutes", INVOICE) == [("s3.minutes", 20)]
    a = ("Про время оператора в описании ничего нет. Единственные 5 минут относятся к бухгалтеру: "
         "столько уходит на один возврат денег через банк-клиент.")
    assert got(a, "s5.minutes", RETURNS) == [("s8.minutes", 5)]


def test_two_steps_in_one_clause_give_nothing():
    a = ("Не знаю. В описании сказано только про сверку счёта (около 20 минут, если всё сходится), "
         "про время ожидания подписи и исправлений.")
    assert got(a, "s6.minutes", INVOICE) == []


def test_the_asked_step_named_or_not_named_keeps_its_minutes():
    assert got("Минут 20, если всё сходится", "s3.minutes", INVOICE) == [("s3.minutes", 20)]
    assert got("Сверка у меня минут 15", "s3.minutes", INVOICE) == [("s3.minutes", 15)]
    assert got("Директор подписывает минут 5, но ждём его 2 дня", "s5.minutes", INVOICE) == [("s5.minutes", 5)]


def test_days_are_minutes_only_when_said_as_work():
    a = "В описании нет времени на подготовку КП. Там сказано только, что КП делается в ворде, а клиент думает неделю."
    assert got(a, "s6.minutes", SALES) == []
    assert got("Подписи ждём обычно 2–3 дня", "s5.minutes", INVOICE) == []
    assert got("У инженера смета занимает день", "s4.minutes", SALES) == [("s4.minutes", 480)]


def test_the_time_of_the_work_is_not_a_wait():
    a = ("Не знаю. Я знаю только, что менеджер тратит около 10 минут на перенос одной заявки в таблицу, "
         "а инженер около 40 минут на подбор и смету.")
    assert got(a, "s4.wait_hours", SALES) == []
    assert got("На одну заявку уходит минут 10.", "s2.wait_hours", SALES) == []
    assert got("Ждём ответа обычно два дня", "s4.wait_hours", SALES) == [("s4.wait_hours", 16)]


def test_how_early_is_not_a_wait():
    a = "Заявку на выход я получаю обычно за неделю до выхода, а иногда за 1 день."
    assert got(a, "s2.wait_hours", ONBOARDING) == []


def test_without_a_draft_as_before():
    assert understand.from_answer("минут 20", "s3.minutes", "minutes") == [
        {"field": "s3.minutes", "value": 20, "unit": "мин"}]
