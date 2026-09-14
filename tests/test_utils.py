"""Тесты разбора чисел отчётности и безопасного деления."""

from decimal import Decimal

import pytest

from finlib.utils import safe_div, to_decimal


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Русские форматы с разделителями разрядов.
        ("1 234,56", Decimal("1234.56")),
        ("1 234,56", Decimal("1234.56")),
        ("1 234,56", Decimal("1234.56")),
        ("1 234 567", Decimal("1234567")),
        ("1.234,56", Decimal("1234.56")),
        ("1,234.56", Decimal("1234.56")),
        ("1,234,567", Decimal("1234567")),
        ("1.234.567", Decimal("1234567")),
        ("1234.56", Decimal("1234.56")),
        # Знак.
        ("(1 234)", Decimal("-1234")),
        ("(1 234,56)", Decimal("-1234.56")),
        ("-1234", Decimal("-1234")),
        ("−1234", Decimal("-1234")),
        ("+1234", Decimal("1234")),
        # Ноль — это раскрытый ноль, а не отсутствие данных.
        ("0", Decimal("0")),
        ("0,0", Decimal("0.0")),
        ("(0)", Decimal("0")),
        # Прочие типы.
        (123, Decimal("123")),
        (-123, Decimal("-123")),
        (Decimal("1.5"), Decimal("1.5")),
        (1.5, Decimal("1.5")),
        ("  42  ", Decimal("42")),
    ],
)
def test_to_decimal_parses(raw: object, expected: Decimal) -> None:
    """Числа в разных форматах приводятся к точному Decimal."""
    result = to_decimal(raw)
    assert isinstance(result, Decimal)
    assert result == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "-",
        "–",
        "—",
        "−",
        "X",
        "x",
        "Х",
        "х",
        "н/д",
        "Н/Д",
        "нет данных",
        "не раскрыто",
        "прочерк",
        "abc",
        "1 2 3 abc",
        "()",
        ".",
        True,
        False,
        [],
        float("nan"),
        float("inf"),
        Decimal("NaN"),
    ],
)
def test_to_decimal_not_disclosed(raw: object) -> None:
    """Нераскрытое, пустое и нечисловое дают None, а не ноль."""
    assert to_decimal(raw) is None


def test_to_decimal_float_is_exact() -> None:
    """float преобразуется через строку, без артефактов двоичной дроби."""
    assert to_decimal(0.1) == Decimal("0.1")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (10, 4, Decimal("2.5")),
        ("1 000", "4", Decimal("250")),
        (Decimal("-10"), Decimal("4"), Decimal("-2.5")),
        ("0", "5", Decimal("0")),
        (Decimal("1"), Decimal("8"), Decimal("0.125")),
    ],
)
def test_safe_div_values(a: object, b: object, expected: Decimal) -> None:
    """Деление выполняется в Decimal и даёт точный результат."""
    result = safe_div(a, b)
    assert isinstance(result, Decimal)
    assert result == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (10, 0),
        (10, Decimal("0")),
        (10, Decimal("0.000")),
        (10, "0"),
        (0, 0),
        (None, 4),
        (10, None),
        (None, None),
        (10, "X"),
        ("—", 4),
    ],
)
def test_safe_div_returns_none(a: object, b: object) -> None:
    """Ноль в знаменателе и нераскрытые аргументы дают None, не ноль и не бесконечность."""
    assert safe_div(a, b) is None


def test_safe_div_precision_is_not_float() -> None:
    """Результат — Decimal, а не float: 1/3 не округляется до двоичного приближения."""
    result = safe_div(1, 3)
    assert isinstance(result, Decimal)
    assert str(result).startswith("0.3333333333")
