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


# Оформление единиц измерения. Разрядность приходит из методики
# (metrics.yaml, блок display): округление в проекте одно на всех, иначе
# текст заключения и приложение расходятся между собой.
#
# Оформление живёт рядом с округлением, а не в сборке контекста модели:
# одну и ту же величину набирают и блоки модели, и приложение документа,
# и тезисы (scoring/theses.py), а импорт из llm/ в scoring/ замкнул бы
# зависимости в кольцо.
UNIT_SUFFIX: dict[Unit, str] = {
    Unit.THOUSAND_RUB: " тыс. руб.",
    Unit.DAYS: " дн.",
    Unit.PERCENT: " %",
    Unit.RATIO: "",
}


# Разряды разделяются неразрывным пробелом, а не обычным: число не должно
# разрываться переносом ни в документе, ни в блоке модели. Константа заведена
# затем, чтобы разделитель не пропал при правке — на вид он от обычного
# пробела не отличается.
DIGIT_SPACE = " "


def digits(value: Decimal, scale: int) -> str:
    """Число с разделителями разрядов и запятой как десятичным знаком."""
    return f"{round_to(value, scale):,}".replace(",", DIGIT_SPACE).replace(".", ",")


def money(value: Decimal) -> str:
    """Денежная величина: целые тысячи рублей с разделителями разрядов."""
    return digits(value, 0)


def ratio(value: Decimal) -> str:
    """Коэффициент: два знака после запятой."""
    return digits(value, 2)


def days(value: Decimal) -> str:
    """Дни: один знак после запятой."""
    return digits(value, 1)


def percent(value: Decimal) -> str:
    """Процент: один знак после запятой, как и дни."""
    return digits(value, 1)


def format_metric(
    value: Decimal,
    unit: Unit,
    scale: int | None = None,
    money: str | None = None,
) -> str:
    """Значение показателя в его единице измерения и разрядности методики.

    `money` — наименование денежной единицы **комплекта**: консолидированная
    отчётность составляется в миллионах, и «тыс. руб.» в заключении по ней —
    ошибка в тысячу раз, которую не ловит ни один контроль сходимости.
    Умолчание одно и остаётся тысячами рублей: у РСБУ единица задана формой.
    """
    if scale is None:
        from finlib.metrics.definitions import load_metrics

        scale = load_metrics().display.scale_for(unit)
    suffix = UNIT_SUFFIX.get(unit, "")
    if unit is Unit.THOUSAND_RUB and money:
        suffix = f" {money}"
    return f"{digits(value, scale)}{suffix}"
