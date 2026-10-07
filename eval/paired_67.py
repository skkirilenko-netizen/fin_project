"""Парная разность прироста рыночных оснований: цена/PV (6) и Z без госбумаг (7). Только чтение.

    # 6 — ранняя и поздняя части календаря, оба варианта подстановки:
    uv run python eval/paired_67.py --until ГГГГ-ММ-ДД --split ГГГГ-ММ-ДД --database БАЗА
    # 6 и 7 — с сохранёнными строками G/Z (`eval/zspread_rows.py`):
    uv run python eval/paired_67.py --until … --split … --database … --input ПУТЬ.pkl

**Замер 6 не считает сам.** Признак по PV — боевой (`distress_zone.measure:
pv_kbd`, `scoring.market.findings`), ряд с отношением — боевая сборка
(`sources.market.build`), сверенная с сохранённым рядом во всём, кроме
отношения. Строки G/Z нужны только замеру 7.

**Решение принимается по поздней части** (ROADMAP, принцип 2): порог 0,6
не подбирается, а вариант подстановки — выбор, и интервал на всём
календаре печатается справочно, как исследовательская сводка.

**Замер 7 размечен так же** — поздняя, ранняя части и весь календарь.
Ступени: а) Z на полном ядре, б) Z на ядре без госбумаг — обе против G
на полном ядре; пороги ступеней варианта — из его ранней части; при каждой —
дни ориентира ниже пола и квантиль пола. Круг и наблюдение — боевого ряда
(`tallies(frame=…)`): ряд из строк G/Z покрывает не те же дни.
"""

import argparse
import contextlib
import io
import logging
import pickle
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))

from market_lead_run import (  # noqa: E402
    CONFIDENCE,
    HORIZON,
    REPLICAS,
    SEED,
    _market_pointwise,
    cutoffs,
    events,
    pointwise,
    systemic_issuers,
)
from zspread_run import build, composition, verify_g, with_steps  # noqa: E402

from finlib.scoring.market import findings  # noqa: E402
from finlib.sources.market import Market, MarketPolicy, load_market, series  # noqa: E402
from finlib.sources.market import build as build_series  # noqa: E402

Tally = tuple[int, int, int, int]


@dataclass(frozen=True)
class PairedResult:
    """Парный результат с явной неопределённостью и знаменателем реплик."""

    old: Decimal | None
    new: Decimal | None
    low: Decimal | None
    high: Decimal | None
    valid: int
    excluded: int
    members: int


def lift(tallies: list[Tally]) -> Decimal | None:
    """Прирост без подмены неопределённости нулём; ноль попаданий допустим."""
    standing, hits, events_total, seen = (sum(row[i] for row in tallies) for i in range(4))
    if not standing or not events_total or not seen:
        return None
    return Decimal(hits) * Decimal(seen) / (Decimal(standing) * Decimal(events_total))


def validate_pair(old: dict[str, Tally], new: dict[str, Tally]) -> None:
    """Отказывает при различии ИНН или знаменателей, не дополняя круг нулями."""
    if old.keys() != new.keys():
        raise ValueError(
            f"охват ИНН различается: только прежний {sorted(old.keys() - new.keys())}; "
            f"только новый {sorted(new.keys() - old.keys())}"
        )
    for label, rows in (("прежний", old), ("новый", new)):
        for inn, tally in rows.items():
            if len(tally) != 4 or any(type(v) is not int or v < 0 for v in tally):
                raise ValueError(f"{label}, {inn}: некорректные счётчики")
            standing, hits, events_total, seen = tally
            if hits > min(standing, events_total) or max(standing, events_total) > seen:
                raise ValueError(f"{label}, {inn}: несогласованные счётчики")
    different = [inn for inn in old if old[inn][2:] != new[inn][2:]]
    if different:
        raise ValueError(f"события/наблюдения различаются по ИНН: {sorted(different)}")


