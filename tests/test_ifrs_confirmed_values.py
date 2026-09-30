"""Подтверждённые строки одного кода складываются, одна строка — один раз."""

from datetime import date
from decimal import Decimal

from finlib.normalize.ifrs_loader import confirmed_values
from finlib.sources.ifrs_confirmed import ConfirmedFact

FORM = "ifrs.statement_of_profit_or_loss"
DATES = (date(2026, 6, 30), date(2025, 6, 30))


class _Profile:
    """Профиль комплекта: нужны только даты формы."""

    def dates_of(self, form_code: str) -> tuple[date, ...]:
        """Две графы: отчётная и сравнительная."""
        return DATES


def test_two_exact_cost_of_sales_lines_are_summed() -> None:
    """ФосАгро 6м2026: себестоимость — сумма двух строк, а не последняя из них."""
    facts = (
        ConfirmedFact(FORM, "ifrs.cost_of_sales",
                      (Decimal(-194587), Decimal(-158932)),
                      "Себестоимость реализованной продукции Группы", 2),
        ConfirmedFact(FORM, "ifrs.cost_of_sales", (Decimal(-8706), Decimal(-5000)),
                      "Себестоимость товаров для перепродажи", 3),
    )  # fmt: skip
    found = confirmed_values(facts, _Profile())  # type: ignore[arg-type]
    assert found[(FORM, DATES[0], "ifrs.cost_of_sales")] == (Decimal(-203293), "current")
    assert found[(FORM, DATES[1], "ifrs.cost_of_sales")] == (Decimal(-163932), "previous")


def test_the_same_row_confirmed_twice_is_not_doubled() -> None:
    """Подтверждённая прежде и размеченная в присесте строка — одна строка."""
    fact = ConfirmedFact(FORM, "ifrs.cost_of_sales", (Decimal(-8706),), "Себестоимость", 3)
    found = confirmed_values((fact, fact), _Profile())  # type: ignore[arg-type]
    assert found[(FORM, DATES[0], "ifrs.cost_of_sales")][0] == Decimal(-8706)
