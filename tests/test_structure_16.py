"""Структура документа (задача 16).

Экспертная оценка пилотных заключений: флаг объявлял группу показателей
неприменимой, разрыв в двадцать месяцев между отчётной датой и формированием
документа нигде не отмечался, «Фактическая база» не содержала величин,
на которых построены следующие разделы, вопросы адресовались строкам вне
набора форм, а вывода о дальнейших действиях в документе не было вовсе.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from finlib.llm.textcheck import (
    SEVERITY,
    Severity,
    TextContext,
    TextRule,
    check_text,
)
from finlib.report.appendix import checks_table, exclusions_table, not_calculated_table
from finlib.report.data import load_report_data
from finlib.report.policy import (
    QuestionSubject,
    ReportPolicy,
    Trigger,
    default_path,
    load_policy,
    months_between,
)
from finlib.report.summary import build_summary
from finlib.scoring.definitions import load_scoring

POLICY = load_policy()
SCORING = load_scoring()

# ПАО «Газпром» — полная отчётность, стоп-фактор и флаг холдинга разом.
FULL_INN = "7736050003"
# ПК «Стройсервис» — упрощённая, отрицательный капитал, надзорные сигналы.
STOPPED_INN = "2100010824"
# ООО «Магнит» — класс не присвоен, комплект за 2025 год в карантине.
NO_CLASS_INN = "2522002003"

# День, на который собираются документы в тестах: разрыв с отчётной датой
# заведомо превышает порог методики.
LATE = datetime(2026, 9, 16, 12, 0)


# --- 1. Флаг и стоп-фактор ----------------------------------------------------


def test_flag_conflict_is_found_where_both_fire(db_conn) -> None:
    """Флаг и стоп-фактор на одних показателях опознаются как столкновение."""
    data = load_report_data(FULL_INN, db_conn)
    conflict = data.flag_conflict()
    assert conflict is not None
    assert conflict.flag_code == "holding_structure"
    assert conflict.stop_factor_code == "weak_coverage"
    assert set(conflict.metrics) == {"nwc", "interest_cover"}


def test_flag_conflict_does_not_soften_the_stop_factor(db_conn) -> None:
    """Класс остаётся присвоенным по методике: флаг — не путь обхода оценки."""
    data = load_report_data(FULL_INN, db_conn)
    assert data.stop_factor_code == "weak_coverage"
    assert data.class_code == "C"
    conflict = data.flag_conflict()
    assert conflict is not None
    assert "не смягчается" in conflict.message


def test_flag_conflict_reaches_the_summary(db_conn) -> None:
    """Столкновение зафиксировано абзацем «Ключевого вывода»."""
    data = load_report_data(FULL_INN, db_conn)
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    assert "ручная проверка" in text
    assert "внутригрупповых расчётов" in text


def test_no_conflict_without_a_stop_factor(db_conn) -> None:
    """Без стоп-фактора столкновения нет, даже если флаг сработал."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    assert data.stop_factor_code is None
    assert data.flag_conflict() is None


# --- 2. Актуальность данных ---------------------------------------------------


@pytest.mark.parametrize(
    ("earlier", "later", "months"),
    [
        (date(2024, 12, 31), date(2026, 9, 16), 20),
        (date(2025, 12, 31), date(2026, 9, 16), 8),
        (date(2026, 9, 16), date(2026, 9, 16), 0),
        # День месяца меньше отчётного: месяц ещё не полный.
        (date(2025, 1, 31), date(2025, 3, 30), 1),
    ],
)
def test_months_between_counts_full_months(
    earlier: date, later: date, months: int
) -> None:
    """Разрыв считается полными месяцами."""
    assert months_between(earlier, later) == months


def test_stale_data_is_stated_in_the_summary(db_conn) -> None:
    """Разрыв сверх порога оговаривается в «Ключевом выводе»."""
    data = load_report_data(STOPPED_INN, db_conn)
    months = data.months_since_report(LATE)
    assert POLICY.freshness.stale(months)
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    assert "на отчётную дату, а не на день чтения" in text
    assert str(months) in text


