"""Сверка агрегатора с документом: допуск единицей грубой стороны, знак, отсутствие."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.normalize.cbonds_loader import read_row
from finlib.quality.reconcile import Outcome, Side, aggregator_reading, compare, reconcile

DAY = date(2025, 12, 31)


def test_millions_and_thousands_match_within_one_coarse_unit() -> None:
    """«663 888 млн» и «663 887 912 тыс.» — одна величина с точностью до округления."""
    said = compare(
        "ifrs.total_assets", DAY, Side(Decimal(663888), "385"), Side(Decimal(663887912), "384")
    )
    assert said.outcome is Outcome.MATCH
    assert said.tolerance == Decimal(1000000)


def test_a_real_difference_is_not_hidden_by_the_tolerance() -> None:
    """Расхождение больше единицы грубой стороны — расхождение."""
    said = compare(
        "ifrs.inventories", DAY, Side(Decimal(171991), "385"), Side(Decimal(169574), "385")
    )
    assert said.outcome is Outcome.DIFFER


def test_the_sign_convention_is_told_apart() -> None:
    """Та же величина с обратным знаком — соглашение о знаке, а не другая величина."""
    said = compare(
        "ifrs.depreciation", DAY, Side(Decimal(-7332), "385"), Side(Decimal(7332), "385")
    )
    assert said.outcome is Outcome.SIGN


def test_absence_on_one_side_is_neither_match_nor_difference() -> None:
    """Нет с одной стороны — свой исход."""
    found = reconcile(
        {"ifrs.cash": Decimal(5)},
        "385",
        {"ifrs.revenue": Decimal(9)},
        "385",
        DAY,
        ("ifrs.cash", "ifrs.revenue"),
    )
    assert [item.outcome for item in found] == [Outcome.NO_AGGREGATOR, Outcome.NO_DOCUMENT]


def test_an_unknown_unit_refuses_instead_of_comparing_raw_numbers() -> None:
    """Без единицы сверять нечем: сырые числа сравнили бы тысячи с миллионами."""
    with pytest.raises(ValueError, match="единица"):
        compare("ifrs.cash", DAY, Side(Decimal(5), None), Side(Decimal(5), "385"))


def test_the_aggregator_row_is_read_the_way_the_loader_writes_it() -> None:
    """Строка из кэша читается путём загрузки; нет строки на дату — None."""
    row = {"emitent_inn": "1234567890", "date": "2025-12-31"}
    assert aggregator_reading([row], "1234567890", date(2024, 12, 31)) is None
    reading = aggregator_reading([row], "1234567890", DAY)
    assert reading is not None
    assert reading == read_row(row)
    assert reading.report_date == DAY
