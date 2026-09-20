"""Тесты кусочно-линейной шкалы: арифметика одна на все справочники.

Правило объявлено методикой: между опорными точками линейная интерполяция,
за крайними точками балл не меняется. Реализаций было две — своя у шкал РСБУ
и своя у шкал МСФО, — и они расходились на нулевой ширине отрезка. Здесь
проверяется, что обе стороны отвечают одним числом.
"""

from decimal import Decimal

from finlib.normalize.ifrs_metrics import Scale
from finlib.scoring.definitions import MetricScale
from finlib.scoring.ifrs import _level
from finlib.scoring.scale import interpolate

# Шкала РСБУ хранится по возрастанию значения показателя, шкала МСФО —
# по возрастанию балла. У показателя «меньше — лучше» это противоположные
# порядки, и обе записи означают одну шкалу.
BY_VALUE = ((Decimal(1), Decimal(100)), (Decimal(2), Decimal(50)), (Decimal(4), Decimal(0)))
BY_SCORE = ((Decimal(4), 0), (Decimal(2), 50), (Decimal(1), 100))


def test_between_points_the_scale_is_linear() -> None:
    """Между опорными точками — линейная интерполяция."""
    assert interpolate(BY_VALUE, Decimal("1.5")) == Decimal(75)
    assert interpolate(BY_VALUE, Decimal(3)) == Decimal(25)


def test_beyond_the_ends_the_score_holds() -> None:
    """За крайними точками балл не меняется: шкала не продолжается."""
    assert interpolate(BY_VALUE, Decimal("0.1")) == Decimal(100)
    assert interpolate(BY_VALUE, Decimal(40)) == Decimal(0)


def test_order_of_points_does_not_matter() -> None:
    """Порядок записи точек на балл не влияет: справочники хранят их по-разному."""
    for value in ("0.5", "1.5", "2", "3", "9"):
        assert interpolate(BY_VALUE, Decimal(value)) == interpolate(
            BY_SCORE, Decimal(value)
        )


def test_both_standards_answer_the_same_number() -> None:
    """Шкала РСБУ и шкала МСФО дают один балл на одном значении.

    Прежде у них были две реализации одного правила, и сравнить их было
    нечем — расхождение не видно, пока не сравнишь.
    """
    rsbu = MetricScale(points=BY_VALUE)
    ifrs = Scale(points=BY_SCORE)
    for value in ("0.5", "1", "1.25", "2", "3.75", "4", "10"):
        assert rsbu.score_for(Decimal(value)) == _level(Decimal(value), ifrs)


def test_zero_width_step_takes_the_upper_score() -> None:
    """Нулевая ширина отрезка — ступень, и балл берётся по верхней точке.

    Выбор объявлен, а не оставлен на случай: прежде одна реализация брала
    верхнюю точку, другая нижнюю, и на одной шкале они расходились вдвое.
    """
    step = ((Decimal(1), Decimal(0)), (Decimal(1), Decimal(100)))
    assert interpolate(step, Decimal(1)) == Decimal(100)
