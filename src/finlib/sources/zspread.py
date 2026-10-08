"""Z-спред и отношение цены к PV по КБД через денежный поток бумаги.

**Z — только замер, в маршрут не идёт.** Отношение цены к PV идёт в ценовой
признак лишь при `distress_zone.measure: pv_kbd` (`market.yaml`).

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, localcontext
from functools import cache
from pathlib import Path

from finlib.sources import cbonds

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


# --- поток бумаги по строке среза биржи --------------------------------------
#
# **Одна реализация на замер и маршрут.** Сопоставление кода торгов с выпуском,
# чтение графика и оферт, обрезка потока офертой и грязная цена жили в замере
# Z-спреда (`eval/zspread_rows.py`); ценовой признак «цена / PV по КБД» берёт
# их отсюда же, и замер с маршрутом не могут разойтись в потоке.


def emission_map() -> tuple[dict[str, str], dict[str, str]]:
    """Код торгов → выпуск Cbonds и выпуск → тип эмитента источника.

    Источники соответствия — все сохранённые ответы о выпусках: перечень
    выпусков в обращении, выпуски эмитентов справочника и поиск бумаг ядра
    по ISIN и номеру регистрации (`scripts/core_flows_fetch.py`). У ОФЗ код
    торгов не ISIN: соответствие — по номеру регистрации в имени файла.
    """
    by_code: dict[str, str] = {}
    kind: dict[str, str] = {}
    files = sorted(cbonds.CACHE.glob("emissions_*.json"))
    files += sorted(cbonds.CACHE.glob("emission_isin_*.json"))
    files += sorted(cbonds.CACHE.glob("emission_regnum_*.json"))
    for path in files:
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in found.get("items", []) if isinstance(found, dict) else []:
            emission = str(item.get("id") or "")
            if not emission:
                continue
            kind[emission] = str(item.get("emitent_type_name_rus") or "")
            isin = str(item.get("isin_code") or "").strip()
            if isin:
                by_code.setdefault(isin, emission)
            number = str(item.get("state_reg_number") or "").strip()
            if path.name.startswith("emission_regnum_") and number:
                by_code.setdefault(f"SU{number}", emission)
    return by_code, kind


def _stamp(path: Path) -> tuple[int, int] | None:
    """Отметка файла для памяти чтений: перезаписанный доставкой файл читается заново."""
    try:
        found = path.stat()
    except OSError:
        return None
    return found.st_mtime_ns, found.st_size


@cache
def _flows_read(
    path: Path, stamp: tuple[int, int] | None
) -> tuple[list[Flow], Decimal | None] | None:
    """График выпуска с диска; None — графика нет."""
    return read_flows(path) if stamp is not None else None


@cache
def _offers_read(path: Path, stamp: tuple[int, int] | None) -> dict[date, Decimal]:
    """Оферты выпуска с диска."""
    return read_offers(path) if stamp is not None else {}


def _flows_at(path: Path) -> tuple[list[Flow], Decimal | None] | None:
    """График выпуска; память чтений привязана к отметке файла."""
    return _flows_read(path, _stamp(path))


def _offers_at(path: Path) -> dict[date, Decimal]:
    """Оферты выпуска; память чтений привязана к отметке файла."""
    return _offers_read(path, _stamp(path))


def trade_code(secid: str) -> str:
    """Ключ соответствия: у ОФЗ — номер регистрации без контрольной цифры."""
    if secid.startswith("SU") and len(secid) == 12:
        return secid[:-1]
    return secid


def bond_flow(
    row: dict, day: date, rate_at: Callable[[Decimal], Decimal],
    price: Decimal | None, by_code: dict[str, str],
) -> tuple[list[tuple[Decimal, Decimal]], list[Decimal], Decimal, Decimal, str]:
    """Поток строки среза, ставки кривой в его сроках, грязная цена, номинал и причина отказа.

    `rate_at` — доходность кривой дня в сроке, годы → проценты. Цена — процент
    непогашенного номинала биржи; грязная — она же плюс НКД биржи, на одну бумагу.
    """
    return _bond_flow(row, day, rate_at, price, by_code, None)[:5]


Flowed = tuple[list[tuple[Decimal, Decimal]], list[Decimal], Decimal, Decimal, str, bool]


def _bond_flow(
    row: dict, day: date, rate_at: Callable[[Decimal], Decimal],
    price: Decimal | None, by_code: dict[str, str],
    coupons: Callable[[str, date], dict[date, Decimal]] | None,
) -> Flowed:
    """`bond_flow` с оценкой неустановленных купонов и признаком «оценка вошла в поток».

    `coupons` — оценки выпуска на день торгов (`floating.pv_coupons`,
    `distress_zone.pv_floating`); без них неустановленный будущий купон
    оставляет бумагу без потока.
    """
    none: Flowed = ([], [], Decimal(0), Decimal(0), "", False)
    secid = str(row.get("SECID") or "")
    emission = by_code.get(trade_code(secid))
    if emission is None:
        return none[:4] + ("выпуск не сопоставлен", False)
    got = _flows_at(cbonds.CACHE / f"flow_{emission}.json")
    if got is None:
        return none[:4] + ("графика нет", False)
    flows, _ = got
    face = _amount(row.get("FACEVALUE"))
    accrued = _amount(row.get("ACCINT")) or Decimal(0)
    if not face or price is None:
        return none[:4] + ("нет цены или номинала", False)
    settle = settlement(day)
    cut = None
    for field in ("BUYBACKDATE", "OFFERDATE"):
        raw = str(row.get(field) or "")[:10]
        if raw and raw != "0000-00-00":
            try:
                cut = date.fromisoformat(raw)
                break
            except ValueError:
                continue
    unknown = [
        item.due for item in flows
        if item.coupon is None and item.due > settle and (cut is None or item.due <= cut)
    ]
    if coupons is not None and unknown:
        guessed = coupons(emission, day)
        flows = [
            replace(item, coupon=guessed.get(item.due)) if item.coupon is None else item
            for item in flows
        ]
    pairs, why = cash_flow(
        flows,
        settle,
        face,
        cut,
        offer_price(_offers_at(cbonds.CACHE / f"offert_{emission}.json"), cut)
        if cut else Decimal(100),
    )
    estimated = coupons is not None and bool(unknown)
    if why == "будущий купон не объявлен" and estimated:
        why = "будущий купон не оценён"
    if why:
        return none[:4] + (why, False)
    rates = [rate_at(years) for years, _ in pairs]
    return pairs, rates, price / 100 * face + accrued, face, "", estimated


@dataclass(frozen=True, slots=True)
class PriceToPv:
    """Грязная цена к приведённой стоимости потока по КБД без надбавки.

    `price` и `pv` — в процентах непогашенного номинала биржи, `ratio` —
    их отношение; пустое `ratio` — поток не построен, причина в `why`.
    """

    ratio: Decimal | None
    price: Decimal | None
    pv: Decimal | None
    why: str
    # В потоке есть оценённый купон (`floating.pv_coupons`): PV — по оценке.
    estimated: bool = False


def price_to_pv(
    row: dict, day: date, rate_at: Callable[[Decimal], Decimal],
    price: Decimal, by_code: dict[str, str],
    coupons: Callable[[str, date], dict[date, Decimal]] | None = None,
) -> PriceToPv:
    """Отношение грязной цены к PV потока по кривой дня; та же реализация, что у Z.

    `coupons` — оценки неустановленных купонов (`bond_flow`); Z их не получает.
    """
    pairs, rates, dirty, face, why, estimated = _bond_flow(
        row, day, rate_at, price, by_code, coupons
    )
    if why:
        return PriceToPv(None, None, None, why)
    value = present_value(pairs, rates)
    if not value:
        return PriceToPv(None, None, None, "приведённая стоимость не определена")
    return PriceToPv(
        dirty / value, dirty / face * 100, value / face * 100, "",
        estimated=estimated,
    )

