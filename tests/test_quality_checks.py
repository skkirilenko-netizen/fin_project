"""Тесты контролей качества: на каждый — случай прохождения и случай провала."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.normalize.lines import ReportingType, load_lines
from finlib.quality.checks import (
    balance_equality,
    jump_detection,
    mandatory_fields,
    period_revised,
    profit_chain,
    retained_earnings_link,
    section_sum,
)
from finlib.quality.codes import CheckStatus, Severity
from finlib.quality.context import LineValue, PeriodFacts, ReportContext
from finlib.quality.thresholds import load_thresholds
from finlib.standards import Standard

BALANCE = "0710001"
PROFIT = "0710002"
CURRENT = date(2024, 12, 31)
PREVIOUS = date(2023, 12, 31)


def facts(report_date: date, values: dict[tuple[str, str], Decimal | None]) -> PeriodFacts:
    """Факты периода из простого словаря."""
    role = "current" if report_date == CURRENT else "previous"
    return PeriodFacts(
        report_date=report_date,
        values={
            key: LineValue(
                value=value,
                value_status="ok" if value is not None else "not_disclosed",
                source_line_code=key[1],
                period_role=role,
            )
            for key, value in values.items()
        },
    )


def context(
    *periods: PeriodFacts,
    reporting_type: ReportingType = ReportingType.FULL,
    revisions: dict | None = None,
) -> ReportContext:
    """Контекст комплекта поверх заданных периодов."""
    by_date = {item.report_date: item for item in periods}
    return ReportContext(
        src_file_id=1,
        inn="7736050003",
        report_year=CURRENT.year,
        reporting_type=reporting_type,
        standard=Standard.RSBU,
        unit_code="384",
        unit_source="assumed",
        status="loaded",
        correction_version=0,
        periods=by_date,
        revisions=revisions or {},
        known_periods=tuple(sorted(by_date, reverse=True)),
        catalog=load_lines(),
        thresholds=load_thresholds(),
    )


def statuses(outcomes) -> list[CheckStatus]:
    """Статусы результатов."""
    return [item.status for item in outcomes]


# --- balance_equality -------------------------------------------------------


def test_balance_equality_passes() -> None:
    """Актив равен пассиву — контроль пройден."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1600"): Decimal(500), (BALANCE, "1700"): Decimal(500)})
    )
    outcomes = list(balance_equality(ctx))
    assert statuses(outcomes) == [CheckStatus.PASS]
    assert outcomes[0].severity is Severity.BLOCKING


def test_balance_equality_fails() -> None:
    """Расхождение больше допуска — блокирующий провал."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1600"): Decimal(500), (BALANCE, "1700"): Decimal(503)})
    )
    outcomes = list(balance_equality(ctx))
    assert statuses(outcomes) == [CheckStatus.FAIL]
    assert outcomes[0].is_blocking_failure
    assert outcomes[0].details["difference"] == "-3"


def test_balance_equality_within_rounding() -> None:
    """Расхождение в пределах допуска на округление провалом не считается."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1600"): Decimal(500), (BALANCE, "1700"): Decimal(501)})
    )
    assert statuses(list(balance_equality(ctx))) == [CheckStatus.PASS]


def test_balance_equality_skipped_when_not_disclosed() -> None:
    """Нераскрытый итог баланса — контроль не выполнялся, а не провален."""
    ctx = context(facts(CURRENT, {(BALANCE, "1600"): Decimal(500), (BALANCE, "1700"): None}))
    outcomes = list(balance_equality(ctx))
    assert statuses(outcomes) == [CheckStatus.INFO]
    assert outcomes[0].severity is Severity.INFO


def test_comparative_period_failure_does_not_block() -> None:
    """Ошибка в сравнительной колонке не отправляет комплект в карантин."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1600"): Decimal(500), (BALANCE, "1700"): Decimal(500)}),
        facts(PREVIOUS, {(BALANCE, "1600"): Decimal(400), (BALANCE, "1700"): Decimal(450)}),
    )
    outcomes = {item.report_date: item for item in balance_equality(ctx)}
    assert outcomes[PREVIOUS].status is CheckStatus.FAIL
    assert outcomes[PREVIOUS].severity is Severity.WARNING
    assert not outcomes[PREVIOUS].is_blocking_failure


# --- section_sum ------------------------------------------------------------


def test_section_sum_passes() -> None:
    """Итог раздела равен сумме слагаемых."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1210"): Decimal(100),
                (BALANCE, "1250"): Decimal(50),
                (BALANCE, "1200"): Decimal(150),
            },
        )
    )
    results = [item for item in section_sum(ctx) if item.line_code == "1200"]
    assert statuses(results) == [CheckStatus.PASS]


