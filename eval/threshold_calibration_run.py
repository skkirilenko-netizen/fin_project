"""Калибровка порогов отчётности (фаза 6): перцентиль по событиям, проверка во времени.

    uv run python eval/threshold_calibration_run.py --pilot   # пять дат: контроль и время
    uv run python eval/threshold_calibration_run.py > data/output/threshold_calibration.md

**Схема согласована владельцем 28.09.2026.** Порог — точка распределения
величины (принцип 2 дорожной карты); по событиям выбирается только
перцентиль и только на обучающей части календаря, до 01.07.2026. Проверка —
на поздней части, которую выбор не видел: порог меняется, лишь если 90 %
интервал парной разности прироста (новый − прежний) на одних и тех же
бутстрэп-выборках эмитентов не содержит нуля. «Улучшить нельзя» — допустимый
исход, и он печатается с числами.

**Замер не считает сам.** Основание при другом пороге даёт боевой маршрут
(`routing_rows(..., variants=)`, `route(..., thresholds=)`): гашение
стоп-фактором, тип эмитента, полоса у края шкалы и знак EBITDA живут там,
и пересчёт по записанным величинам здесь был бы вторым путём к вердикту.
Вариант «прежний» — тот же маршрут без замены, и он обязан совпасть
с записанной историей: это контроль, и у него печатается знаменатель.

**Мера — появление основания**, та же, что у замеров упреждения
(`market_lead_run.first_new_ground`): основание, стоявшее с первого дня
истории, ничего не предсказало. Событие — объявленный неплатёж
(`market_lead_run.events`).
"""

import contextlib
import io
import json
import logging
import random
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_lead_run import events, first_new_ground  # noqa: E402

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import Overrides, load_routing  # noqa: E402
from finlib.scoring.routing_catalogue import catalogue_for  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.scoring.theses import load_theses  # noqa: E402
from finlib.sources.cbonds import CACHE  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

# --- устройство замера, согласованное владельцем 28.09.2026 ---------------------
# Точка раздела: обучение — события до неё, проверка — с неё (15 и 13).
SPLIT = date(2026, 7, 1)
# Сетка перцентилей объявлена до замера: «больше — хуже» p25–p99,
# «меньше — хуже» p1–p60, шаг 5 п. п. в середине и прежние точки в хвостах.
# **Расширена 28.09.2026 по итогам пилота** (решение владельца): пилот
# смотрел только распределения, не события, — нынешние пороги
# рефинансирования (≈ p41) и нижней части автономии РСБУ и лизинга
# (46–55 % по худшую сторону) лежали вне прежней сетки p75–p99 и p1–p25,
# и замер отвечал бы только на вопрос «помогло бы ужесточение». Перцентиль
# нынешнего порога входит в сетку отдельным вариантом (`CURRENT_MARK`).
HIGH_GRID = (
    *(Decimal(step) for step in range(25, 96, 5)),
    Decimal("97.5"),
    Decimal("99"),
)
LOW_GRID = (
    Decimal("1"),
    Decimal("2.5"),
    *(Decimal(step) for step in range(5, 61, 5)),
)
CURRENT_MARK = "перцентиль нынешнего порога"
# Вариант на обучении выбирается при числе сработавших не меньше этого:
# прирост на пяти сработавших — шум, а не свойство порога.
MIN_FIRED = 10
# Бутстрэп: выборок, зерно, доля интервала.
REPLICAS = 2000
SEED = 20260928
CONFIDENCE = Decimal("90")
# Корзины корпоративного периметра: распределение считается по ним. Очереди
# типов (структурные, вне периметра, поручитель, статус) о величинах
# не говорят.
PERIMETER = ("review", "attention", "clear")
# Отрасль, у которой автономия мерится отдельно (вопрос владельца 24.09.2026,
# `BACKLOG.md`); строка — отрасль карточки источника, как в `routing.yaml`.
LEASING = "Лизинг и аренда"
PILOT_DATES = 5

LEVEL = frozenset({"level_off_scale", "metric_at_edge"})
LOWER = frozenset({"metric_in_lower_band"})
# Основания, которые калибровка трогает: по ним и сверяется «прежний»
# вариант с записанной историей.
CALIBRATED = LEVEL | LOWER | {
    "refinancing_gap", "refinancing_offers", "bound_above_threshold",
}

_POINTS = """
SELECT inn, as_of, standard, basket, grounds_all, inputs
FROM routing_history WHERE kind = 'backfill' ORDER BY as_of, inn
"""


@dataclass(frozen=True, slots=True)
class Subject:
    """Величина и ступень калибровки: что меняется, у кого и что считается появлением.

    `stage` — `review` (конец шкалы), `attention` (нижняя часть шкалы) либо
    `cover` (отсечка рефинансирования). `key` — код величины у маршрута:
    показатель либо основание рефинансирования. `value` — где величина
    лежит во входах истории.
    """

    name: str
    stage: str
    key: str
    value: str
    grounds: frozenset[str]
    finding_subject: str
    high_bad: bool
    current: Decimal
    standard: Standard | None = None
    branch_in: frozenset[str] | None = None
    branch_out: frozenset[str] = frozenset()

    @property
    def grid(self) -> tuple[Decimal, ...]:
        """Перцентили сетки в сторону худшего хвоста."""
        return HIGH_GRID if self.high_bad else LOW_GRID

    def overrides(self, edge: Decimal) -> Overrides:
        """Замена порога этой ступени величиной `edge`."""
        slot = {"review": "review", "attention": "attention", "cover": "cover"}
        return Overrides(
            **{slot[self.stage]: {self.key: edge}},
            standard=self.standard,
            branch_in=self.branch_in,
            branch_out=self.branch_out,
        )


def _scale_edges(code: str) -> tuple[Decimal, Decimal, bool]:
    """Нынешние отсечки шкалы: конец шкалы, нижняя часть и сторона худшего.

    Нижняя часть объявлена баллом (`bands.lower_below`), и величина на ней
    нужна здесь только затем, чтобы назвать её перцентиль и выбрать
    ближайший вариант при равенстве: решение по нынешнему порогу даёт
    маршрут без замены, а не это число.
    """
    scale = catalogue_for(Standard.IFRS).scale(code)
    assert scale is not None, f"шкалы {code} нет"
    points = [(Decimal(str(x)), Decimal(str(s))) for x, s in scale.points]
    lower = Decimal(str(load_theses().bands.lower_below))
    edge = points[0][0]
    for (x0, s0), (x1, s1) in zip(points, points[1:], strict=False):
        if s0 <= lower <= s1 and s1 != s0:
            return edge, x0 + (x1 - x0) * (lower - s0) / (s1 - s0), points[0][0] > points[-1][0]
    raise AssertionError(f"балл {lower} вне шкалы {code}")


def subjects() -> tuple[Subject, ...]:
    """Величины в порядке владельца: рефинансирование, нагрузка, ликвидность, автономия."""
    cover = Decimal(str(load_routing().refinancing.cover_ratio))
    found: list[Subject] = [
        Subject("Рефинансирование: платежи года к деньгам", "cover",
                "refinancing_gap", "refinance.due", frozenset({"refinancing_gap"}),
                "refinancing", True, cover),
        Subject("Рефинансирование: оферты года к деньгам", "cover",
                "refinancing_offers", "refinance.offered",
                frozenset({"refinancing_offers"}), "refinancing", True, cover),
    ]
    debt_edge, debt_lower, debt_high = _scale_edges("net_debt_ebitda")
    found += [
        Subject("Долговая нагрузка МСФО: конец шкалы", "review", "net_debt_ebitda",
                "metrics.net_debt_ebitda", LEVEL, "net_debt_ebitda", debt_high,
                debt_edge, Standard.IFRS),
        Subject("Долговая нагрузка МСФО: нижняя часть", "attention", "net_debt_ebitda",
                "metrics.net_debt_ebitda", LEVEL | LOWER, "net_debt_ebitda",
                debt_high, debt_lower, Standard.IFRS),
        Subject("Долговая нагрузка РСБУ: граница по прибыли от продаж", "review",
                "net_debt_ebitda", "metrics.debt_to_op_profit",
                frozenset({"bound_above_threshold"}), "debt_to_op_profit",
                debt_high, debt_edge, Standard.RSBU),
    ]
    for code, label, groups in (
        ("cur_liq", "Ликвидность", (("МСФО", Standard.IFRS, None, frozenset()),
                                    ("РСБУ", Standard.RSBU, None, frozenset()))),
        ("equity_ratio", "Автономия", (
            ("МСФО", Standard.IFRS, None, frozenset({LEASING})),
            ("РСБУ", Standard.RSBU, None, frozenset({LEASING})),
            ("лизинг", None, frozenset({LEASING}), frozenset()),
        )),
    ):
        edge, lower, high = _scale_edges(code)
        for where, standard, branch_in, branch_out in groups:
            found += [
                Subject(f"{label} {where}: конец шкалы", "review", code,
                        f"metrics.{code}", LEVEL, code, high, edge, standard,
                        branch_in, branch_out),
                Subject(f"{label} {where}: нижняя часть", "attention", code,
                        f"metrics.{code}", LEVEL | LOWER, code, high, lower,
                        standard, branch_in, branch_out),
            ]
    return tuple(found)


