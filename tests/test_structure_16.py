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
    check_calculated,
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


def _fact_base_of(inn: str, db_conn) -> list[str]:
    """Раздел «Фактическая база», собранный расчётом."""
    from finlib.metrics.definitions import load_metrics
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.report.composition import fact_base
    from finlib.report.data import load_report_data
    from finlib.scoring.definitions import load_scoring

    data = load_report_data(inn, db_conn)
    return fact_base(
        data,
        POLICY,
        load_lines(),
        load_metrics(),
        load_scoring(),
        ReportingType(data.organization["reporting_type"]),
    )


def test_fact_base_is_built_by_calculation(db_conn) -> None:
    """Перечень собирает расчёт, а не модель: состав задан методикой.

    Прежде перечень уходил модели блоком, и она его переписывала — в проверке
    17.09.2026 писала производные вместо величин и теряла обязательные
    позиции. Писать его ей больше не поручено.
    """
    found = "\n".join(_fact_base_of(FULL_INN, db_conn))
    # Показатель называется наименованием: код — внутренний идентификатор
    # методики, и читателю он ничего не говорит. Код строки отчётности
    # остаётся: по нему величина находится в самой отчётности.
    assert "Совокупный долг" in found
    assert "debt_total" not in found
    assert "(1600)" in found
    # Модели состав фактической базы больше не передаётся: раздел не её.
    from finlib.llm.context import build_context

    assert "СОСТАВ РАЗДЕЛОВ" not in build_context(FULL_INN, db_conn).blocks()


def test_extra_values_are_selected_by_machine_grounds(db_conn) -> None:
    """Сверх обязательных перечисляются участники стоп-фактора и сдвигов.

    Отбор машинный: иначе состав раздела зависел бы от того, что модель сочтёт
    заслуживающим упоминания, — и в него попадало нераскрытие одной строки
    вместо совокупного долга.
    """
    found = _fact_base_of(FULL_INN, db_conn)
    text = "\n".join(found)
    assert POLICY.fact_base_section.extra_intro_text in found
    extra = text[text.index(POLICY.fact_base_section.extra_intro_text) :]
    assert "изменение за период" in extra
    # Обязательные величины во второй перечень не дублируются.
    head = text[: text.index(POLICY.fact_base_section.extra_intro_text)]
    assert "Чистый оборотный капитал" in head
    assert "Чистый оборотный капитал" not in extra


def test_extra_values_are_limited_to_the_declared_number(db_conn) -> None:
    """Наибольших изменений столько, сколько объявила методика."""
    text = "\n".join(_fact_base_of(FULL_INN, db_conn))
    extra = text[text.index(POLICY.fact_base_section.extra_intro_text) :]
    assert extra.count("изменение за период") <= POLICY.fact_base.top_changes


def test_missing_fact_base_value_blocks_the_answer() -> None:
    """Раздел без обязательной величины отклоняется.

    Раздел 2 собирает расчёт, поэтому правило переехало в `check_calculated`:
    оно о содержании, а не об авторстве. Перечень задан методикой, а расчёт
    печатает только те величины, которые сумел отрендерить.
    """
    context = TextContext(fact_base=("1600", "nwc"))
    issues = check_calculated(
        {2: "Валюта баланса (1600) — 418 тыс. руб."}, context
    )
    codes = {item.rule for item in issues}
    assert TextRule.FACT_BASE_INCOMPLETE in codes
    assert SEVERITY[TextRule.FACT_BASE_INCOMPLETE] is Severity.BLOCKING


def test_complete_fact_base_passes() -> None:
    """Названы все обязательные — замечания нет."""
    text = "Валюта баланса (1600) — 418, оборотный капитал (nwc) — -12."
    issues = check_calculated({2: text}, TextContext(fact_base=("1600", "nwc")))
    assert not [
        item for item in issues if item.rule is TextRule.FACT_BASE_INCOMPLETE
    ]