def test_fresh_data_is_not_oговорена(db_conn) -> None:
    """Свежая отчётность оговорки не требует."""
    data = load_report_data(STOPPED_INN, db_conn)
    soon = datetime(2025, 3, 31, 12, 0)
    assert not POLICY.freshness.stale(data.months_since_report(soon))
    text = "\n".join(item.text for item in build_summary(data, SCORING, soon))
    assert "на отчётную дату, а не на день чтения" not in text


def test_header_names_both_dates_and_the_gap(db_conn, tmp_path) -> None:
    """Шапка называет отчётную дату, дату формирования и разрыв."""
    from docx import Document

    from finlib.report.document import build_report

    report = build_report(
        STOPPED_INN, db_conn, directory=tmp_path, with_text=False, generated_at=LATE
    )
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert "Отчётная дата: 31.12.2024" in text
    assert "Дата формирования документа: 16.09.2026" in text
    assert "Разрыв между отчётной датой и формированием: 20 мес." in text


# --- 3. Фактическая база ------------------------------------------------------


def test_fact_base_is_prescribed_by_methodology() -> None:
    """Состав обязательных величин задан методикой, а не кодом."""
    assert POLICY.fact_base.lines == ("1600", "1300", "2110", "2400")
    assert POLICY.fact_base.metrics == ("debt_total", "net_debt", "nwc")
    assert POLICY.fact_base.origin.strip()


def test_fact_base_covers_the_values_the_expert_missed(db_conn) -> None:
    """По Газпрому обязательны и долг, и чистый оборотный капитал, и выручка."""
    codes = load_report_data(FULL_INN, db_conn).fact_base_codes(POLICY)
    assert {"1600", "1300", "2110", "2400", "debt_total", "net_debt", "nwc"} <= set(
        codes
    )


def test_fact_base_drops_what_the_organisation_does_not_have(db_conn) -> None:
    """Нерассчитанный показатель в обязательные не попадает.

    Требовать назвать величину, которой нет, значило бы требовать её выдумать.
    """
    codes = load_report_data(STOPPED_INN, db_conn).fact_base_codes(POLICY)
    assert "nwc" not in codes
    assert "1600" in codes


def test_fact_base_block_reaches_the_model(db_conn) -> None:
    """Перечень уходит модели блоком, а не остаётся в методике."""
    from finlib.llm.context import build_context

    context = build_context(FULL_INN, db_conn)
    assert "СОСТАВ РАЗДЕЛОВ" in context.composition
    assert "debt_total" in context.composition
    assert "СОСТАВ РАЗДЕЛОВ" in context.blocks()


def test_extra_values_are_selected_by_machine_grounds(db_conn) -> None:
    """Сверх обязательных перечисляются участники стоп-фактора, флага и сдвигов.

    Отбор машинный: иначе состав раздела зависел бы от того, что модель сочтёт
    заслуживающим упоминания, — и в него попадало нераскрытие одной строки
    вместо совокупного долга.
    """
    from finlib.llm.context import build_context

    composition = build_context(FULL_INN, db_conn).composition
    extra = composition[composition.index("Сверх обязательных") :]
    assert "участвует в стоп-факторе" in extra
    assert "участвует в условии флага" in extra
    assert "изменение за период" in extra
    # Обязательные величины во второй перечень не дублируются.
    head = composition[: composition.index("Сверх обязательных")]
    assert "nwc" in head
    assert "nwc" not in extra


def test_extra_values_are_limited_to_the_declared_number(db_conn) -> None:
    """Наибольших изменений столько, сколько объявила методика."""
    from finlib.llm.context import build_context

    composition = build_context(FULL_INN, db_conn).composition
    extra = composition[composition.index("Сверх обязательных") :]
    assert extra.count("изменение за период") <= POLICY.fact_base.top_changes


