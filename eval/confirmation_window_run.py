"""Окно подтверждения «5 из 10»: как сейчас против «только дни со спредом». Только диск и чтение БД.

    uv run python eval/confirmation_window_run.py > отчёт.md

Сейчас (`scoring.market.confirmed_days`) точка дня с ценой без спреда
входит в окно «K из N» как несработавшая. Вариант — окно только по дням
со спредом: день без спреда — нет наблюдения, как в правиле срока жизни
(ce9cff2). **Замер не считает сам**: вариант подменяет у ступени ряд на
дни со спредом и зовёт тот же `_level_finding`; цена не затронута.
Мера — поточечная `market_lead_run` на календаре событий и корзины
`routing_rows` на первых торговых днях месяцев.
"""

import contextlib
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402
from market_lead_run import _market_pointwise, events  # noqa: E402

from finlib.scoring import market as scoring_market  # noqa: E402
from finlib.sources.market import load_market, series  # noqa: E402

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def spread_days_only():  # noqa: ANN201
    """Вариант: окно ступени считается только по дням со спредом."""
    original = scoring_market._level_finding

    def level(policy, step, market, points, today, of, out_of):  # noqa: ANN001, ANN202
        own = [item for item in points if item.spread is not None]
        if not own:
            return None
        return original(policy, step, market, own, today, of, out_of)

    scoring_market._level_finding = level
    try:
        yield
    finally:
        scoring_market._level_finding = original


def main() -> int:
    """Печатает обе меры для двух вариантов окна."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    policy = load_market()
    market = series()
    when = events()
    print("# Окно подтверждения «5 из 10»: все дни ряда против дней со спредом\n")
    variants = (
        ("как сейчас: все дни ряда", contextlib.nullcontext),
        ("только дни со спредом", spread_days_only),
    )
    moments = route_variants.monthly_moments(market)
    memo: dict = {}
    said: dict[str, dict] = {}
    for name, around in variants:
        buffer = io.StringIO()
        with around(), contextlib.redirect_stdout(buffer):
            _market_pointwise(policy, market, when)
        print(f"## Календарь событий: {name}\n")
        print(buffer.getvalue().replace("## Основная мера: поточечно", "").strip() + "\n")
        with around():
            said[name] = route_variants.baskets(moments, memo)
    first, second = (name for name, _ in variants)
    found = route_variants.changes(said[first], said[second])
    print("## Смены корзины маршрута против нынешнего окна\n")
    print(route_variants.summary(found, moments) + "\n")
    for moment, inn, was, now in found:
        print(f"- {moment:%d.%m.%Y} {inn}: {was} → {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