def _value(inputs: dict, where: str) -> Decimal | None:
    """Величина решения из записанных входов; у рефинансирования — к деньгам."""
    head, name = where.split(".")
    if head == "refinance":
        money = inputs.get("refinance") or {}
        cash, paid = money.get("cash"), money.get(name)
        if cash is None or paid is None or Decimal(cash) <= 0:
            return None
        return Decimal(paid) / Decimal(cash)
    raw = (inputs.get("metrics") or {}).get(name)
    return Decimal(raw) if raw is not None else None


def _branches() -> dict[str, str]:
    """Отрасль карточки источника по ИНН."""
    path = CACHE / "emitents.json"
    cards = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {inn: str(card.get("branch_name_rus") or "") for inn, card in cards.items()}


def _mine(subject: Subject, standard: str | None, branch: str) -> bool:
    """Относится ли точка истории к кругу величины."""
    if subject.standard is not None and standard != subject.standard.value:
        return False
    if subject.branch_in is not None and branch not in subject.branch_in:
        return False
    return branch not in subject.branch_out


def _rank(values: list[Decimal], share: Decimal) -> Decimal:
    """Перцентиль ближайшим наблюдением: порог обязан быть наблюдавшейся величиной."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * share / 100))]


@dataclass(slots=True)
class Distribution:
    """Распределение величины на обучающей части и перцентили сетки."""

    dates: int = 0
    issuers: set[str] = field(default_factory=set)
    points: int = 0
    edges: dict[Decimal, Decimal] = field(default_factory=dict)
    # Где стоит нынешний порог: доля наблюдений по худшую сторону от него.
    current_share: Decimal | None = None
    # Перцентиль нынешнего порога — отдельный вариант сетки; ключ в `edges`.
    current_pct: Decimal | None = None


def distributions(
    rows: list[dict], branches: dict[str, str], found: tuple[Subject, ...]
) -> dict[Subject, Distribution]:
    """Перцентили сетки: медиана дневных перцентилей по датам обучения."""
    by_day: dict[Subject, dict[date, list[Decimal]]] = {s: defaultdict(list) for s in found}
    said = {s: Distribution() for s in found}
    for row in rows:
        if row["as_of"] >= SPLIT or row["basket"] not in PERIMETER:
            continue
        for subject in found:
            if not _mine(subject, row["standard"], branches.get(row["inn"], "")):
                continue
            value = _value(row["inputs"] or {}, subject.value)
            if value is None:
                continue
            by_day[subject][row["as_of"]].append(value)
            said[subject].issuers.add(row["inn"])
            said[subject].points += 1
    for subject in found:
        days = by_day[subject]
        said[subject].dates = len(days)
        if not days:
            continue
        for share in subject.grid:
            said[subject].edges[share] = statistics.median(
                _rank(values, share) for values in days.values()
            )
        pooled = [value for values in days.values() for value in values]
        worse = sum(
            1
            for value in pooled
            if (value >= subject.current if subject.high_bad else value <= subject.current)
        )
        share = Decimal(worse) / len(pooled) * 100
        said[subject].current_share = share
        # Перцентиль нынешнего порога — точка того же распределения, взятая
        # тем же правилом, что и сетка: вариант, отличающийся от прежнего
        # только тем, что он точка распределения, а не опорная точка шкалы.
        mark = (100 - share if subject.high_bad else share).quantize(Decimal("0.1"))
        if mark not in said[subject].edges:
            said[subject].edges[mark] = statistics.median(
                _rank(values, mark) for values in days.values()
            )
        said[subject].current_pct = mark
        said[subject].edges = dict(sorted(said[subject].edges.items()))
    return said


def variants(
    said: dict[Subject, Distribution],
) -> tuple[dict[str, Overrides], dict[str, Subject]]:
    """Варианты прохода: «прежний» и по перцентилю сетки у каждой ступени.

    Вторым возвращается ступень каждого варианта: у варианта хранится
    только его основание, иначе 240 вариантов × 900 эмитентов × 280 дат
    не поместились бы в память.
    """
    found: dict[str, Overrides] = {"прежний": Overrides()}
    owner: dict[str, Subject] = {}
    for subject, spread in said.items():
        for share, edge in spread.edges.items():
            name = _name(subject, share)
            found[name] = subject.overrides(edge)
            owner[name] = subject
    return found, owner


def _fires(subject: Subject, found: frozenset) -> bool:
    """Есть ли в основаниях точки основание ступени о её предмете."""
    return any(
        ground in subject.grounds and about == subject.finding_subject
        for ground, about in found
    )


def _name(subject: Subject, share: Decimal) -> str:
    """Имя варианта: ступень величины и перцентиль."""
    return f"{subject.name} · p{_pct(share)}"


def _pct(share: Decimal) -> str:
    """Перцентиль словами без показателя степени: «80», а не «8E+1»."""
    return format(share.normalize(), "f")


@dataclass(slots=True)
class Pass:
    """Итог прохода: основания «прежнего» по дню и дни срабатывания вариантов.

    У «прежнего» хранятся все основания калибровки — по ним идёт контроль
    с историей и меры нынешних порогов всех ступеней; у варианта — только
    дни, когда сработало основание его ступени.
    """

    base: dict[str, dict[date, frozenset]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    hits: dict[str, dict[str, set[date]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(set))
    )
    seconds: list[float] = field(default_factory=list)

    def days_of(self, name: str, subject: Subject) -> dict[str, set[date]]:
        """Дни срабатывания основания ступени у варианта по эмитенту."""
        if name != "прежний":
            return self.hits[name]
        return {
            inn: {day for day, found in by_day.items() if _fires(subject, found)}
            for inn, by_day in self.base.items()
        }


def run_pass(
    dates: list[date], chosen: dict[str, Overrides], owner: dict[str, Subject]
) -> Pass:
    """Проход по датам: боевой маршрут с каждым вариантом порогов."""
    said = Pass()
    memo: dict = {}
    with connection() as conn:
        for moment in dates:
            started = time.monotonic()
            verdicts: dict = {}
            routing_rows(
                conn, moment, as_of=moment, memo=memo, variants=chosen, verdicts=verdicts
            )
            for name, by_inn in verdicts.items():
                for inn, verdict in by_inn.items():
                    found = frozenset(
                        (item.ground, item.subject)
                        for item in verdict.findings
                        if item.ground in CALIBRATED
                    )
                    if name == "прежний":
                        said.base[inn][moment] = found
                    elif _fires(owner[name], found):
                        said.hits[name][inn].add(moment)
            said.seconds.append(time.monotonic() - started)
            logger.warning("%s: %.1f с", moment, said.seconds[-1])
        conn.rollback()
    return said


def control(rows: list[dict], done: Pass, dates: list[date]) -> tuple[int, list[str]]:
    """«Прежний» вариант против записанной истории: сверено и расхождения."""
    wanted = set(dates)
    compared, differ = 0, []
    base = done.base
    for row in rows:
        if row["as_of"] not in wanted or row["inn"] not in base:
            continue
        mine = base[row["inn"]].get(row["as_of"])
        if mine is None:
            continue
        compared += 1
        stored = set(row["grounds_all"] or ()) & CALIBRATED
        now = {ground for ground, _ in mine}
        if stored != now:
            differ.append(
                f"{row['inn']} {row['as_of']:%d.%m.%Y}: записано {sorted(stored)}, "
                f"сейчас {sorted(now)}"
            )
    return compared, differ


@dataclass(frozen=True, slots=True)
class Part:
    """Часть календаря: окно дат и события в нём."""

    name: str
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class Flags:
    """Эмитент круга на части: событие в ней и появилось ли основание до срока."""

    positive: bool
    fired: bool
    lead: int | None


@dataclass(frozen=True, slots=True)
class Score:
    """Мера варианта на части: знаменатели, прирост и упреждение."""

    population: int
    positives: int
    fired: int
    caught: int
    leads: tuple[int, ...]

    @property
    def lift(self) -> Decimal | None:
        """Прирост: точность против базовой доли; None — делить не на что."""
        return _lift(self.population, self.positives, self.fired, self.caught)

    @property
    def recall(self) -> Decimal | None:
        """Выявляемость: пойманные из событий части."""
        return Decimal(self.caught) / self.positives if self.positives else None


def _lift(population: int, positives: int, fired: int, caught: int) -> Decimal | None:
    """Прирост по счётчикам; None, если сработавших или событий нет."""
    if not fired or not positives or not population:
        return None
    return (Decimal(caught) / fired) / (Decimal(positives) / population)


def circle(
    rows: list[dict], branches: dict[str, str], subject: Subject, part: Part
) -> set[str]:
    """Круг величины на части: у кого величина была хоть раз в её окне."""
    return {
        row["inn"]
        for row in rows
        if part.start <= row["as_of"] <= part.end
        and _mine(subject, row["standard"], branches.get(row["inn"], ""))
        and _value(row["inputs"] or {}, subject.value) is not None
    }


def flags(
    hits: dict[str, set[date]],
    observed: dict[str, set[date]],
    members: set[str],
    calendar: dict[str, date],
    part: Part,
) -> dict[str, Flags]:
    """Появилось ли основание у каждого из круга до своего срока.

    `hits` — дни, когда основание ступени стояло; `observed` — все дни,
    когда эмитент маршрутизировался. Срок у эмитента с событием части — день
    до события (сработавшее после события не ловит его), у прочих — конец
    части. Эмитент, чьё событие было до части, в неё не входит: он уже
    не предупреждается.
    """
    said: dict[str, Flags] = {}
    for inn in members:
        moment = calendar.get(inn)
        if moment is not None and moment < part.start:
            continue
        positive = moment is not None and moment <= part.end
        until = moment - timedelta(days=1) if positive else part.end
        on = hits.get(inn, set())
        history = {
            day: {"основание"} if day in on else set()
            for day in observed.get(inn, set())
        }
        day = first_new_ground(history, lambda _: True, until) if history else None
        said[inn] = Flags(
            positive=positive,
            fired=day is not None,
            lead=(moment - day).days if positive and day is not None else None,
        )
    return said


def score(marks: dict[str, Flags]) -> Score:
    """Мера по отметкам эмитентов."""
    caught = [item for item in marks.values() if item.positive and item.fired]
    return Score(
        population=len(marks),
        positives=sum(1 for item in marks.values() if item.positive),
        fired=sum(1 for item in marks.values() if item.fired),
        caught=len(caught),
        leads=tuple(sorted(item.lead for item in caught if item.lead is not None)),
    )


def paired(
    old: dict[str, Flags], new: dict[str, Flags]
) -> tuple[Decimal | None, Decimal | None, int]:
    """90 % интервал парной разности прироста (новый − прежний) и число пустых выборок.

    Выборка — эмитенты с возвращением, одни и те же для обоих порогов.
    Выборка, где у одного из порогов прирост не определён (нет сработавших
    или событий), в интервал не идёт и считается.
    """
    members = sorted(set(old) & set(new))
    if not members:
        return None, None, REPLICAS
    rng = random.Random(SEED)
    diffs: list[Decimal] = []
    empty = 0
    for _ in range(REPLICAS):
        counts = [0, 0, 0, 0, 0]  # события, сработал прежний, пойман, новый, пойман
        for inn in rng.choices(members, k=len(members)):
            was, now = old[inn], new[inn]
            counts[0] += was.positive
            counts[1] += was.fired
            counts[2] += was.positive and was.fired
            counts[3] += now.fired
            counts[4] += now.positive and now.fired
        before = _lift(len(members), counts[0], counts[1], counts[2])
        after = _lift(len(members), counts[0], counts[3], counts[4])
        if before is None or after is None:
            empty += 1
            continue
        diffs.append(after - before)
    if not diffs:
        return None, None, empty
    tail = (100 - CONFIDENCE) / 2
    return _rank(diffs, tail), _rank(diffs, 100 - tail), empty


def _worse_share(subject: Subject, share: Decimal) -> Decimal:
    """Какую долю распределения отсекает перцентиль сетки."""
    return 100 - share if subject.high_bad else share


def choose(
    subject: Subject, spread: Distribution, train: dict[str, Score]
) -> str:
    """Вариант обучения: наибольший прирост при сработавших не меньше порога.

    При равенстве — ближайший к нынешнему порогу по доле отсекаемого;
    нынешний сам стоит в перечне и при равенстве побеждает.
    """
    def distance(name: str) -> Decimal:
        if name == "прежний":
            return Decimal(0)
        share = next(s for s in spread.edges if _name(subject, s) == name)
        return abs(_worse_share(subject, share) - (spread.current_share or Decimal(0)))

    eligible = [
        name for name, item in train.items()
        if item.fired >= MIN_FIRED and item.lift is not None
    ]
    if not eligible:
        return "прежний"
    return max(eligible, key=lambda name: (train[name].lift, -distance(name)))


def main() -> int:
    """Пилот либо полный проход калибровки."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    pilot = "--pilot" in sys.argv
    found = subjects()
    with connection() as conn:
        rows = fetch_all(_POINTS, {}, conn=conn)
    grid = sorted({row["as_of"] for row in rows})
    said = distributions(rows, _branches(), found)
    chosen, owner = variants(said)
    calendar = events()
    start, end = grid[0], grid[-1]
    train = sorted(d for d in calendar.values() if start < d < SPLIT)
    test = sorted(d for d in calendar.values() if SPLIT <= d <= end)

    print("# Калибровка порогов отчётности (фаза 6)\n")
    print(
        f"История: {len(grid)} дат, {start:%d.%m.%Y} — {end:%d.%m.%Y}. Раздел "
        f"{SPLIT:%d.%m.%Y}: событий в обучении **{len(train)}**, в проверке "
        f"**{len(test)}** ({len(test) / max(len(train) + len(test), 1):.0%}).\n"
    )
    high = sum(1 for s in found if s.high_bad)
    print(
        f"Вариантов прохода **{len(chosen)}**: «прежний» и по сетке у каждой "
        f"из {len(found)} ступеней — {len(HIGH_GRID)} перцентилей у {high} "
        f"ступеней «больше — хуже» (p25–p99), {len(LOW_GRID)} у "
        f"{len(found) - high} ступеней «меньше — хуже» (p1–p60), и у каждой "
        f"ещё {CURRENT_MARK}, если его нет в сетке.\n"
    )
    print(
        "**Сетка расширена 28.09.2026 по итогам пилота, до прохода.** Пилот "
        "смотрел только распределения, не события. Нынешние пороги "
        "рефинансирования (≈ p41) и нижней части автономии РСБУ и лизинга "
        "(46–55 % наблюдений по худшую сторону) лежали вне прежней сетки "
        "p75–p99 и p1–p25: все её варианты были строже нынешнего, и замер "
        "отвечал бы только на вопрос «помогло бы ужесточение». Сетка "
        "расширена для всех 15 ступеней одним правилом, перцентиль нынешнего "
        "порога входит в неё отдельным вариантом.\n"
    )
    _print_distributions(found, said)
    parts = (
        Part("обучение", start, max(d for d in grid if d < SPLIT)),
        Part("проверка", SPLIT, end),
    )
    if pilot:
        return _pilot(rows, grid, chosen, owner, found, said, calendar, parts)
    done = run_pass(grid, chosen, owner)
    compared, differ = control(rows, done, grid)
    print(
        f"**Контроль**: «прежний» вариант против записанной истории — сверено "
        f"**{compared}** точек, расхождений **{len(differ)}**. Проход "
        f"{sum(done.seconds) / 60:.0f} мин.\n"
    )
    for line in differ[:20]:
        print(f"- {line}")
    if differ:
        print("\n**Расхождения есть — мерам ниже верить нельзя, пока они не объяснены.**\n")
    _measure(rows, _branches(), found, said, done, calendar, parts)
    return 0 if not differ else 1


