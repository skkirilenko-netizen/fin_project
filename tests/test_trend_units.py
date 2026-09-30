"""Тренд LTM: комплекты в разных единицах приводятся к единице последнего."""

from datetime import date
from decimal import Decimal

from finlib.metrics.interim import rolling_flow
from finlib.scoring.routing_store import trend_series


def test_values_are_brought_to_the_latest_unit() -> None:
    """Брусника: до 30.06.2024 тысячи, дальше миллионы — LTM складывался из разных единиц."""
    rows = [
        {"report_date": date(2024, 6, 30), "line_code": "ifrs.revenue",
         "value": Decimal(31359436), "unit_code": "384"},
        {"report_date": date(2024, 12, 31), "line_code": "ifrs.revenue",
         "value": Decimal(75832), "unit_code": "385"},
        {"report_date": date(2025, 6, 30), "line_code": "ifrs.revenue",
         "value": Decimal(35985), "unit_code": "385"},
    ]  # fmt: skip
    series = trend_series(rows, ["ifrs.revenue"])["ifrs.revenue"]
    assert series[date(2024, 6, 30)] == Decimal("31359.436")
    rolled = rolling_flow(series, date(2025, 6, 30))
    assert rolled.value == Decimal(75832) + Decimal(35985) - Decimal("31359.436")


def test_an_unknown_unit_leaves_no_value() -> None:
    """Единица не известна — величина ни с чем не сравнима, и её нет."""
    rows = [
        {"report_date": date(2025, 6, 30), "line_code": "ifrs.revenue",
         "value": Decimal(1), "unit_code": None},
        {"report_date": date(2025, 12, 31), "line_code": "ifrs.revenue",
         "value": Decimal(2), "unit_code": "385"},
    ]  # fmt: skip
    series = trend_series(rows, ["ifrs.revenue"])["ifrs.revenue"]
    assert series[date(2025, 6, 30)] is None
    assert series[date(2025, 12, 31)] == Decimal(2)