def validate_cuts(cuts: list[date], until: date) -> None:
    """Требует полного горизонта у выбранного торгового дня, а не начала месяца."""
    if cuts != sorted(set(cuts)):
        raise ValueError("срезы повторяются или нарушен порядок")
    bad = [cut.isoformat() for cut in cuts if cut + timedelta(days=HORIZON) > until]
    if bad:
        raise ValueError(f"неполный горизонт {HORIZON} дней: {bad}")


def tallies(
    policy: MarketPolicy, market: Market, when: dict[str, date], systemic: set[str],
    grounds_of: dict[str, set[str]], cuts: list[date], frame: Market | None = None,
) -> dict:
    """ИНН → счётчик поточечной меры по набору оснований, боевым `findings`.

    **Круг и наблюдение — у ряда-рамки** (`frame`, по умолчанию сам ряд).
    Ряд варианта, собранный из строк G/Z, покрывает не те же дни и не тех же
    эмитентов, что боевой: день без ядра варианта выпадает у всех, а парная
    разность требует одного знаменателя. Эмитент, которого в ряду варианта
    нет к срезу, в нём наблюдается, но основания не получает — это и есть
    цена смены охвата, и она считается, а не прячется.
    """
    frame = frame or market
    circle = set(frame.issuers)
    cache: dict = {}

    def said(inn: str, day: date) -> tuple:
        if (inn, day) not in cache:
            cache[(inn, day)] = findings(policy, market, inn, day, systemic=inn in systemic)
        return cache[(inn, day)]

    def observed(inn: str, day: date) -> bool:
        own = frame.ordered(inn)
        return bool(own) and own[0].day <= day

    found = {}
    for name, wanted in grounds_of.items():
        done = pointwise(
            name,
            lambda inn, day, w=wanted: any(item.ground in w for item in said(inn, day)),
            observed,
            circle,
            when,
            cuts,
        )
        found[name] = (dict(zip(sorted(circle), done.tallies, strict=True)), done)
    return found


def paired(old: dict[str, Tally], new: dict[str, Tally]) -> PairedResult:
    """Прирост прежнего, нового и 90 % интервал разности (новый − прежний) по эмитентам."""
    validate_pair(old, new)
    members = sorted(old)
    a = [old[inn] for inn in members]
    b = [new[inn] for inn in members]
    rng = random.Random(SEED)
    diffs = []
    empty = 0
    for _ in range(REPLICAS):
        pick = [rng.randrange(len(members)) for _ in members]
        before = lift([a[i] for i in pick])
        after = lift([b[i] for i in pick])
        if before is None or after is None:
            empty += 1
            continue
        diffs.append(after - before)
    diffs.sort()
    tail = (100 - CONFIDENCE) / 200
    low = diffs[int(tail * (len(diffs) - 1))] if diffs else None
    high = diffs[int((1 - tail) * (len(diffs) - 1))] if diffs else None
    return PairedResult(lift(a), lift(b), low, high, len(diffs), empty, len(members))


def shown(value: Decimal | None) -> str:
    """Печатает неопределённость словами, определённый прирост — одной цифрой."""
    return "не определено" if value is None else str(value.quantize(Decimal("0.1")))


def result_row(label: str, key: str, result: PairedResult) -> str:
    """Называет обе меры, интервал и все пригодные/исключённые реплики."""
    return (
        f"| {label} | {key} | {shown(result.old)} | {shown(result.new)} | "
        f"[{shown(result.low)}; {shown(result.high)}] | "
        f"{result.valid} / {result.valid + result.excluded} | "
        f"{result.excluded} | {result.members} |"
    )


def pv_policy(policy: MarketPolicy, substitution: bool) -> MarketPolicy:
    """Боевая методика с признаком по PV и явно названной подстановкой."""
    zone = policy.distress_zone.model_copy(
        update={"measure": "pv_kbd", "substitution": substitution}
    )
    return policy.model_copy(update={"distress_zone": zone})