def test_section_sum_fails() -> None:
    """Сумма слагаемых не сходится с итогом — блокирующий провал."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1210"): Decimal(100),
                (BALANCE, "1250"): Decimal(50),
                (BALANCE, "1200"): Decimal(200),
            },
        )
    )
    results = [item for item in section_sum(ctx) if item.line_code == "1200"]
    assert results[0].status is CheckStatus.FAIL
    assert results[0].is_blocking_failure


def test_section_sum_treats_undisclosed_as_zero() -> None:
    """Нераскрытое слагаемое считается нулём — иначе упрощённые формы не проверить."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1210"): Decimal(100),
                (BALANCE, "1220"): None,
                (BALANCE, "1250"): Decimal(50),
                (BALANCE, "1200"): Decimal(150),
            },
        )
    )
    results = [item for item in section_sum(ctx) if item.line_code == "1200"]
    assert results[0].status is CheckStatus.PASS
    assert "1220" in results[0].details["undisclosed_components"]


def test_section_sum_skipped_without_components() -> None:
    """Раскрыт только итог — проверять нечего."""
    ctx = context(facts(CURRENT, {(BALANCE, "1200"): Decimal(150)}))
    results = [item for item in section_sum(ctx) if item.line_code == "1200"]
    assert results[0].status is CheckStatus.INFO
    assert results[0].severity is Severity.INFO


def test_section_sum_not_verifiable_on_unloaded_line() -> None:
    """Незагруженная из-за неоднозначности строка даёт «не проверяемо», а не провал."""
    period = facts(
        CURRENT,
        {
            (BALANCE, "1150"): Decimal(100),
            (BALANCE, "1100"): Decimal(300),
        },
    )
    period.unverifiable[(BALANCE, "1170")] = "код 1190 допускают строки 1150, 1170"
    ctx = context(period)
    results = [item for item in section_sum(ctx) if item.line_code == "1100"]
    assert results[0].status is CheckStatus.INFO
    assert results[0].severity is Severity.WARNING
    assert not results[0].is_blocking_failure
    assert "не загружены" in results[0].message


# --- profit_chain -----------------------------------------------------------


def test_profit_chain_passes() -> None:
    """Цепочка прибыли сходится."""
    ctx = context(
        facts(
            CURRENT,
            {
                (PROFIT, "2110"): Decimal(1000),
                (PROFIT, "2120"): Decimal(600),
                (PROFIT, "2100"): Decimal(400),
                (PROFIT, "2210"): Decimal(100),
                (PROFIT, "2220"): Decimal(50),
                (PROFIT, "2200"): Decimal(250),
            },
        )
    )
    assert set(statuses(list(profit_chain(ctx)))) == {CheckStatus.PASS}


def test_profit_chain_fails() -> None:
    """Валовая прибыль не равна выручке за вычетом себестоимости."""
    ctx = context(
        facts(
            CURRENT,
            {
                (PROFIT, "2110"): Decimal(1000),
                (PROFIT, "2120"): Decimal(600),
                (PROFIT, "2100"): Decimal(500),
            },
        )
    )
    results = [item for item in profit_chain(ctx) if item.line_code == "2100"]
    assert results[0].status is CheckStatus.FAIL
    assert results[0].is_blocking_failure


def test_profit_chain_simplified_formula() -> None:
    """В упрощённом наборе проверяется формула чистой прибыли."""
    ctx = context(
        facts(
            CURRENT,
            {
                (PROFIT, "2110"): Decimal(44771),
                (PROFIT, "2120"): Decimal(1082),
                (PROFIT, "2340"): Decimal(54),
                (PROFIT, "2350"): Decimal(201),
                (PROFIT, "2410"): Decimal(2703),
                (PROFIT, "2400"): Decimal(40839),
            },
        ),
        reporting_type=ReportingType.SIMPLIFIED,
    )
    results = list(profit_chain(ctx))
    assert [item.line_code for item in results] == ["2400"]
    assert results[0].status is CheckStatus.PASS


# --- mandatory_fields -------------------------------------------------------


def test_mandatory_fields_pass() -> None:
    """Все обязательные строки заполнены."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1600"): Decimal(1),
                (BALANCE, "1700"): Decimal(1),
                (BALANCE, "1300"): Decimal(1),
                (PROFIT, "2110"): Decimal(1),
            },
        )
    )
    assert set(statuses(list(mandatory_fields(ctx)))) == {CheckStatus.PASS}


def test_mandatory_fields_fail() -> None:
    """Нераскрытая обязательная строка — блокирующий провал."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1600"): Decimal(1),
                (BALANCE, "1700"): Decimal(1),
                (BALANCE, "1300"): None,
                (PROFIT, "2110"): Decimal(1),
            },
        )
    )
    failed = [item for item in mandatory_fields(ctx) if item.status is CheckStatus.FAIL]
    assert [item.line_code for item in failed] == ["1300"]
    assert failed[0].is_blocking_failure


def test_mandatory_fields_ignore_comparative_periods() -> None:
    """Неполнота сравнительной колонки не проверяется: за неё отвечает свой комплект."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1600"): Decimal(1),
                (BALANCE, "1700"): Decimal(1),
                (BALANCE, "1300"): Decimal(1),
                (PROFIT, "2110"): Decimal(1),
            },
        ),
        facts(PREVIOUS, {(BALANCE, "1600"): Decimal(1), (BALANCE, "1300"): None}),
    )
    assert {item.report_date for item in mandatory_fields(ctx)} == {CURRENT}


def test_mandatory_fields_skip_absent_form() -> None:
    """Если формы за период нет, её строки не требуются."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1600"): Decimal(1),
                (BALANCE, "1700"): Decimal(1),
                (BALANCE, "1300"): Decimal(1),
            },
        )
    )
    assert "2110" not in {item.line_code for item in mandatory_fields(ctx)}


