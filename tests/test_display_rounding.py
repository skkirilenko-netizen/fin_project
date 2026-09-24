"""Тесты единой точки округления (задача 13).

Дефект: дельты считались по полной точности, уровни отображались
округлёнными. «Рентабельность активов снизилась с 0,41 до 0,31 (изменение
0,11)» — разность отображаемых уровней 0,10, а заявленный темп −25,6 %
из 0,41 и 0,31 не выводится, получается −24,4 %.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.definitions import Unit, load_metrics
from finlib.metrics.display import displayed, format_metric, money, ratio, round_to
from finlib.report.consistency import check_document
from finlib.report.data import MetricRow, ReportData, load_report_data
from finlib.standards import Standard

CATALOG = load_metrics()
NOW = date(2024, 12, 31)
BEFORE = date(2023, 12, 31)

ORGS = ("7736050003", "2100010824", "2522002003")


# --- разрядность объявлена в методике ---------------------------------------


def test_precision_is_declared_for_every_unit() -> None:
    """Разрядность задана для каждой единицы измерения и с происхождением."""
    display = CATALOG.display
    for unit in Unit:
        assert display.scale_for(unit) >= 0, unit
    assert display.origin.strip()


@pytest.mark.parametrize(
    ("code", "scale"),
    [("roa", 2), ("nwc", 0), ("receivables_days", 1), ("equity", 0)],
)
def test_metric_scale_follows_its_unit(code: str, scale: int) -> None:
    """Разрядность показателя берётся по его единице измерения."""
    assert CATALOG.scale_for(code) == scale


def test_precision_lives_in_methodology_not_in_code() -> None:
    """Разрядность можно поменять в YAML, не трогая код."""
    assert CATALOG.display.by_unit[Unit.RATIO] == 2
    assert CATALOG.display.by_unit[Unit.THOUSAND_RUB] == 0


# --- округление одно на всех ------------------------------------------------


def test_round_to_is_half_up() -> None:
    """Округление арифметическое, а не банковское."""
    assert round_to(Decimal("0.125"), 2) == Decimal("0.13")
    assert round_to(Decimal("0.135"), 2) == Decimal("0.14")
    assert displayed(None, 2) is None


def test_rendering_uses_the_declared_precision() -> None:
    """Текст и приложение печатают одну и ту же округлённую величину."""
    value = Decimal("0.3072838824")
    assert ratio(value) == "0,31"
    assert format_metric(value, Unit.RATIO) == "0,31"
    assert format_metric(value, Unit.RATIO, CATALOG.scale_for("roa")) == "0,31"


def test_nonzero_value_does_not_print_as_zero() -> None:
    """Ненулевая величина, округляющаяся в ноль, печатается словами.

    У Сегежи коэффициент автономии 0,002 при капитале 255 млн печатался как
    «0,00» — то есть как отсутствие собственных источников, которым он
    не является. Ноль здесь читается как утверждение, которого расчёт
    не делал.
    """
    assert format_metric(Decimal("0.002"), Unit.RATIO) == "менее 0,01"
    # **У отрицательной величины «более −0,01» вводило в заблуждение**
    # (поправка 22.09.2026): формально это верхняя граница, а читается
    # как «почти ноль, но сверху» — у эмитента с отрицательным капиталом
    # формулировка звучала утешительно.
    assert format_metric(Decimal("-0.002"), Unit.RATIO) == "≈0 (отриц., -0,01)"
    # Настоящий ноль остаётся нулём: он раскрыт и равен нулю, и слова здесь
    # означали бы обратное.
    assert format_metric(Decimal(0), Unit.RATIO) == "0,00"
    # Правило общее для всех единиц: сумма, округляющаяся в ноль, читается
    # как ноль так же ложно.
    assert format_metric(
        Decimal("0.4"), Unit.THOUSAND_RUB, money="тыс. руб."
    ).startswith("менее 1")


def test_money_keeps_group_separators() -> None:
    """Денежные величины остаются читаемыми."""
    assert money(Decimal("25736328136")).replace(" ", " ") == "25 736 328 136"


# --- дельты считаются от округлённых величин --------------------------------


def test_delta_equals_difference_of_displayed_levels(db_conn) -> None:
    """Заявленное изменение равно разности отображаемых уровней.

    Ровно тот случай из экспертной оценки: roa 0,41 → 0,31, изменение −0,10,
    а не −0,11.
    """
    data = load_report_data("2522002003", db_conn)
    roa = next(item for item in data.metrics if item.code == "roa")
    scale = CATALOG.scale_for("roa")
    levels = {period: round_to(value, scale) for period, value in roa.values.items() if value}

    change = next(
        row
        for row in data.derived
        if row["metric_code"] == "roa_chg_abs" and row["report_date"] == NOW
    )
    assert round_to(change["value"], scale) == levels[NOW] - levels[BEFORE]


def test_rate_is_derived_from_displayed_levels(db_conn) -> None:
    """Темп выводится из отображаемых уровней, а не из полных."""
    data = load_report_data("2522002003", db_conn)
    roa = next(item for item in data.metrics if item.code == "roa")
    scale = CATALOG.scale_for("roa")
    current = round_to(roa.values[NOW], scale)
    previous = round_to(roa.values[BEFORE], scale)

    rate = next(
        row
        for row in data.derived
        if row["metric_code"] == "roa_chg_pct" and row["report_date"] == NOW
    )
    expected = (current - previous) / previous * Decimal(100)
    assert round_to(rate["value"], 1) == round_to(expected, 1)


@pytest.mark.parametrize("inn", ORGS)
def test_no_delta_mismatch_on_real_data(inn: str, db_conn) -> None:
    """На всех пробах равенство выполняется."""
    data = load_report_data(inn, db_conn)
    assert data.derived, "производные величины должны быть рассчитаны"
    problems = [
        item.code
        for item in check_document(data, "")
        if item.code == "delta_does_not_match_levels"
    ]
    assert problems == []


# --- контроль срабатывает при нарушении -------------------------------------


def test_control_catches_a_broken_delta() -> None:
    """Контроль ловит расхождение, а не просто молчит на исправных данных."""
    metric = MetricRow(
        code="roa",
        name="Рентабельность активов",
        unit="ratio",
        group_name="Рентабельность",
        values={NOW: Decimal("0.3072838824"), BEFORE: Decimal("0.4130226508")},
        reasons={},
        reason_codes={},
        included=True,
        score=None,
        level_score=None,
        dynamics_score=None,
        periods_used=2,
        exclusion_reason=None,
        exclusion_kind=None,
    )
    data = ReportData(
        inn="1",
        report_date=NOW,
        standard=Standard.RSBU,
        organization={},
        unit_name="тыс. руб.",
        assessment=None,
        metrics=[metric],
        # Прежнее поведение: дельта по полной точности даёт -0,11 при разности
        # отображаемых уровней -0,10.
        derived=[
            {
                "metric_code": "roa_chg_abs",
                "report_date": NOW,
                "value": Decimal("-0.1057387684"),
                "status": "ok",
            }
        ],
    )
    problems = [item for item in check_document(data, "")]
    assert any(item.code == "delta_does_not_match_levels" for item in problems)
    assert "не равно" in next(
        item.message for item in problems if item.code == "delta_does_not_match_levels"
    )


def test_control_passes_a_consistent_delta() -> None:
    """Согласованная дельта нарушением не считается."""
    metric = MetricRow(
        code="roa",
        name="Рентабельность активов",
        unit="ratio",
        group_name="Рентабельность",
        values={NOW: Decimal("0.31"), BEFORE: Decimal("0.41")},
        reasons={},
        reason_codes={},
        included=True,
        score=None,
        level_score=None,
        dynamics_score=None,
        periods_used=2,
        exclusion_reason=None,
        exclusion_kind=None,
    )
    data = ReportData(
        inn="1",
        report_date=NOW,
        standard=Standard.RSBU,
        organization={},
        unit_name="тыс. руб.",
        assessment=None,
        metrics=[metric],
        derived=[
            {
                "metric_code": "roa_chg_abs",
                "report_date": NOW,
                "value": Decimal("-0.10"),
                "status": "ok",
            }
        ],
    )
    assert not [
        item for item in check_document(data, "")
        if item.code == "delta_does_not_match_levels"
    ]