def test_missing_fact_base_value_blocks_the_answer() -> None:
    """Раздел без обязательной величины отклоняется."""
    context = TextContext(fact_base=("1600", "nwc"))
    issues = check_text(
        {2: "Валюта баланса (1600) — 418 тыс. руб."},
        context,
        raw_sections={2: "Валюта баланса (1600) — 418 тыс. руб."},
    )
    codes = {item.rule for item in issues}
    assert TextRule.FACT_BASE_INCOMPLETE in codes
    assert SEVERITY[TextRule.FACT_BASE_INCOMPLETE] is Severity.BLOCKING


def test_complete_fact_base_passes() -> None:
    """Названы все обязательные — замечания нет."""
    marked = "Валюта баланса (1600) — 418, оборотный капитал (nwc) — -12."
    issues = check_text({2: marked}, TextContext(fact_base=("1600", "nwc")), {2: marked})
    assert not [
        item for item in issues if item.rule is TextRule.FACT_BASE_INCOMPLETE
    ]


def test_fact_base_is_checked_before_cleanup() -> None:
    """Правило смотрит в размеченный текст: очистка коды показателей снимает.

    В очищенном тексте кода `nwc` уже нет, и проверка по нему дала бы ложное
    срабатывание на верном ответе.
    """
    issues = check_text(
        {2: "Оборотный капитал — -12 тыс. руб."},
        TextContext(fact_base=("nwc",)),
        raw_sections={2: "Оборотный капитал (nwc) — -12 тыс. руб."},
    )
    assert not issues


# --- 4. Вопросы к организации -------------------------------------------------


def test_question_subjects_are_ranked_by_risk(db_conn) -> None:
    """Основания идут по убыванию связанного риска, надзорный сигнал первым."""
    from finlib.llm.context import build_context

    composition = build_context(STOPPED_INN, db_conn).composition
    # Смотреть надо перечень оснований, а не блок целиком: слово «стоп-фактор»
    # встречается и выше, среди величин, которые надо назвать.
    listed = composition[composition.index("Раздел 6") :]
    assert listed.index("надзорный сигнал") < listed.index("стоп-фактор")


def test_questions_are_limited_in_number() -> None:
    """Вопросов от трёх до пяти: меньше — не перечень, больше — не читают."""
    assert POLICY.questions.min_count == 3
    assert POLICY.questions.max_count == 5
    text = "\n".join(f"Вопрос {number}?" for number in range(8))
    issues = check_text({6: text}, TextContext(questions=POLICY.questions))
    assert TextRule.QUESTION_COUNT in {item.rule for item in issues}


def test_duplicate_questions_are_a_warning() -> None:
    """Дубль портит перечень, но верный в остальном ответ не отменяет."""
    text = (
        "Чем объясняется рост дебиторской задолженности?\n"
        "Чем объясняются рост дебиторских задолженностей?\n"
        "За счёт чего получена прибыль?"
    )
    issues = check_text({6: text}, TextContext(questions=POLICY.questions))
    duplicates = [item for item in issues if item.rule is TextRule.QUESTION_DUPLICATE]
    assert duplicates
    assert not duplicates[0].blocking


def test_question_about_non_disclosure_is_caught() -> None:
    """Вопрос о нераскрытии строки содержательного ответа не имеет."""
    text = (
        "Почему не раскрыты строки 1410 и 1510?\n"
        "За счёт чего получена прибыль?\n"
        "Кто сторона расчётов по дебиторской задолженности?"
    )
    issues = check_text({6: text}, TextContext(questions=POLICY.questions))
    assert TextRule.QUESTION_ABOUT_DISCLOSURE in {item.rule for item in issues}


def test_prompt_forbids_questions_about_non_disclosure() -> None:
    """Запрет стоит и в инструкции модели, а не только в проверке."""
    from finlib.llm.service import load_prompt

    prompt = load_prompt()
    assert "почему не раскрыта строка" in prompt
    assert "От трёх до пяти вопросов" in prompt


