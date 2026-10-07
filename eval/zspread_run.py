"""Замер Z-спреда против G: проверка на ОФЗ и три ступени ядра. Только диск, БД — чтение.

    uv run python eval/zspread_rows.py                 # строки дней (раз)
    uv run python eval/zspread_run.py > отчёт.md        # замер

**Замер не считает сам.** Ряд и ориентир собираются тем же правилом, что
у `sources.market.build` (сначала проверяется, что на G он воспроизводит
боевой ряд точь-в-точь), основания — `scoring.market.findings`, корзина —
`routing_rows` с подменённым рядом, календарь — поточечная мера
`market_lead_run`. Подменяется только величина спреда и состав ядра.

**Ступени** (решение владельца 01.10.2026), каждая против одной базы —
нынешнего G-ряда:

- а) Z на нынешнем ядре; рядом — G на тех же строках, у которых Z есть,
  чтобы отделить смену величины от смены охвата;
- б) Z на ядре без госбумаг — эмитенты типа «Государственный»
  и «Муниципальный» по источнику; госкомпании источник не отмечает;
- в) где пол 100 б. п. стоит в распределении ориентира чистого ядра.

Пороги ступеней p95/p99 у варианта пересчитываются из его распределения
(тем же `_quantiles`), пол и срок жизни прежние.
"""

import contextlib
import io
import logging
import pickle
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402
from market_lead_run import _market_pointwise, _quantiles, events  # noqa: E402
from zspread_rows import OUT  # noqa: E402

from finlib.sources.market import (  # noqa: E402
    Market,
    Point,
    _of_day,
    load_market,
    percentile,
    series,
)

logger = logging.getLogger(__name__)

GOVERNMENT = ("Государственный", "Муниципальный")
QUANTS = (10, 25, 50, 75, 90)


def _f(value: Decimal | float) -> str:
    """Число для таблицы: одна цифра после запятой."""
    return f"{float(value):+.1f}".replace(".", ",")


def build(rows: dict, value: int, keep, census: dict) -> Market:  # noqa: ANN001
    """Ряд варианта: величина спреда — столбец `value`, ядро — строки, прошедшие `keep`."""
    benchmark: dict[date, Decimal] = {}
    issuers: dict[str, dict[date, object]] = defaultdict(dict)
    policy = load_market()
    ceiling = Decimal(str(policy.spread["ceiling_bp"]))
    for name, found in rows.items():
        day = date.fromisoformat(name)
        core = sorted(
            item[value]
            for item in found
            if item[5] and item[value] is not None and item[value] <= ceiling and keep(item)
        )
        if len(core) < 5:
            continue
        benchmark[day] = percentile(core, int(policy.benchmark["percentile"]))
        mine: dict[str, list] = defaultdict(list)
        for item in found:
            if not item[1]:
                continue
            spread = item[value]
            if spread is not None and spread > ceiling:
                spread = None
            # **Отношения к PV у строк G/Z нет** (`zspread_rows.py` его
            # не пишет): четвёртое поле строки `_of_day` — «не считали»,
            # и точка выходит такой же, как у ряда, собранного без отношения.
            if spread is not None or item[7] is not None:
                mine[item[1]].append((spread, item[6], item[7], None))
        for inn, own in mine.items():
            issuers[inn][day] = _of_day(day, own)
    return Market(
        benchmark=benchmark,
        issuers=dict(issuers),
        counted={"строк": sum(len(found) for found in rows.values())},
        census=census,
        universe=0,
        with_isin=0,
    )


def with_steps(policy, market: Market):  # noqa: ANN001, ANN201
    """Методика варианта: пороги ступеней пересчитаны из его распределения."""
    places = dict(_quantiles(market))
    steps = [
        step.model_copy(update={"multiple": places.get(step.percentile, step.multiple)})
        for step in policy.ladder.steps
    ]
    ladder = policy.ladder.model_copy(update={"steps": steps})
    return policy.model_copy(update={"ladder": ladder})


def shift(base: Market, other: Market) -> str:
    """Сдвиг ориентира по дням: квантили разности и дни ниже пола."""
    policy = load_market()
    floor = policy.floor or Decimal(0)
    common = sorted(set(base.benchmark) & set(other.benchmark))
    diff = sorted(other.benchmark[day] - base.benchmark[day] for day in common)
    places = " / ".join(_f(percentile(diff, place)) for place in QUANTS)
    below = sum(1 for value in other.benchmark.values() if value < floor)
    autumn = [
        other.benchmark[day] - base.benchmark[day]
        for day in common
        if date(2024, 9, 1) <= day <= date(2024, 12, 31)
    ]
    late = [
        other.benchmark[day] - base.benchmark[day] for day in common if day >= date(2026, 1, 1)
    ]
    return (
        f"| {len(common)} | {places} | {_f(statistics.median(autumn)) if autumn else '—'} "
        f"| {_f(statistics.median(late)) if late else '—'} "
        f"| {_f(statistics.median(other.benchmark.values()))} | {below} |"
    )


def holders(policy, market: Market, day: date, systemic: set[str]) -> dict[str, str]:  # noqa: ANN001
    """Держатели рыночных оснований на дату: ИНН → основание."""
    from finlib.scoring.market import findings

    found: dict[str, str] = {}
    for inn in market.issuers:
        said = findings(policy, market, inn, day, systemic=inn in systemic)
        level = [item.ground for item in said if item.ground.startswith("market_spread")]
        if level:
            found[inn] = level[0]
    return found


def baskets(policy, market: Market, moments: list[date], memo: dict) -> dict:  # noqa: ANN001
    """Корзины маршрута на даты при подменённом ряде и методике рынка."""
    with route_variants.patched(policy, market):
        return route_variants.baskets(moments, memo)


def ofz_check(rows: dict) -> None:
    """Проверка реализации: ОФЗ к своей кривой, медиана и p95 |Z| по выпускам."""
    print("## Проверка реализации: ОФЗ к КБД\n")
    print(
        "Величина по выпуску — медиана за его дни; сводка — медиана и 95-й "
        "перцентиль |величины| по выпускам.\n"
    )
    print("| Период | Выпусков с Z | G: медиана / p95 | Z годовой | Z непрерывный | Z биржи |")
    print("|---|---|---|---|---|---|")
    periods = (
        ("все дни", date.min, date.max),
        ("09–12.2024", date(2024, 9, 1), date(2024, 12, 31)),
        ("2025", date(2025, 1, 1), date(2025, 12, 31)),
        ("2026", date(2026, 1, 1), date.max),
    )
    for label, start, end in periods:
        own: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for name, found in rows.items():
            day = date.fromisoformat(name)
            if not start <= day <= end:
                continue
            for item in found:
                if not item[0].startswith("SU") or item[2] is None:
                    continue
                own[item[0]]["g"].append(float(item[2]))
                if item[3] is not None:
                    own[item[0]]["a"].append(float(item[3]))
                if item[8] is not None:
                    own[item[0]]["c"].append(float(item[8]))
                if item[9] is not None:
                    own[item[0]]["x"].append(float(item[9]))

        def said(key: str, own: dict = own) -> str:
            values = sorted(
                abs(statistics.median(v[key])) for v in own.values() if v.get(key)
            )
            if not values:
                return "—"
            p95 = values[min(int(0.95 * len(values)), len(values) - 1)]
            return f"{statistics.median(values):.1f} / {p95:.1f} ({len(values)})"

        with_z = sum(1 for v in own.values() if v.get("a"))
        print(
            f"| {label} | {with_z} из {len(own)} | {said('g')} | {said('a')} "
            f"| {said('c')} | {said('x')} |"
        )
    print()


def coverage(rows: dict) -> None:
    """Охват Z: у скольких строк со спредом Z есть, и почему нет у остальных."""
    reasons: Counter[str] = Counter()
    core: Counter[str] = Counter()
    for found in rows.values():
        for item in found:
            if item[2] is None:
                continue
            reasons[item[4] or "есть"] += 1
            if item[5]:
                core[item[4] or "есть"] += 1
    print("## Охват Z\n")
    print("| Причина | Строк со спредом | из них ядро |")
    print("|---|---|---|")
    for name, count in reasons.most_common():
        print(f"| {name} | {count} | {core.get(name, 0)} |")
    print()


def composition(rows: dict, kind: dict, by_code: dict) -> set[str]:
    """Состав госбумаг в ядре: тип эмитента источника; возвращает их коды торгов."""
    def code(secid: str) -> str:
        return secid[:-1] if secid.startswith("SU") and len(secid) == 12 else secid

    days: Counter[str] = Counter()
    papers: dict[str, set[str]] = defaultdict(set)
    gov: set[str] = set()
    total = 0
    for found in rows.values():
        for item in found:
            if not item[5]:
                continue
            total += 1
            emission = by_code.get(code(item[0]))
            label = kind.get(emission, "") if emission else ""
            label = label or "тип не известен"
            days[label] += 1
            papers[label].add(item[0])
            if label in GOVERNMENT:
                gov.add(item[0])
    print("## Состав ядра по типу эмитента источника\n")
    print("| Тип эмитента (Cbonds `emitent_type_name_rus`) | Бумаг | Бумаго-дней | Доля |")
    print("|---|---|---|---|")
    for label, count in days.most_common():
        print(f"| {label} | {len(papers[label])} | {count} | {count / total:.1%} |")
    print(
        "\nГоскомпании источник не отмечает: признака государственного участия "
        "в записи выпуска нет, и отбор по ним был бы перечнем, а не правилом.\n"
    )
    unknown = sorted(papers.get("тип не известен", set()))
    if unknown:
        print(f"Тип не известен у {len(unknown)}: {', '.join(unknown[:30])}\n")
    names = sorted(gov)
    print(f"Госбумаги ядра ({len(names)}): {', '.join(names)}\n")
    return gov


def _g_only(point: object) -> object:
    """Точка без отношения к PV: строки G/Z его не несут, и сверяется только G."""
    if isinstance(point, Point):
        return replace(point, ratio=None, ratio_price=None, ratio_pv=None, unflowed=None)
    return point


def verify_g(rows: dict, base: Market) -> None:
    """Сверяет ориентир и все точки G в обе стороны до сравнения вариантов.

    **Отношение к PV в сверку не входит**: ряд на диске собирается с ним при
    `distress_zone.measure: pv_kbd`, а строки G/Z его не несут; спред, цена
    и оборот сверяются полностью.
    """
    again = build(rows, 2, lambda item: True, base.census)
    seen = {date.fromisoformat(name) for name in rows}
    expected_bench = {day: value for day, value in base.benchmark.items() if day in seen}
    expected_points = {
        inn: {day: _g_only(point) for day, point in own.items() if day in seen}
        for inn, own in base.issuers.items() if any(day in seen for day in own)
    }
    problems = []
    if again.benchmark != expected_bench:
        problems.append("ориентир")
    if again.issuers != expected_points:
        problems.append("точки эмитентов (включая лишние и пропущенные)")
    if problems:
        raise ValueError("G из pickle не воспроизводит сохранённый ряд: " + "; ".join(problems))