def _measure(
    rows: list[dict],
    branches: dict[str, str],
    found: tuple[Subject, ...],
    said: dict[Subject, Distribution],
    done: Pass,
    calendar: dict[str, date],
    parts: tuple["Part", "Part"],
) -> list[str]:
    """Меры по ступеням: обучение, выбор, проверка; итог строками сводки."""
    train_part, test_part = parts
    summary: list[str] = []
    observed = {inn: set(by_day) for inn, by_day in done.base.items()}
    for subject in found:
        spread = said[subject]
        names = ["прежний"] + [_name(subject, share) for share in spread.edges]
        members = {part.name: circle(rows, branches, subject, part) for part in parts}
        marks = {
            name: {
                part.name: flags(
                    done.days_of(name, subject),
                    observed,
                    members[part.name],
                    calendar,
                    part,
                )
                for part in parts
            }
            for name in names
        }
        train = {name: score(marks[name][train_part.name]) for name in names}
        best = choose(subject, spread, train)
        print(f"## {subject.name}\n")
        print(
            f"Перебрано вариантов **{len(names) - 1}** и прежний. Сработавших "
            f"для выбора не меньше {MIN_FIRED}.\n"
        )
        _table(subject, spread, train, best)
        old = marks["прежний"][test_part.name]
        new = marks[best][test_part.name]
        before, after = score(old), score(new)
        print(
            f"\n**Проверка** ({test_part.start:%d.%m.%Y} — {test_part.end:%d.%m.%Y}): "
            f"круг {before.population}, событий {before.positives}.\n"
        )
        print("| Порог | Сработал | Пойман | Выявляемость | Прирост | Упреждение |")
        print("|---|---|---|---|---|---|")
        for label, item in (("прежний", before), (best, after)):
            print(_line(label, item))
        if best == "прежний":
            verdict = "прежний порог на обучении не превзойдён — порог остаётся"
        else:
            low, high, empty = paired(old, new)
            interval = (
                f"[{low:.2f}; {high:.2f}]"
                if low is not None and high is not None
                else "не определён"
            )
            print(
                f"\nПарная разность прироста (новый − прежний), {CONFIDENCE:.0f} % "
                f"интервал на {REPLICAS} выборках эмитентов: **{interval}**; "
                f"выборок без определённого прироста {empty}.\n"
            )
            verdict = (
                "интервал выше нуля — порог меняется (решение владельца)"
                if low is not None and low > 0
                else "интервал содержит ноль либо ниже его — улучшить нельзя, порог остаётся"
            )
        print(f"\n**Исход:** {verdict}.\n")
        summary.append(f"| {subject.name} | {best} | {verdict} |")
    print("## Сводка\n")
    print("| Ступень | Выбран на обучении | Исход |")
    print("|---|---|---|")
    for line in summary:
        print(line)
    return summary


