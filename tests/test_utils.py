"""Тесты разбора чисел отчётности и безопасного деления."""

from decimal import Decimal
from pathlib import Path

import pytest

from finlib.utils import ParseOutcome, ValueStatus, parse_value, safe_div, to_decimal

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"


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


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        # Ячейки нет вовсе — «нет данных».
        (None, ParseOutcome.MISSING),
        ("", ParseOutcome.MISSING),
        ("   ", ParseOutcome.MISSING),
        # Явный отказ от раскрытия.
        ("-", ParseOutcome.NOT_DISCLOSED),
        ("—", ParseOutcome.NOT_DISCLOSED),
        ("X", ParseOutcome.NOT_DISCLOSED),
        ("х", ParseOutcome.NOT_DISCLOSED),
        ("н/д", ParseOutcome.NOT_DISCLOSED),
        # Содержимое есть, но это не число.
        ("abc", ParseOutcome.INVALID),
        ("1 2 3 abc", ParseOutcome.INVALID),
        ("()", ParseOutcome.INVALID),
        (float("nan"), ParseOutcome.INVALID),
        # Раскрытые значения.
        ("0", ParseOutcome.OK),
        ("(1 234,56)", ParseOutcome.OK),
        (123, ParseOutcome.OK),
    ],
)
def test_parse_value_outcome(raw: object, outcome: ParseOutcome) -> None:
    """Разбор отличает пустую ячейку, прочерк, мусор и раскрытое значение."""
    assert parse_value(raw).outcome is outcome


def test_parse_value_missing_and_not_disclosed_are_distinguishable() -> None:
    """«Нет данных» и «не раскрыто» — разные исходы, хотя значение в обоих случаях None."""
    missing = parse_value(None)
    dash = parse_value("—")
    assert missing.outcome is not dash.outcome
    assert missing.value is dash.value is None
    assert missing.value_status is dash.value_status is ValueStatus.NOT_DISCLOSED


def test_parse_value_keeps_raw_for_dq_log() -> None:
    """Исходное представление сохраняется — оно нужно в журнале контролей."""
    assert parse_value("  X  ").raw == "X"
    assert parse_value("abc").raw == "abc"


def test_parse_value_ok_agrees_with_to_decimal() -> None:
    """Разобранное значение совпадает с to_decimal, статус — ok."""
    parsed = parse_value("1 234,56")
    assert parsed.value == to_decimal("1 234,56") == Decimal("1234.56")
    assert parsed.is_ok
    assert parsed.value_status is ValueStatus.OK


def test_value_status_matches_schema_check() -> None:
    """Перечисление статусов не расходится с CHECK в sql/001_schema.sql."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    check = "value_status IN ('ok', 'not_disclosed', 'not_applicable')"
    assert check in schema
    assert {status.value for status in ValueStatus} == {"ok", "not_disclosed", "not_applicable"}


def test_safe_div_precision_is_not_float() -> None:
    """Результат — Decimal, а не float: 1/3 не округляется до двоичного приближения."""
    result = safe_div(1, 3)
    assert isinstance(result, Decimal)
    assert str(result).startswith("0.3333333333")
