"""Сквозная сверка чисел: проверенный текст против готового документа.

Постпроверка отвечает за текст, который выдала модель. Всё, что происходит
после неё, находилось вне контроля: снятие разметки, разбор на разделы,
оформление абзацев, запись docx. Любая из этих операций может исказить
уже проверенное.

Так и случилось: очистка разметки съедала минус величины, и «(-0,41)»
превращалось в «(0,41)» — число меняло знак уже после того, как проверка
его подтвердила. Поймать это могла только сверка исходного текста
с итоговым.

Поэтому числа разделов 2–6 готового документа сверяются с числами
проверенного текста. Расхождение — документ не выпускается: искажение
после проверки хуже отсутствия документа, потому что выглядит проверенным.
"""

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from finlib.utils import to_decimal

logger = logging.getLogger(__name__)

# Число русского написания: разряды пробелами, запятая как десятичный знак.
_NUMBER = re.compile(
    r"[-−]?\d{1,3}(?:[\s  ]\d{3})+(?:[.,]\d+)?|[-−]?\d+(?:[.,]\d+)?"
)


class NumbersAlteredError(RuntimeError):
    """Числа изменились после постпроверки: документ не выпускается."""

    def __init__(self, problems: list[str]) -> None:
        listed = "; ".join(problems)
        super().__init__(
            f"числа искажены после постпроверки, документ не сформирован: {listed}"
        )
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Alteration:
    """Расхождение между проверенным текстом и документом."""

    kind: str
    message: str


def numbers_of(text: str) -> list[Decimal]:
    """Все числа текста значениями, со знаком и в порядке появления."""
    found: list[Decimal] = []
    for match in _NUMBER.finditer(text):
        value = to_decimal(match.group())
        if value is not None:
            found.append(value)
    return found


def compare(
    verified: str, rendered: str, extra: Sequence[str] = ()
) -> list[Alteration]:
    """Сверяет числа проверенного текста с числами документа.

    Сверяются мультимножества, а не последовательности: разбор на разделы
    и оформление вправе переставить абзацы, но не изменить величины.
    Коды показателей из проверенного текста к этому моменту сняты, поэтому
    сверка идёт по числам документа: каждое обязано найтись в исходнике.
    """
    # В разделы модели входит и детерминированная часть: номера заголовков
    # и предписанные формулировки сигналов. Их числа в тексте модели
    # отсутствуют по построению, и сверять их с ним бессмысленно.
    before = Counter(numbers_of("\n".join([verified, *extra])))
    after = Counter(numbers_of(rendered))

    found: list[Alteration] = []
    for value, count in after.items():
        available = before.get(value, 0)
        if count > available:
            found.append(
                _alteration(value, count - available, before)
            )
    return found


def _alteration(value: Decimal, extra: int, before: Counter[Decimal]) -> Alteration:
    """Описывает лишнее число документа, отделяя смену знака от прочего."""
    if before.get(-value, 0) > 0:
        return Alteration(
            "sign_changed",
            f"число {value} сменило знак после проверки: в проверенном тексте "
            f"было {-value}",
        )
    return Alteration(
        "number_added",
        f"число {value} появилось в документе после проверки "
        f"({extra} раз), в проверенном тексте его нет",
    )


def check_numbers(
    verified: str, rendered: str, extra: Sequence[str] = ()
) -> None:
    """Поднимает исключение, если числа документа расходятся с проверенными."""
    problems = compare(verified, rendered, extra)
    if not problems:
        return
    # Смена знака выносится вперёд: это не потеря сведений, а ложное
    # утверждение, выглядящее проверенным.
    problems.sort(key=lambda item: item.kind != "sign_changed")
    raise NumbersAlteredError([item.message for item in problems])