def _line(label: str, item: Score) -> str:
    """Строка таблицы меры."""
    recall = f"{item.recall:.0%}" if item.recall is not None else "—"
    lift = f"{item.lift:.2f}×" if item.lift is not None else "—"
    lead = (
        f"{statistics.median(item.leads):.0f} дн. (от {item.leads[0]} до {item.leads[-1]})"
        if item.leads
        else "—"
    )
    return (
        f"| {label} | {item.fired} | {item.caught} из {item.positives} | {recall} "
        f"| {lift} | {lead} |"
    )


def _table(
    subject: Subject, spread: Distribution, train: dict[str, Score], best: str
) -> None:
    """Таблица обучения по всем вариантам."""
    first = next(iter(train.values()))
    print(
        f"**Обучение**: круг {first.population}, событий {first.positives}.\n"
    )
    print("| Вариант | Порог | Сработал | Пойман | Выявляемость | Прирост | Упреждение |")
    print("|---|---|---|---|---|---|---|")
    edges = {_name(subject, share): edge for share, edge in spread.edges.items()}
    current = (
        _name(subject, spread.current_pct) if spread.current_pct is not None else ""
    )
    for name, item in train.items():
        edge = edges.get(name, subject.current)
        mark = f" ({CURRENT_MARK})" if name == current else ""
        mark += " **выбран**" if name == best else ""
        row = _line(name + mark, item)
        head, rest = row.split(" | ", 1)
        print(f"{head} | {edge:.3f} | {rest}")


