"""Арифметические хелперы: разбор чисел отчётности и безопасное деление."""

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

# Маркеры нераскрытия показателя: в БД им соответствует NULL, но не ноль.
NOT_DISCLOSED: frozenset[str] = frozenset(
    {
        "-",
        "–",
        "—",
        "−",
        "x",
        "х",
        "н/д",
        "н.д.",
        "нд",
        "нет данных",
        "не раскрыто",
        "прочерк",
    }
)

# Разделители разрядов, встречающиеся в выгрузках отчётности.
_GROUP_SEPARATORS: tuple[str, ...] = (
    " ",  # неразрывный пробел
    " ",  # узкий неразрывный пробел
    " ",  # тонкий пробел
    " ",  # цифровой пробел
    " ",
    "'",
    "’",
)


class ValueStatus(StrEnum):
    """Статус значения в fact_report; значения совпадают с CHECK в схеме БД."""

    OK = "ok"
    NOT_DISCLOSED = "not_disclosed"
    NOT_APPLICABLE = "not_applicable"


class ParseOutcome(StrEnum):
    """Итог разбора ячейки отчётности, более подробный, чем статус в БД."""

    OK = "ok"
    NOT_DISCLOSED = "not_disclosed"  # явный маркер: прочерк, «X», «н/д»
    MISSING = "missing"  # ячейки нет вовсе: None или пустая строка
    INVALID = "invalid"  # содержимое есть, но числом не является


@dataclass(frozen=True, slots=True)
class ParsedValue:
    """Разобранная ячейка: значение, итог разбора и исходное представление."""

    value: Decimal | None
    outcome: ParseOutcome
    raw: str | None

    @property
    def value_status(self) -> ValueStatus:
        """Статус для записи в fact_report: всё, кроме числа, — нераскрытие."""
        return ValueStatus.OK if self.outcome is ParseOutcome.OK else ValueStatus.NOT_DISCLOSED

    @property
    def is_ok(self) -> bool:
        """Значение раскрыто и разобрано."""
        return self.outcome is ParseOutcome.OK


def parse_value(raw: object) -> ParsedValue:
    """Разбирает ячейку отчётности, различая прочерк, пустую ячейку и мусор."""
    text = raw.strip() if isinstance(raw, str) else None

    if raw is None:
        return ParsedValue(None, ParseOutcome.MISSING, None)
    if isinstance(raw, str) and not text:
        return ParsedValue(None, ParseOutcome.MISSING, raw)
    if isinstance(raw, str) and text is not None and text.casefold() in NOT_DISCLOSED:
        return ParsedValue(None, ParseOutcome.NOT_DISCLOSED, text)

    value = to_decimal(raw)
    if value is None:
        return ParsedValue(None, ParseOutcome.INVALID, text if text is not None else str(raw))
    return ParsedValue(value, ParseOutcome.OK, text)


def to_decimal(raw: object) -> Decimal | None:
    """Разбирает значение отчётности в Decimal; нераскрытое и мусор дают None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        return Decimal(str(raw))
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if not text or text.casefold() in NOT_DISCLOSED:
        return None

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1].strip()

    for separator in _GROUP_SEPARATORS:
        text = text.replace(separator, "")
    text = text.replace("−", "-")  # математический минус
    if text.startswith("+"):
        text = text[1:]

    text = _normalize_decimal_separator(text)
    if not text:
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if not value.is_finite():
        return None
    return -value if negative else value


def _normalize_decimal_separator(text: str) -> str:
    """Приводит запятую и точку к единому десятичному разделителю."""
    has_comma = "," in text
    has_dot = "." in text

    if has_comma and has_dot:
        # Десятичный — тот разделитель, который стоит правее.
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")
    if has_comma:
        # Несколько запятых — это разряды, одна — десятичный разделитель.
        return text.replace(",", "") if text.count(",") > 1 else text.replace(",", ".")
    if has_dot and text.count(".") > 1:
        return text.replace(".", "")
    return text


def json_loads_decimal(raw: str | bytes) -> Any:
    """Читает JSON, превращая дробные числа в Decimal, а не в float."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    # parse_int не переопределяем: целые в Python и так точны, а идентификаторы
    # источника должны остаться int.
    return json.loads(text, parse_float=Decimal)


def safe_div(a: object, b: object) -> Decimal | None:
    """Делит с приведением к Decimal; None и деление на ноль дают None, не ноль."""
    numerator = to_decimal(a)
    denominator = to_decimal(b)
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator
