"""Тесты интерпретатора формул показателей."""

from decimal import Decimal

import pytest

from finlib.metrics.formula import (
    AvgRef,
    BinOp,
    ConstRef,
    FormulaError,
    LineRef,
    ZeroDenominatorError,
    average_codes,
    constant_names,
    describe,
    evaluate,
    line_codes,
    parse_formula,
)

CONSTANTS = {"DAYS": Decimal(365)}


def calc(text: str, current: dict, previous: dict | None = None) -> Decimal:
    """Считает формулу на заданных значениях."""
    return evaluate(parse_formula(text), current, previous, CONSTANTS)


# --- разбор -----------------------------------------------------------------


def test_line_code_is_parsed() -> None:
    """Четырёхзначное число — это код строки, а не число."""
    assert parse_formula("1600") == LineRef("1600")


def test_operators_and_precedence() -> None:
    """Умножение связывает сильнее сложения, скобки перекрывают порядок."""
    values = {"1100": Decimal(1), "1200": Decimal(2), "1300": Decimal(3)}
    assert calc("1100 + 1200 * 1300", values) == 7
    assert calc("(1100 + 1200) * 1300", values) == 9


def test_unary_minus() -> None:
    """Унарный минус разбирается."""
    assert calc("-1600", {"1600": Decimal(5)}) == -5


def test_avg_requires_line_code() -> None:
    """avg() принимает только код строки."""
    with pytest.raises(FormulaError, match="avg"):
        parse_formula("avg(DAYS)")


def test_numeric_literal_is_rejected() -> None:
    """Числовой литерал в формуле запрещён: это магическое число."""
    with pytest.raises(FormulaError, match="магическое"):
        parse_formula("365 * 1230 / 2110")


def test_unknown_function_is_rejected() -> None:
    """Неизвестная функция выявляется при разборе, а не при расчёте."""
    with pytest.raises(FormulaError, match="неизвестная функция"):
        parse_formula("sum(1600)")


@pytest.mark.parametrize("text", ["", "   ", "1600 +", "(1600", "1600)", "1600 1700"])
def test_broken_formula_is_rejected(text: str) -> None:
    """Сломанная формула не загружается."""
    with pytest.raises(FormulaError):
        parse_formula(text)


def test_required_codes_are_collected() -> None:
    """Из формулы извлекаются нужные коды и константы."""
    tree = parse_formula("DAYS * avg(1230) / 2110")
    assert line_codes(tree) == {"1230", "2110"}
    assert average_codes(tree) == {"1230"}
    assert constant_names(tree) == {"DAYS"}


def test_describe_is_readable() -> None:
    """Описание узла пригодно для сообщения об ошибке."""
    assert describe(LineRef("2330")) == "строка 2330"
    assert describe(AvgRef("1600")) == "средняя величина строки 1600"
    assert describe(ConstRef("DAYS")) == "DAYS"
    assert describe(BinOp("+", LineRef("1"), LineRef("2"))) == "(строка 1 + строка 2)"


# --- вычисление -------------------------------------------------------------


def test_result_is_decimal() -> None:
    """Результат — Decimal, а не float."""
    result = calc("1200 / 1500", {"1200": Decimal(1), "1500": Decimal(3)})
    assert isinstance(result, Decimal)
    assert str(result).startswith("0.3333333")


def test_average_is_half_sum() -> None:
    """Средняя величина — полусумма на начало и конец периода."""
    result = calc("avg(1600)", {"1600": Decimal(300)}, {"1600": Decimal(100)})
    assert result == Decimal(200)


def test_constant_is_taken_from_methodology() -> None:
    """Константа берётся из методики, а не из кода."""
    assert calc("DAYS * 1230 / 2110", {"1230": Decimal(10), "2110": Decimal(365)}) == 10


def test_zero_denominator_names_the_line() -> None:
    """Деление на ноль даёт ошибку, называющую виновную строку."""
    with pytest.raises(ZeroDenominatorError) as info:
        calc("2200 / 2330", {"2200": Decimal(100), "2330": Decimal(0)})
    assert info.value.expression == "строка 2330"


def test_zero_denominator_is_not_infinity() -> None:
    """Деление на ноль не превращается в бесконечность."""
    with pytest.raises(ZeroDenominatorError):
        calc("1600 / (1100 - 1200)", {"1600": Decimal(5), "1100": Decimal(7), "1200": Decimal(7)})


def test_missing_average_base_is_an_error() -> None:
    """Без предыдущего периода средняя величина не вычисляется."""
    with pytest.raises(FormulaError, match="предыдущ"):
        calc("avg(1600)", {"1600": Decimal(300)}, None)
