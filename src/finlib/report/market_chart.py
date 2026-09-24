"""График рыночного ряда: спред против ориентира и цена против границы.

**Рисуется он одним кодом на все выходы.** Карточка эмитента и страница
списка показывают одно и то же, и второй рисовальщик разошёлся бы с первым
в первом же масштабе — тот же запрет, что у величин.

**График рисуется из тех же точек, что и основание.** Ряд берётся
у `sources.market`, отсечка и ориентир — у методики; ничего своего здесь
не считается, кроме координат.

**Никаких обращений наружу.** Страница открывается в браузере и остаётся
в локальном контуре: график — встроенный SVG, а не ссылка на библиотеку
рисования. Данные из контура не выходят.
"""

import logging
import math
from base64 import b64encode
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finlib.metrics.display import digits
from finlib.sources.market import Market, MarketPolicy, Point

logger = logging.getLogger(__name__)

# Размеры одной рамки. Ширина выбрана под ширину текста карточки, высота —
# чтобы две рамки читались рядом и не занимали экран целиком.
WIDTH = 720
HEIGHT = 140
PAD_LEFT = 64
PAD_RIGHT = 12
PAD_TOP = 14
PAD_BOTTOM = 22


def _x(day: date, first: date, last: date) -> float:
    """Координата дня: ось времени линейна и общая у обеих рамок."""
    span = (last - first).days or 1
    inner = WIDTH - PAD_LEFT - PAD_RIGHT
    return PAD_LEFT + inner * (day - first).days / span


# **Нижняя граница логарифмической оси.** Логарифм нуля и отрицательного
# не определён, а отрицательный спред правомерен: корпоративная бумага
# изредка торгуется ниже кривой на коротком конце. Такие точки прижимаются
# к низу рамки, и число их называется в подписи — молча их терять нельзя.
FLOOR_BP = Decimal(1)

# **Шкала цены постоянна у всех карточек**: они сравниваются между собой,
# а своя шкала у каждой делает сравнение невозможным. Сто десять процентов
# сверху — запас над номиналом: бумага с высоким купоном торгуется выше него.
PRICE_LOW = Decimal(0)
PRICE_HIGH = Decimal(110)


@dataclass(frozen=True, slots=True)
class Axis:
    """Ось значений: как считать координату и где ставить отметки.

    **Шкала — часть утверждения графика, а не оформление.** Весь рыночный
    слой построен на кратности к ориентиру, и линейная ось прятала обычный
    диапазон бумаги в нижние проценты высоты: у Кириллицы она шла от 29
    до 16 348 б. п., и видно было плоскую линию со всплеском.
    """

    low: Decimal
    high: Decimal
    scale: int
    logarithmic: bool = False
    # Отметки помимо краёв: величина и признак «подписать особо» (граница).
    marks: tuple[tuple[Decimal, bool], ...] = ()
    clamped: int = 0

    def y(self, value: Decimal) -> float:
        """Координата величины; ряд без размаха рисуется по середине рамки."""
        inner = HEIGHT - PAD_TOP - PAD_BOTTOM
        low, high = self.low, self.high
        if high <= low:
            return PAD_TOP + inner / 2
        if self.logarithmic:
            spot = _log(max(value, low))
            return PAD_TOP + inner * float(
                (_log(high) - spot) / (_log(high) - _log(low))
            )
        value = min(max(value, low), high)
        return PAD_TOP + inner * float((high - value) / (high - low))

    def ticks(self) -> tuple[tuple[Decimal, bool], ...]:
        """Края шкалы и промежуточные отметки — снизу вверх."""
        return ((self.low, False), *self.marks, (self.high, False))


def _log(value: Decimal) -> Decimal:
    """Десятичный логарифм величины; ниже нижней границы — сама граница."""
    return Decimal(math.log10(float(max(value, FLOOR_BP))))


