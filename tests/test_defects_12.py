"""Тесты семи дефектов, найденных экспертной оценкой пилотных заключений.

Каждый тест назван по номеру дефекта из TASKS-2.md, чтобы регресс был виден
сразу: по имени понятно, что именно вернулось.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from finlib.metrics.definitions import EXCLUSION_ORDER, ExclusionKind, load_metrics
from finlib.normalize.lines import UnitSource, load_lines
from finlib.quality.codes import CheckCode
from finlib.quality.thresholds import load_thresholds
from finlib.report.appendix import (
    _percent,
    checks_table,
    groups_table,
    metrics_table,
    not_calculated_table,
    provenance,
)
from finlib.report.consistency import check_document
from finlib.report.data import load_report_data
from finlib.scoring.definitions import load_scoring

FULL_INN = "7736050003"
STOPPED_INN = "2100010824"
NO_CLASS_INN = "2522002003"

LINES = load_lines()
METRICS = load_metrics()
SCORING = load_scoring()


# --- 1. Единица измерения ---------------------------------------------------


def test_defect_1_unit_is_determined_by_the_form() -> None:
    """Единица берётся из формы, а не принимается как предположение."""
    units = LINES.units
    assert units.okei_code == "384"
    assert units.source_for(["0710001", "0710002"]) is UnitSource.FORM_STANDARD
    assert units.origin.strip(), "происхождение правила обязано быть названо"


def test_defect_1_unknown_forms_leave_unit_undetermined() -> None:
    """Комплект из неизвестных форм единицы не получает."""
    assert LINES.units.source_for(["9999999"]) is UnitSource.UNKNOWN
    assert LINES.units.source_for([]) is UnitSource.UNKNOWN


def test_defect_1_control_is_blocking() -> None:
    """Неопределённая единица отправляет комплект в карантин."""
    severity = load_thresholds().severity_of(CheckCode.UNIT_NOT_DETERMINED.value)
    assert severity.value == "blocking"


def test_defect_1_no_assumption_wording_in_the_document(db_conn) -> None:
    """Формулировки-предположения в документе нет."""
    data = load_report_data(FULL_INN, db_conn)
    text = "\n".join(provenance(data, "модель", datetime(2026, 1, 1)))
    assert "предположение" not in text
    assert "по умолчанию" not in text
    assert "определена формой отчётности" in text


# --- 2. Веса групп ----------------------------------------------------------


def test_defect_2_percent_formatting() -> None:
    """Доля 0,3 печатается как 30,0 %, а не как 3000,0 %."""
    assert _percent(Decimal("0.3")) == "30,0 %"
    assert _percent(Decimal("0.2")) == "20,0 %"
    assert _percent(Decimal("1")) == "100,0 %"
    assert _percent(None) == "—"


def test_defect_2_weights_are_stored_as_shares(db_conn) -> None:
    """Номинальный и фактический вес — в одних единицах, оба доли."""
    data = load_report_data(FULL_INN, db_conn)
    assert data.groups
    for group in data.groups:
        assert Decimal(0) <= group["nominal_weight"] <= Decimal(1), group["group_code"]
        assert Decimal(0) <= group["effective_weight"] <= Decimal(1), group["group_code"]
    total = sum(item["nominal_weight"] for item in data.groups)
    assert total == Decimal(1)


def test_defect_2_rendered_weights_are_sane(db_conn) -> None:
    """В таблице приложения веса не превышают ста процентов."""
    table = groups_table(load_report_data(FULL_INN, db_conn))
    assert table is not None
    for row in table.rows:
        for cell in row:
            if cell.endswith(" %"):
                value = Decimal(cell.removesuffix(" %").replace(",", "."))
                assert value <= 100, row


# --- 3. «Рассчитано» против «участвует в балле» -----------------------------


def test_defect_3_column_says_what_it_counts(db_conn) -> None:
    """Графа названа так, как считает: показатели в балле."""
    data = load_report_data(FULL_INN, db_conn)
    groups = groups_table(data)
    assert groups is not None
    assert "Показателей в балле" in groups.header
    assert "Показателей" not in groups.header

    metrics = metrics_table(data)
    assert "Участвует в балле" in metrics.header


def test_defect_3_zero_in_the_column_means_zero_in_the_score(db_conn) -> None:
    """Ноль в графе не означает, что показатель не рассчитан."""
    data = load_report_data(FULL_INN, db_conn)
    turnover = [item for item in data.groups if item["group_code"] == "turnover"]
    assert turnover, "группа «Оборачиваемость» есть в разложении"
    assert turnover[0]["metrics_used"] == 0
    calculated = [
        item
        for item in data.metrics
        if item.group_name == "Оборачиваемость" and any(item.values.values())
    ]
    assert calculated, "при нуле в графе показатели группы всё же рассчитаны"


# --- 4. Класс и отказ в классе ----------------------------------------------


def test_defect_4_stop_factor_outranks_sufficiency(db_conn) -> None:
    """При стоп-факторе класс присваивается, а не отменяется узостью основания."""
    data = load_report_data(STOPPED_INN, db_conn)
    assert data.stop_factor_code
    assert data.class_code, "стоп-фактор обязан присвоить класс"
    assert data.assessment["no_class_reason"] is None
    assert data.breadth_reason, "узость основания фиксируется отдельно"


def test_defect_4_no_contradiction_in_the_document(db_conn) -> None:
    """Документ не содержит одновременно присвоения и неприсвоения класса."""
    for inn in (FULL_INN, STOPPED_INN, NO_CLASS_INN):
        data = load_report_data(inn, db_conn)
        problems = [item.code for item in check_document(data, "")]
        assert "verdict_is_ambiguous" not in problems, inn


def test_defect_4_score_is_withheld_without_scoring(db_conn) -> None:
    """Класс от стоп-фактора при узком основании балла не раскрывает."""
    data = load_report_data(STOPPED_INN, db_conn)
    assert not data.score_in_summary
    assert not data.score_in_appendix
    assert groups_table(data) is None


# --- 5. Причины исключения --------------------------------------------------


def test_defect_5_every_exclusion_declares_its_kind() -> None:
    """У каждой причины исключения есть машинный вид."""
    checked = 0
    for metric in METRICS.metrics:
        if metric.in_scoring:
            assert metric.scoring_exclusion_kind is None, metric.code
            continue
        checked += 1
        assert metric.scoring_exclusion_kind is not None, metric.code
    assert checked >= 10


def test_defect_5_hierarchy_is_fixed() -> None:
    """Иерархия причин задана явно и в согласованном порядке."""
    assert EXCLUSION_ORDER == (
        ExclusionKind.STOP_FACTOR,
        ExclusionKind.NO_LEVEL_SCALE,
        ExclusionKind.DUPLICATE,
        ExclusionKind.NO_DATA,
    )
    assert ExclusionKind.STOP_FACTOR.rank < ExclusionKind.NO_DATA.rank


def test_defect_5_reasons_are_ordered_by_hierarchy(db_conn) -> None:
    """В приложении причины идут по иерархии, а не как попало."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    ranks = [item.exclusion_rank for item in data.excluded_by_methodology]
    assert ranks == sorted(ranks)


