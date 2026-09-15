"""Тесты записи показателей и чтения рядов. Идут в findb с откатом."""

from datetime import date
from decimal import Decimal

import pytest
from probes import CORRECTED_BFO, FULL_BFO, read_probe

from finlib.db import execute, fetch_all, fetch_one
from finlib.metrics.engine import MetricStatus, compute_all
from finlib.metrics.store import MetricSeries, SeriesPoint, load_series, save_results
from finlib.normalize.loader import load_report_set
from finlib.quality.periods import PeriodConfidence
from finlib.quality.runner import run_checks
from finlib.sources.girbo import Organization, parse_report_sets
from finlib.utils import json_loads_decimal

FULL_INN = "7736050003"
CORRECTED_INN = "2522002003"
ALL_INNS = [FULL_INN, CORRECTED_INN]


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute("DELETE FROM organization WHERE inn = ANY(%(i)s)", {"i": ALL_INNS}, conn=db_conn)
    execute("DELETE FROM dq_log WHERE inn = ANY(%(i)s)", {"i": ALL_INNS}, conn=db_conn)
    execute("DELETE FROM metric_value WHERE inn = ANY(%(i)s)", {"i": ALL_INNS}, conn=db_conn)
    return db_conn


def prepare(probe, inn: str, conn, years: list[int] | None = None) -> None:
    """Загружает комплекты и прогоняет контроли."""
    sets = parse_report_sets(json_loads_decimal(read_probe(probe)), inn)
    org = Organization(inn=inn, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ")
    for item in sorted(sets, key=lambda r: r.report_year):
        if years is not None and item.report_year not in years:
            continue
        run_checks(load_report_set(item, org, conn).src_file_id, conn)


def test_results_are_saved(db_conn) -> None:
    """Показатели записываются в metric_value."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    results = compute_all(FULL_INN, db_conn)
    assert save_results(FULL_INN, results, db_conn) == len(results)

    row = fetch_one(
        "SELECT value, status, confidence, methodology_version FROM metric_value "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND metric_code = 'cur_liq'",
        {"inn": FULL_INN, "d": date(2025, 12, 31)},
        conn=db_conn,
    )
    assert row is not None
    assert row["status"] == "ok"
    assert row["confidence"] == "verified"
    assert row["methodology_version"]
    assert row["value"].quantize(Decimal("0.0001")) == Decimal("0.8213")


def test_recalculation_is_idempotent(db_conn) -> None:
    """Повторный расчёт не создаёт дублей."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    results = compute_all(FULL_INN, db_conn)
    save_results(FULL_INN, results, db_conn)
    before = fetch_one(
        "SELECT count(*) AS n FROM metric_value WHERE inn = %(i)s", {"i": FULL_INN}, conn=db_conn
    )
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)
    after = fetch_one(
        "SELECT count(*) AS n FROM metric_value WHERE inn = %(i)s", {"i": FULL_INN}, conn=db_conn
    )
    assert before is not None and after is not None
    assert before["n"] == after["n"] > 0


def test_not_calculable_is_stored_with_reason(db_conn) -> None:
    """Нерасчётный показатель хранится со статусом и причиной, а не отбрасывается."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)

    rows = fetch_all(
        "SELECT metric_code, reason, reason_code FROM metric_value "
        "WHERE inn = %(i)s AND status = 'not_calculable'",
        {"i": FULL_INN},
        conn=db_conn,
    )
    assert rows, "нерасчётных показателей не нашлось"
    assert all(row["reason"] for row in rows)
    assert all(row["reason_code"] for row in rows)
    assert all(row["value"] is None for row in fetch_all(
        "SELECT value FROM metric_value WHERE inn = %(i)s AND status = 'not_calculable'",
        {"i": FULL_INN},
        conn=db_conn,
    ))


def test_earliest_period_has_no_averages(db_conn) -> None:
    """За самый ранний период показатели по средним величинам не считаются."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    results = compute_all(FULL_INN, db_conn)
    earliest = min(item.report_date for item in results)

    by_code = {item.metric_code: item for item in results if item.report_date == earliest}
    for code in ("roa", "roe", "asset_turnover", "receivables_days", "inventory_days"):
        assert by_code[code].status is MetricStatus.NOT_CALCULABLE, code
        assert by_code[code].reason_code in {"no_previous_period", "missing_lines"}