def _line(
    points: list[tuple[date, Decimal]],
    first: date,
    last: date,
    axis: Axis,
    colour: str,
    dashed: bool = False,
) -> str:
    """Ломаная по точкам ряда; пустой ряд линии не даёт вовсе."""
    if not points:
        return ""
    spots = " ".join(
        f"{_x(day, first, last):.0f},{axis.y(value):.0f}" for day, value in points
    )
    dash = ' stroke-dasharray="4 3"' if dashed else ""
    return (
        f'<polyline fill="none" stroke="{colour}" stroke-width="1.4"'
        f'{dash} points="{spots}"/>'
    )


def _frame(
    title: str,
    lines: list[tuple[list[tuple[date, Decimal]], str, bool]],
    first: date,
    last: date,
    axis: Axis,
) -> str:
    """Одна рамка: подпись, отметки оси, ломаные и линии сетки.

    **Подписаны края и одна-две промежуточные отметки.** Одни края отвечают
    на вопрос «в каких пределах это двигалось» и не отвечают на вопрос «где
    сейчас»: уровень приходилось прикидывать на глаз.
    """
    if not any(series for series, _, _ in lines):
        return ""
    drawn = "".join(
        _line(series, first, last, axis, colour, dashed)
        for series, colour, dashed in lines
    )
    grid = "".join(
        f'<line x1="{PAD_LEFT}" y1="{axis.y(value):.0f}" '
        f'x2="{WIDTH - PAD_RIGHT}" y2="{axis.y(value):.0f}" '
        f'stroke="#ececec" stroke-width="1"/>'
        for value, _ in axis.marks
    )
    labels = "".join(
        f'<text x="{PAD_LEFT - 6}" y="{axis.y(value) + 4:.0f}" '
        f'text-anchor="end" fill="{"#8a2b2b" if bold else "#666"}">'
        f"{digits(value, axis.scale)}</text>"
        for value, bold in axis.ticks()
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="system-ui, sans-serif" font-size="11">'
        f'<rect x="{PAD_LEFT}" y="{PAD_TOP}" '
        f'width="{WIDTH - PAD_LEFT - PAD_RIGHT}" '
        f'height="{HEIGHT - PAD_TOP - PAD_BOTTOM}" fill="none" '
        f'stroke="#d8d8d8"/>'
        f"{grid}"
        f'<text x="{PAD_LEFT}" y="10" fill="#333">{title}</text>'
        f"{labels}"
        f'<text x="{PAD_LEFT}" y="{HEIGHT - 6}" fill="#666">'
        f"{first:%d.%m.%Y}</text>"
        f'<text x="{WIDTH - PAD_RIGHT}" y="{HEIGHT - 6}" text-anchor="end" '
        f'fill="#666">{last:%d.%m.%Y}</text>'
        f"{drawn}</svg>"
    )