# --- 5. Предложения по дальнейшим действиям -----------------------------------


def test_actions_follow_machine_triggers() -> None:
    """Предложение выводится по признаку расчёта, а не по суждению."""
    always = {item.code for item in POLICY.actions_for(set())}
    assert always == {"monitoring"}
    escalated = {item.code for item in POLICY.actions_for({Trigger.SUPERVISORY_SIGNAL})}
    assert "escalation" in escalated
    assert "request_explanations" in escalated


def test_actions_section_is_written_without_the_model(db_conn, tmp_path) -> None:
    """Раздел детерминирован: он есть и в справке без текстовой части."""
    from docx import Document

    from finlib.report.document import ACTIONS_TITLE, build_report

    report = build_report(
        STOPPED_INN, db_conn, directory=tmp_path, with_text=False, generated_at=LATE
    )
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert ACTIONS_TITLE in text
    assert "Эскалация" in text, "надзорный сигнал требует эскалации"
    assert "Постановка на мониторинг" in text


def test_stale_data_asks_for_interim_statements(db_conn, tmp_path) -> None:
    """Разрыв сверх порога добавляет запрос более свежей отчётности."""
    from docx import Document

    from finlib.report.document import build_report

    report = build_report(
        STOPPED_INN, db_conn, directory=tmp_path, with_text=False, generated_at=LATE
    )
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert "промежуточную бухгалтерскую отчётность" in text


def test_action_wording_is_prescribed() -> None:
    """Формулировки предложений заданы методикой и не сочиняются."""
    for action in POLICY.actions:
        assert len(action.message) > 80, action.code
        assert action.when


# --- 6. Ключевой вывод: балл, шкала, класс до и после -------------------------


def test_summary_states_the_scale_where_the_score_is_disclosed(db_conn) -> None:
    """Рядом с баллом стоит шкала соответствия балла классу."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    if data.score_in_summary:  # pragma: no cover — зависит от данных пробы
        text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
        assert "Соответствие балла классу" in text


def test_scale_is_built_from_the_methodology() -> None:
    """Шкала печатается из методики, а не из зашитых чисел."""
    from finlib.report.summary import _scale

    text = _scale(SCORING)
    for item in SCORING.classes:
        assert item.code in text
    assert "граница относится к старшему классу" in text


def test_class_before_and_after_the_stop_factor(db_conn) -> None:
    """Видно, что именно сделал стоп-фактор с классом."""
    data = load_report_data(FULL_INN, db_conn)
    assert data.assessment is not None
    before = data.assessment["class_before_stop"]
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    if before and before != data.class_code:
        assert f"класс {before}" in text
        assert "стоп-фактор изменил его" in text


def test_score_stays_hidden_where_methodology_forbids_it(db_conn) -> None:
    """При стоп-факторе балл в «Ключевой вывод» не выносится.

    Правило старше задачи 16 и ею не отменяется: класс определён стоп-фактором,
    и соседство «балл 85, класс E» подрывает доверие к оценке.
    """
    data = load_report_data(STOPPED_INN, db_conn)
    assert data.stop_factor_code
    assert not data.score_in_summary
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    assert "Балл:" not in text


def test_confidence_explains_how_it_was_obtained(db_conn) -> None:
    """Порядок определения уверенности раскрыт, а не подразумевается."""
    data = load_report_data(FULL_INN, db_conn)
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    assert "Уверенность определяется числом оснований" in text
    assert "при двух и более — низкая" in text


# --- 7. Две таблицы причин ----------------------------------------------------


def test_constant_and_period_reasons_live_in_separate_tables(db_conn) -> None:
    """Постоянная причина исключения и периодная причина разведены."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    constant = exclusions_table(data)
    periodic = not_calculated_table(data)
    assert constant is not None and periodic is not None
    assert "Вид причины" in constant.header
    assert "Период" in periodic.header
    assert "Период" not in constant.header