def main() -> int:
    """Печатает проверку на ОФЗ, охват и три ступени."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    with Path(OUT).open("rb") as handle:
        saved = pickle.load(handle)
    rows, kind, by_code = saved["rows"], saved["kind"], saved["by_code"]
    policy = load_market()
    base = series()
    print("# Z-спред против G-спреда: проверка и три ступени ядра\n")
    verify_g(rows, base)
    print(
        "Сверка сборки с боевым рядом на G: ориентир совпал, точки эмитентов совпали "
        f"({len(base.benchmark)} дней, {len(base.issuers)} эмитентов).\n"
    )
    ofz_check(rows)
    coverage(rows)
    gov = composition(rows, kind, by_code)

    has_z = lambda item: item[3] is not None  # noqa: E731
    variants = {
        "G на строках с Z": build(rows, 2, has_z, base.census),
        "а) Z, нынешнее ядро": build(rows, 3, lambda item: True, base.census),
        "б) Z, ядро без госбумаг": build(
            rows, 3, lambda item: item[0] not in gov, base.census
        ),
        "G, ядро без госбумаг": build(rows, 2, lambda item: item[0] not in gov, base.census),
    }
    print("## Сдвиг ориентира против базы (G, нынешнее ядро), б. п.\n")
    print(
        f"| Вариант | Дней | Разность по дням p{' / p'.join(map(str, QUANTS))} "
        "| Медиана 09–12.2024 | Медиана 2026 | Ориентир, медиана | Дней ниже пола |"
    )
    print("|---|---|---|---|---|---|---|")
    floor = policy.floor or Decimal(0)
    print(
        f"| база G | {len(base.benchmark)} | 0 | 0 | 0 "
        f"| {_f(statistics.median(base.benchmark.values()))} "
        f"| {sum(1 for v in base.benchmark.values() if v < floor)} |"
    )
    for name, market in variants.items():
        print(f"| {name} {shift(base, market)}")

    clean = variants["б) Z, ядро без госбумаг"]
    values = sorted(clean.benchmark.values())
    share = sum(1 for v in values if v < floor) / len(values)
    g_clean = sorted(variants["G, ядро без госбумаг"].benchmark.values())
    g_share = sum(1 for v in g_clean if v < floor) / len(g_clean)
    base_share = sum(1 for v in base.benchmark.values() if v < floor) / len(base.benchmark)
    print(
        f"\n## в) Пол {floor} б. п. в распределении ориентира\n\n"
        f"- база (G, ядро с госбумагами): квантиль **{base_share:.1%}**;\n"
        f"- G, ядро без госбумаг: **{g_share:.1%}**;\n"
        f"- Z, ядро без госбумаг: **{share:.1%}** "
        f"(p5 {percentile(values, 5):.0f}, p9 {percentile(values, 9):.0f}, "
        f"p25 {percentile(values, 25):.0f} б. п.). Пол не меняется.\n"
    )

    print("## Пороги ступеней из распределения варианта\n")
    print("| Вариант | " + " | ".join(f"p{p}" for p, _ in _quantiles(base)) + " |")
    print("|---|" + "---|" * len(_quantiles(base)))
    for name, market in [("база G", base), *variants.items()]:
        cells = " | ".join(f"{float(v):.2f}" for _, v in _quantiles(market))
        print(f"| {name} | {cells} |")
    print()

    from market_lead_run import systemic_issuers

    systemic = systemic_issuers()
    today = max(base.benchmark)
    chosen = {
        "а) Z, нынешнее ядро": variants["а) Z, нынешнее ядро"],
        "б) Z, ядро без госбумаг": clean,
    }
    print(f"## Держатели рыночных оснований на {today:%d.%m.%Y}\n")
    before = holders(policy, base, today, systemic)
    print(f"База: p99 {sum(1 for v in before.values() if v.endswith('extreme'))}, "
          f"p95 {sum(1 for v in before.values() if v.endswith('wide'))}.\n")
    for name, market in chosen.items():
        now = holders(with_steps(policy, market), market, today, systemic)
        came = sorted(set(now) - set(before))
        gone = sorted(set(before) - set(now))
        moved = sorted(inn for inn in set(now) & set(before) if now[inn] != before[inn])
        print(
            f"- {name}: p99 {sum(1 for v in now.values() if v.endswith('extreme'))}, "
            f"p95 {sum(1 for v in now.values() if v.endswith('wide'))}; пришли {len(came)}, "
            f"ушли {len(gone)}, сменили ступень {len(moved)}"
        )
    print()

    if "--no-baskets" not in sys.argv:
        moments = route_variants.monthly_moments(base)
        memo: dict = {}
        print(
            f"## Смены корзины маршрута ({len(moments)} дат: первые торговые дни "
            f"месяцев с 10.2025 и {today:%d.%m.%Y})\n"
        )
        said_base = baskets(policy, base, moments, memo)
        for name, market in chosen.items():
            said = baskets(with_steps(policy, market), market, moments, memo)
            found = route_variants.changes(said_base, said)
            print(f"### {name}\n")
            print(route_variants.summary(found, moments) + "\n")
            last = [item for item in found if item[0] == today]
            if last:
                listed = "; ".join(f"{inn} {a} → {b}" for _, inn, a, b in last)
                print(f"На {today:%d.%m.%Y}: {listed}\n")

    when = events()
    for name, market, rules in [
        ("база G", base, policy),
        *[(n, m, with_steps(policy, m)) for n, m in chosen.items()],
    ]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _market_pointwise(rules, market, when)
        print(f"## Календарь событий: {name}\n")
        print(buffer.getvalue().replace("## Основная мера: поточечно", "").strip() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
