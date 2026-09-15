"""Тесты производных величин: изменения за период и доли в валюте баланса.

Расчёт синтетический, без БД: проверяется арифметика и отказы, а не выборка.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.definitions import Unit, load_metrics
from finlib.metrics.derived import (
    DerivedKind,
    compute_derived,
    describe,
    parse,
    unit_of,
)
from finlib.metrics.engine import MetricResult, MetricStatus, PeriodValues
from finlib.normalize.lines import ReportingType, load_lines
from finlib.quality.periods import PeriodConfidence

NOW = date(2025, 12, 31)
BEFORE = date(2024, 12, 31)
EARLIER = date(2023, 12, 31)

CATALOG = load_metrics()
LINES = load_lines()


def values(**codes: str) -> dict[str, Decimal | None]:
    """Значения строк одного периода."""
    return {code: Decimal(value) for code, value in codes.items()}


def run(
    periods: dict[date, dict[str, Decimal | None]],
    metrics: list[MetricResult] | None = None,
) -> dict[str, MetricResult]:
    """Считает производные и раскладывает по коду за последний период."""
    ordered = sorted(periods, reverse=True)
    results = compute_derived(
        {d: PeriodValues(d, periods[d]) for d in periods},
        metrics or [],
        ordered,
        dict.fromkeys(ordered, PeriodConfidence.VERIFIED),
        CATALOG,
    )
    return {item.metric_code: item for item in results if item.report_date == ordered[0]}


def test_absolute_change_is_difference() -> None:
    """Абсолютное изменение — разность двух периодов, без округления."""
    got = run({NOW: values(**{"1230": "400"}), BEFORE: values(**{"1230": "1000"})})
    assert got["1230_chg_abs"].value == Decimal(-600)
    assert got["1230_chg_abs"].is_ok


def test_percent_change_matches_manual_arithmetic() -> None:
    """Процентное изменение считается от значения на начало периода."""
    got = run({NOW: values(**{"1230": "400"}), BEFORE: values(**{"1230": "1000"})})
    assert got["1230_chg_pct"].value == Decimal(-60)


def test_share_is_taken_to_balance_total() -> None:
    """Доля строки считается к валюте баланса, строке 1600."""
    got = run({NOW: values(**{"1230": "250", "1600": "1000"})})
    assert got["1230_share"].value == Decimal(25)


def test_balance_total_has_no_share_of_itself() -> None:
    """Доля валюты баланса в себе самой равна 100 и в контекст не идёт."""
    got = run({NOW: values(**{"1600": "1000"})})
    assert "1600_share" not in got


def test_percent_change_refused_on_negative_base() -> None:
    """Процент от отрицательной базы не считается: «рост на 55 %» вводит в заблуждение."""
    got = run({NOW: values(**{"4200": "-200"}), BEFORE: values(**{"4200": "-442"})})
    assert got["4200_chg_abs"].value == Decimal(242)
    refused = got["4200_chg_pct"]
    assert refused.status is MetricStatus.NOT_CALCULABLE
    assert refused.value is None
    assert refused.reason_code == "negative_denominator"


def test_percent_change_refused_on_zero_base() -> None:
    """Деления на ноль нет и здесь."""
    got = run({NOW: values(**{"4200": "500"}), BEFORE: values(**{"4200": "0"})})
    assert got["4200_chg_pct"].reason_code == "zero_denominator"


def test_percent_change_refused_on_sign_change() -> None:
    """Переход в минус процентом не описывается: «−150 %» читается как невозможное."""
    got = run({NOW: values(**{"2400": "-50"}), BEFORE: values(**{"2400": "100"})})
    assert got["2400_chg_abs"].value == Decimal(-150)
    assert got["2400_chg_pct"].reason_code == "sign_change"


def test_missing_period_yields_no_rows_at_all() -> None:
    """Без предыдущего периода изменение не пишется вовсе, даже как not_calculable.

    Иначе за самый ранний период в блок ушли бы десятки строк «нет
    предыдущего периода» и содержательный отказ утонул бы среди них.
    """
    got = run({NOW: values(**{"1230": "400", "1600": "1000"})})
    assert "1230_chg_abs" not in got
    assert "1230_chg_pct" not in got
    assert got["1230_share"].is_ok


def test_undisclosed_line_yields_no_rows() -> None:
    """Нераскрытая строка производных не даёт: нуля вместо неё не подставляется."""
    got = run({NOW: {"1230": None, "1600": Decimal(1000)}, BEFORE: values(**{"1230": "10"})})
    assert "1230_chg_abs" not in got
    assert "1230_share" not in got


def test_share_refused_when_total_is_not_positive() -> None:
    """Доля к нулевой валюте баланса не считается."""
    got = run({NOW: values(**{"1230": "250", "1600": "0"})})
    assert got["1230_share"].reason_code == "zero_denominator"


def test_metric_changes_are_computed_too() -> None:
    """Изменение считается и для показателей, не только для строк."""
    metrics = [
        MetricResult("cur_liq", NOW, Decimal("0.80"), MetricStatus.OK, PeriodConfidence.VERIFIED),
        MetricResult(
            "cur_liq", BEFORE, Decimal("1.00"), MetricStatus.OK, PeriodConfidence.VERIFIED
        ),
    ]
    got = run({NOW: {}, BEFORE: {}}, metrics)
    assert got["cur_liq_chg_abs"].value == Decimal("-0.20")
    assert got["cur_liq_chg_pct"].value == Decimal(-20)


def test_not_calculable_metric_gives_no_change() -> None:
    """Нерассчитанный показатель изменения не порождает."""
    metrics = [
        MetricResult("cur_liq", NOW, None, MetricStatus.NOT_CALCULABLE, PeriodConfidence.VERIFIED),
        MetricResult(
            "cur_liq", BEFORE, Decimal("1.00"), MetricStatus.OK, PeriodConfidence.VERIFIED
        ),
    ]
    got = run({NOW: {}, BEFORE: {}}, metrics)
    assert "cur_liq_chg_abs" not in got


def test_confidence_is_the_worse_of_two_periods() -> None:
    """Изменение опирается на два периода, доверие к нему — по худшему из них."""
    ordered = [NOW, BEFORE]
    results = compute_derived(
        {
            NOW: PeriodValues(NOW, values(**{"1230": "400"})),
            BEFORE: PeriodValues(BEFORE, values(**{"1230": "1000"})),
        },
        [],
        ordered,
        {NOW: PeriodConfidence.VERIFIED, BEFORE: PeriodConfidence.COMPARATIVE_ONLY},
        CATALOG,
    )
    change = next(item for item in results if item.metric_code == "1230_chg_abs")
    assert change.confidence is PeriodConfidence.COMPARATIVE_ONLY


def test_change_base_is_the_neighbouring_period() -> None:
    """База изменения — ближайший предыдущий период, а не самый ранний."""
    periods = {
        NOW: values(**{"1230": "400"}),
        BEFORE: values(**{"1230": "1000"}),
        EARLIER: values(**{"1230": "800"}),
    }
    ordered = sorted(periods, reverse=True)
    results = compute_derived(
        {d: PeriodValues(d, periods[d]) for d in periods},
        [],
        ordered,
        dict.fromkeys(ordered, PeriodConfidence.VERIFIED),
        CATALOG,
    )
    by_date = {
        item.report_date: item for item in results if item.metric_code == "1230_chg_abs"
    }
    assert by_date[NOW].value == Decimal(-600)
    assert by_date[BEFORE].value == Decimal(200)
    assert EARLIER not in by_date


@pytest.mark.parametrize(
    ("code", "base", "kind"),
    [
        ("1230_chg_abs", "1230", DerivedKind.CHANGE_ABS),
        ("1230_chg_pct", "1230", DerivedKind.CHANGE_PCT),
        ("1230_share", "1230", DerivedKind.SHARE),
        ("cur_liq_chg_abs", "cur_liq", DerivedKind.CHANGE_ABS),
        ("receivables_days_chg_pct", "receivables_days", DerivedKind.CHANGE_PCT),
    ],
)
def test_code_is_parsed_back_to_base_and_kind(code: str, base: str, kind: DerivedKind) -> None:
    """Код производной разбирается на базу и вид однозначно."""
    parsed = parse(code)
    assert parsed is not None
    assert (parsed.base, parsed.kind) == (base, kind)
    assert parsed.code == code


@pytest.mark.parametrize("code", ["cur_liq", "equity_ratio", "1230", "roa"])
def test_ordinary_code_is_not_derived(code: str) -> None:
    """Обычный код показателя или строки производной не считается."""
    assert parse(code) is None


def test_catalog_forbids_metric_code_colliding_with_derived() -> None:
    """Показатель с кодом производной справочник не принимает."""
    assert all(
        not metric.code.endswith(("_chg_abs", "_chg_pct", "_share"))
        for metric in CATALOG.metrics
    )


def test_units_follow_the_base() -> None:
    """Проценты — проценты, абсолютное изменение — в единицах базы."""
    assert unit_of(parse("1230_chg_pct"), None) is Unit.PERCENT
    assert unit_of(parse("1230_share"), None) is Unit.PERCENT
    assert unit_of(parse("1230_chg_abs"), None) is Unit.THOUSAND_RUB
    days_metric = CATALOG.require("receivables_days")
    assert unit_of(parse("receivables_days_chg_abs"), days_metric) is Unit.DAYS


def test_name_carries_base_and_its_code() -> None:
    """Наименование производной называет базу и её код — иначе якорь не построить."""
    name = describe(parse("1230_chg_pct"), LINES, CATALOG, ReportingType.FULL)
    assert name is not None
    assert "Дебиторская задолженность" in name
    assert "(1230)" in name
    assert "процент" in name.lower()


def test_unknown_base_has_no_name() -> None:
    """Производная от неизвестной базы в блок не попадает."""
    assert describe(parse("9999_share"), LINES, CATALOG, ReportingType.FULL) is None


def test_declared_lines_exist_in_the_catalog() -> None:
    """Каждая строка из derived есть в справочнике строк хотя бы одного набора."""
    spec = CATALOG.derived
    checked = 0
    for code in set(spec.change.lines) | set(spec.share.lines) | {spec.share.denominator}:
        assert any(LINES.has(code, item) for item in ReportingType), code
        checked += 1
    assert checked >= 25


def test_share_is_declared_only_for_balance_lines() -> None:
    """Доля в валюте баланса объявлена только для строк баланса."""
    assert CATALOG.derived.share.lines
    for code in CATALOG.derived.share.lines:
        assert code.startswith("1"), f"{code} — не строка баланса"
