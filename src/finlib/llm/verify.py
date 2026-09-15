"""Постпроверка ответа модели на посторонние числа.

Инвариант 3: каждое число в заключении привязано к коду строки или показателя.
Модель не вычисляет (инвариант 1), поэтому любое число в её ответе обязано
встречаться во входных блоках. Число, которого там нет, — признак того, что
модель посчитала сама или выдумала, и такой ответ пользователю не показывается.
"""

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from finlib.utils import to_decimal

logger = logging.getLogger(__name__)

# Числа, разрешённые без привязки к входным данным: они не несут
# содержательной информации об организации.
ALWAYS_ALLOWED: frozenset[Decimal] = frozenset(
    {Decimal(0), Decimal(1), Decimal(100)}
)

# Диапазон, в котором число считается номером года.
YEAR_MIN, YEAR_MAX = 1990, 2100

# Число с русским оформлением: разряды пробелами, запятая как десятичный знак.
_NUMBER = re.compile(
    r"[-−]?\d{1,3}(?:[    ]\d{3})+(?:[.,]\d+)?"  # с разделителями разрядов
    r"|[-−]?\d+(?:[.,]\d+)?"  # без них
)

# Номер пункта списка или раздела: число в начале строки перед точкой или
# скобкой, в том числе после решёток заголовка Markdown.
_LIST_ITEM = re.compile(r"^[\s#>*-]*(\d{1,2})[.)]\s", re.MULTILINE)

# Рассуждение модели: в заключение не идёт и в проверке не участвует.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ForeignNumber:
    """Число из ответа, не найденное во входных блоках."""

    text: str
    value: Decimal
    context: str


@dataclass
class VerificationResult:
    """Итог постпроверки."""

    verified: bool
    foreign: list[ForeignNumber] = field(default_factory=list)
    checked: int = 0

    @property
    def foreign_values(self) -> list[str]:
        """Посторонние числа строками — для записи в журнал."""
        return [item.text for item in self.foreign]

    def summary(self) -> str:
        """Однострочная сводка."""
        if self.verified:
            return f"проверка пройдена, сверено чисел: {self.checked}"
        return (
            f"проверка не пройдена, посторонних чисел {len(self.foreign)} "
            f"из {self.checked}: {', '.join(self.foreign_values[:5])}"
        )


def strip_reasoning(text: str) -> str:
    """Убирает блок рассуждения модели.

    Рассуждающие модели выводят черновик в <think>. В заключение он не идёт,
    и в постпроверке не участвует: иначе числа из черновика считались бы
    посторонними, а ход мысли попал бы в документ.
    """
    return _THINK.sub("", text).strip()


def extract_numbers(text: str) -> list[tuple[str, Decimal]]:
    """Все числа текста в исходном написании и в виде Decimal."""
    found: list[tuple[str, Decimal]] = []
    for match in _NUMBER.finditer(text):
        value = to_decimal(match.group())
        if value is not None:
            found.append((match.group(), value))
    return found


def _list_item_spans(text: str) -> list[tuple[int, int]]:
    """Позиции номеров пунктов списка."""
    return [match.span(1) for match in _LIST_ITEM.finditer(text)]


def _decimal_places(text: str) -> int:
    """Сколько знаков после запятой в написании числа."""
    cleaned = text.replace(",", ".")
    return len(cleaned.split(".")[1]) if "." in cleaned else 0


def _rounded_forms(values: set[Decimal]) -> dict[int, set[Decimal]]:
    """Входные числа, округлённые до каждой встречающейся разрядности.

    Модель вправе процитировать значение с меньшей точностью, чем оно дано.
    Округление до той же разрядности — не вычисление, а цитирование; при этом
    перевод единиц или иная арифметика совпадения не дадут.
    """
    forms: dict[int, set[Decimal]] = {}
    for places in range(0, 7):
        quant = Decimal(1).scaleb(-places)
        forms[places] = {value.quantize(quant) for value in values}
    return forms


def allowed_values(blocks: str) -> set[Decimal]:
    """Числа, которые модели позволено называть, — из входных блоков."""
    return {value for _, value in extract_numbers(blocks)}


def verify(response: str, blocks: str) -> VerificationResult:
    """Сверяет числа ответа с числами входных блоков."""
    text = strip_reasoning(response)
    allowed = allowed_values(blocks)
    rounded = _rounded_forms(allowed)
    skip = _list_item_spans(text)

    foreign: list[ForeignNumber] = []
    checked = 0
    for match in _NUMBER.finditer(text):
        span = match.span()
        if any(start <= span[0] and span[1] <= end for start, end in skip):
            continue  # номер пункта списка
        value = to_decimal(match.group())
        if value is None:
            continue
        checked += 1
        if _is_allowed(value, match.group(), allowed, rounded):
            continue
        foreign.append(
            ForeignNumber(
                text=match.group(),
                value=value,
                context=_context_of(text, span),
            )
        )

    result = VerificationResult(verified=not foreign, foreign=foreign, checked=checked)
    logger.info("постпроверка: %s", result.summary())
    return result


def _is_allowed(
    value: Decimal, text: str, allowed: set[Decimal], rounded: dict[int, set[Decimal]]
) -> bool:
    """Разрешено ли число без привязки к конкретному коду."""
    if value in allowed or abs(value) in ALWAYS_ALLOWED:
        return True
    if value == value.to_integral_value() and YEAR_MIN <= int(value) <= YEAR_MAX:
        return True  # номер года
    places = _decimal_places(text)
    return value in rounded.get(places, set())


def _context_of(text: str, span: tuple[int, int], width: int = 40) -> str:
    """Окружение числа — чтобы в журнале было видно, где оно появилось."""
    start = max(0, span[0] - width)
    end = min(len(text), span[1] + width)
    return " ".join(text[start:end].split())