def test_metric_is_sought_by_its_name_not_its_code() -> None:
    """Показатель опознаётся по наименованию: кода его в документе больше нет.

    Прежде раздел печатал «Чистый оборотный капитал (nwc)», и правило состава
    искало величины по кодам. Код показателя — внутренний идентификатор
    методики, читателю он ничего не говорит, и правило технических
    идентификаторов требовало его отсутствия: два правила отменяли друг друга
    на одном тексте.
    """
    context = TextContext(
        fact_base=("1600", "nwc"),
        metric_names={"nwc": "Чистый оборотный капитал"},
    )
    named = "БАЛАНС (актив) (1600) — 418. Чистый оборотный капитал — -12."
    assert not [
        item
        for item in check_calculated({2: named}, context)
        if item.rule is TextRule.FACT_BASE_INCOMPLETE
    ]

    silent = "БАЛАНС (актив) (1600) — 418 тыс. руб."
    missing = [
        item
        for item in check_calculated({2: silent}, context)
        if item.rule is TextRule.FACT_BASE_INCOMPLETE
    ]
    assert missing
    # В замечании названа величина, а не её код: читать его будет человек.
    assert "Чистый оборотный капитал" in missing[0].message


def test_calculated_sections_carry_no_internal_identifiers(db_conn, tmp_path) -> None:
    """Внутренних идентификаторов методики в разделах документа нет.

    Правило действует и на разделы расчёта: коды строк отчётности остаются,
    коды показателей и производных — нет.
    """
    from docx import Document

    from finlib.llm.cleanup import has_identifiers
    from finlib.report.document import build_report

    report = build_report(FULL_INN, db_conn, directory=tmp_path, with_text=False)
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert not has_identifiers(text), has_identifiers(text)[:5]
    assert "(1600)" in text, "код строки отчётности остаётся"


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
    from finlib.metrics.definitions import load_metrics
    from finlib.report.composition import questions
    from finlib.report.data import load_report_data
    from finlib.scoring.definitions import load_scoring

    data = load_report_data(STOPPED_INN, db_conn)
    found = questions(data, POLICY, load_metrics(), load_scoring(), [])
    text = "\n".join(found)
    assert "надзорным сигналам" in text
    assert text.index("надзорным сигналам") < text.index("стоп-фактором")
    assert len(found) <= POLICY.questions.max_count


def test_questions_are_prescribed_not_written(db_conn) -> None:
    """Формулировки вопросов предписаны методикой, а не сочиняются моделью.

    Проверка 17.09.2026: из пяти сочинённых вопросов по ООО «Магнит» три были
    вида «какие строки отсутствуют в расчёте» — ровно те, что методика
    запрещает, — а по ПК «Стройсервис» модель ввела порог «90 % выручки»
    и посчитала разность величин двух периодов.
    """
    from finlib.metrics.definitions import load_metrics
    from finlib.report.composition import questions
    from finlib.report.data import load_report_data
    from finlib.scoring.definitions import load_scoring

    data = load_report_data(STOPPED_INN, db_conn)
    found = questions(data, POLICY, load_metrics(), load_scoring(), [])
    prescribed = {" ".join(item.split()) for item in POLICY.questions.texts.values()}
    for question in found:
        # Формулировка совпадает с предписанной с точностью до подстановок.
        assert any(
            question.startswith(item.split("{", 1)[0]) for item in prescribed
        ), question


def test_question_count_is_declared_inapplicable() -> None:
    """Число вопросов задаёт расчёт, и правило объявлено неприменимым явно.

    Не удалено и не переведено в предупреждение: удалённое правило нельзя
    отличить от забытого, а замолчавшее — от работающего. В реестре стоят
    причина и дата.
    """
    from finlib.llm.textcheck import NOT_APPLICABLE

    assert POLICY.questions.min_count == 3
    assert POLICY.questions.max_count == 5
    assert TextRule.QUESTION_COUNT in NOT_APPLICABLE
    assert "17.09.2026" in NOT_APPLICABLE[TextRule.QUESTION_COUNT]


def test_duplicate_questions_are_a_warning() -> None:
    """Дубль портит перечень, но верный в остальном документ не отменяет.

    Правило переехало на текст расчёта: он собирает вопросы сам и снимает
    дубли точным сравнением, а два основания одного рода дают разные строки
    с одним смыслом.
    """
    text = (
        "Чем объясняется рост дебиторской задолженности?\n"
        "Чем объясняются рост дебиторских задолженностей?\n"
        "За счёт чего получена прибыль?"
    )
    issues = check_calculated({6: text}, TextContext(questions=POLICY.questions))
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
    issues = check_calculated({6: text}, TextContext(questions=POLICY.questions))
    assert TextRule.QUESTION_ABOUT_DISCLOSURE in {item.rule for item in issues}