def verify_rebuild(saved: Market, rebuilt: Market) -> None:
    """Ряд, пересобранный с отношением, обязан совпасть с сохранённым во всём прочем.

    Иначе разность прироста мерила бы вместе с признаком и смену ряда.
    Расхождение называется числом эмитентов, без их перечня: вывод замера —
    только агрегаты.
    """
    if not rebuilt.ratios:
        raise ValueError("ряд пересобран без отношения цены к PV")
    if saved.benchmark != rebuilt.benchmark:
        raise ValueError("ориентир пересобранного ряда расходится с сохранённым")
    if saved.issuers.keys() != rebuilt.issuers.keys():
        raise ValueError(
            "состав эмитентов пересобранного ряда расходится с сохранённым: "
            f"{len(saved.issuers.keys() ^ rebuilt.issuers.keys())}"
        )
    different = sum(
        1
        for inn, own in saved.issuers.items()
        if own.keys() != rebuilt.issuers[inn].keys()
        or any(
            (own[day].spread, own[day].price)
            != (rebuilt.issuers[inn][day].spread, rebuilt.issuers[inn][day].price)
            for day in own
        )
    )
    if different:
        raise ValueError(f"цена или спред расходятся с сохранённым рядом у {different} эмитентов")


def split_cuts(cuts: list[date], split: date) -> tuple[list[date], list[date]]:
    """Ранняя часть — срезы до `split`, поздняя — с него; пустая часть — отказ."""
    early = [cut for cut in cuts if cut < split]
    late = [cut for cut in cuts if cut >= split]
    if not early or not late:
        raise ValueError(
            f"ранняя ({len(early)}) или поздняя ({len(late)}) часть пуста: выберите другой --split"
        )
    return early, late


def training(market: Market, split: date) -> Market:
    """Ряд ранней части: по нему пересчитываются пороги ступеней варианта (принцип 2)."""
    return Market(
        benchmark={day: value for day, value in market.benchmark.items() if day < split},
        issuers=market.issuers,
        counted=market.counted,
        census=market.census,
        universe=market.universe,
        with_isin=market.with_isin,
        ratios=market.ratios,
    )


def below_floor(market: Market, floor: Decimal, within: Callable[[date], bool]) -> tuple[int, int]:
    """Дней ориентира части ниже пола и всего дней ориентира части."""
    values = [value for day, value in market.benchmark.items() if within(day)]
    return sum(1 for value in values if value < floor), len(values)


def uncovered(frame: Market, market: Market, cuts: list[date]) -> tuple[int, int]:
    """Наблюдений эмитент-срез у рамки, где у варианта к срезу точки нет, и всех наблюдений."""
    missing = total = 0
    for inn in frame.issuers:
        own, mine = frame.ordered(inn), market.ordered(inn)
        for cut in cuts:
            if own and own[0].day <= cut:
                total += 1
                missing += int(not mine or mine[0].day > cut)
    return missing, total


Part = tuple[str, list[date], Callable[[date], bool]]


def measure7(
    policy: MarketPolicy, base: Market, variants: dict[str, tuple[MarketPolicy, Market]],
    when: dict[str, date], systemic: set[str], grounds: dict[str, set[str]],
    parts: list[Part],
) -> list[str]:
    """Таблицы замера 7 по частям календаря: каждая ступень против G на полном ядре.

    **Пустой результат — отказ с причиной, а не пустая таблица.** Строки
    собираются целиком до печати: таблица с заголовком и без строк читалась
    бы как «изменений нет», хотя замер не досчитан.
    """
    floor = policy.floor
    if floor is None:
        raise ValueError("замер 7: пол ориентира в методике не объявлен")
    if not grounds:
        raise ValueError("замер 7: сравнивать нечего — у лестницы нет ступеней и нет «Разбора»")
    if not variants:
        raise ValueError("замер 7: ступеней нет")
    lines: list[str] = []
    for title, cuts, within in parts:
        if not cuts:
            raise ValueError(f"замер 7, {title}: срезов нет")
        before = tallies(policy, base, when, systemic, grounds, cuts)
        rows: list[str] = []
        below, days = below_floor(base, floor, within)
        if not days:
            raise ValueError(f"замер 7, {title}: у базы G нет дней ориентира в части")
        floors = [f"| G, полное ядро (база) | {below} из {days} | {below / days:.1%} | — |"]
        for label, (rule, market) in variants.items():
            new = tallies(rule, market, when, systemic, grounds, cuts, frame=base)
            for key in grounds:
                result = paired(before[key][0], new[key][0])
                if not result.members:
                    raise ValueError(f"замер 7, {title}, {label}, {key}: круг эмитентов пуст")
                rows.append(result_row(label, key, result))
            below, days = below_floor(market, floor, within)
            if not days:
                raise ValueError(
                    f"замер 7, {title}, {label}: у варианта нет дней ориентира в части"
                )
            missing, total = uncovered(base, market, cuts)
            floors.append(
                f"| {label} | {below} из {days} | {below / days:.1%} | {missing} из {total} |"
            )
        if not rows:
            raise ValueError(f"замер 7, {title}: ни одной строки сравнения")
        lines += [
            f"## {title}: срезов {len(cuts)}, {cuts[0]} — {cuts[-1]}\n",
            "| Ступень | Основание | Прирост G | Прирост варианта | "
            f"{CONFIDENCE} % интервал разности | пригодные / все | исключены | ИНН |",
            "|---|---|---|---|---|---|---|---|",
            *rows,
            "",
            f"| Ряд | Дней ориентира ниже пола {floor} б. п. | Квантиль пола в ядре "
            "| Наблюдений без точки варианта |",
            "|---|---|---|---|",
            *floors,
            "",
        ]
    return lines


def check_snapshot(metadata: dict, expected: str) -> None:
    """Требует явно согласованную базу и серверный запрет записи."""
    if metadata != {"db": expected, "ro": "on"}:
        raise ValueError("замер требует явно согласованную базу снимка и READ ONLY")


