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
from base64 import b64encode
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


def _y(value: Decimal, low: Decimal, high: Decimal) -> float:
    """Координата величины; ряд без размаха рисуется по середине рамки."""
    span = high - low
    inner = HEIGHT - PAD_TOP - PAD_BOTTOM
    if span <= 0:
        return PAD_TOP + inner / 2
    return PAD_TOP + inner * float((high - value) / span)


def _line(
    points: list[tuple[date, Decimal]],
    first: date,
    last: date,
    low: Decimal,
    high: Decimal,
    colour: str,
    dashed: bool = False,
) -> str:
    """Ломаная по точкам ряда; пустой ряд линии не даёт вовсе."""
    if not points:
        return ""
    spots = " ".join(
        f"{_x(day, first, last):.0f},{_y(value, low, high):.0f}"
        for day, value in points
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
    scale: int,
) -> str:
    """Одна рамка: подпись, ось значений по краям и ломаные внутри.

    Подписываются **только края шкалы и края времени**: сетка с десятком
    подписей на графике величиной в две строки читается хуже, чем без неё,
    а край шкалы отвечает на вопрос «в каких пределах это двигалось».
    """
    values = [value for series, _, _ in lines for _, value in series]
    if not values:
        return ""
    low, high = min(values), max(values)
    drawn = "".join(
        _line(series, first, last, low, high, colour, dashed)
        for series, colour, dashed in lines
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="system-ui, sans-serif" font-size="11">'
        f'<rect x="{PAD_LEFT}" y="{PAD_TOP}" '
        f'width="{WIDTH - PAD_LEFT - PAD_RIGHT}" '
        f'height="{HEIGHT - PAD_TOP - PAD_BOTTOM}" fill="none" '
        f'stroke="#d8d8d8"/>'
        f'<text x="{PAD_LEFT}" y="10" fill="#333">{title}</text>'
        f'<text x="{PAD_LEFT - 6}" y="{PAD_TOP + 4}" text-anchor="end" '
        f'fill="#666">{digits(high, scale)}</text>'
        f'<text x="{PAD_LEFT - 6}" y="{HEIGHT - PAD_BOTTOM}" '
        f'text-anchor="end" fill="#666">{digits(low, scale)}</text>'
        f'<text x="{PAD_LEFT}" y="{HEIGHT - 6}" fill="#666">'
        f"{first:%d.%m.%Y}</text>"
        f'<text x="{WIDTH - PAD_RIGHT}" y="{HEIGHT - 6}" text-anchor="end" '
        f'fill="#666">{last:%d.%m.%Y}</text>'
        f"{drawn}</svg>"
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
    return (
        _frame(
            "Спред к кривой ОФЗ, б. п.: наибольший за неделю "
            "(серым — ориентир рынка)",
            [(spread, "#24486b", False), (level, "#9a9a9a", False)],
            first,
            last,
            int(policy.display["spread_scale"]),
        ),
        _frame(
            f"Цена, % номинала: наименьшая за неделю "
            f"(пунктиром — граница {digits(below, 0)} %)",
            [(price, "#8a2b2b", False), (edge, "#9a9a9a", True)],
            first,
            last,
            int(policy.display["price_scale"]),
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
