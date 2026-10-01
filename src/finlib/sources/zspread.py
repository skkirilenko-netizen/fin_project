"""Z-спред к кривой ОФЗ через денежный поток бумаги. **Только замер, в маршрут не идёт.**

G-спред (`sources.market.build`) — доходность к погашению купонной бумаги
минус бескупонная доходность кривой в точке дюрации; при крутой кривой это
систематическое смещение (`BACKLOG.md`, «G-спред смещён при крутой кривой»).
Z — постоянная надбавка к бескупонной кривой, при которой приведённая
стоимость будущих платежей графика равна грязной цене.

**Допущения названы здесь и печатаются замером**, потому что каждое из них
сдвигает ответ:

- кривая — опубликованные точки `yearyields` биржи, линейная интерполяция
  доходности между ними и плоское продолжение за краями — то же правило, что
  у G (`sources.market.curve_at`); формула по параметрам не воспроизводится;
- компаундинг — параметр: `annual` (точки кривой — эффективная годовая
  доходность, дисконт `(1 + y + z)^-t`) или `continuous` (`exp(-(y + z)·t)`);
  какой верен, решает проверка на самих ОФЗ;
- время — Act/365 от дня расчётов, расчёты — следующий рабочий день после
  торгов (Т+1, праздники не учитываются);
- цена — средневзвешенная, если доходность взята средневзвешенная, иначе
  закрытия; грязная цена — процент от непогашенного номинала биржи плюс НКД
  биржи;
- поток — график Cbonds на одну бумагу; бумага с офертой обрезается датой
  выкупа биржи (`BUYBACKDATE`, иначе `OFFERDATE`) с выкупом остатка номинала
  по цене оферты Cbonds (нет записи — 100 %);
- будущий купон не объявлен — Z не определён (как G у флоатера);
- погашения графика не сходятся с номиналом биржи больше чем на допуск —
  Z не определён: поток не тот, что у бумаги.

**Денежные величины и спред — `Decimal`** (инвариант 5): метод Ньютона
сходится за 4–8 шагов против 40 у деления пополам.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

ONE = Decimal(1)
DAYS_IN_YEAR = Decimal(365)
# Допуск сходимости графика с номиналом биржи — технический параметр разбора:
# доля номинала, на которую погашения графика могут расходиться с биржей.
NOMINAL_TOLERANCE = Decimal("0.005")
# Сходимость Ньютона — в долях единицы ставки (0,01 б. п.) и предел шагов.
TOLERANCE = Decimal("0.000001")
STEPS = 30
PRECISION = 20


@dataclass(frozen=True, slots=True)
class Flow:
    """Платёж графика на одну бумагу: срок, купон и погашение; купон None — не объявлен."""

    due: date
    coupon: Decimal | None
    redemption: Decimal


def _amount(value: object) -> Decimal | None:
    """Величина источника; пустое — None, а не ноль: «не объявлен» ≠ «нет»."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def read_flows(path: Path) -> tuple[list[Flow], Decimal | None]:
    """График выпуска с диска и номинал выпуска."""
    items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    flows: list[Flow] = []
    nominal: Decimal | None = None
    for item in items:
        try:
            due = date.fromisoformat(str(item.get("date") or "")[:10])
        except ValueError:
            continue
        if nominal is None:
            nominal = _amount(item.get("emission_nominal_price"))
        flows.append(
            Flow(
                due=due,
                coupon=_amount(item.get("cupon_sum")),
                redemption=_amount(item.get("redemtion")) or Decimal(0),
            )
        )
    return sorted(flows, key=lambda item: item.due), nominal


def read_offers(path: Path) -> dict[date, Decimal]:
    """Оферты выпуска: дата → цена выкупа в процентах номинала."""
    if not path.exists():
        return {}
    found: dict[date, Decimal] = {}
    for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        try:
            when = date.fromisoformat(str(item.get("date") or "")[:10])
        except ValueError:
            continue
        price = _amount(item.get("price"))
        found[when] = price if price is not None and price > 0 else Decimal(100)
    return found