def main() -> int:
    """Печатает парные интервалы для двух решений."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="строки G/Z: нужны только замеру 7")
    parser.add_argument("--until", type=date.fromisoformat, required=True)
    parser.add_argument("--split", type=date.fromisoformat, required=True,
                        help="первый срез поздней части")
    parser.add_argument("--database", required=True, help="согласованная неизменная база снимка")
    args = parser.parse_args()
    from finlib.db import connection, fetch_one

    with connection() as conn:
        got = fetch_one(
            "SELECT current_database() AS db, current_setting('transaction_read_only') AS ro",
            conn=conn,
        )
        check_snapshot(got, args.database)
    loaded = load_market()
    # Прежний признак — от номинала, какое бы измерение ни стояло в методике:
    # сравнение «новый − прежний» не должно зависеть от того, переключено ли оно.
    policy = loaded.model_copy(update={
        "distress_zone": loaded.distress_zone.model_copy(update={"measure": "nominal"})})
    base = series()
    days = base.calendar()
    if not days or max(days) != args.until:
        raise ValueError("конец ряда не совпадает с зафиксированной конечной датой")
    cuts = cutoffs(days, days[0] + timedelta(days=HORIZON), args.until)
    validate_cuts(cuts, args.until)
    early, late = split_cuts(cuts, args.split)
    when = events()
    systemic = systemic_issuers()
    price = policy.distress_zone.ground
    steps = {f"p{s.percentile}": {s.ground} for s in policy.route_steps}
    review = {s.ground for s in policy.route_steps if s.basket == "review"} | {price}

    print(f"Конец ряда: {args.until}; срезов: {len(cuts)}; полный горизонт {HORIZON} дней.\n")
    print("# 6. Цена / PV по КБД: парная разность прироста ценового основания\n")
    rebuilt = build_series(policy, ratios=True)
    verify_rebuild(base, rebuilt)
    points = [item for own in rebuilt.issuers.values() for item in own.values()]
    print(f"Ряд пересобран с отношением и совпал с сохранённым по ориентиру, ценам "
          f"и спредам. Точек эмитент-день с ценой: "
          f"{sum(1 for item in points if item.price is not None)}, с отношением: "
          f"{sum(1 for item in points if item.ratio is not None)}, с ценой бумаги "
          f"без потока: {sum(1 for item in points if item.unflowed is not None)}.\n")
    print("Строк бумаг, у которых поток не построен, по причинам:\n")
    for reason, count in sorted(rebuilt.counted.items()):
        if reason.startswith("поток не построен: "):
            print(f"- {reason.removeprefix('поток не построен: ')}: {count}")
    print()
    grounds6 = {"цена": {price}, "Разбор": review}
    variants = (("цена/PV, без потока — цена от номинала", pv_policy(policy, True)),
                ("цена/PV, только где поток построен", pv_policy(policy, False)))
    for part, part_cuts in (
        ("Поздняя часть — по ней решение", late),
        ("Ранняя часть", early),
        ("Весь календарь — исследовательская сводка, не основание решения", cuts),
    ):
        print(f"## {part}: срезов {len(part_cuts)}, {part_cuts[0]} — {part_cuts[-1]}\n")
        old = tallies(policy, base, when, systemic, grounds6, part_cuts)
        print("| Вариант | Основание | Прирост прежний | Прирост новый | "
              f"{CONFIDENCE} % интервал разности | пригодные / все | исключены | ИНН |")
        print("|---|---|---|---|---|---|---|---|")
        for label, rule in variants:
            new = tallies(rule, rebuilt, when, systemic, grounds6, part_cuts)
            for key in grounds6:
                print(result_row(label, key, paired(old[key][0], new[key][0])))
        print()

    if args.input is None:
        print("# 7. Не считался: строки G/Z не переданы (--input)\n")
        return 0
    with args.input.open("rb") as handle:
        saved = pickle.load(handle)
    rows, kind, by_code = saved["rows"], saved["kind"], saved["by_code"]
    verify_g(rows, base)
    with contextlib.redirect_stdout(io.StringIO()):
        gov = composition(rows, kind, by_code)
    clean = build(rows, 3, lambda item: item[0] not in gov, base.census)
    full = build(rows, 3, lambda item: True, base.census)
    # Пороги ступеней варианта — из его распределения ранней части: поздняя
    # часть, по которой решение, в выбор порога не входит (принцип 2).
    variants = {
        "а) Z, полное ядро": (with_steps(policy, training(full, args.split)), full),
        "б) Z, ядро без госбумаг": (with_steps(policy, training(clean, args.split)), clean),
    }
    grounds = {**steps, "Разбор": review}
    parts: list[Part] = [
        ("Поздняя часть — по ней решение", late, lambda day: day >= args.split),
        ("Ранняя часть", early, lambda day: day < args.split),
        ("Весь календарь — исследовательская сводка, не основание решения", cuts,
         lambda day: True),
    ]
    tables = measure7(policy, base, variants, when, systemic, grounds, parts)
    eve = []
    clean_rule = variants["б) Z, ядро без госбумаг"][0]
    for name, market, rule in (("G, полное ядро", base, policy),
                               ("б) Z, ядро без госбумаг", clean, clean_rule)):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _market_pointwise(rule, market, when)
        text = buffer.getvalue()
        eve += [f"## Накануне события, весь календарь: {name}\n",
                text[text.index("| Основание | Стоит накануне"):].split("\n\n")[0] + "\n"]
    print("# 7. Z и ядро без ОФЗ, субфедеральных и муниципальных против G "
          "на полном ядре\n")
    print(f"Госбумаг в ядре исключено: {len(gov)}. Каждая ступень — против G "
          "на полном ядре (боевой ряд), круг и наблюдение эмитентов — его; "
          "эмитент без точки варианта к срезу наблюдается, но основания не получает.\n")
    print("Пороги ступеней варианта — из его ранней части (до "
          f"{args.split:%d.%m.%Y}): " + "; ".join(
              f"{label}: " + ", ".join(
                  f"p{step.percentile} {step.multiple:.2f}" for step in rule.ladder.steps)
              for label, (rule, _) in variants.items()) + "\n")
    print("\n".join(tables))
    print("\n".join(eve))
    return 0


if __name__ == "__main__":
    sys.exit(main())
