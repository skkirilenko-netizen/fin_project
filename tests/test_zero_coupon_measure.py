"""Перезамер zero_coupon при цене к PV: вариант без правила и его сверка с действующим рядом."""

import sys
from datetime import date
from decimal import Decimal

import pytest

from finlib.config import settings
from finlib.sources.market import Market, Point, load_market

sys.path.insert(0, str(settings.base_dir / "eval"))

import zero_coupon_measure as measure  # noqa: E402

DAY = date(2026, 3, 2)
NEXT = date(2026, 3, 3)


def _point(day: date, spread: str | None, price: str | None) -> Point:
    """Точка ряда: спред и цена."""
    return Point(
        day=day,
        spread=Decimal(spread) if spread else None,
        price=Decimal(price) if price else None,
        weight=Decimal(1),
    )


def _market(issuers: dict) -> Market:
    """Ряд с постоянным ориентиром на два дня."""
    return Market(
        benchmark={DAY: Decimal(100), NEXT: Decimal(100)}, issuers=issuers,
        counted={}, census={}, universe=len(issuers), with_isin=len(issuers), ratios=True,
    )


def test_the_variant_drops_exactly_one_rule() -> None:
    """Вариант — та же методика без одного правила; неизвестное правило — отказ."""
    policy = load_market()
    variant = measure.without_rule(policy, measure.RULE)
    codes = [item["code"] for item in policy.comparability["exclude"]]
    left = [item["code"] for item in variant.comparability["exclude"]]
    assert measure.RULE in codes and measure.RULE not in left
    assert len(left) == len(codes) - 1
    # Прочие разделы сравнимости не тронуты.
    assert {k: v for k, v in variant.comparability.items() if k != "exclude"} == {
        k: v for k, v in policy.comparability.items() if k != "exclude"
    }
    with pytest.raises(ValueError, match="нет"):
        measure.without_rule(policy, "нет_такого_правила")


def test_the_variant_may_only_add_prices() -> None:
    """Совпали ориентир и спреды — сверка считает добавленное; иначе отказ."""
    current = _market({"1": {DAY: _point(DAY, "300", None)}})
    variant = _market({
        "1": {DAY: _point(DAY, "300", "55"), NEXT: _point(NEXT, None, "54")},
        "2": {DAY: _point(DAY, None, "50")},
    })
    assert measure.verify_variant(current, variant) == (1, 2)
    spread_moved = _market({"1": {DAY: _point(DAY, "301", "55")}})
    with pytest.raises(ValueError, match="спред"):
        measure.verify_variant(current, spread_moved)
    lost = _market({})
    with pytest.raises(ValueError, match="пропали эмитенты"):
        measure.verify_variant(current, lost)