def test_defect_5_reason_does_not_assert_a_fact_about_the_organisation() -> None:
    """Текст причины не утверждает того, чего у организации может не быть.

    У организации с капиталом 82 251 тыс. руб. документ разъяснял, чем плох
    отрицательный собственный капитал.
    """
    equity = METRICS.require("equity")
    assert equity.scoring_exclusion_kind is ExclusionKind.STOP_FACTOR
    assert "отрицательный собственный капитал опускает" not in equity.scoring_exclusion_reason


def test_defect_5_table_names_the_kind(db_conn) -> None:
    """Таблица постоянных причин называет вид причины отдельной графой.

    Задача 16 разделила таблицу надвое: постоянные причины исключения
    по методике и периоды, за которые показатель не рассчитан. Вид причины
    относится к первой — у периодной причины вида нет, есть период.
    """
    from finlib.report.appendix import exclusions_table

    table = exclusions_table(load_report_data(NO_CLASS_INN, db_conn))
    assert table is not None
    assert "Вид причины" in table.header


# --- 6. Состав отчётности ---------------------------------------------------


def test_defect_6_quarantined_sets_are_not_called_accepted(db_conn) -> None:
    """Отбракованный комплект не перечисляется среди принятых."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    assert data.quarantined_sources, "у пробы есть отбракованный комплект"
    accepted = {item["report_year"] for item in data.accepted_sources}
    quarantined = {item["report_year"] for item in data.quarantined_sources}
    assert not (accepted & quarantined)

    text = "\n".join(provenance(data, "модель", datetime(2026, 1, 1)))
    year = str(next(iter(quarantined)))
    accepted_line = next(line for line in text.split("\n") if "в расчёте:" in line)
    assert year not in accepted_line


def test_defect_6_consistency_control_catches_the_contradiction(db_conn) -> None:
    """Контроль ловит расхождение между «Ограничениями» и «Происхождением»."""
    data = load_report_data(NO_CLASS_INN, db_conn)
    year = data.quarantined_sources[0]["report_year"]

    silent = check_document(data, "- Оговорок нет")
    assert any(item.code == "quarantine_not_disclosed" for item in silent)

    honest = check_document(
        data, f"- Отчётность за период 31.12.{year} не включена в расчёт"
    )
    assert not any(item.code.startswith("source") for item in honest)


def test_defect_6_accepted_year_called_excluded_is_caught(db_conn) -> None:
    """Принятый комплект, названный невключённым, — тоже противоречие."""
    data = load_report_data(FULL_INN, db_conn)
    year = data.accepted_sources[0]["report_year"]
    problems = check_document(data, f"- Отчётность за {year} год не включена в расчёт")
    assert any(item.code == "source_both_excluded_and_accepted" for item in problems)


# --- 7. Нумерация таблиц ----------------------------------------------------


@pytest.mark.parametrize("inn", [FULL_INN, STOPPED_INN, NO_CLASS_INN])
def test_defect_7_table_titles_carry_no_numbers(inn: str, db_conn) -> None:
    """Номер в заголовке таблицы не зашит: его ставит сборщик документа."""
    from finlib.report.appendix import exclusions_table

    data = load_report_data(inn, db_conn)
    tables = [
        metrics_table(data),
        exclusions_table(data),
        not_calculated_table(data),
        groups_table(data),
        checks_table(data),
    ]
    for table in tables:
        if table is not None:
            assert not table.title.startswith("Таблица"), table.title


def test_defect_7_numbering_is_continuous(db_conn, tmp_path) -> None:
    """Нумерация сквозная и без пропусков даже там, где таблица не строится."""
    from docx import Document

    from finlib.llm.service import Conclusion
    from finlib.report.document import build_report

    answer = "\n".join(
        f"### {number}. {title}\nТекст."
        for number, title in (
            (2, "Фактическая база"),
            (3, "Аналитическая интерпретация"),
            (4, "Риски"),
            (5, "Ограничения анализа"),
            (6, "Вопросы к организации"),
        )
    )
    conclusion = Conclusion(
        inn=STOPPED_INN,
        report_date=date(2024, 12, 31),
        text=answer,
        model="тестовая-модель",
        attempt=1,
        checked_numbers=0,
    )
    report = build_report(
        STOPPED_INN, db_conn, conclusion=conclusion, directory=tmp_path
    )
    document = Document(report.path)
    numbers = [
        int(item.text.split()[1].rstrip("."))
        for item in document.paragraphs
        if item.text.startswith("Таблица ")
    ]
    assert numbers == list(range(1, len(numbers) + 1)), numbers
    assert len(numbers) == len(document.tables)


# --- 1 (продолжение). Правдоподобие величин --------------------------------


def test_magnitude_bounds_have_origin() -> None:
    """Пороги правдоподобия объявлены с происхождением, как калибровочные точки."""
    magnitude = load_thresholds().magnitude
    assert magnitude.balance_total.origin.strip()
    assert magnitude.period_shift.origin.strip()
    assert magnitude.balance_total.min < magnitude.balance_total.max


@pytest.mark.parametrize(
    ("total", "flagged"),
    [
        (Decimal(418), False),
        (Decimal("26162827395"), False),
        (Decimal(5), True),
        (Decimal("500000000000"), True),
        (Decimal(0), False),
    ],
)
def test_balance_bounds_catch_only_implausible(total: Decimal, flagged: bool) -> None:
    """Границы ловят подмену единицы и не задевают реальные величины проб.

    Ноль исключён намеренно: умножение на тысячу его не меняет, подменой
    единицы он быть не может.
    """
    bounds = load_thresholds().magnitude.balance_total
    assert (bounds.implausible(total) is not None) is flagged


@pytest.mark.parametrize(
    ("current", "previous", "shifted"),
    [
        (Decimal(418000), Decimal(418), True),
        (Decimal(418), Decimal(418000), True),
        (Decimal(125400), Decimal(418), False),
        (Decimal(500), Decimal(418), False),
        (Decimal(1000), Decimal(1), False),
    ],
)
def test_period_shift_catches_only_thousandfold(
    current: Decimal, previous: Decimal, shifted: bool
) -> None:
    """Ловится кратность тысяче, а не всякий большой скачок.

    Рост в триста раз бывает хозяйственным событием и ловится jump_detection;
    ровно тысячекратный — подмена единицы. На микровеличинах (база ниже
    min_base) отношение бессмысленно и не проверяется.
    """
    rule = load_thresholds().magnitude.period_shift
    assert (rule.shifted(current, previous) is not None) is shifted


def test_magnitude_severities_match_the_decision() -> None:
    """Границы предупреждают, тысячекратный скачок блокирует."""
    thresholds = load_thresholds()
    assert thresholds.severity_of(CheckCode.BALANCE_MAGNITUDE.value).value == "warning"
    assert (
        thresholds.severity_of(CheckCode.PERIOD_MAGNITUDE_SHIFT.value).value == "blocking"
    )


def test_magnitude_checks_ran_on_real_data(db_conn) -> None:
    """Оба контроля выполняются на пробах и не срабатывают ложно."""
    from finlib.db import fetch_all

    rows = fetch_all(
        "SELECT check_code, status, count(*) AS n FROM dq_log "
        "WHERE check_code = ANY(%(c)s) GROUP BY 1, 2",
        {"c": [CheckCode.BALANCE_MAGNITUDE.value, CheckCode.PERIOD_MAGNITUDE_SHIFT.value]},
    )
    assert rows, "контроли правдоподобия не выполнялись"
    assert all(row["status"] == "pass" for row in rows), rows


# --- 4 (продолжение). Узость основания видна при присвоенном классе --------


def test_defect_4_breadth_is_stated_next_to_the_class(db_conn) -> None:
    """Класс от стоп-фактора сопровождается фактом несформированной оценки.

    Иначе читатель видит класс E и не знает, что расчёт возможен по двум
    группам из пяти: противоречие устранено, но ценой потери сведений.
    """
    from finlib.report.summary import build_summary

    data = load_report_data(STOPPED_INN, db_conn)
    paragraphs = build_summary(data, SCORING)
    text = "\n".join(item.text for item in paragraphs)

    assert f"Класс финансового состояния: {data.class_code}" in paragraphs[0].text
    assert "Класс определён стоп-фактором" in paragraphs[1].text
    assert "Балльная оценка не формируется" in paragraphs[1].text
    assert "группам показателей из" in paragraphs[1].text
    # Сырой текст breadth_reason рядом с присвоенным классом читался бы
    # как противоречие: он написан для случая, когда класса нет.
    assert "интегральный класс не формируется" not in text


def test_defect_4_group_count_is_not_repeated(db_conn) -> None:
    """Счёт групп называется один раз, а не в двух абзацах подряд."""
    from finlib.report.summary import build_summary

    data = load_report_data(STOPPED_INN, db_conn)
    text = "\n".join(item.text for item in build_summary(data, SCORING))
    assert text.count("группам показателей из") == 1


def test_defect_4_count_matches_the_methodology(db_conn) -> None:
    """Числа в фразе взяты из разложения и методики, а не выдуманы."""
    from finlib.report.summary import build_summary

    data = load_report_data(STOPPED_INN, db_conn)
    used = len([item for item in data.groups if item["score"] is not None])
    text = "\n".join(item.text for item in build_summary(data, SCORING))
    assert f"по {used} группам показателей из {len(SCORING.groups)}" in text


# --- документ без модели не выглядит пустым ---------------------------------


def test_document_without_model_explains_itself(db_conn, tmp_path) -> None:
    """Документ без текстовой части объясняет своё содержимое сам."""
    from docx import Document

    from finlib.report.document import DISCLAIMER, DISCLAIMER_NO_TEXT, build_report

    report = build_report(
        NO_CLASS_INN, db_conn, directory=tmp_path, with_text=False
    )
    document = Document(report.path)
    text = "\n".join(item.text for item in document.paragraphs)

    # Дисклеймер контекстный: утверждать, что текст подготовлен моделью
    # и проверен, в документе без модели — ложь.
    assert DISCLAIMER_NO_TEXT in text
    assert DISCLAIMER not in text
    assert "не привлекалась" in text

    # Пустым он при этом не выглядит: раздел 1 содержателен, приложение полно.
    assert "Класс финансового состояния не присвоен" in text
    assert "группам показателей из" in text
    assert len(document.tables) >= 3


# --- шаблонные тексты: условие вывода ---------------------------------------


def test_document_title_matches_its_content(db_conn, tmp_path) -> None:
    """Документ без текстовой части называется справкой, а не заключением.

    Оговорка внутри прямо называет его расчётной справкой, и заголовок
    «Заключение» ей противоречил бы.
    """
    from docx import Document

    from finlib.report.document import TITLE, TITLE_NO_TEXT, build_report

    report = build_report(FULL_INN, db_conn, directory=tmp_path, with_text=False)
    text = "\n".join(item.text for item in Document(report.path).paragraphs)
    assert TITLE_NO_TEXT in text
    assert TITLE not in text


def test_footnote_does_not_point_at_a_missing_table(db_conn) -> None:
    """Сноска ссылается на следующую таблицу, только если та строится."""
    from finlib.report.appendix import metrics_table, not_calculated_table

    for inn in (FULL_INN, STOPPED_INN, NO_CLASS_INN):
        data = load_report_data(inn, db_conn)
        note = metrics_table(data).note or ""
        if not_calculated_table(data) is None:
            assert "Периоды, за которые" not in note, inn
        else:
            # Ссылка идёт по наименованию таблицы: её номер и место
            # в приложении ставит сборщик, соседство не гарантировано.
            assert "Периоды, за которые" in note, inn


def test_prompt_makes_group_scores_conditional() -> None:
    """Инструкция не велит ссылаться на баллы групп, которых может не быть."""
    from finlib.llm.service import load_prompt

    prompt = load_prompt()
    assert "Если баллы по группам" in prompt
    assert "не приводятся, не ссылайся" in prompt


def test_document_contains_no_machine_codes(db_conn) -> None:
    """Машинные коды не доходят до читателя ни из какого источника.

    Разметка модели снимается `llm/cleanup.py`, но коды просачивались и из
    нашей собственной детерминированной части: «Источник данных: gir_bo»
    и «сработали флаги: holding_structure».
    """
    from datetime import datetime

    from finlib.llm.cleanup import has_identifiers
    from finlib.report.appendix import provenance
    from finlib.report.summary import build_summary

    for inn in (FULL_INN, STOPPED_INN, NO_CLASS_INN):
        data = load_report_data(inn, db_conn)
        text = "\n".join(
            [
                *(item.text for item in build_summary(data, SCORING)),
                *provenance(data, "модель", datetime(2026, 1, 1)),
            ]
        )
        assert has_identifiers(text) == [], inn
        assert "gir_bo" not in text, inn
        assert "holding_structure" not in text, inn