# --- period_revised ---------------------------------------------------------


def test_period_revised_passes() -> None:
    """Расхождений с более поздней отчётностью нет."""
    ctx = context(facts(CURRENT, {(BALANCE, "1600"): Decimal(1)}))
    results = [item for item in period_revised(ctx) if item.report_date == CURRENT]
    assert results[0].status is CheckStatus.PASS


def test_period_revised_warns() -> None:
    """Пересмотр отчётности прошлых периодов — предупреждение со списком строк."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1600"): Decimal(1)}),
        facts(PREVIOUS, {(BALANCE, "1600"): Decimal(1)}),
        revisions={
            (PREVIOUS, BALANCE, "1600"): (Decimal(100), Decimal(200)),
            (PREVIOUS, BALANCE, "1370"): (Decimal(10), Decimal(20)),
        },
    )
    warned = [item for item in period_revised(ctx) if item.status is CheckStatus.WARNING]
    assert warned[0].report_date == PREVIOUS
    assert warned[0].details["count"] == 2
    assert warned[0].details["lines"] == ["1370", "1600"]


def test_period_revised_reports_missing_previous() -> None:
    """Отсутствие предыдущего периода отмечается: средние величины не посчитать."""
    ctx = context(facts(CURRENT, {(BALANCE, "1600"): Decimal(1)}))
    notes = [item for item in period_revised(ctx) if item.status is CheckStatus.INFO]
    assert notes and "средние балансовые величины" in notes[0].message
    assert notes[0].severity is Severity.WARNING


# --- jump_detection ---------------------------------------------------------


def test_jump_detection_warns() -> None:
    """Рост более чем в пять раз — предупреждение, не провал."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1210"): Decimal(60000)}),
        facts(PREVIOUS, {(BALANCE, "1210"): Decimal(10000)}),
    )
    results = list(jump_detection(ctx))
    assert statuses(results) == [CheckStatus.WARNING]
    assert results[0].severity is Severity.WARNING
    assert not results[0].is_blocking_failure


def test_jump_detection_silent_within_factor() -> None:
    """Изменение в пределах порога не сигналит."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1210"): Decimal(40000)}),
        facts(PREVIOUS, {(BALANCE, "1210"): Decimal(10000)}),
    )
    assert list(jump_detection(ctx)) == []


def test_jump_detection_ignores_small_base() -> None:
    """Скачок на микровеличине не сигналит: содержательно он ничего не значит."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1210"): Decimal(500)}),
        facts(PREVIOUS, {(BALANCE, "1210"): Decimal(10)}),
    )
    assert list(jump_detection(ctx)) == []


# --- retained_earnings_link -------------------------------------------------


def test_retained_earnings_link_passes() -> None:
    """Прирост нераспределённой прибыли объясняется чистой прибылью."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1370"): Decimal(1500),
                (BALANCE, "1600"): Decimal(10000),
                (PROFIT, "2400"): Decimal(500),
            },
        ),
        facts(PREVIOUS, {(BALANCE, "1370"): Decimal(1000)}),
    )
    results = list(retained_earnings_link(ctx))
    assert results[0].status is CheckStatus.PASS


def test_retained_earnings_link_warns() -> None:
    """Крупное необъяснённое расхождение — предупреждение с суммой."""
    ctx = context(
        facts(
            CURRENT,
            {
                (BALANCE, "1370"): Decimal(1500),
                (BALANCE, "1600"): Decimal(10000),
                (PROFIT, "2400"): Decimal(5000),
            },
        ),
        facts(PREVIOUS, {(BALANCE, "1370"): Decimal(1000)}),
    )
    results = list(retained_earnings_link(ctx))
    assert results[0].status is CheckStatus.WARNING
    assert results[0].details["unexplained"] == "-4500"
    assert not results[0].is_blocking_failure


def test_retained_earnings_link_skipped_without_data() -> None:
    """Без чистой прибыли или прошлого капитала контроль не выполняется."""
    ctx = context(
        facts(CURRENT, {(BALANCE, "1370"): Decimal(1500), (PROFIT, "2400"): None}),
        facts(PREVIOUS, {(BALANCE, "1370"): Decimal(1000)}),
    )
    results = list(retained_earnings_link(ctx))
    assert results[0].status is CheckStatus.INFO
    assert results[0].severity is Severity.INFO


@pytest.mark.parametrize("reporting_type", list(ReportingType))
def test_every_check_has_declared_severity(reporting_type: ReportingType) -> None:
    """У каждого контроля задан уровень в методике, умолчаний в коде нет."""
    thresholds = load_thresholds()
    for code in (
        "balance_equality",
        "section_sum",
        "profit_chain",
        "mandatory_fields",
        "period_revised",
        "jump_detection",
        "retained_earnings_link",
    ):
        assert thresholds.severity_of(code)
    assert thresholds.mandatory_for(reporting_type)
