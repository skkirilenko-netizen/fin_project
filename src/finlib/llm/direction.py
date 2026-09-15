"""Согласование глагола при числе со знаком изменения.

Постпроверка чисел не ловит неверное направление: во фразе «коэффициент вырос
с 1,23 до 0,82» оба числа взяты из входных данных, а утверждение ложно.
Но направление проверяемо механически — знак величины `*_chg_abs` известен,
и глагол при ней обязан ему соответствовать.

Проверка намеренно осторожна: при сомнении она молчит. Ложное отклонение
верного ответа обходится дороже пропуска, потому что отклонённый ответ
пользователю не показывается вовсе.
"""

import re
from decimal import Decimal
from enum import StrEnum

# Слова роста и снижения, по корням: русские формы склоняются и спрягаются,
# а окончания для направления значения не имеют.
_GROWTH = re.compile(
    r"вырос|возрос|увелич|прирост|прибав|повыс|подня|наращ|нарос|\bрост|росл|подрос",
    re.IGNORECASE,
)
_DECLINE = re.compile(
    r"сниж|снизи|сократ|сокращ|уменьш|упал|упа[лв]|паден|спад|просе[лд]|сжат|сжал|убыл",
    re.IGNORECASE,
)

# Насколько далеко назад от числа искать глагол. Дальше начинается соседнее
# утверждение, и глагол оттуда к этому числу уже не относится.
LOOKBEHIND = 160

_SENTENCE_END = re.compile(r"[.!?;\n]")


class Direction(StrEnum):
    """Направление, заявленное словом при числе."""

    GROWTH = "growth"
    DECLINE = "decline"


def stated_direction(text: str, position: int) -> Direction | None:
    """Направление по ближайшему слову перед числом; None — если слова нет.

    Ищется в пределах одного предложения: за точкой начинается утверждение
    о другой величине, и его глагол к этому числу не относится.
    """
    start = max(0, position - LOOKBEHIND)
    window = text[start:position]
    boundary = 0
    for match in _SENTENCE_END.finditer(window):
        boundary = match.end()
    window = window[boundary:]

    growth = _last(_GROWTH, window)
    decline = _last(_DECLINE, window)
    if growth is None and decline is None:
        return None
    if decline is None or (growth is not None and growth > decline):
        return Direction.GROWTH
    return Direction.DECLINE


def agrees(value: Decimal, stated: Direction | None) -> bool:
    """Согласовано ли заявленное направление со знаком изменения.

    Нулевое изменение направления не имеет: о нём говорят «не изменился»,
    и придираться к формулировке не за что.
    """
    if stated is None or value == 0:
        return True
    return (stated is Direction.GROWTH) == (value > 0)


def _last(pattern: re.Pattern[str], window: str) -> int | None:
    """Позиция последнего совпадения в окне."""
    found = None
    for match in pattern.finditer(window):
        found = match.start()
    return found
