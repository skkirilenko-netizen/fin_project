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
from enum import StrEnum

from finlib.llm.pairs import Anchor, build_index, find_anchor
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

# Дата: числом отчётности не является, разбирать её на части нельзя.
_DATE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")

# Код формы по ОКУД: ссылка на форму, а не величина.
_FORM_CODE = re.compile(r"\b0\d{6}\b")

# Номер нормативного документа: «приказ № 84н» — реквизит, а не величина.
_DOC_NUMBER = re.compile(r"№\s?\d+[а-яёa-z]?", re.IGNORECASE)


class Violation(StrEnum):
    """Чем именно плохо число в ответе."""

    NOT_IN_BLOCKS = "not_in_blocks"
    NO_ANCHOR = "no_anchor"
    WRONG_ANCHOR = "wrong_anchor"


@dataclass(frozen=True, slots=True)
class ForeignNumber:
    """Число из ответа, не прошедшее проверку."""

    text: str
    value: Decimal
    context: str
    violation: Violation = Violation.NOT_IN_BLOCKS
    anchor: str | None = None

    def describe(self) -> str:
        """Человеческое объяснение, чем число плохо."""
        if self.violation is Violation.NO_ANCHOR:
            return f"{self.text} — приведено без кода показателя"
        if self.violation is Violation.WRONG_ANCHOR:
            return f"{self.text} — не является значением «{self.anchor}»"
        return f"{self.text} — отсутствует во входных данных"


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


def verify(response: str, blocks: str, *, require_anchor: bool = True) -> VerificationResult:
    """Сверяет пары «число — код» в ответе с входными блоками.

    Число обязано не только встречаться во входных данных, но и стоять при том
    показателе, которому принадлежит: верное значение при чужом коде — ложное
    утверждение, а не опечатка. При require_anchor число без кода рядом тоже
    считается нарушением: проверить его не с чем.
    """
    text = strip_reasoning(response)
    allowed = allowed_values(blocks)
    rounded = _rounded_forms(allowed)
    index = build_index(blocks)
    skip = (
        _list_item_spans(text)
        + [match.span() for match in _DATE.finditer(text)]
        + [match.span() for match in _FORM_CODE.finditer(text)]
        + [match.span() for match in _DOC_NUMBER.finditer(text)]
    )

    foreign: list[ForeignNumber] = []
    checked = 0
    for match in _NUMBER.finditer(text):
        span = match.span()
        if any(start <= span[0] and span[1] <= end for start, end in skip):
            continue  # номер пункта списка
        value = to_decimal(match.group())
        if value is None:
            continue
        if index.get(match.group()) is not None:
            continue  # это сам код строки, ссылка на показатель, а не величина
        if text[span[1] : span[1] + 1] == "_":
            continue  # начало кода производной величины: 1230 в 1230_chg_pct
        checked += 1
        if _is_trivial(value):
            continue

        # Якорь ищется первым: он задаёт, с чем именно сверять число.
        # Общий набор чисел блоков — запасная проверка для числа без якоря.
        anchor = find_anchor(text, span, index)
        if anchor is not None:
            if _matches_anchor(value, match.group(), anchor):
                continue
            violation = (
                Violation.WRONG_ANCHOR
                if _is_allowed(value, match.group(), allowed, rounded)
                else Violation.NOT_IN_BLOCKS
            )
            foreign.append(_foreign(text, match, value, violation, anchor.key))
            continue

        if not _is_allowed(value, match.group(), allowed, rounded):
            foreign.append(_foreign(text, match, value, Violation.NOT_IN_BLOCKS))
        elif require_anchor:
            foreign.append(_foreign(text, match, value, Violation.NO_ANCHOR))

    result = VerificationResult(verified=not foreign, foreign=foreign, checked=checked)
    logger.info("постпроверка: %s", result.summary())
    return result


def _foreign(
    text: str,
    match: re.Match[str],
    value: Decimal,
    violation: Violation,
    anchor: str | None = None,
) -> ForeignNumber:
    """Собирает запись о непрошедшем числе."""
    return ForeignNumber(
        text=match.group(),
        value=value,
        context=_context_of(text, match.span()),
        violation=violation,
        anchor=anchor,
    )


def _matches_anchor(value: Decimal, text: str, anchor: Anchor) -> bool:
    """Принадлежит ли число тому показателю, при котором стоит."""
    if value in anchor.values:
        return True
    places = _decimal_places(text)
    quant = Decimal(1).scaleb(-places)
    return value in {item.quantize(quant) for item in anchor.values}


def _is_trivial(value: Decimal) -> bool:
    """Число, не несущее сведений об организации: ноль, единица, сто, год."""
    if abs(value) in ALWAYS_ALLOWED:
        return True
    return value == value.to_integral_value() and YEAR_MIN <= int(value) <= YEAR_MAX


def _is_allowed(
    value: Decimal, text: str, allowed: set[Decimal], rounded: dict[int, set[Decimal]]
) -> bool:
    """Встречается ли число во входных блоках хотя бы где-нибудь."""
    if value in allowed:
        return True
    places = _decimal_places(text)
    return value in rounded.get(places, set())


def _context_of(text: str, span: tuple[int, int], width: int = 40) -> str:
    """Окружение числа — чтобы в журнале было видно, где оно появилось."""
    start = max(0, span[0] - width)
    end = min(len(text), span[1] + width)
    return " ".join(text[start:end].split())