def _print_distributions(found: tuple[Subject, ...], said: dict) -> None:
    """Распределения на обучающей части: знаменатели и перцентили сетки."""
    print("## Распределения на обучающей части\n")
    print("| Ступень | Дат | Эмитентов | Точек | Нынешний порог | Его место | Сетка |")
    print("|---|---|---|---|---|---|---|")
    for subject in found:
        spread = said[subject]
        where = (
            f"{spread.current_share:.1f} % по худшую сторону"
            if spread.current_share is not None
            else "—"
        )
        edges = "; ".join(
            f"p{_pct(share)} {edge:.3f}" for share, edge in spread.edges.items()
        )
        print(
            f"| {subject.name} | {spread.dates} | {len(spread.issuers)} | "
            f"{spread.points} | {subject.current:.3f} | {where} | {edges or '—'} |"
        )
    print()


def _pilot(
    rows: list[dict],
    grid: list[date],
    chosen: dict[str, Overrides],
    owner: dict[str, Subject],
    found: tuple[Subject, ...],
    said: dict[Subject, Distribution],
    calendar: dict[str, date],
    parts: tuple[Part, Part],
) -> int:
    """Пять дат: контроль «прежнего» варианта и оценка времени полного прохода.

    Меры на пяти датах не печатаются — они ничего не значат, — но считаются:
    путь до них должен пройти без ошибки прежде полного прохода.
    """
    step = max(len(grid) // PILOT_DATES, 1)
    dates = [grid[min(i * step + step // 2, len(grid) - 1)] for i in range(PILOT_DATES)]
    done = run_pass(dates, chosen, owner)
    seconds = done.seconds
    compared, differ = control(rows, done, dates)
    with contextlib.redirect_stdout(io.StringIO()):
        smoke = _measure(rows, _branches(), found, said, done, calendar, parts)
    print("## Пилот\n")
    print(f"Даты: {', '.join(f'{d:%d.%m.%Y}' for d in dates)}.\n")
    print(
        f"**Контроль**: «прежний» вариант против записанной истории по основаниям "
        f"калибровки — сверено **{compared}** точек, расхождений **{len(differ)}**.\n"
    )
    for line in differ[:20]:
        print(f"- {line}")
    if differ:
        print()
    per_day = statistics.mean(seconds[1:]) if len(seconds) > 1 else seconds[0]
    print(
        f"**Время**: первая дата {seconds[0]:.0f} с (память пересчёта пуста), "
        f"следующие в среднем {per_day:.1f} с на дату с {len(chosen)} вариантами. "
        f"Полный проход по {len(grid)} датам — около "
        f"**{(seconds[0] + per_day * (len(grid) - 1)) / 60:.0f} мин**.\n"
    )
    print(
        f"Путь мер пройден на пилотных датах без ошибки: ступеней {len(smoke)} "
        "(числа пилота не мера и не печатаются).\n"
    )
    return 0 if not differ else 1


if __name__ == "__main__":
    sys.exit(main())
