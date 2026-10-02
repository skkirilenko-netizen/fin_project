"""Корзины маршрута при подменённом рынке: общий инструмент ночных замеров.

**Замер не считает сам**: корзину называет `routing_rows`, подменяются
только ряд рынка и его методика (`routing_store.load_market`,
`routing_store._market_series`) — на время вызова и с возвратом прежних.
БД только читается, транзакция откатывается.
"""

from collections import Counter
from contextlib import contextmanager
from datetime import date

from finlib.db import connection
from finlib.scoring import routing_store
from finlib.sources.market import Market


def monthly_moments(market: Market, start: date = date(2025, 10, 1)) -> list[date]:
    """Первые торговые дни месяцев с `start` и последний день ряда."""
    firsts: dict[tuple[int, int], date] = {}
    for day in market.calendar():
        if day >= start:
            firsts.setdefault((day.year, day.month), day)
    return sorted({max(market.benchmark), *firsts.values()})


@contextmanager
def patched(policy, market: Market):  # noqa: ANN001, ANN201
    """Подмена рынка маршрута на время блока."""
    keep = (routing_store.load_market, routing_store._market_series)
    routing_store.load_market = lambda: policy
    routing_store._market_series = lambda: market
    try:
        yield
    finally:
        routing_store.load_market, routing_store._market_series = keep


def baskets(
    moments: list[date], memo: dict
) -> dict[date, dict[str, tuple[str, tuple[str, ...]]]]:
    """Корзина и основания каждого эмитента на даты."""
    found: dict[date, dict[str, tuple[str, tuple[str, ...]]]] = {}
    with connection() as conn:
        for moment in moments:
            rows, _ = routing_store.routing_rows(conn, moment, as_of=moment, memo=memo)
            found[moment] = {
                row.inn: (
                    row.verdict.basket,
                    tuple(sorted({item.ground for item in row.verdict.findings})),
                )
                for row in rows
            }
        conn.rollback()
    return found


def changes(before: dict, after: dict) -> list[tuple[date, str, str, str]]:
    """Смены корзины: дата, ИНН, было, стало."""
    return [
        (moment, inn, before[moment].get(inn, ("—",))[0], said[0])
        for moment, by_inn in after.items()
        for inn, said in by_inn.items()
        if said[0] != before[moment].get(inn, ("—",))[0]
    ]


def calendar(name: str, said: dict, when: dict[str, date], last_day: date) -> str:
    """Поточечная мера корзины на календаре событий: «Разбор» и «Разбор или Внимание».

    Срез — дата из `said`, у которой весь горизонт уже наблюдён (не позже
    `last_day` минус горизонт); стоит — корзина на срезе; событие — первое
    неисполненное в горизонте после среза. Мера та же, что у рынка
    (`market_lead_run.pointwise`): с интервалами по эмитентам.
    """
    from datetime import timedelta

    from market_lead_run import HORIZON, POINTWISE_HEAD, pointwise

    cuts = sorted(day for day in said if day + timedelta(days=HORIZON) <= last_day)
    circle = set().union(*(set(by_inn) for by_inn in said.values())) if said else set()
    lines = [POINTWISE_HEAD]
    for label, chosen in (
        (f"{name}: «Разбор»", {"review"}),
        (f"{name}: «Разбор» или «Внимание»", {"review", "attention"}),
    ):
        lines.append(
            pointwise(
                label,
                lambda inn, day, chosen=chosen: said[day].get(inn, ("",))[0] in chosen,
                lambda inn, day: inn in said[day],
                circle,
                when,
                cuts,
            ).row()
        )
    return "\n".join(lines)


def summary(found: list[tuple[date, str, str, str]], moments: list[date]) -> str:
    """Сводка смен: всего, по датам и по переходам."""
    per_day = Counter(moment for moment, *_ in found)
    turns = Counter((was, now) for _, _, was, now in found)
    days = ", ".join(f"{moment:%d.%m.%Y} {per_day.get(moment, 0)}" for moment in moments)
    pairs = ", ".join(f"{was} → {now}: {count}" for (was, now), count in turns.most_common())
    return (
        f"Смен корзины {len(found)} у {len({inn for _, inn, *_ in found})} эмитентов "
        f"на {len(moments)} датах. По датам: {days}. Переходы: {pairs or 'нет'}."
    )