def _log_axis(values: list[Decimal], scale: int) -> Axis:
    """Логарифмическая ось спреда с отметками по круглым кратностям.

    Отметки берутся из ряда 1, 2, 5 × 10ⁿ — тех же, по которым читается
    кратность: между 100 и 1 000 б. п. разница не в девятистах пунктах,
    а в десяти разах, и ось обязана показывать именно это.
    """
    low = max(min(values), FLOOR_BP)
    high = max(max(values), low * 10)
    marks: list[tuple[Decimal, bool]] = []
    step = Decimal(1)
    while step <= high:
        for size in (Decimal(1), Decimal(2), Decimal(5)):
            value = step * size
            # Отметка ближе двух крат к краю накладывается на его подпись:
            # «10 000» и «16 348» в одиннадцать пунктов не расходятся.
            if low * 2 < value < high / 2:
                marks.append((value, False))
        step *= 10
    # Больше трёх отметок на рамку высотой в сто пикселей не читается:
    # берутся крайние и средняя.
    if len(marks) > 3:
        marks = [marks[0], marks[len(marks) // 2], marks[-1]]
    return Axis(
        low=low,
        high=high,
        scale=scale,
        logarithmic=True,
        marks=tuple(marks),
        clamped=sum(1 for value in values if value < FLOOR_BP),
    )


def _worst(
    points: list[tuple[date, Decimal]], take_lowest: bool
) -> list[tuple[date, Decimal]]:
    """Ряд по неделям, и берётся из недели **худшая** точка.

    Две сотни дневных точек на семистах пикселях неразличимы глазом, а весят
    вчетверо; но прореживать «каждой седьмой» нельзя — именно провал цены
    и всплеск спреда суть то, на что признак и срабатывает. Поэтому из недели
    берётся та точка, по которой сработало бы основание: у цены наименьшая,
    у спреда наибольшая.
    """
    weeks: dict[tuple[int, int], tuple[date, Decimal]] = {}
    for day, value in points:
        key = day.isocalendar()[:2]
        seen = weeks.get(key)
        if seen is None or (value < seen[1] if take_lowest else value > seen[1]):
            weeks[key] = (day, value)
    return sorted(weeks.values())


def charts(
    points: list[Point], market: Market, policy: MarketPolicy
) -> tuple[str, str]:
    """Две рамки: спред с ориентиром дня и цена с отсечкой зоны дефолта.

    **Спред без ориентира дня не говорит ничего**: уровень рынка двигается
    вместе со ставкой, и одна и та же тысяча базисных пунктов означает
    в разные годы разное. Поэтому ориентир нарисован рядом, а не вычтен.

    Пустая строка вместо рамки означает, что величины нет вовсе, — и это
    не то же, что ровная линия по нулю.
    """
    if not points:
        return "", ""
    first, last = points[0].day, points[-1].day
    spread = _worst(
        [(item.day, item.spread) for item in points if item.spread is not None],
        take_lowest=False,
    )
    level = _worst(
        [
            (item.day, market.benchmark[item.day])
            for item in points
            if item.day in market.benchmark
        ],
        take_lowest=False,
    )
    price = _worst(
        [(item.day, item.price) for item in points if item.price is not None],
        take_lowest=True,
    )
    below = policy.distress_zone.price_below_percent
    edge = [(first, below), (last, below)] if price else []
    # **Шкала спреда логарифмическая, и это сказано в подписи.** Слой
    # построен на кратности к ориентиру: между 100 и 1 000 б. п. разница
    # не в девятистах пунктах, а в десяти разах.
    spread_axis = _log_axis(
        [value for _, value in spread + level] or [FLOOR_BP],
        int(policy.display["spread_scale"]),
    )
    lost = (
        f", ниже {digits(FLOOR_BP, 0)} б. п. прижато к низу: {spread_axis.clamped}"
        if spread_axis.clamped
        else ""
    )
    # **Шкала цены одна на все карточки.** Своя у каждой делала их
    # несравнимыми между собой: у эмитента с ценой 5,8 % нижняя подпись
    # стояла на 5,8, и граница 60 % сливалась с осью.
    price_axis = Axis(
        low=PRICE_LOW,
        high=PRICE_HIGH,
        scale=int(policy.display["price_scale"]),
        marks=((below, True), (Decimal(100), False)),
    )
    return (
        _frame(
            "Спред к кривой ОФЗ, б. п.: наибольший за неделю, шкала "
            f"логарифмическая (серым — ориентир рынка{lost})",
            [(spread, "#24486b", False), (level, "#9a9a9a", False)],
            first,
            last,
            spread_axis,
        ),
        _frame(
            f"Цена, % номинала: наименьшая за неделю, шкала "
            f"{digits(PRICE_LOW, 0)}–{digits(PRICE_HIGH, 0)} у всех карточек "
            f"(пунктиром — граница {digits(below, 0)} %)",
            [(price, "#8a2b2b", False), (edge, "#9a9a9a", True)],
            first,
            last,
            price_axis,
        ),
    )


def as_image(svg: str, alt: str) -> str:
    """Рамка картинкой Markdown: файл карточки остаётся одним файлом.

    Данные внутри самой картинки, ссылок наружу нет: карточка открывается
    без сети, как и страница списка.
    """
    if not svg:
        return ""
    packed = b64encode(svg.encode("utf-8")).decode("ascii")
    return f"![{alt}](data:image/svg+xml;base64,{packed})"
