"""Кусочно-линейная шкала: одна арифметика на все справочники.

Правило объявлено методикой: между опорными точками — линейная интерполяция,
за крайними точками балл не меняется. Ступень давала бы скачок класса при
изменении показателя на сотую долю, а объяснить это читателю нельзя.

**Реализация одна, потому что двух было две.** Шкала РСБУ считалась
`MetricScale.score_for`, шкала МСФО — собственным `_level` в `scoring/ifrs.py`,
и они уже расходились: при нулевой ширине отрезка одна возвращала балл
верхней точки, другая — нижней. Расхождения не видно, пока обе не сравнить,
и ровно это уже случилось с составами итогов — экран разметки знал
о запасных составах, а экран сверки нет.

Порядок точек здесь не требуется, и это не небрежность: справочники хранят
их по-разному. В РСБУ точки идут по возрастанию значения показателя,
в МСФО — по возрастанию балла, а у показателя «меньше — лучше» это
противоположные порядки.
"""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from finlib.metrics.display import round_to

logger = logging.getLogger(__name__)

# Разрядность балла — та, с какой он печатается в «Ключевом выводе»
# и в приложении (`report/summary.py`, `report/appendix.py`): два знака,
# и одна на оба стандарта.
SCORE_SCALE = 2


class ScoreClass(Protocol):
    """Класс состояния со стороны, которой касается порог балла."""

    @property
    def min_score(self) -> Decimal:
        """Наименьший балл, при котором присваивается этот класс."""


def class_by_printed_score[T: ScoreClass](
    total: Decimal, classes: Sequence[T], scale: int = SCORE_SCALE
) -> T:
    """Класс по баллу в том виде, в каком балл напечатан в документе.

    Порог сверяется с округлённым числом, а не с полной точностью: у ПАО
    «Левенгук» балл 79,997 давал класс B при напечатанных «80,00 из 100»
    и правиле «A — от 80». Каждая величина была верна, а документ противоречил
    себе — единая точка округления (задача 13) относится и к порогу класса.

    Классы перечисляются от старшего к младшему, границы нестрогие сверху;
    реализация одна на РСБУ и МСФО, потому что величина одна.
    """
    shown = round_to(total, scale)
    for item in classes:
        if shown >= item.min_score:
            return item
    return classes[-1]


def interpolate(points: Sequence[tuple[Decimal, Decimal]], value: Decimal) -> Decimal:
    """Балл по значению: линейная интерполяция, крайние точки не продолжаются.

    **Значение, совпавшее с опорной точкой, берёт её балл**, а если такая
    точка не одна — наибольший из них. Две точки с одной абсциссой означают
    ступень, которой методика не допускает; выбор объявлен здесь, а не
    оставлен на случай: прежде две реализации отвечали по-разному, одна
    брала верхнюю точку, другая нижнюю.
    """
    ordered = sorted((Decimal(x), Decimal(y)) for x, y in points)
    exact = [score for abscissa, score in ordered if abscissa == value]
    if exact:
        return max(exact)
    if value < ordered[0][0]:
        return ordered[0][1]
    if value > ordered[-1][0]:
        return ordered[-1][1]
    for (low, low_score), (high, high_score) in zip(
        ordered, ordered[1:], strict=False
    ):
        if low < value < high:
            return low_score + (high_score - low_score) * (value - low) / (high - low)
    return ordered[-1][1]  # pragma: no cover — значение вне всех условий выше
