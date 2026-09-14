"""Арифметические хелперы: разбор чисел отчётности и безопасное деление."""

import math
from decimal import Decimal, InvalidOperation

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


def safe_div(a: object, b: object) -> Decimal | None:
    """Делит с приведением к Decimal; None и деление на ноль дают None, не ноль."""
    numerator = to_decimal(a)
    denominator = to_decimal(b)
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator
