"""Купон флоатера: текущий индекс против форварда КБД на зафиксированных купонах. Только диск.

    uv run python eval/forward_index_check.py > отчёт.md
    uv run python eval/forward_index_check.py --basis-days 60 --horizons 0,3,6,12,24

Проверка из проекта `docs/proposals/forward_index.md` до перезамера 6.
Купоны флоатеров, уже зафиксированные в графиках на диске (`cupon_sum`
заполнен), — готовый ответ. Каждый такой купон оценивается на день
`начало периода − h месяцев` двумя способами и сравнивается с фактом:

- **текущий индекс** — боевое правило `sources.floating` (`rate_on`
  и формула условий с полом и потолком), то же, что в рефинансировании;
- **форвард** — форвардная ставка КБД того же дня на купонный период
  (линейная интерполяция `curve_at`, годовой компаундинг, как у потока
  к PV); ключевая — минус базис короткого конца (медиана «кратчайшая
  точка КБД − ключевая» за окно торговых дней до дня оценки), RUONIA —
  ещё минус медиана «ключевая − RUONIA» за то же окно, КБД N лет —
  форвардная точка на N лет от начала периода. Пол и потолок — те же,
  к форвардному значению индекса.

Ошибка — оценка минус факт, в процентных пунктах годовой ставки купона.
Сравнение парное: в таблицы идут купоны, у которых на дату есть оба
способа; отказы — отдельной таблицей с причинами. h = 0 — контроль
разбора, а не проверка: на день начала периода текущий индекс и есть
индекс фиксации, и большая ошибка там означает дефект наш, а не способа.

**Замер не считает сам**: ставка по формуле условий — `floating.coupon_rate`,
индекс на дату — `floating.rate_on`, точка кривой — `market.curve_at`.
Новое здесь только форвард и базис. Допущения проекта не устраняются:
премия за срок остаётся в форварде, компаундинг не переводится
(ключевая — простая, КБД — эффективная годовая), лаг фиксации
не моделируется. Направление ставки — знак изменения ключевой между днём
оценки и началом периода. История рядов печатается в шапке: где ряда
на дату нет, это отказ, а не ошибка оценки.
"""

import argparse
import bisect
import calendar
import logging
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from finlib.scoring.routing import load_routing
from finlib.sources import floating
from finlib.sources.cbonds_flows import CACHE, Schedule, schedule_of
from finlib.sources.market import curve_at, percentile

logger = logging.getLogger(__name__)

HORIZONS = (0, 3, 6, 12, 24)
BASIS_DAYS = 60
YEAR = Decimal(365)
PRECISION = 20
GROUPS = {
    "key_rate": "ключевая",
    "refinancing_rate": "ключевая",
    "ruonia": "RUONIA",
    "ofz_curve": "КБД N лет",
}
METHODS = ("текущий индекс", "форвард")


@dataclass(frozen=True, slots=True)
class Row:
    """Один купон на одном горизонте: факт и обе оценки, % годовых."""

    emission: str
    group: str
    horizon: int
    actual: Decimal
    current: Decimal
    forward: Decimal
    move: str

    def error(self, method: str) -> Decimal:
        """Ошибка способа: оценка минус факт, п. п."""
        return (self.current if method == METHODS[0] else self.forward) - self.actual


def months_before(day: date, months: int) -> date:
    """Тот же день месяца `months` месяцев назад; нет такого дня — последний день месяца."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def forward_rate(points: list[tuple[Decimal, Decimal]], t1: Decimal, t2: Decimal) -> Decimal | None:
    """Форвардная ставка кривой на отрезок [t1, t2] лет, % годовых; годовой компаундинг."""
    if t2 <= t1 or t2 <= 0:
        return None
    t1 = max(t1, Decimal(0))
    with localcontext() as ctx:
        ctx.prec = PRECISION
        far = 1 + curve_at(points, t2)[0] / 100
        near = 1 + curve_at(points, t1)[0] / 100 if t1 > 0 else Decimal(1)
        if far <= 0 or near <= 0:
            return None
        growth = (t2 * far.ln() - t1 * near.ln()) / (t2 - t1)
        return (growth.exp() - 1) * 100


class Series:
    """Ряды с диска, упорядоченные для поиска по дате."""

    def __init__(self, stale_days: int) -> None:
        self.stale = timedelta(days=stale_days)
        self.key = list(floating.key_rate())
        self.key_days = [day for day, _ in self.key]
        self.ruonia = [(day, published, value) for day, published, value in floating.ruonia()]
        self.ruonia_days = [day for day, _, _ in self.ruonia]
        raw = floating.curves()
        self.curve_days = sorted(date.fromisoformat(day) for day in raw)
        self.curves = {date.fromisoformat(day): points for day, points in raw.items()}
        self._basis: dict[tuple[date, int], tuple[Decimal, Decimal | None] | None] = {}

    def key_on(self, day: date) -> Decimal | None:
        """Ключевая, действующая на день; день раньше начала ряда — None."""
        place = bisect.bisect_right(self.key_days, day)
        return self.key[place - 1][1] if place else None

    def ruonia_on(self, day: date, known_by: date) -> Decimal | None:
        """RUONIA за последний день ставки не позже дня, опубликованная к `known_by`."""
        place = bisect.bisect_right(self.ruonia_days, day)
        while place and self.ruonia[place - 1][1] > known_by:
            place -= 1
        if not place or day - self.ruonia[place - 1][0] > self.stale:
            return None
        return self.ruonia[place - 1][2]

    def curve_on(self, day: date) -> list[tuple[Decimal, Decimal]] | None:
        """Кривая последнего дня не позже дня, если она не старше свежести."""
        place = bisect.bisect_right(self.curve_days, day)
        if not place or day - self.curve_days[place - 1] > self.stale:
            return None
        return self.curves[self.curve_days[place - 1]]

    def basis(self, day: date, window: int) -> tuple[Decimal, Decimal | None] | None:
        """Базис короткого конца к ключевой и «ключевая − RUONIA» за окно до дня; None — нет."""
        if (day, window) in self._basis:
            return self._basis[day, window]
        place = bisect.bisect_right(self.curve_days, day)
        short: list[Decimal] = []
        spread: list[Decimal] = []
        for seen in self.curve_days[max(0, place - window):place]:
            key = self.key_on(seen)
            if key is None:
                continue
            short.append(self.curves[seen][0][1] - key)
            overnight = self.ruonia_on(seen, day)
            if overnight is not None:
                spread.append(key - overnight)
        found = (
            (statistics.median(short), statistics.median(spread) if spread else None)
            if short
            else None
        )
        self._basis[day, window] = found
        return found

    def span(self) -> list[str]:
        """Покрытие рядов для шапки: первый и последний день, число дней."""
        def line(name: str, days: list[date]) -> str:
            if not days:
                return f"- {name}: ряда нет"
            return f"- {name}: {days[0]:%d.%m.%Y} — {days[-1]:%d.%m.%Y}, дней {len(days)}"

        return [
            line("ключевая", self.key_days),
            line("RUONIA", self.ruonia_days),
            line("КБД", self.curve_days),
        ]


def _forward_index(
    terms: floating.Terms,
    series: Series,
    day: date,
    t1: Decimal,
    t2: Decimal,
    window: int,
) -> tuple[Decimal | None, str]:
    """Форвардное значение индекса на купонный период и причина отказа."""
    points = series.curve_on(day)
    if points is None:
        return None, "кривой на дату нет"
    if terms.index == "ofz_curve":
        if terms.term is None:
            return None, "срок индекса КБД не разобран"
        found = forward_rate(points, max(t1, Decimal(0)), max(t1, Decimal(0)) + terms.term)
        return found, "" if found is not None else "форвард не посчитан"
    forward = forward_rate(points, t1, t2)
    if forward is None:
        return None, "форвард не посчитан"
    basis = series.basis(day, window)
    if basis is None:
        return None, "базиса к ключевой за окно нет"
    short, spread = basis
    if terms.index in ("key_rate", "refinancing_rate"):
        return forward - short, ""
    if terms.index == "ruonia":
        if spread is None:
            return None, "базиса «ключевая − RUONIA» за окно нет"
        return forward - short - spread, ""
    return None, "индекс без форварда"


def _move(series: Series, day: date, start: date) -> str:
    """Направление ключевой между днём оценки и началом периода."""
    then, now = series.key_on(day), series.key_on(start)
    if then is None or now is None:
        return "неизвестно"
    if now > then:
        return "рост"
    if now < then:
        return "снижение"
    return "без изменения"


def check(
    plans: Iterable[tuple[dict, Schedule]],
    rules: dict,
    series: Series,
    horizons: tuple[int, ...],
    window: int,
) -> tuple[list[Row], Counter[str], Counter[str]]:
    """Строки сравнения, отказы по причинам и знаменатели."""
    stale = int(rules["rate_stale_days"])
    rows: list[Row] = []
    refused: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    for record, plan in plans:
        counts["графиков с записью выпуска"] += 1
        if str(record.get("floating_rate") or "") != "1":
            continue
        counts["флоатеров"] += 1
        terms = floating.terms_of(record, rules, None)
        if terms.kind != floating.ESTIMATE:
            refused[f"выпуск: формула не разобрана ({terms.kind})"] += 1
            continue
        group = GROUPS.get(terms.index, terms.index)
        counts[f"разобранных: {group}"] += 1
        placed = min((item.start for item in plan.payments if item.start), default=None)
        for payment in plan.payments:
            if not payment.coupon_known or payment.coupon <= 0:
                continue
            counts["зафиксированных купонов"] += 1
            if payment.start is None or plan.nominal is None:
                refused["купон: нет начала периода или номинала"] += 1
                continue
            if any(first <= (payment.number or -1) <= last for first, last, _ in terms.fixed):
                refused["купон: ставка по условиям выпуска"] += 1
                continue
            days = (payment.due - payment.start).days
            face = plan.nominal - sum(
                (item.redemption for item in plan.payments if item.due < payment.due),
                Decimal(0),
            )
            if days <= 0 or face <= 0:
                refused["купон: нет длины периода или номинала"] += 1
                continue
            actual = payment.rate
            if actual is None:
                actual = payment.coupon / face * 100 * YEAR / days
                counts["факт: ставка из суммы купона"] += 1
            else:
                counts["факт: ставка источника"] += 1
            for horizon in horizons:
                day = months_before(payment.start, horizon)
                if placed is not None and day < placed:
                    refused[f"h={horizon}: выпуска на дату ещё не было"] += 1
                    continue
                got = floating.rate_on(terms, day, stale)
                if got is None:
                    refused[f"h={horizon}: индекса на дату нет"] += 1
                    continue
                t1 = Decimal((payment.start - day).days) / YEAR
                t2 = Decimal((payment.due - day).days) / YEAR
                index, why = _forward_index(terms, series, day, t1, t2, window)
                if index is None:
                    refused[f"h={horizon}: {why}"] += 1
                    continue
                rows.append(
                    Row(
                        emission=plan.emission_id,
                        group=group,
                        horizon=horizon,
                        actual=actual,
                        current=floating.coupon_rate(terms, got[0])[0],
                        forward=floating.coupon_rate(terms, index)[0],
                        move=_move(series, day, payment.start),
                    )
                )
    return rows, refused, counts


def _pp(value: Decimal) -> str:
    """Процентные пункты для печати: два знака, запятая, знак минуса."""
    return f"{value:.2f}".replace(".", ",").replace("-", "−")


def _quantiles(errors: list[Decimal]) -> str:
    """Медиана, p10, p25, p75, p90 и медиана модуля ошибки — ячейки таблицы."""
    ordered = sorted(errors)
    absolute = sorted(abs(value) for value in errors)
    cells = [percentile(ordered, place) for place in (50, 10, 25, 75, 90)]
    cells.append(percentile(absolute, 50))
    return " | ".join(_pp(value) for value in cells)


def report(
    rows: list[Row],
    refused: Counter[str],
    counts: Counter[str],
    series: Series,
    horizons: tuple[int, ...],
    window: int,
) -> str:
    """Отчёт в markdown: шапка, знаменатели, отказы, ошибки по видам и горизонтам."""
    lines = [
        "# Купон флоатера: текущий индекс против форварда КБД",
        "",
        f"Горизонты, месяцев до начала периода: {', '.join(map(str, horizons))}; "
        f"окно базиса — {window} торговых дней КБД до дня оценки.",
        "",
        "## Ряды на диске",
        "",
        *series.span(),
        "",
        "## Знаменатели",
        "",
        *(f"- {name}: {value}" for name, value in sorted(counts.items())),
        f"- строк сравнения (купон × горизонт): {len(rows)}",
        "",
        "## Отказы",
        "",
        "| Причина | Число |",
        "|---|---|",
        *(f"| {name} | {value} |" for name, value in sorted(refused.items())),
        "",
        "## Ошибка оценки, п. п. (оценка − факт)",
        "",
        "h = 0 — контроль разбора. Купоны парные: оба способа на одной дате.",
        "",
        "| Индекс | h | Купонов | Выпусков | Способ | Медиана | p10 | p25 | p75 | p90 "
        "| Медиана модуля |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    grouped: dict[tuple[str, int], list[Row]] = defaultdict(list)
    for row in rows:
        grouped[row.group, row.horizon].append(row)
    for (group, horizon), found in sorted(grouped.items()):
        issues = len({row.emission for row in found})
        for method in METHODS:
            errors = [row.error(method) for row in found]
            lines.append(
                f"| {group} | {horizon} | {len(found)} | {issues} | {method} | "
                f"{_quantiles(errors)} |"
            )
    lines += [
        "",
        "## По направлению ключевой ставки между днём оценки и началом периода",
        "",
        "| Индекс | h | Направление | Купонов | Медиана: текущий | Медиана: форвард "
        "| Модуль: текущий | Модуль: форвард |",
        "|---|---|---|---|---|---|---|---|",
    ]
    moved: dict[tuple[str, int, str], list[Row]] = defaultdict(list)
    for row in rows:
        moved[row.group, row.horizon, row.move].append(row)
    for (group, horizon, move), found in sorted(moved.items()):
        cells = []
        for method in METHODS:
            cells.append(_pp(statistics.median(row.error(method) for row in found)))
        for method in METHODS:
            cells.append(_pp(statistics.median(abs(row.error(method)) for row in found)))
        lines.append(f"| {group} | {horizon} | {move} | {len(found)} | {' | '.join(cells)} |")
    return "\n".join(lines) + "\n"


def _plans() -> Iterable[tuple[dict, Schedule]]:
    """Графики с диска вместе с записью выпуска; без записи — пропуск."""
    for path in sorted(Path(CACHE).glob("flow_*.json")):
        emission = path.stem.removeprefix("flow_")
        record = floating.record_of(emission)
        plan = schedule_of(emission)
        if record is not None and plan is not None:
            yield record, plan


def main(argv: list[str] | None = None) -> int:
    """Проверка по всем графикам на диске; отчёт в stdout."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--basis-days", type=int, default=BASIS_DAYS)
    parser.add_argument(
        "--horizons", default=",".join(map(str, HORIZONS)),
        help="месяцы до начала периода через запятую",
    )
    args = parser.parse_args(argv)
    horizons = tuple(int(value) for value in args.horizons.split(","))
    rules = load_routing().refinancing.floating_coupons
    series = Series(int(rules["rate_stale_days"]))
    rows, refused, counts = check(_plans(), rules, series, horizons, args.basis_days)
    sys.stdout.write(report(rows, refused, counts, series, horizons, args.basis_days))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
