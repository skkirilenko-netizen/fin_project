"""Рыночный слой: методика, подтверждение и состав оснований.

Три вещи, каждая из которых однажды стоила замера:

- **подтверждение** — без него признак, отсекающий процент рынка в день,
  за два года срабатывает почти у каждого, и мера отвечает на вопрос о длине
  ряда, а не о признаке;
- **цена помимо правила сравнимости** — правило это о доходности, и у бумаги
  с неопределённой доходностью цена определена;
- **одна ступень — одна корзина** — две ступени одной корзины различают то,
  что не различается.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
import yaml

from finlib.scoring.market import (
    findings,
    first_day_when,
    holds_level,
    holds_price,
    holds_widening,
)
from finlib.sources.market import Market, MarketPolicy, Point, load_market


def _policy() -> MarketPolicy:
    """Боевая методика: проверяется она сама, а не её копия."""
    return load_market()


def _market(points: dict[str, list[Point]], level: Decimal = Decimal(100)) -> Market:
    """Ряд из готовых точек с постоянным ориентиром дня."""
    days = {item.day for own in points.values() for item in own}
    return Market(
        benchmark={day: level for day in days},
        issuers={
            inn: {item.day: item for item in own} for inn, own in points.items()
        },
        counted={"строк": len(days)},
        census={},
        universe=len(points),
        with_isin=len(points),
    )


def _row(day: date, spread: Decimal | None, price: Decimal | None) -> Point:
    """Точка ряда: спред бывает пуст при известной цене."""
    return Point(day=day, spread=spread, price=price, weight=Decimal(1))


def test_the_methodology_loads_and_names_its_route_steps() -> None:
    """Ступень маршрута обязана назвать основание, корзину и подгруппу."""
    policy = _policy()
    assert policy.route_steps, "в маршрут не идёт ни одна ступень"
    for step in policy.route_steps:
        assert step.ground and step.basket
        if step.basket == "attention":
            assert step.subgroup and step.escalation is not None


def test_two_steps_of_one_basket_are_refused(tmp_path) -> None:  # noqa: ANN001
    """Две ступени одной корзины — различение, которого нет."""
    raw = yaml.safe_load(_policy_path().read_text(encoding="utf-8"))
    for step in raw["ladder"]["steps"]:
        if step["code"] == "level_p90":
            step.update(
                in_route=True, ground="market_spread_wide_twin",
                basket="attention", subgroup="market", escalation=False,
            )
    path = tmp_path / "market.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="две ступени"):
        load_market(path)


def _policy_path():  # noqa: ANN202
    """Путь к боевой методике: тест правит её копию, а не её саму."""
    from finlib.config import settings

    return settings.methodology_dir / "market.yaml"


def test_confirmation_turns_a_touch_into_a_signal() -> None:
    """«Хотя бы раз» и «7 из 10» отвечают на разные вопросы.

    Ряд, где признак держался один день из сорока, без подтверждения даёт
    срабатывание, с подтверждением — нет. Это и есть та разница, из-за
    которой уровень p99 выглядел срабатывающим у 272 эмитентов из 431.
    """
    first = date(2025, 1, 1)
    points = [
        _row(first + timedelta(days=number), Decimal(50), None) for number in range(40)
    ]
    points[10] = _row(points[10].day, Decimal(5000), None)
    holds = holds_level(_market({"1": points}), Decimal(10))
    assert first_day_when(points, holds, date.max) == points[10].day
    assert first_day_when(points, holds, date.max, 7, 10) is None


def test_price_works_where_the_spread_does_not() -> None:
    """У флоатера доходности к сроку нет, а цена есть — и признак считается."""
    first = date(2025, 1, 1)
    points = [
        _row(first + timedelta(days=number), None, Decimal(45)) for number in range(12)
    ]
    assert first_day_when(points, holds_price(Decimal(60)), date.max) == first
    assert first_day_when(points, holds_level(_market({"1": points}), Decimal(2)),
                          date.max) is None


def test_the_level_and_the_price_are_two_grounds() -> None:
    """Сработавшая ступень называется одна — старшая, и цена отдельно."""
    policy = _policy()
    first = date(2025, 1, 1)
    extreme = next(
        step.multiple for step in policy.route_steps if step.basket == "review"
    )
    points = [
        _row(
            first + timedelta(days=number),
            Decimal(100) * (extreme + 1),
            Decimal(30),
        )
        for number in range(20)
    ]
    market = _market({"7700000000": points})
    said = findings(policy, market, "7700000000", points[-1].day)
    grounds = [item.ground for item in said]
    assert len(grounds) == len(set(grounds)) == 2
    assert {item.basket for item in said} == {"review"}
    # Величина берётся у последнего дня и печатается вместе с ориентиром:
    # «кратность 31» без ориентира дня не говорит ничего.
    level = next(item for item in said if item.ground.endswith("extreme"))
    assert level.benchmark == Decimal(100)
    # **Подтверждение откладывает вывод ровно на то, чем он подтверждается,
    # и берётся оно у самой ступени.** У p99 оно смягчено до «5 из 10»:
    # признак держится с первого дня, а основание возникает на пятом.
    step = next(item for item in policy.route_steps if item.basket == "review")
    rule = step.confirmation or policy.confirmation.default
    assert level.since == first + timedelta(days=rule.of - 1)
    price = next(item for item in said if item.ground.endswith("distress"))
    assert price.since == first, "у цены подтверждения нет намеренно"


def test_a_silent_issuer_gives_no_grounds() -> None:
    """Ряда нет — оснований нет, и это не ноль, а отсутствие ответа."""
    policy = _policy()
    assert findings(policy, _market({}), "7700000000", date(2026, 9, 24)) == ()


def test_widening_is_declared_and_idle() -> None:
    """Недействующее правило живёт в методике вместе с числами замера.

    Удалённое правило неотличимо от забытого. Реализация при этом одна:
    замер зовёт ту же функцию, которая стала бы боевой.
    """
    policy = _policy()
    assert policy.widening["in_route"] is False
    assert policy.widening["measured"]
    assert policy.own_norm["in_route"] is False
    first = date(2025, 1, 1)
    points = [
        _row(first, Decimal(100), None),
        _row(first + timedelta(days=1), Decimal(110), None),
        _row(first + timedelta(days=2), Decimal(120), None),
        _row(first + timedelta(days=3), Decimal(130), None),
        _row(first + timedelta(days=4), Decimal(200), None),
    ]
    holds = holds_widening(Decimal("0.6"), 4, False)
    assert first_day_when(points, holds, date.max) == points[4].day