def test_questions_never_ask_about_non_disclosure(db_conn) -> None:
    """Вопрос о нераскрытии строки не может быть задан вовсе.

    Прежде запрет стоял в инструкции модели, и она его нарушала. Теперь
    вопросы собирает расчёт из предписанных формулировок, и нарушить запрет
    нечем: формулировки о нераскрытии в справочнике нет.
    """
    forbidden = ("не раскрыт", "отсутствуют в расчёте", "почему не раскры")
    for text in POLICY.questions.texts.values():
        lowered = " ".join(text.split()).lower()
        for item in forbidden:
            assert item not in lowered, text


def test_calculated_sections_are_checked_on_a_real_document(db_conn, tmp_path) -> None:
    """Разделы расчёта проходят проверку при сборке документа.

    Правила переехали к тем разделам, которые теперь собирает расчёт. Раньше
    они стояли в `check_text` и объектов не имели вовсе: `verify` отдаёт
    только разделы модели. Здесь проверяется, что вызов на месте и правила
    видят настоящий текст, а не пустые разделы.
    """
    from finlib.llm import textcheck
    from finlib.report.document import build_report

    original = textcheck.check_calculated
    seen: list[dict[int, str]] = []

    def watching(sections, context):
        seen.append(sections)
        return original(sections, context)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(textcheck, "check_calculated", watching)
    try:
        build_report(STOPPED_INN, db_conn, directory=tmp_path, with_text=False)
    finally:
        monkey.undo()

    assert seen, "проверка разделов расчёта не вызывалась"
    sections = seen[0]
    assert sections[2].strip(), "раздел «Фактическая база» пуст"
    assert sections[6].strip(), "раздел «Вопросы к организации» пуст"
    assert sections[3].strip(), "раздел «Аналитическая интерпретация» пуст"


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


def test_section_four_is_built_by_calculation() -> None:
    """Раздел 4 собирает расчёт, и модели он не поручается.

    Прежде раздел писала модель поверх детерминированного перечня сигналов,
    и замер 17.09.2026 показал, что он вырождается: по ООО «Магнит» весь
    раздел свёлся к фразе «Надзорный сигнал имеет величину 20,8».
    """
    from finlib.llm.service import PromptScheme, load_prompt
    from finlib.report.document import SIGNALS_SECTION, SIGNALS_TITLE
    from finlib.report.policy import load_policy
    from finlib.report.sections import EXPECTED

    assert SIGNALS_SECTION not in dict(EXPECTED)
    for scheme in PromptScheme:
        prompt = load_prompt(scheme=scheme)
        assert f"### {SIGNALS_SECTION}. {SIGNALS_TITLE}" not in prompt
        # Модели прямо сказано, что раздел не её и написанное будет отброшено.
        assert "формируются расчётом" in prompt
    # Тексты раздела предписаны методикой, а не зашиты в сборку документа.
    assert load_policy().risks.none_found_text


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


def test_risk_rule_is_declared_inapplicable() -> None:
    """Правило о величине при тезисе риска объявлено неприменимым явно.

    Раздел 4 собирает расчёт, и величина с отсечкой печатаются при каждой
    формулировке сигнала: тезиса без величины в разделе не возникает.
    Код правила оставлен — предмет вернётся, если раздел снова отдадут
    модели, — но молчать без объяснения оно не вправе.
    """
    from finlib.llm.textcheck import NOT_APPLICABLE

    assert TextRule.RISK_WITHOUT_VALUE in NOT_APPLICABLE
    assert "17.09.2026" in NOT_APPLICABLE[TextRule.RISK_WITHOUT_VALUE]


def test_inapplicable_rules_do_not_fire() -> None:
    """Неприменимое правило не вправе одновременно и молчать, и применяться."""
    from finlib.llm.textcheck import NOT_APPLICABLE

    loose = (
        "Организация испытывает существенные трудности с обслуживанием своих "
        "обязательств, и положение её выглядит крайне неустойчивым по всем "
        "признакам, которые принято принимать во внимание."
    )
    sections = {4: loose, 6: "\n".join(f"Вопрос {number}?" for number in range(8))}
    context = TextContext(questions=POLICY.questions)
    fired = {item.rule for item in check_text(sections, context)}
    fired |= {item.rule for item in check_calculated(sections, context)}
    assert not fired & set(NOT_APPLICABLE)


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
