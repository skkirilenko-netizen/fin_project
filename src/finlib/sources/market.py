"""Рыночный слой: спред к кривой ОФЗ, цена бумаги и ориентир дня.

**Счёт живёт здесь, а не в замере.** Пока методика не была утверждена, спред
считался в `eval/market_lead_run.py` — и это был объявленный долг: два пути
к одному ответу расходятся, и расхождения не видно, пока их не сравнить.
Методика утверждена решением владельца 24.09.2026, и счёт переехал сюда;
замер теперь зовёт эти же функции.

**Что здесь считается.** Дневной срез торгов по рынку облигаций целиком
превращается в три величины: ориентир дня (перцентиль ликвидного ядра),
спред эмитента ко кривой ОФЗ и цена его бумаг. Правила — `market.yaml`,
и ни одно число здесь не зашито.

**Цена берётся раньше правила сравнимости.** Правило это о доходности:
у флоатера доходность к сроку не определена, пока не известен будущий купон.
Цена определена у любой бумаги, и отбросив строку целиком, мы теряли бы
вместе с несравнимой доходностью вполне сравнимую цену: ценовой ряд есть
у 484 эмитентов, спредовый — у 431.
"""

import bisect
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.metrics.display import digits
from finlib.sources.cbonds import bond_issuers
from finlib.sources.cbonds_events import issues_of
from finlib.sources.moex import CACHE
from finlib.sources.zspread import PriceToPv, emission_map, price_to_pv

logger = logging.getLogger(__name__)

# **Посчитанное хранится, сырое — нет** (`market.yaml`, блок `storage`).
# Сырые срезы занимают полтора гигабайта и переспрашиваются у источника
# в любой день; ряд спредов восстанавливается только пересчётом.
SERIES = settings.data_dir / "market" / "series.json"

_RULES = settings.methodology_dir / "market.yaml"

# Чем мерится зона дефолта (`distress_zone.measure`). Перечень один: по нему
# же справочник маршрута проверяет наименования основания по мере.
DistressMeasure = Literal["nominal", "pv_kbd"]

# Ряд, прочитанный в этом процессе: двести тысяч точек читаются с диска
# однажды. `None` означает «ещё не читали», а не «ряда нет».
_LOADED: "Market | None" = None


class Rule(BaseModel):
    """Подтверждение «K из N»: сколько наблюдений из скольких."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    of: int = Field(gt=0)
    out_of: int = Field(gt=0)

    @model_validator(mode="after")
    def _fits(self) -> "Rule":
        """Требовать больше наблюдений, чем окно, нельзя."""
        if self.of > self.out_of:
            raise ValueError("подтверждение требует больше точек, чем в окне")
        return self


class Step(BaseModel):
    """Ступень лестницы кратности: порог вместе с тем, что он отсекает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    percentile: int = Field(gt=0, lt=100)
    multiple: Decimal = Field(gt=0)
    calibration_status: str = Field(min_length=1)
    # Что ступень отсекает и чего стоит: доля рынка, прирост, точность,
    # выявляемость. Порог без этих чисел — не порог, а число.
    measured: dict[str, Decimal] = Field(min_length=1)
    in_route: bool
    ground: str = ""
    basket: str = ""
    subgroup: str = ""
    escalation: bool | None = None
    why: str = ""
    statement_origin: str = ""
    # **Подтверждение бывает своим у ступени.** Оно объявлено рядом с числами
    # размена: у p99 смягчено до «5 из 10» решением владельца 24.09.2026,
    # у прочих ступеней действует общее правило (`confirmation.default`).
    # Отсутствие поля означает «общее», а не «без подтверждения».
    confirmation: Rule | None = None
    confirmation_status: str = ""
    # Цена подтверждения основной, поточечной мерой: варианты и интервалы
    # прироста, на которых оставлено нынешнее (решение владельца 29.09.2026).
    confirmation_measured_pointwise: dict[str, dict[str, Decimal]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _route_step_is_named(self) -> "Step":
        """Ступень маршрута обязана назвать основание и корзину."""
        if self.confirmation is not None and not self.confirmation_status:
            raise ValueError(
                f"у ступени {self.code} своё подтверждение без объявленной "
                "зрелости: порог без статуса калибровки выглядит проверенным"
            )
        if not self.in_route:
            return self
        if not self.ground or not self.basket:
            raise ValueError(f"ступень {self.code} в маршруте без основания или корзины")
        if self.basket == "attention" and (not self.subgroup or self.escalation is None):
            raise ValueError(
                f"ступень {self.code} во внимании без подгруппы либо без "
                "объявления об эскалации"
            )
        return self


class Ladder(BaseModel):
    """Лестница кратности спреда к ориентиру дня."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    steps: tuple[Step, ...] = Field(min_length=1)
    measured_on: date
    measured_origin: str = Field(min_length=1)
    # Цена подтверждения, измеренная на самой ступени: прирост и упреждение
    # при разных «K из N». Без неё выбранное правило выглядит единственным.
    confirmation_measured: dict[str, dict[str, Decimal]] = Field(min_length=1)
    # Упреждение появления против упреждения стояния: то же измерение
    # без оснований, стоявших с первого наблюдавшегося дня.
    appearance_measured: dict[str, dict[str, Decimal]] = Field(min_length=1)
    # **Подтверждение здесь не украшение, а условие осмысленности.** Порог,
    # отсекающий процент рынка в день, за два года срабатывает почти у каждого.
    requires_confirmation: bool

    @model_validator(mode="after")
    def _route_steps_are_distinct(self) -> "Ladder":
        """Одна корзина — одна ступень: две ступени одной корзины не различают."""
        taken: set[str] = set()
        for step in self.steps:
            if not step.in_route:
                continue
            if step.basket in taken:
                raise ValueError(
                    f"корзину {step.basket} называют две ступени лестницы: "
                    "различение, которого нет"
                )
            taken.add(step.basket)
        return self


class Confirmation(BaseModel):
    """Правило подтверждения устойчивости признака."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default: Rule
    systemic: Rule
    systemic_origin: str = Field(min_length=1)


class Distress(BaseModel):
    """Зона дефолта по цене: ниже границы доходность смысла не имеет."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_below_percent: Decimal = Field(gt=0, le=100)
    calibration_status: str = Field(min_length=1)
    in_route: bool
    basket: str = Field(min_length=1)
    ground: str = Field(min_length=1)
    narrowing_declined: dict[str, Decimal | str]
    measured: dict[str, dict[str, Decimal]] = Field(min_length=1)
    measured_origin: str = Field(min_length=1)
    # Прежняя мера — по эмитентам, сработавшим хоть раз: справочно, для рынка
    # не годится (решение владельца 29.09.2026). Запись о том, на чём
    # принимались решения, а не число для формулировок.
    measured_ever: dict[str, dict[str, Decimal]] = Field(default_factory=dict)
    measured_ever_origin: str = ""
    confirmation_declined: dict[str, Decimal]
    # **Наблюдение — не правило, и место у него своё.** Случай, увиденный
    # однажды, порога не даёт; записанный рядом с правилами, он бы читался
    # как правило, а забытый — искался бы заново.
    observed: tuple[dict[str, str], ...] = ()
    statement_origin: str = Field(min_length=1)
    yield_is_meaningless: bool
    # **Чем мерится зона: ценой от номинала или ценой к PV потока по КБД.**
    # `nominal` — прежний признак, `pv_kbd` — грязная цена к приведённой
    # стоимости потока по кривой дня (`sources.zspread.price_to_pv`).
    # `substitution` — у бумаги без потока берётся цена от номинала
    # с пометкой `nominal_mark`; без подстановки такая бумага признака
    # не даёт. Умолчаний нет: молчание читалось бы как решение методики.
    measure: DistressMeasure
    measure_status: str = Field(min_length=1)
    measure_origin: str = Field(min_length=1)
    ratio_below: Decimal = Field(gt=0)
    substitution: bool | None
    nominal_mark: str = Field(min_length=1)

    @model_validator(mode="after")
    def _pv_declares_substitution(self) -> "Distress":
        """У признака по PV подстановка объявлена явно: от неё зависит круг бумаг."""
        if self.measure == "pv_kbd" and self.substitution is None:
            raise ValueError("measure: pv_kbd требует явного substitution: true | false")
        return self

    @property
    def threshold(self) -> Decimal:
        """Граница признака в процентах: номинала либо PV (отношение × 100)."""
        if self.measure == "pv_kbd":
            return self.ratio_below * 100
        return self.price_below_percent

    @property
    def ratio_below_said(self) -> str:
        """Порог отношения так, как объявлен методикой: «0,6», а не «0,60»."""
        places = max(0, -int(self.ratio_below.normalize().as_tuple().exponent))
        return digits(self.ratio_below, places)


class Lifetime(BaseModel):
    """Срок жизни рыночного основания: сколько торговых дней стоит подтверждённое.

    **Основание стоит, пока подтверждено недавно** (решение владельца
    29.09.2026): мера «сработал хоть раз за ряд» мерила длину ряда.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trading_days: int = Field(gt=0)
    status: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    applies_to: tuple[str, ...] = Field(min_length=1)
    # `any_within` — цена ниже границы хоть раз за срок; `last` — последняя
    # цена ниже границы и не старше срока. Умолчания нет: оба правила
    # измерены, и молчание читалось бы как решение методики.
    price_rule: Literal["any_within", "last"]
    since: Literal["episode_start"]
    measured_on: date
    measured_origin: str = Field(min_length=1)
    measured: dict[str, dict[str, Decimal]] = Field(min_length=1)
    lost_against_ever: tuple[str, ...] = ()


class MarketPolicy(BaseModel):
    """Методика рыночного слоя целиком: числа только отсюда."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    status: str = Field(min_length=1)
    status_origin: str = Field(min_length=1)
    source: dict
    comparability: dict
    spread: dict
    cleaning: dict
    benchmark: dict
    ladder: Ladder
    widening: dict
    own_norm: dict
    confirmation: Confirmation
    lifetime: Lifetime
    distress_zone: Distress
    display: dict
    storage: dict

    @model_validator(mode="after")
    def _idle_rules_say_why(self) -> "MarketPolicy":
        """Недействующее правило объявляет числа, по которым отвергнуто.

        Удалённое правило неотличимо от забытого, а замолчавшее — от
        работающего. Поэтому `in_route: false` требует блока `measured`.
        """
        for name in ("widening", "own_norm"):
            rule = getattr(self, name)
            if rule.get("in_route") is None:
                raise ValueError(f"правило {name} не объявило, идёт ли оно в маршрут")
            if not rule.get("in_route") and not rule.get("measured"):
                raise ValueError(f"правило {name} недействующее и без чисел замера")
        return self

    @property
    def route_steps(self) -> tuple[Step, ...]:
        """Ступени, которые называют корзину."""
        return tuple(step for step in self.ladder.steps if step.in_route)

    @property
    def floor(self) -> Decimal | None:
        """Пол ориентира: ниже него кратность делится на пол; None — пола нет."""
        value = self.benchmark.get("floor_bp")
        return None if value is None else Decimal(str(value))


@lru_cache(maxsize=2)
def load_market(path: Path | None = None) -> MarketPolicy:
    """Методика рыночного слоя; величины отсюда только читаются."""
    return MarketPolicy.model_validate(
        yaml.safe_load((path or _RULES).read_text(encoding="utf-8"))
    )


@dataclass(frozen=True, slots=True)
class Point:
    """Рыночный день эмитента: спред, цена и оборот.

    **Спред бывает пуст при известной цене.** У флоатера доходность к сроку
    не определена, а цена определена — и ценовой признак по такому дню
    считается, спредовый нет.
    """

    day: date
    spread: Decimal | None
    price: Decimal | None
    weight: Decimal
    # **Цена к PV по КБД** — у бумаги эмитента с наименьшим отношением в этот
    # день: отношение, её грязная цена и PV в процентах непогашенного
    # номинала. Пусто — ни у одной бумаги дня поток не построен либо ряд
    # собран без отношения (`Market.ratios`).
    ratio: Decimal | None = None
    ratio_price: Decimal | None = None
    ratio_pv: Decimal | None = None
    # Наименьшая цена от номинала среди бумаг дня, у которых поток
    # не построен: её берёт подстановка.
    unflowed: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Market:
    """Ряд рыночного слоя: ориентир дня, точки эмитентов и знаменатели."""

    benchmark: dict[date, Decimal]
    issuers: dict[str, dict[date, Point]]
    counted: dict[str, int]
    # Перепись строк по эмитенту: «рынок молчал» — три разных ответа, и без
    # неё они выглядят одинаково.
    census: dict[str, dict[str, int]]
    universe: int
    with_isin: int
    # Собран ли ряд с отношением цены к PV. Пустое отношение в ряду без
    # расчёта значит «не считали», а не «потока нет», и признак по PV
    # на таком ряду отказывает, а не молчит.
    ratios: bool = False
    # Ряд по возрастанию дня, собранный при первом спросе: пересчёт истории
    # спрашивает об одном эмитенте двести раз, и сортировать заново каждый
    # раз значило бы платить за один и тот же ответ.
    sorted_by_day: dict[str, list[Point]] = field(default_factory=dict, repr=False)
    # Торговые дни биржи по возрастанию и их номера — календарь срока жизни.
    days_cache: list[date] = field(default_factory=list, repr=False)
    number_cache: dict[date, int] = field(default_factory=dict, repr=False)

    def calendar(self) -> list[date]:
        """Торговые дни ряда по возрастанию: дни, у которых есть ориентир.

        **Срок считается днями биржи, а не наблюдениями эмитента**: день без
        ядра в ряд не идёт ни у кого (`build`), и календарь тот же, что у всех
        точек.
        """
        if not self.days_cache:
            self.days_cache.extend(sorted(self.benchmark))
            self.number_cache.update({day: n for n, day in enumerate(self.days_cache)})
        return self.days_cache

    def day_number(self, day: date) -> int:
        """Номер торгового дня не позже названного; −1 — раньше ряда."""
        days = self.calendar()
        if day in self.number_cache:
            return self.number_cache[day]
        return bisect.bisect_right(days, day) - 1

    def points(self, inn: str) -> dict[date, Point]:
        """Ряд эмитента; пусто — рынок о нём не высказывался."""
        return self.issuers.get(inn, {})

    def ordered(self, inn: str) -> list[Point]:
        """Ряд эмитента по возрастанию дня."""
        if inn not in self.sorted_by_day:
            self.sorted_by_day[inn] = [
                item for _, item in sorted(self.issuers.get(inn, {}).items())
            ]
        return self.sorted_by_day[inn]

    def silence(self, inn: str) -> str:
        """Почему у эмитента нет ряда: три разных ответа, не один."""
        own = self.census.get(inn)
        if not own or not own["rows"]:
            return "выпусков эмитента в истории биржи нет вовсе"
        if not own["with_price"]:
            return (
                f"бумаги допущены, но не торговались: строк среза {own['rows']}, "
                "цены нет ни в одной"
            )
        return (
            f"строк среза {own['rows']}, с ценой {own['with_price']}, "
            f"со спредом {own['with_spread']}"
        )


def holders() -> dict[str, str]:
    """ISIN → ИНН по всем выпускам всех эмитентов справочника.

    **Круг задан справочником эмитентов, а не выпусками в обращении**, и это
    третий случай того же дефекта универсума. У Кириллицы бумаги погашены
    и одна в дефолте по погашению, выпусков «в обращении» нет ни одного —
    и рыночного ряда у неё не было вовсе, притом что именно её случай
    и завёл рыночный слой. Слой, слепой у того, у кого дефолт уже случился,
    отвечал бы на вопрос о своей доставке.

    Мера: эмитентов с карточкой 977, с выпусками в обращении 702; у 108
    из остальных 275 выпуски с ISIN известны, и это 414 бумаг.
    """
    found: dict[str, str] = {}
    for inn in universe():
        issues, known = issues_of(inn)
        if not known:
            continue
        for item in issues:
            if item.isin:
                found[item.isin] = inn
    return found


def universe() -> list[str]:
    """Эмитенты, о которых рынок вообще может что-то сказать.

    Справочник эмитентов, а не выпуски в обращении: у эмитента в дефолте
    бумаг «в обращении» не остаётся, а история их торгов — ровно то, ради
    чего слой заведён. Карточки читаются тем же кодом, что и в маршруте.
    """
    from finlib.scoring.routing_store import cards

    return sorted(set(cards()) | set(bond_issuers()))


def curve_of(points: list[dict]) -> list[tuple[Decimal, Decimal]]:
    """Опубликованные точки кривой парами «годы, доходность»."""
    return sorted(
        (Decimal(str(item["period"])), Decimal(str(item["value"])))
        for item in points
        if item.get("period") is not None and item.get("value") is not None
    )


def curve_at(
    points: list[tuple[Decimal, Decimal]], years: Decimal
) -> tuple[Decimal, bool]:
    """Кривая в точке дюрации и признак «это край, а не значение».

    Линейная интерполяция между опубликованными точками; за их пределами
    берётся крайняя точка, и строка помечается — продлевать кривую собственным
    правилом мы не будем, а формулу параметров не воспроизводим по памяти.
    """
    if years <= points[0][0]:
        return points[0][1], True
    if years >= points[-1][0]:
        return points[-1][1], True
    for (left, low), (right, high) in zip(points, points[1:], strict=False):
        if left <= years <= right:
            share = (years - left) / (right - left)
            return low + (high - low) * share, False
    return points[-1][1], True


def percentile(values: list[Decimal], place: int) -> Decimal:
    """Перцентиль отсортированного ряда; ряд пуст — вызывающий не спрашивает."""
    if len(values) == 1:
        return values[0]
    spot = Decimal(len(values) - 1) * Decimal(place) / Decimal(100)
    low = int(spot)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (spot - low)


def excluded(row: dict, policy: MarketPolicy, what: str = "yield") -> str:
    """Почему величина у этой бумаги не считается; пусто — считается.

    Возвращается **код правила**, а не «да/нет»: перечень отброшенного
    печатается построчно, и без кода нельзя сказать, что именно отсекло
    половину рынка.

    **Правило объявляет, что оно ломает** (`breaks`): у флоатера не определена
    доходность, а цена определена; у структурной бумаги не означает ничего
    и цена. Спрашивать надо порознь — иначе одно исправление заводит обратный
    дефект, как это и случилось 24.09.2026 дважды подряд.
    """
    for item in policy.comparability["exclude"]:
        if what not in item["breaks"]:
            continue
        if item.get("keep_only"):
            if str(row.get(item["by"]) or "") not in item["keep_only"]:
                return str(item["code"])
            continue
        if item.get("equals_zero"):
            # Пустое поле нулём не считается: «купон не объявлен» и «купона
            # нет» — разные сведения, и второе из первого не следует.
            got = _number(row.get(item["by"]))
            if got is not None and got == 0:
                return str(item["code"])
            continue
        if str(row.get(item["by"]) or "") not in item.get("values", ()):
            continue
        # Оговорка правила: то же значение при другом горизонте правомерно.
        spare = item.get("unless")
        if spare and str(row.get(spare["by"]) or "") in spare["values"]:
            continue
        return str(item["code"])
    return ""


def _number(value: object) -> Decimal | None:
    """Число среза в `Decimal`; пусто — величины нет."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def build(policy: MarketPolicy | None = None, ratios: bool | None = None) -> Market:
    """Пересчёт ряда из срезов: один проход по доставленным дням.

    Хранится только сведённое — по эмитенту на дату: полтора миллиона строк
    не помещаются ни в память, ни в осмысленный файл.

    **Отношение цены к PV считается, когда его спрашивают**: методикой
    (`distress_zone.measure: pv_kbd`) либо явно (`ratios`, замер). Поток
    каждой бумаги в каждый день дисконтируется в `Decimal`, и при признаке
    от номинала эта работа никому не нужна.
    """
    policy = policy or load_market()
    with_ratios = policy.distress_zone.measure == "pv_kbd" if ratios is None else ratios
    by_code = emission_map()[0] if with_ratios else {}
    core = policy.benchmark["liquid_core"]
    place = int(policy.benchmark["percentile"])
    ceiling = Decimal(str(policy.spread["ceiling_bp"]))
    mine_of = holders()
    curves = json.loads((CACHE / "zcyc_by_day.json").read_text(encoding="utf-8"))
    by_issuer: dict[str, dict[date, Point]] = defaultdict(dict)
    benchmark: dict[date, Decimal] = {}
    counted: dict[str, int] = defaultdict(int)
    census: dict[str, dict[str, int]] = defaultdict(
        lambda: {"rows": 0, "with_price": 0, "with_spread": 0}
    )
    for path in sorted(CACHE.glob("xsec_*.json")):
        if "_p" in path.name:
            continue
        name = path.name[len("xsec_") : -len(".json")]
        points = curves.get(name, {}).get("yearyields")
        if not points:
            continue
        day = date.fromisoformat(name)
        curve = curve_of(points)
        market: list[Decimal] = []
        mine: dict[
            str, list[tuple[Decimal | None, Decimal, Decimal | None, PriceToPv | None]]
        ] = defaultdict(list)
        for row in json.loads(path.read_text(encoding="utf-8")).get("history") or []:
            counted["строк"] += 1
            inn = mine_of.get(str(row.get("SECID") or ""))
            turnover = _number(row.get("VALUE")) or Decimal(0)
            trades = _number(row.get("NUMTRADES")) or Decimal(0)
            price = _number(row.get("LEGALCLOSEPRICE")) or _number(row.get("CLOSE"))
            # **Цена живёт по своим правилам, а не по правилу доходности.**
            # Своим — потому что процент от номинала у структурной, валютной,
            # индексируемой и дисконтной бумаги означает не то; и потому что
            # цена без единой сделки есть расчётная величина биржи, а не
            # мнение рынка.
            if price is not None:
                if refused := excluded(row, policy, "price"):
                    counted[f"цена отброшена: {refused}"] += 1
                    price = None
                elif policy.comparability["price_needs_trade"] and trades <= 0:
                    counted["цена без сделок"] += 1
                    price = None
            spread: Decimal | None = None
            if why := excluded(row, policy):
                counted[f"отброшено: {why}"] += 1
            elif (got := _number(row.get("YIELDATWAP") or row.get("YIELDCLOSE"))) is None:
                counted["без доходности"] += 1
            elif (days := _number(row.get("DURATION"))) is None or not days:
                counted["без дюрации"] += 1
            else:
                level, edge = curve_at(curve, days / Decimal(365))
                counted["край кривой"] += int(edge)
                spread = (got - level) * 100
                if spread > ceiling:
                    counted["выше потолка"] += 1
                    spread = None
                elif (
                    trades >= core["min_trades"]
                    and turnover >= core["min_turnover_rub"]
                ):
                    market.append(spread)
            flowed: PriceToPv | None = None
            if with_ratios and inn is not None and price is not None:
                # Цена та же, что у признака от номинала, — после правил
                # сравнимости и сделок: меняется одна величина, не круг бумаг.
                flowed = price_to_pv(
                    row, day, lambda years, c=curve: curve_at(c, years)[0], price, by_code
                )
                if flowed.ratio is None:
                    counted[f"поток не построен: {flowed.why}"] += 1
            if inn is not None:
                census[inn]["rows"] += 1
                census[inn]["with_price"] += int(price is not None)
                census[inn]["with_spread"] += int(spread is not None)
                if spread is not None or price is not None:
                    mine[inn].append((spread, turnover, price, flowed))
        if len(market) < 5:
            # Ядро из трёх бумаг ориентиром не является: день остаётся
            # без ориентира, и спреды этого дня в кратность не идут.
            continue
        benchmark[day] = percentile(sorted(market), place)
        for inn, rows in mine.items():
            by_issuer[inn][day] = _of_day(day, rows)
    return Market(
        benchmark=benchmark,
        issuers=dict(by_issuer),
        counted=dict(counted),
        census=dict(census),
        universe=len(universe()),
        with_isin=len(set(mine_of.values())),
        ratios=with_ratios,
    )


def _of_day(
    day: date,
    rows: list[tuple[Decimal | None, Decimal, Decimal | None, PriceToPv | None]],
) -> Point:
    """Величина дня у эмитента: оборотом взвешенный спред и наименьшая цена.

    **Порядок частей — часть правила.** Сперва величина дня у выпуска, потом
    у эмитента: сложив сделки всех выпусков в кучу, мы дали бы эмитенту
    с десятью выпусками десятикратный вес против эмитента с одним.
    """
    weight = sum((item[1] for item in rows), Decimal(0)) or Decimal(len(rows))
    spreads = [item for item in rows if item[0] is not None]
    spread = None
    if spreads:
        total = sum((item[1] or Decimal(1) for item in spreads), Decimal(0))
        spread = sum(
            (item[0] * (item[1] or Decimal(1)) for item in spreads), Decimal(0)
        ) / (total or Decimal(len(spreads)))
    prices = [item[2] for item in rows if item[2]]
    # Отношение — у бумаги с наименьшим, как наименьшая цена у признака
    # от номинала; цена бумаги без потока — отдельно, для подстановки.
    flowed = [item[3] for item in rows if item[3] is not None and item[3].ratio is not None]
    lowest = min(flowed, key=lambda item: item.ratio or Decimal(0), default=None)
    unflowed = [
        item[2] for item in rows
        if item[2] and item[3] is not None and item[3].ratio is None
    ]
    return Point(
        day=day,
        spread=spread,
        price=min(prices) if prices else None,
        weight=weight,
        ratio=lowest.ratio if lowest else None,
        ratio_price=lowest.price if lowest else None,
        ratio_pv=lowest.pv if lowest else None,
        unflowed=min(unflowed) if unflowed else None,
    )


def series(refresh: bool = False, policy: MarketPolicy | None = None) -> Market:
    """Ряд с диска, а при его отсутствии — пересчёт и запись.

    Пересчёт идёт по доставленным срезам и сети не касается вовсе: доставка —
    отдельный прогон (`scripts/moex_market_fetch.py`).

    **Ряд читается один раз на процесс.** Сборка девятисот карточек
    спрашивала его девятьсот раз, и чтение файла в двести тысяч точек занимало
    больше, чем всё остальное: четыре минуты против сорока секунд. `refresh`
    память сбрасывает — ежедневный прогон пересчитывает ряд после доставки.
    """
    global _LOADED
    if _LOADED is not None and not refresh:
        return _LOADED
    if SERIES.exists() and not refresh:
        _LOADED = _read(json.loads(SERIES.read_text(encoding="utf-8")))
        return _LOADED
    if not (CACHE / "zcyc_by_day.json").exists():
        # **Пустой ряд означает «доставки не было», а не «рынок молчал».**
        # Считать отсутствие срезов отсутствием сигнала — тот же дефект,
        # что ноль срабатываний при неизвестном числе проверок.
        logger.warning(
            "срезов биржи на диске нет: рыночный ряд пуст, доставка — "
            "scripts/moex_market_fetch.py"
        )
        return Market({}, {}, {}, {}, 0, 0)
    found = build(policy)
    SERIES.parent.mkdir(parents=True, exist_ok=True)
    SERIES.write_text(_written(found), encoding="utf-8")
    _LOADED = found
    return found


def _written(found: Market) -> str:
    """Ряд в JSON: величины строками, иначе `Decimal` станет `float`."""
    return json.dumps(
        {
            "benchmark": {
                f"{day}": str(value) for day, value in found.benchmark.items()
            },
            "issuers": {
                inn: {
                    f"{day}": {
                        "spread": None if item.spread is None else str(item.spread),
                        "price": None if item.price is None else str(item.price),
                        "weight": str(item.weight),
                        **{
                            name: None if value is None else str(value)
                            for name, value in (
                                ("ratio", item.ratio),
                                ("ratio_price", item.ratio_price),
                                ("ratio_pv", item.ratio_pv),
                                ("unflowed", item.unflowed),
                            )
                        },
                    }
                    for day, item in own.items()
                }
                for inn, own in found.issuers.items()
            },
            "counted": found.counted,
            "census": found.census,
            "universe": found.universe,
            "with_isin": found.with_isin,
            "ratios": found.ratios,
        },
        ensure_ascii=False,
    )


def _read(raw: dict) -> Market:
    """Ряд из JSON: величины обратно в `Decimal`."""
    return Market(
        benchmark={
            date.fromisoformat(day): Decimal(value)
            for day, value in raw["benchmark"].items()
        },
        issuers={
            inn: {
                date.fromisoformat(day): Point(
                    day=date.fromisoformat(day),
                    spread=None if item["spread"] is None else Decimal(item["spread"]),
                    price=None if item["price"] is None else Decimal(item["price"]),
                    weight=Decimal(item["weight"]),
                    **{
                        name: None if item.get(name) is None else Decimal(item[name])
                        for name in ("ratio", "ratio_price", "ratio_pv", "unflowed")
                    },
                )
                for day, item in own.items()
            }
            for inn, own in raw["issuers"].items()
        },
        counted=raw["counted"],
        census=raw["census"],
        universe=raw["universe"],
        with_isin=raw["with_isin"],
        # Ряд, записанный до отношения к PV, его не содержит: «не считали».
        ratios=bool(raw.get("ratios", False)),
    )
