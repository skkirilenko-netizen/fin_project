"""Парная разность прироста рыночных оснований: цена/PV (6) и Z без госбумаг (7). Только чтение."""

import argparse
import contextlib
import io
import logging
import pickle
import random
import sys
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
from price_ratio_run import variant  # noqa: E402
from zspread_run import build, composition, verify_g, with_steps  # noqa: E402

from finlib.scoring.market import findings  # noqa: E402
from finlib.sources.market import Market, MarketPolicy, load_market, series  # noqa: E402

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
    grounds_of: dict[str, set[str]], cuts: list[date],
) -> dict:
    """ИНН → счётчик поточечной меры по набору оснований, боевым `findings`."""
    circle = set(market.issuers)
    cache: dict = {}

    def said(inn: str, day: date) -> tuple:
        if (inn, day) not in cache:
            cache[(inn, day)] = findings(policy, market, inn, day, systemic=inn in systemic)
        return cache[(inn, day)]

    def observed(inn: str, day: date) -> bool:
        own = market.ordered(inn)
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


def check_snapshot(metadata: dict, expected: str) -> None:
    """Требует явно согласованную базу и серверный запрет записи."""
    if metadata != {"db": expected, "ro": "on"}:
        raise ValueError("замер требует явно согласованную базу снимка и READ ONLY")


def main() -> int:
    """Печатает парные интервалы для двух решений."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--until", type=date.fromisoformat, required=True)
    parser.add_argument("--database", required=True, help="согласованная неизменная база снимка")
    args = parser.parse_args()
    from finlib.db import connection, fetch_one

    with connection() as conn:
        got = fetch_one(
            "SELECT current_database() AS db, current_setting('transaction_read_only') AS ro",
            conn=conn,
        )
        check_snapshot(got, args.database)
    with args.input.open("rb") as handle:
        saved = pickle.load(handle)
    rows, kind, by_code = saved["rows"], saved["kind"], saved["by_code"]
    policy = load_market()
    base = series()
    verify_g(rows, base)
    days = base.calendar()
    if not days or max(days) != args.until:
        raise ValueError("конец ряда не совпадает с зафиксированной конечной датой")
    cuts = cutoffs(days, days[0] + timedelta(days=HORIZON), args.until)
    validate_cuts(cuts, args.until)
    when = events()
    systemic = systemic_issuers()
    price = policy.distress_zone.ground
    steps = {f"p{s.percentile}": {s.ground} for s in policy.route_steps}
    review = {s.ground for s in policy.route_steps if s.basket == "review"} | {price}

    print("# Исследовательская сводка 6–7; не поздняя проверка принятия методики\n")
    print(f"Конец ряда: {args.until}; срезов: {len(cuts)}; полный горизонт {HORIZON} дней.\n")
    print("# 6. Цена / PV по КБД: парная разность прироста ценового основания\n")
    old = tallies(policy, base, when, systemic, {"цена": {price}, "Разбор": review}, cuts)
    with_fallback, _, _ = variant(base, rows, True)
    pure, _, _ = variant(base, rows, False)
    print("| Вариант | Основание | Прирост прежний | Прирост новый | "
          f"{CONFIDENCE} % интервал разности | пригодные / все | исключены | ИНН |")
    print("|---|---|---|---|---|---|---|---|")
    for label, market in (("цена/PV, без отношения — от номинала", with_fallback),
                          ("цена/PV, только где отношение есть", pure)):
        new = tallies(policy, market, when, systemic, {"цена": {price}, "Разбор": review}, cuts)
        for key in ("цена", "Разбор"):
            print(result_row(label, key, paired(old[key][0], new[key][0])))
    print()

    print("# 7. Z, ядро без ОФЗ, субфедеральных и муниципальных, против G и полного ядра\n")
    with contextlib.redirect_stdout(io.StringIO()):
        gov = composition(rows, kind, by_code)
    clean = build(rows, 3, lambda item: item[0] not in gov, base.census)
    full = build(rows, 3, lambda item: True, base.census)
    rules = with_steps(policy, clean)
    full_rules = with_steps(policy, full)
    grounds = {**steps, "Разбор": review}
    before = tallies(policy, base, when, systemic, grounds, cuts)
    middle = tallies(full_rules, full, when, systemic, grounds, cuts)
    after = tallies(rules, clean, when, systemic, grounds, cuts)
    print(f"Госбумаг в ядре исключено: {len(gov)}\n")
    print("| Ступень | Основание | Прирост прежний | Прирост новый | "
          f"{CONFIDENCE} % интервал разности | пригодные / все | исключены | ИНН |")
    print("|---|---|---|---|---|---|---|---|")
    for key in grounds:
        print(result_row("G → Z, полное ядро", key, paired(before[key][0], middle[key][0])))
        print(result_row("Z: полное → чистое ядро", key, paired(middle[key][0], after[key][0])))
    print()
    floor = policy.floor
    print(f"Дней ниже пола {floor}: G полное — "
          f"{sum(1 for v in base.benchmark.values() if v < floor)} из {len(base.benchmark)}, "
          f"Z чистое — {sum(1 for v in clean.benchmark.values() if v < floor)} "
          f"из {len(clean.benchmark)}\n")
    for name, market, rule in (("G, полное ядро", base, policy), ("Z, чистое ядро", clean, rules)):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _market_pointwise(rule, market, when)
        text = buffer.getvalue()
        print(f"## Накануне события: {name}\n")
        print(text[text.index("| Основание | Стоит накануне"):].split("\n\n")[0] + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