def settlement(trade: date) -> date:
    """День расчётов: следующий рабочий день после торгов (Т+1, без праздников)."""
    day = trade + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def offer_price(offers: dict[date, Decimal], when: date) -> Decimal:
    """Цена выкупа оферты, ближайшей к дате биржи в пределах недели; нет — 100 %."""
    near = [day for day in offers if abs((day - when).days) <= 7]
    if not near:
        return Decimal(100)
    return offers[min(near, key=lambda day: abs((day - when).days))]


def cash_flow(
    flows: list[Flow],
    settle: date,
    face: Decimal,
    cut: date | None,
    cut_price: Decimal,
) -> tuple[list[tuple[Decimal, Decimal]], str]:
    """Будущий поток парами «годы, сумма» и причина отказа; пусто — поток есть.

    `face` — непогашенный номинал биржи на одну бумагу; с ним сверяются
    будущие погашения графика. `cut` — дата выкупа по оферте: платежи
    после неё не идут, остаток номинала выкупается по `cut_price`.
    """
    ahead = [item for item in flows if item.due > settle]
    if cut is not None:
        if cut <= settle:
            return [], "оферта не позже дня расчётов"
        ahead = [item for item in ahead if item.due <= cut]
    if not ahead and cut is None:
        return [], "будущих платежей в графике нет"
    if any(item.coupon is None for item in ahead):
        return [], "будущий купон не объявлен"
    redeemed = sum((item.redemption for item in ahead), Decimal(0))
    pairs: list[tuple[Decimal, Decimal]] = [
        (Decimal((item.due - settle).days) / DAYS_IN_YEAR, item.coupon + item.redemption)  # type: ignore[operator]
        for item in ahead
    ]
    if cut is not None:
        rest = face - redeemed
        if rest < 0:
            return [], "погашения графика больше номинала биржи"
        if rest > 0:
            pairs.append(
                (Decimal((cut - settle).days) / DAYS_IN_YEAR, rest * cut_price / 100)
            )
    elif abs(redeemed - face) > face * NOMINAL_TOLERANCE:
        return [], "погашения графика не сходятся с номиналом биржи"
    return [(years, amount) for years, amount in pairs if amount != 0], ""


def present_value(
    pairs: list[tuple[Decimal, Decimal]], rates: list[Decimal]
) -> Decimal | None:
    """Приведённая стоимость потока по самой кривой, без надбавки; годовой компаундинг."""
    if not pairs:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        total = Decimal(0)
        for (years, amount), rate in zip(pairs, rates, strict=True):
            level = ONE + rate / 100
            if level <= 0:
                return None
            total += amount * (-years * level.ln()).exp()
        return total


def z_spread(
    pairs: list[tuple[Decimal, Decimal]],
    rates: list[Decimal],
    price: Decimal,
    compounding: str = "annual",
) -> Decimal | None:
    """Z-спред в б. п.: надбавка к ставкам кривой, сводящая поток к цене; None — не сошлось.

    `rates` — доходность кривой в процентах в сроке каждого платежа.
    """
    if not pairs or price <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = PRECISION
        base = [rate / 100 for rate in rates]
        z = Decimal(0)
        for _ in range(STEPS):
            value = -price
            slope = Decimal(0)
            for (years, amount), rate in zip(pairs, base, strict=True):
                if compounding == "continuous":
                    factor = (-(rate + z) * years).exp()
                    value += amount * factor
                    slope -= years * amount * factor
                else:
                    level = ONE + rate + z
                    if level <= 0:
                        return None
                    factor = (-years * level.ln()).exp()
                    value += amount * factor
                    slope -= years * amount * factor / level
            if slope == 0:
                return None
            step = value / slope
            z -= step
            if abs(step) < TOLERANCE:
                return z * 10000
    return None