def test_period_reasons_name_their_period(db_conn) -> None:
    """Причина стоит рядом с периодом, а не в слитой ячейке.

    Берётся упрощённая отчётность: у неё часть показателей не считается
    из-за нераскрытых строк, и периодные причины есть.
    """
    table = not_calculated_table(load_report_data(NO_CLASS_INN, db_conn))
    assert table is not None
    periods = {row[2] for row in table.rows}
    assert len(periods) >= 1
    for row in table.rows:
        assert row[2].count(".") == 2, row


# --- 8. Сводка контролей ------------------------------------------------------


def test_checks_table_names_period_and_object(db_conn) -> None:
    """По сводке видно, какой период отбракован и какая строка не сошлась."""
    table = checks_table(load_report_data(FULL_INN, db_conn))
    assert "Период" in table.header
    assert "Объект контроля" in table.header
    assert table.rows


def test_blocking_failure_is_stated_in_the_summary(db_conn) -> None:
    """Провал блокирующего контроля попадает в «Ключевой вывод» наименованием."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    failures = data.blocking_failures
    if not failures:  # pragma: no cover — зависит от данных пробы
        pytest.skip("у пробы нет провалившихся блокирующих контролей")
    text = "\n".join(item.text for item in build_summary(data, SCORING, LATE))
    assert "Блокирующие контроли качества дали отказ" in text
    # Код контроля — механизм проверки, читателю идёт наименование.
    assert not any(item["check_code"] in text for item in failures)


# --- 9. Раздел 4 и привязка тезисов ------------------------------------------


def test_section_four_is_named_after_signals() -> None:
    """Раздел 4 называется «Риски и надзорные сигналы» всюду одинаково."""
    from finlib.report.document import SIGNALS_SECTION, SIGNALS_TITLE
    from finlib.report.sections import EXPECTED

    assert dict(EXPECTED)[SIGNALS_SECTION] == SIGNALS_TITLE
    from finlib.llm.service import load_prompt

    assert f"### {SIGNALS_SECTION}. {SIGNALS_TITLE}" in load_prompt()


@pytest.mark.parametrize("inn", [STOPPED_INN, NO_CLASS_INN, FULL_INN])
def test_signal_basis_names_value_and_threshold(inn: str, db_conn, tmp_path) -> None:
    """У каждого сигнала приведены величина и отсечка, по которой он сработал.

    Отсечка приходит из деталей срабатывания, а не подбирается при выводе:
    у структурного сдвига и интенсивности пересмотра её прежде не было,
    и тезис в разделе о рисках оставался без порога.
    """
    from docx import Document

    from finlib.report.document import build_report

    data = load_report_data(inn, db_conn)
    if not data.signals:  # pragma: no cover — зависит от данных пробы
        pytest.skip("у организации сигналы не сработали")
    for signal in data.signals:
        assert (signal["details"] or {}).get("threshold"), signal["signal_code"]

    report = build_report(
        inn, db_conn, directory=tmp_path, with_text=False, generated_at=LATE
    )
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert text.count("Расчётная величина") == len(data.signals)
    assert text.count("отсечка") == len(data.signals)
    assert "предварительной" in text


@pytest.mark.parametrize("inn", [STOPPED_INN, NO_CLASS_INN, FULL_INN])
def test_signal_basis_repeats_the_value_of_the_wording(inn: str, db_conn) -> None:
    """Величина основания набрана так же, как величина формулировки.

    Прежде основание округляло само и печатало знак, которого в формулировке
    нет: рядом стояли «изменение — 181,2 п. п.» и «Расчётная величина:
    -181,18». Каждое число по отдельности верно, вместе они читаются
    как расхождение расчёта.
    """
    from decimal import Decimal

    from finlib.report.document import _signal_basis
    from finlib.scoring.signals import load_signals, shown

    data = load_report_data(inn, db_conn)
    if not data.signals:  # pragma: no cover — зависит от данных пробы
        pytest.skip("у организации сигналы не сработали")
    catalog = load_signals()
    for signal in data.signals:
        rule = catalog.rule_for(signal["signal_code"])
        assert rule is not None, signal["signal_code"]
        value = shown(rule, Decimal(str(signal["value"])))
        assert value in _signal_basis(signal), signal["signal_code"]
        if "{value}" in rule.text:
            assert value in signal["message"], signal["signal_code"]


def test_risk_thesis_without_a_value_is_a_warning() -> None:
    """Утверждение о риске без числа проверить нечем."""
    loose = (
        "Организация испытывает существенные трудности с обслуживанием своих "
        "обязательств, и положение её выглядит крайне неустойчивым по всем "
        "признакам, которые принято принимать во внимание."
    )
    issues = check_text({4: loose}, TextContext())
    found = [item for item in issues if item.rule is TextRule.RISK_WITHOUT_VALUE]
    assert found
    assert not found[0].blocking


def test_risk_thesis_with_a_value_passes() -> None:
    """Тезис с величиной замечания не вызывает."""
    text = (
        "Чистый оборотный капитал (nwc) отрицателен и составляет "
        "-521 415 920 тыс. руб., краткосрочные обязательства покрываются "
        "оборотными активами лишь частично."
    )
    issues = check_text({4: text}, TextContext())
    assert not [item for item in issues if item.rule is TextRule.RISK_WITHOUT_VALUE]


# --- методика: проверки самого справочника ------------------------------------


def raw() -> dict:
    """Справочник правил состава документа в исходном виде."""
    return yaml.safe_load(default_path().read_text(encoding="utf-8"))


def test_every_threshold_declares_its_origin() -> None:
    """Порог без происхождения неотличим от выдуманного."""
    assert POLICY.freshness.origin.strip()
    assert POLICY.fact_base.origin.strip()
    assert POLICY.questions.origin.strip()


def test_question_order_is_declared_not_hardcoded() -> None:
    """Порядок оснований объявлен в методике и полон."""
    assert POLICY.questions.subject_order[0] is QuestionSubject.SUPERVISORY_SIGNAL
    assert set(POLICY.questions.subject_order) == set(QuestionSubject)


def test_action_without_a_condition_does_not_load() -> None:
    """Предложение без условия применения методику не проходит."""
    payload = raw()
    del payload["actions"][0]["when"]
    with pytest.raises(ValidationError, match="when"):
        ReportPolicy.model_validate(payload)


def test_unknown_trigger_does_not_load() -> None:
    """Признак вне перечня не принимается: условие должно быть машинным."""
    payload = raw()
    payload["actions"][0]["when"] = ["когда станет тревожно"]
    with pytest.raises(ValidationError):
        ReportPolicy.model_validate(payload)


def test_fact_base_lines_must_be_line_codes() -> None:
    """Строка фактической базы задаётся кодом РСБУ, а не наименованием."""
    payload = raw()
    payload["fact_base"]["lines"] = ["валюта баланса"]
    with pytest.raises(ValidationError, match="код строки"):
        ReportPolicy.model_validate(payload)


def test_question_bounds_are_checked() -> None:
    """Нижняя граница числа вопросов не может быть выше верхней."""
    payload = raw()
    payload["questions"]["min_count"] = 9
    with pytest.raises(ValidationError, match="вопросов не может быть"):
        ReportPolicy.model_validate(payload)


def test_freshness_message_substitutes_the_gap() -> None:
    """Разрыв подставляется в предписанную оговорку, а не пишется словами."""
    message = POLICY.freshness.message(20)
    assert "20 месяцев" in message
    assert "{months}" not in message


def test_decimal_values_are_not_floats() -> None:
    """Пороги методики читаются точными величинами."""
    assert isinstance(POLICY.freshness.max_months, int)
    assert not isinstance(POLICY.fact_base.top_changes, Decimal | float)
