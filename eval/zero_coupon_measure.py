"""Перезамер правила zero_coupon при цене к PV: выключено против действующего. Только чтение.

    uv run python eval/zero_coupon_measure.py --until ГГГГ-ММ-ДД --split ГГГГ-ММ-ДД --database БАЗА

Поручение владельца 09.10.2026 (разбор диагностики, п. 4). Правило
сравнимости `zero_coupon` (`market.yaml`, `comparability.exclude`) отбрасывает
цену бумаги с купоном ноль: от номинала она низка по устройству. При мере
`pv_kbd` цена сравнивается с PV потока, и дисконт за срок в PV уже учтён —
довод правила под вопросом. **Замер по принципу 2**: парная разность прироста
(вариант − действующий) на одних и тех же бутстрэп-выборках эмитентов,
90 % интервал; решение — по поздней части календаря, ранняя и весь календарь
печатаются справочно. Применение — после 05.11.2026 при любом результате.

**Замер не считает сам.** Основания — боевые (`scoring.market.findings`),
ряды — боевая сборка (`sources.market.build`) с отношением к PV: действующий
ряд пересобирается и сверяется с сохранённым во всём, вариант собирается
той же сборкой без одного правила. Правило ломает только цену, поэтому
ориентир и спреды у варианта обязаны совпасть с действующим — это
проверяется, иначе разность мерила бы смену ряда.

**Круг и наблюдение — ряда варианта.** Снятое правило только добавляет точки
с ценой, и эмитент, у которого цена появилась лишь в варианте, в действующем
наблюдается, но основания не получает: это и есть цена правила, и она
считается, а не прячется.
"""

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL))

from market_lead_run import CONFIDENCE, HORIZON, cutoffs, events, systemic_issuers  # noqa: E402
from paired_67 import (  # noqa: E402
    check_snapshot,
    paired,
    result_row,
    split_cuts,
    tallies,
    validate_cuts,
    verify_rebuild,
)

from finlib.sources.market import Market, MarketPolicy, load_market, series  # noqa: E402
from finlib.sources.market import build as build_series  # noqa: E402

RULE = "zero_coupon"


def without_rule(policy: MarketPolicy, code: str) -> MarketPolicy:
    """Боевая методика без одного правила сравнимости; правила нет — отказ."""
    rules = policy.comparability["exclude"]
    kept = [item for item in rules if item["code"] != code]
    if len(kept) == len(rules):
        raise ValueError(f"правила {code} в методике нет: сравнивать нечего")
    return policy.model_copy(
        update={"comparability": {**policy.comparability, "exclude": kept}}
    )


def verify_variant(current: Market, variant: Market) -> tuple[int, int]:
    """Вариант совпадает с действующим во всём, кроме цены; число новых эмитентов и точек.

    Снятое правило ломало только цену: ориентир и спред у общих точек обязаны
    совпасть, а точек и эмитентов у варианта — не меньше.
    """
    if current.benchmark != variant.benchmark:
        raise ValueError("ориентир варианта расходится с действующим")
    if not current.issuers.keys() <= variant.issuers.keys():
        raise ValueError("у варианта пропали эмитенты: правило не только отбрасывало цену")
    added = 0
    for inn, own in current.issuers.items():
        theirs = variant.issuers[inn]
        if not own.keys() <= theirs.keys():
            raise ValueError("у варианта пропали точки эмитента")
        if any(own[day].spread != theirs[day].spread for day in own):
            raise ValueError("спред варианта расходится с действующим")
        added += len(theirs.keys() - own.keys())
    fresh = variant.issuers.keys() - current.issuers.keys()
    added += sum(len(variant.issuers[inn]) for inn in fresh)
    return len(fresh), added


def main() -> int:
    """Печатает парные интервалы по частям календаря."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
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
    policy = load_market()
    if policy.distress_zone.measure != "pv_kbd":
        raise ValueError("замер о правиле при цене к PV: в методике мера не pv_kbd")
    variant_policy = without_rule(policy, RULE)
    saved = series()
    days = saved.calendar()
    if not days or max(days) != args.until:
        raise ValueError("конец ряда не совпадает с зафиксированной конечной датой")
    cuts = cutoffs(days, days[0] + timedelta(days=HORIZON), args.until)
    validate_cuts(cuts, args.until)
    early, late = split_cuts(cuts, args.split)
    when = events()
    systemic = systemic_issuers()
    price = policy.distress_zone.ground
    review = {s.ground for s in policy.route_steps if s.basket == "review"} | {price}
    grounds = {"цена": {price}, "Разбор": review}

    print(f"Конец ряда: {args.until}; срезов: {len(cuts)}; полный горизонт {HORIZON} дней.\n")
    print(f"# Правило {RULE} при цене к PV: выключено против действующего\n")
    current = build_series(policy, ratios=True)
    verify_rebuild(saved, current)
    variant = build_series(variant_policy, ratios=True)
    fresh, added = verify_variant(current, variant)
    refused = sum(
        count for reason, count in current.counted.items() if reason == f"цена отброшена: {RULE}"
    )
    print(f"Действующий ряд пересобран и совпал с сохранённым. Строк, у которых "
          f"правило отбрасывает цену: {refused}. У варианта добавилось точек "
          f"эмитент-день: {added}, эмитентов, которых в действующем ряду нет: "
          f"{fresh}; ориентир и спреды совпали.\n")
    for part, part_cuts in (
        ("Поздняя часть — по ней решение", late),
        ("Ранняя часть", early),
        ("Весь календарь — исследовательская сводка, не основание решения", cuts),
    ):
        print(f"## {part}: срезов {len(part_cuts)}, {part_cuts[0]} — {part_cuts[-1]}\n")
        old = tallies(policy, current, when, systemic, grounds, part_cuts, frame=variant)
        new = tallies(policy, variant, when, systemic, grounds, part_cuts, frame=variant)
        print("| Вариант | Основание | Прирост действующий | Прирост вариант | "
              f"{CONFIDENCE} % интервал разности | пригодные / все | исключены | ИНН |")
        print("|---|---|---|---|---|---|---|---|")
        for key in grounds:
            print(result_row(f"{RULE} выключено", key, paired(old[key][0], new[key][0])))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
