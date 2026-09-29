"""Рыночные признаки маршрута: уровень спреда и цена бумаги.

**Два признака «Разбора», и оба измерены** (замер `eval/market_lead_run.py`,
решение владельца 24.09.2026; числа — в `market.yaml`). Кратность спреда
к ориентиру на девяносто девятом перцентиле с подтверждением «5 из 10» —
прирост 3,6× при упреждении 139 дней; цена ниже 60 % номинала — прирост
6,6× при упреждении 43 дней. Первый отбирает раньше, второй точнее, и
в «Разбор» идут оба: 139 дней — время что-то сделать, 43 — время
зафиксировать. Третий — кратность на p95 — даёт «Внимание».

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
    # Пол ориентира, если кратность посчитана к нему, а не к ориентиру дня.
    floor: Decimal | None = None
    # Наименьшая цена срока жизни и её день — у ценового основания, когда
    # последняя цена уже выше границы, а основание ещё стоит.
    low: Decimal | None = None
    low_day: date | None = None

    @property
    def variant(self) -> str:
        """Какая формулировка основания называет этот случай; пусто — обычная."""
        if self.floor is not None:
            return "floored"
        if self.low is not None and self.value >= self.threshold:
            return "recovered"
        return ""

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
            "floor": "",
            "low": "",
            "low_day": "",
        }
        if self.benchmark:
            said["benchmark"] = digits(self.benchmark, spread)
            # Кратность делится на то же, на что её делил признак: на пол,
            # когда ориентир дня ниже него.
            said["multiple"] = digits(self.value / (self.floor or self.benchmark), ratio)
        if self.floor is not None:
            said["floor"] = digits(self.floor, spread)
        if self.low is not None and self.low_day is not None:
            said["low"] = digits(self.low, price)
            said["low_day"] = f"{self.low_day:%d.%m.%Y}"
        # **Упреждение, которое формулировка называет, берётся из замера,
        # а не пишется строкой** (решение владельца 25.09.2026): «43 дня»
        # в тексте основания пережили перемер, давший 61, и читатель получал
        # число, которого замер больше не показывает.
        said["lead_days"] = ""
        if not self.benchmark:
            zone = policy.distress_zone
            measured = zone.measured.get(f"at_{zone.price_below_percent:.0f}")
            if measured is not None:
                said["lead_days"] = f"{measured['lead_days']:.0f}"
        return said


def holds_level(
    market: Market, multiple: Decimal, floor: Decimal | None = None
) -> Holds:
    """Признак дня: кратность спреда к ориентиру не ниже названной.

    **Ориентир берётся не ниже пола** (`market.yaml`, `benchmark.floor_bp`):
    кратность к ориентиру около нуля смысла не имеет, а осенью 2024 года
    ориентир стоял на 74–78 б. п. и бывал отрицательным. Без пола — как
    прежде: день с неположительным ориентиром признака не даёт.
    """

    def holds(points: list[Point], number: int) -> bool:
        item = points[number]
        level = market.benchmark.get(item.day)
        if item.spread is None or level is None:
            return False
        base = level if floor is None else max(level, floor)
        if base <= 0:
            return False
        return item.spread / base >= multiple

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
    с кратностью 3× прирост 2,1× против 3,6× у уровня p99.
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


def confirmed_days(
    points: list[Point],
    holds: Holds,
    until: date,
    of: int = 1,
    out_of: int = 1,
) -> list[date]:
    """Все дни, когда признак держался в K точках из последних N.

    То же правило, что у `first_day_when`, только дни перечисляются все:
    срок жизни основания спрашивает о последнем подтверждении, а не о первом.
    """
    seen: list[bool] = []
    found: list[date] = []
    for number, item in enumerate(points):
        if item.day > until:
            break
        seen.append(bool(holds(points, number)))
        window = seen[-out_of:]
        if len(window) >= of and sum(window) >= of:
            found.append(item.day)
    return found


def standing_since(
    market: Market, days: list[date], today: date, lifetime: int | None
) -> date | None:
    """Начало нынешнего стояния основания; None — основание не стоит.

    **Основание стоит, пока подтверждено в последние `lifetime` торговых
    дней** (решение владельца 29.09.2026): мера «сработал хоть раз за ряд»
    мерила длину ряда — у 48 из 65 держателей p99 основание держалось
    с осени 2024 года. Стояние непрерывно, пока между подтверждениями
    не больше срока; начало — первое подтверждение этой непрерывной полосы,
    а не ряда. `None` вместо срока — прежняя мера, право вызывающего замера.
    """
    if not days:
        return None
    if lifetime is None:
        return days[0]
    last = market.day_number(days[-1])
    if market.day_number(today) - last >= lifetime:
        return None
    start = days[-1]
    for day in reversed(days[:-1]):
        if market.day_number(start) - market.day_number(day) > lifetime:
            break
        start = day
    return start


def _ordered(market: Market, inn: str, until: date) -> list[Point]:
    """Ряд эмитента по возрастанию дня, не позже названного."""
    return [item for item in market.ordered(inn) if item.day <= until]


def _lifetime(policy: MarketPolicy, code: str) -> int | None:
    """Срок жизни основания в торговых днях; None — срок к нему не относится."""
    rule = policy.lifetime
    return rule.trading_days if code in rule.applies_to else None


def _level_finding(
    policy: MarketPolicy,
    step: Step,
    market: Market,
    points: list[Point],
    today: date,
    of: int,
    out_of: int,
) -> MarketFinding | None:
    """Сработавшая ступень лестницы: величина берётся у последнего дня."""
    floor = policy.floor
    days = confirmed_days(
        points, holds_level(market, step.multiple, floor), today, of, out_of
    )
    since = standing_since(market, days, today, _lifetime(policy, step.code))
    if since is None:
        return None
    last = points[-1]
    level = market.benchmark.get(last.day)
    base = None if level is None else (level if floor is None else max(level, floor))
    if last.spread is None or base is None or base <= 0:
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
        floor=floor if floor is not None and level < floor else None,
    )


def _price_finding(
    policy: MarketPolicy, market: Market, points: list[Point], today: date
) -> MarketFinding | None:
    """Ценовое основание: цена ниже границы в пределах срока жизни.

    **Правило срока объявлено методикой** (`lifetime.price_rule`): `any_within`
    — цена была ниже границы хоть раз за срок, `last` — последняя цена ниже
    границы и не старше срока. Последняя цена у бумаги около 60 % пересекает
    границу туда и обратно, и основание по ней входило бы в корзину 2,7 раза
    за год на эмитента против 1,2.
    """
    zone = policy.distress_zone
    below = zone.price_below_percent
    priced = [item for item in points if item.price is not None]
    if not priced:
        return None
    hits = confirmed_days(points, holds_price(below), today)
    lifetime = _lifetime(policy, "price_distress")
    since = standing_since(market, hits, today, lifetime)
    if since is None:
        return None
    last = priced[-1]
    low: Point | None = None
    if lifetime is not None:
        if policy.lifetime.price_rule == "last":
            if last.price is None or last.price >= below:
                return None
        else:
            # Наименьшая цена срока называется, когда последняя уже выше
            # границы: «цена 63 %, ниже 60 % с 01.09» читалось бы противоречием.
            edge = market.day_number(today) - lifetime
            inside = [
                item
                for item in priced
                if market.day_number(item.day) > edge and item.price is not None
            ]
            low = min(inside, key=lambda item: (item.price, item.day), default=None)
    elif points[-1].price is None:
        # Прежняя мера: цена последнего дня обязательна.
        return None
    assert last.price is not None
    return MarketFinding(
        ground=zone.ground,
        basket=zone.basket,
        subgroup="",
        escalation=False,
        value=last.price,
        threshold=below,
        since=since,
        low=low.price if low is not None and last.price >= below else None,
        low_day=low.day if low is not None and last.price >= below else None,
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

    **Подтверждение бывает своим у ступени** и тогда берётся у неё: у p99 оно
    смягчено до «5 из 10», потому что размен измерен — выявляемость с 69 %
    до 81 %, упреждение с 79 до 138 дней при приросте с 4,7× до 3,6×.
    Системное правило старше: у системно значимого эмитента цена ложной
    тревоги выше, и мягкая ступень ему послаблением служить не должна.
    """
    points = _ordered(market, inn, today)
    if not points:
        return ()
    found: list[MarketFinding] = []
    for step in sorted(
        policy.route_steps, key=lambda item: item.percentile, reverse=True
    ):
        rule = (
            policy.confirmation.systemic
            if systemic
            else (step.confirmation or policy.confirmation.default)
        )
        of, out_of = (
            (rule.of, rule.out_of) if policy.ladder.requires_confirmation else (1, 1)
        )
        said = _level_finding(policy, step, market, points, today, of, out_of)
        if said is not None:
            found.append(said)
            break
    if policy.distress_zone.in_route:
        price = _price_finding(policy, market, points, today)
        if price is not None:
            found.append(price)
    return tuple(found)
