"""Неустановленные купоны (v2 с уточнениями а–б): смены корзины и календарь. Только диск, чтение БД.

    uv run python eval/coupons_run.py > отчёт.md

База — пустой купон читается нулём (как в main); вариант — оценка
`sources.floating` по блоку `refinancing.floating_coupons`. **Замер
не считает сам**: корзину называет `routing_rows`, база получается
отключением оценщика, а не вторым расчётом.

Ограничение истории названо: страница ключевой ставки Банка России
сохранена с 03.01.2025, RUONIA — только за сентябрь 2026 года; на датах
раньше ряд RUONIA несвежий, и такие выпуски идут границей снизу, а не
оценкой. Запросов к Банку России ночью не было.
"""

import logging
import sys
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402
from market_lead_run import events  # noqa: E402

from finlib.db import fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources import floating  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402
from finlib.sources.cbonds_flows import schedule_of  # noqa: E402
from finlib.sources.market import series, universe  # noqa: E402

logger = logging.getLogger(__name__)


def census(today: date, rules: dict) -> None:
    """Выпуски с неустановленным купоном в окне года на дату: правило оценки по видам."""
    kinds: Counter[str] = Counter()
    lowers: list[str] = []
    floors: list[str] = []
    for inn in universe():
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            if str(getattr(issue, "status", "")) not in ("в обращении", "размещается"):
                continue
            plan = schedule_of(issue.emission_id)
            if plan is None:
                continue
            ahead = [
                item
                for item in plan.payments
                if today <= item.due < today + timedelta(days=365) and not item.coupon_known
            ]
            if not ahead:
                continue
            record = floating.record_of(issue.emission_id) or {}
            terms = floating.terms_of(record, rules, floating.last_rate(plan))
            label = terms.kind + (f": {terms.index}" if terms.index else "")
            # Ставка по условиям выпуска (3.1) — данные: выпуск считается
            # здесь, если она покрывает хоть один купон окна.
            if any(
                first <= (item.number or 0) <= last
                for item in ahead
                for first, last, _ in terms.fixed
            ):
                label = "по условиям выпуска"
            elif terms.kind == floating.ESTIMATE and (terms.floor or terms.cap):
                found = floating.estimate(
                    terms, Decimal(100), 365, today, int(rules["rate_stale_days"])
                )
                label += (
                    ", пол связывает" if "пол" in found.basis
                    else ", потолок связывает" if "потолок" in found.basis
                    else ", пол/потолок не связывает"
                )
            kinds[label] += 1
            name = f"{inn} {record.get('document_rus') or issue.emission_id}"
            if terms.kind == floating.LOWER and label != "по условиям выпуска":
                lowers.append(f"{name} — {record.get('reference_rate_name_rus') or 'не флоатер'}")
            if terms.kind == floating.FLOOR and label != "по условиям выпуска":
                floors.append(f"{name} — пол {terms.floor} %")
    print(f"## Выпуски с неустановленным купоном в окне на {today:%d.%m.%Y}\n")
    print("| Правило | Выпусков |\n|---|---|")
    for label, count in kinds.most_common():
        print(f"| {label} | {count} |")
    print(f"\nГраница по полу ({len(floors)}):\n")
    for line in floors:
        print(f"- {line}")
    print(f"\n«Не менее» ({len(lowers)}), первые 60:\n")
    for line in lowers[:60]:
        print(f"- {line}")
    print()


def main() -> int:
    """Печатает перепись правил, смены корзины и календарь."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    rules = load_routing().refinancing.floating_coupons
    if not rules:
        print("блока refinancing.floating_coupons в методике нет — мерить нечего")
        return 1
    market = series()
    when = events()
    moments = route_variants.monthly_moments(market)
    last_day = max(market.benchmark)
    print("# Неустановленные купоны (v2, уточнения а–б): замер против базы\n")
    census(date(2026, 10, 1), rules)
    memo: dict = {}
    before = route_variants.baskets(moments, memo, coupons=False)
    after = route_variants.baskets(moments, memo, coupons=True)
    print("## Календарь событий\n")
    print(route_variants.calendar("база (пустой купон — ноль)", before, when, last_day) + "\n")
    print(route_variants.calendar("оценка купонов", after, when, last_day) + "\n")
    found = route_variants.changes(before, after)
    print("## Смены корзины\n")
    print(route_variants.summary(found, moments) + "\n")
    names = {row["inn"]: row["name"] for row in fetch_all("SELECT inn, name FROM organization")}
    today = max(moments)
    for moment, inn, was, now in found:
        if moment != today:
            continue
        print(
            f"- {inn} {names.get(inn, '')}: {was} → {now}; основания: "
            + ", ".join(after[moment][inn][1])
        )
    grown = [
        inn
        for inn, said in after[today].items()
        if "refinancing_gap" in said[1]
        and "refinancing_gap" not in before[today].get(inn, ("", ()))[1]
    ]
    print(
        f"\nОснование «платежи года» появилось на {today:%d.%m.%Y} у {len(grown)} "
        f"эмитентов (корзину из них сменили {sum(1 for m, *_ in found if m == today)}).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
