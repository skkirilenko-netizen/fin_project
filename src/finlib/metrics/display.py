"""Единая точка округления на границе расчётного слоя.

Прежде дельты считались по полной точности, а уровни отображались
округлёнными, и документ становился арифметически несогласованным:
«рентабельность активов снизилась с 0,41 до 0,31 (изменение 0,11)» —
разность отображаемых уровней 0,10.

Теперь округление выполняется один раз, здесь, и всё дальнейшее — дельты,
темпы, текст заключения, приложение, вывод в терминале — строится на одних
и тех же округлённых величинах. В `metric_value` значения хранятся полными:
округление относится к представлению, а не к точности расчёта.
"""

from decimal import ROUND_HALF_UP, Decimal

from finlib.metrics.definitions import MetricsCatalog, Unit


def round_to(value: Decimal, scale: int) -> Decimal:
    """Округляет величину до заданной разрядности."""
    return value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)


def displayed(value: Decimal | None, scale: int) -> Decimal | None:
    """Отображаемое значение; None остаётся None."""
    return None if value is None else round_to(value, scale)


def scale_of(catalog: MetricsCatalog, code: str) -> int:
    """Разрядность отображения показателя по его коду."""
    return catalog.scale_for(code)


def scale_of_unit(catalog: MetricsCatalog, unit: Unit) -> int:
    """Разрядность отображения величины в этой единице."""
    return catalog.display.scale_for(unit)