def test_comparative_period_is_marked(db_conn) -> None:
    """Показатели по непроверенному периоду помечаются пониженным доверием."""
    prepare(FULL_BFO, FULL_INN, db_conn, years=[2025])
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)

    row = fetch_one(
        "SELECT confidence FROM metric_value WHERE inn = %(i)s AND report_date = %(d)s "
        "AND metric_code = 'cur_liq'",
        {"i": FULL_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert row is not None
    assert row["confidence"] == "comparative_only"


def test_quarantined_period_is_not_calculated(db_conn) -> None:
    """По периоду в карантине показатели не считаются вовсе."""
    prepare(CORRECTED_BFO, CORRECTED_INN, db_conn, years=[2025])
    results = compute_all(CORRECTED_INN, db_conn)
    assert all(item.report_date != date(2025, 12, 31) for item in results)


def test_series_exposes_confidence(db_conn) -> None:
    """Ряд показателя несёт доверие каждой точки."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)

    series = load_series(FULL_INN, "cur_liq", db_conn)
    assert series.metric_code == "cur_liq"
    assert len(series.calculated) >= 2
    assert series.is_trend_possible
    assert {point.confidence for point in series.points} <= set(PeriodConfidence)


def test_series_warns_about_unverified_trend(db_conn) -> None:
    """Динамика по непроверенным периодам сопровождается оговоркой."""
    prepare(FULL_BFO, FULL_INN, db_conn, years=[2025])
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)

    series = load_series(FULL_INN, "cur_liq", db_conn)
    assert series.has_unverified_points
    note = series.trend_note()
    assert note is not None
    assert "не проверенные" in note


def test_series_reports_impossible_trend() -> None:
    """Меньше двух рассчитанных точек — динамику оценить нельзя, и это сказано прямо.

    Проверка безусловная: ряд строится здесь же, а не берётся из базы,
    где число точек зависит от загруженных комплектов.
    """
    single = MetricSeries(
        metric_code="roa",
        points=(
            SeriesPoint(
                report_date=date(2025, 12, 31),
                value=Decimal("0.05"),
                status=MetricStatus.OK,
                confidence=PeriodConfidence.VERIFIED,
            ),
            SeriesPoint(
                report_date=date(2024, 12, 31),
                value=None,
                status=MetricStatus.NOT_CALCULABLE,
                confidence=PeriodConfidence.VERIFIED,
            ),
        ),
    )
    assert not single.is_trend_possible
    assert "динамику оценить нельзя" in (single.trend_note() or "")


def test_series_with_two_points_allows_trend() -> None:
    """Две рассчитанные точки — динамика оценима, оговорки о её отсутствии нет."""
    pair = MetricSeries(
        metric_code="roa",
        points=tuple(
            SeriesPoint(
                report_date=date(year, 12, 31),
                value=Decimal("0.05"),
                status=MetricStatus.OK,
                confidence=PeriodConfidence.VERIFIED,
            )
            for year in (2025, 2024)
        ),
    )
    assert pair.is_trend_possible
    assert pair.trend_note() is None


def test_values_stay_decimal(db_conn) -> None:
    """Значения в БД остаются Decimal."""
    prepare(FULL_BFO, FULL_INN, db_conn)
    save_results(FULL_INN, compute_all(FULL_INN, db_conn), db_conn)
    row = fetch_one(
        "SELECT value FROM metric_value WHERE inn = %(i)s AND metric_code = 'equity' "
        "AND report_date = %(d)s",
        {"i": FULL_INN, "d": date(2025, 12, 31)},
        conn=db_conn,
    )
    assert row is not None
    assert isinstance(row["value"], Decimal)
    assert row["value"] == Decimal("16432222886")
