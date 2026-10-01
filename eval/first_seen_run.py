"""Проверка на истории: запись дефолта без объявления — с первого появления в перечне.

    uv run python eval/first_seen_run.py > отчёт.md

Сколько записей появилось в снимках перечня после первого, у скольких из них
нет даты объявления, на сколько дней первое появление раньше даты, которой
запись датировалась прежде (`moment` — дата дефолта, то есть конец
льготного срока), и что меняется в маршруте на днях снимков. **Замер
не считает сам**: день известности — `DefaultRecord.known_on`, корзины —
`routing_rows`; база получается отключением дня появления.
"""

import contextlib
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402

from finlib.sources import cbonds_events  # noqa: E402

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def without_first_seen():  # noqa: ANN201
    """База: день появления не известен — запись датируется как прежде."""
    original = cbonds_events.first_seen
    cbonds_events.first_seen = dict
    try:
        yield
    finally:
        cbonds_events.first_seen = original


def main() -> int:
    """Печатает перепись записей и смены корзины на днях снимков."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    seen = cbonds_events.first_seen()
    records = [
        item
        for items in cbonds_events.default_records().values()
        for item in items
        if item.seen is not None
    ]
    silent = [item for item in records if item.announced is None]
    shifts = Counter(
        (item.moment - item.seen).days
        for item in silent
        if item.moment is not None
    )
    print("# Дефолт без даты объявления: с первого появления в перечне\n")
    print(
        f"Записей с днём первого появления (не было в прежнем снимке): **{len(seen)}**; "
        f"из них без даты объявления — **{len(silent)}**.\n"
    )
    print("| Первое появление раньше прежней даты, дней | Записей |\n|---|---|")
    for days, count in sorted(shifts.items()):
        print(f"| {days} | {count} |")
    print()
    for item in silent:
        print(
            f"- выпуск {item.emission_id}: {item.kind.lower()}, {item.status}; срок "
            f"{item.due}, дата дефолта {item.when}, в перечне с {item.seen}, "
            f"исполнено {item.met or '—'}"
        )
    moments = sorted({day for day in seen.values() if day >= date(2026, 9, 25)})
    # Память пересчёта у вариантов своя: она хранит события эмитента,
    # а они-то и различаются.
    with without_first_seen():
        before = route_variants.baskets(moments, {})
    after = route_variants.baskets(moments, {})
    found = route_variants.changes(before, after)
    grounds = [
        (moment, inn, set(after[moment][inn][1]) - set(before[moment].get(inn, ("", ()))[1]))
        for moment in moments
        for inn in after[moment]
        if set(after[moment][inn][1]) != set(before[moment].get(inn, ("", ()))[1])
    ]
    print("\n## Маршрут на днях снимков\n")
    print(route_variants.summary(found, moments) + "\n")
    for moment, inn, was, now in found:
        print(f"- {moment:%d.%m.%Y} {inn}: {was} → {now}")
    print(f"\nОснования сменились без смены корзины либо вместе с ней: {len(grounds)}\n")
    for moment, inn, added in grounds:
        print(f"- {moment:%d.%m.%Y} {inn}: добавилось {', '.join(sorted(added)) or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
