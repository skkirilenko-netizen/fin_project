"""Рыночные признаки маршрута: уровень спреда и цена бумаги.

**Два признака, и оба измерены** (замер `eval/market_lead_run.py`, решение
владельца 24.09.2026). Кратность спреда к ориентиру на девяносто девятом
перцентиле с подтверждением «7 из 10» — прирост 4,9× при упреждении 79 дней;
цена ниже 60 % номинала — прирост 6,2× при упреждении 43 дней и полной
выявляемости. Первый отбирает раньше, второй точнее, и в «Разбор» идут оба:
79 дней — время что-то сделать, 43 — время зафиксировать.

**Подтверждение — условие осмысленности уровня, а не украшение.** Порог,
отсекающий процент рынка в день, за два года срабатывает почти у каждого:
мера «сработал хотя бы раз» отвечает на вопрос о длине ряда. У цены
подтверждения нет намеренно — оно снимает не шум, а сам срок упреждения:
десять наблюдений подряд у торгуемой бумаги укладываются в две недели,
и упреждение падает с 43 дней до 10.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median

from finlib.metrics.display import digits
from finlib.sources.market import Market, MarketPolicy, Point, Step

logger = logging.getLogger(__name__)

# Признак дня: держится ли он в этой точке ряда.
Holds = Callable[[list[Point], int], bool]


@dataclass(frozen=True, slots=True)
class MarketFinding:
    """Сработавший рыночный признак: чем, у какой величины и с какого дня.

    **День первого срабатывания хранится вместе с признаком.** Рыночная
    величина меняется ежедневно, и «цена ниже 60 %» без даты не отличает
    вчерашнее падение от полугодового состояния.
    """

    ground: str
    basket: str
    subgroup: str
    escalation: bool
    value: Decimal
    threshold: Decimal
    since: date
    benchmark: Decimal | None = None

    def slots(self, policy: MarketPolicy) -> dict[str, str]:
        """Величины основания так, как они печатаются читателю.

        **Разрядность объявлена методикой** (`market.yaml`, блок `display`),
        и печатает их единая точка округления: второй способ печати тех же
        величин разошёлся бы с первым в первой же строке списка.
        """
        spread = int(policy.display["spread_scale"])
        ratio = int(policy.display["multiple_scale"])
        price = int(policy.display["price_scale"])
        said = {
            "value": digits(self.value, spread if self.benchmark else price),
            "threshold": digits(self.threshold, ratio if self.benchmark else price),
            "since": f"{self.since:%d.%m.%Y}",
            "benchmark": "",
            "multiple": "",
        }
        if self.benchmark:
            said["benchmark"] = digits(self.benchmark, spread)
            said["multiple"] = digits(self.value / self.benchmark, ratio)
        return said


def holds_level(market: Market, multiple: Decimal) -> Holds:
    """Признак дня: кратность спреда к ориентиру не ниже названной."""

    def holds(points: list[Point], number: int) -> bool:
        item = points[number]
        level = market.benchmark.get(item.day)
        if item.spread is None or level is None or level <= 0:
            return False
        return item.spread / level >= multiple

    return holds


def holds_price(below: Decimal) -> Holds:
    """Признак дня: цена ушла ниже границы зоны дефолта."""

    def holds(points: list[Point], number: int) -> bool:
        price = points[number].price
        return price is not None and price < below

    return holds


def holds_widening(growth: Decimal, back: int, calendar: bool) -> Holds:
    """Признак дня: спред вырос на долю от прежнего наблюдения.

    **В маршрут не идёт** (`market.yaml`, `widening.in_route: false`), и живёт
    здесь по тому же правилу, по которому остаётся в справочнике: удалённое
    не отличить от забытого, а замер зовёт ту же реализацию, которая стала бы
    боевой, если правило когда-нибудь объявят действующим.

    Прежнее наблюдение берётся либо по их числу, либо по календарю: у
    неликвидной бумаги четыре наблюдения растягиваются на месяцы, и два
    способа отвечают на разные вопросы. Отрицательный прежний спред сравнением
    не годится — рост от −20 до +100 в долях не выражается.
    """

    def holds(points: list[Point], number: int) -> bool:
        now = points[number].spread
        if now is None:
            return False
        seen = [item for item in points[:number] if item.spread is not None]
        if calendar:
            edge = points[number].day - timedelta(days=back)
            seen = [item for item in seen if item.day <= edge]
            if not seen:
                return False
            was = seen[-1].spread
        else:
            if len(seen) < back:
                return False
            was = seen[-back].spread
        return was is not None and was > 0 and (now - was) / was >= growth

    return holds


def holds_own_norm(multiple: Decimal, window: int, least: int) -> Holds:
    """Признак дня: спред выше собственной нормы эмитента кратностью.

    Норма — медиана спреда за прошедшие дни у него же. Признак отвечает
    на вопрос «дорого **для него**», а не «дорого вообще»: у бумаги, всегда
    стоявшей втрое дороже рынка, кратность к ориентиру говорит об отрасли
    и размере, а не о перемене. **В маршрут не идёт** — замерено 24.09.2026:
    прирост 2,3× против 4,9× у уровня p99.
    """

    def holds(points: list[Point], number: int) -> bool:
        now = points[number].spread
        if now is None:
            return False
        edge = points[number].day - timedelta(days=window)
        past = [
            item.spread
            for item in points[:number]
            if item.day >= edge and item.spread is not None
        ]
        if len(past) < least:
            return False
        norm = median(past)
        return norm > 0 and now / norm >= multiple

    return holds


def first_day_when(
    points: list[Point],
    holds: Holds,
    until: date,
    of: int = 1,
    out_of: int = 1,
) -> date | None:
    """Первый день, когда признак держался в K точках из последних N.

    **Без подтверждения «сработал хотя бы раз» насыщается.** У эмитента
    пятьсот наблюдений за два года, и порог, отсекающий процент рынка в день,
    срабатывает почти у каждого: та же ступень p99 даёт 70 эмитентов
    с подтверждением и 272 без него. Мера «хотя бы раз» отвечает не на вопрос
    о признаке, а на вопрос о длине ряда; `of=1, out_of=1` означает «без
    подтверждения» и остаётся правом вызывающего, а не умолчанием методики.
    """
    seen: list[bool] = []
    for number, item in enumerate(points):
        if item.day > until:
            return None
        seen.append(bool(holds(points, number)))
        window = seen[-out_of:]
        if len(window) >= of and sum(window) >= of:
            return item.day
    return None


def _ordered(market: Market, inn: str, until: date) -> list[Point]:
    """Ряд эмитента по возрастанию дня, не позже названного."""
    return [item for item in market.ordered(inn) if item.day <= until]


def _level_finding(
    step: Step, market: Market, points: list[Point], today: date, of: int, out_of: int
) -> MarketFinding | None:
    """Сработавшая ступень лестницы: величина берётся у последнего дня."""
    since = first_day_when(
        points, holds_level(market, step.multiple), today, of, out_of
    )
    if since is None:
        return None
    last = points[-1]
    level = market.benchmark.get(last.day)
    if last.spread is None or level is None or level <= 0:
        # Признак держался раньше, а последний день спреда не даёт: величину
        # брать неоткуда, и печатать её было бы выдумкой.
        return None
    return MarketFinding(
        ground=step.ground,
        basket=step.basket,
        subgroup=step.subgroup,
        escalation=bool(step.escalation),
        # **Величина здесь спред, а не кратность.** Кратность — отношение
        # к ориентиру дня, и печатается она вместе с ним: «31×» без «при
        # ориентире 310 б. п.» не говорит ничего, потому что уровень рынка
        # двигается.
        value=last.spread,
        threshold=step.multiple,
        since=since,
        benchmark=level,
    )


def findings(
    policy: MarketPolicy,
    market: Market,
    inn: str,
    today: date,
    systemic: bool = False,
) -> tuple[MarketFinding, ...]:
    """Рыночные основания эмитента на дату: ступень лестницы и цена.

    **Ступень называется одна — старшая.** Лестница упорядочена по перцентилю,
    и две ступени одного эмитента говорили бы об одном обстоятельстве дважды:
    тот же запрет, что у величины при сработавшем стоп-факторе.

    У системно значимого эмитента подтверждение длиннее (`confirmation.
    systemic`): цена ложной тревоги там выше, и это решение владельца.
    """
    points = _ordered(market, inn, today)
    if not points:
        return ()
    rule = policy.confirmation.systemic if systemic else policy.confirmation.default
    of, out_of = (
        (rule.of, rule.out_of) if policy.ladder.requires_confirmation else (1, 1)
    )
    found: list[MarketFinding] = []
    for step in sorted(
        policy.route_steps, key=lambda item: item.percentile, reverse=True
    ):
        said = _level_finding(step, market, points, today, of, out_of)
        if said is not None:
            found.append(said)
            break
    zone = policy.distress_zone
    if zone.in_route:
        below = zone.price_below_percent
        since = first_day_when(points, holds_price(below), today)
        price = points[-1].price
        if since is not None and price is not None:
            found.append(
                MarketFinding(
                    ground=zone.ground,
                    basket=zone.basket,
                    subgroup="",
                    escalation=False,
                    value=price,
                    threshold=below,
                    since=since,
                )
            )
    return tuple(found)
