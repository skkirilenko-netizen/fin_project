"""Влияние периметра по выпускам: у кого меняется корзина. Только чтение.

    uv run python eval/no_bonds_impact.py [--as-of ГГГГ-ММ-ДД]

Поручение владельца 09.10.2026: до слияния `claude/no-bonds-perimeter` —
список эмитентов, у которых меняется корзина (эмитент, ИНН, было → стало,
основания), и итог по корзинам. **«Было»** — последняя точка `routing_day`
(маршрут боевого кода на эту дату), **«стало»** — боевой путь этой ветки
(`routing_rows`) на тот же день с откатом транзакции. Данные между ними
те же, если запуск идёт до следующей доставки; смена по иной причине
называется своей строкой («иное: данные или календарь»), а не выдаётся
за действие периметра.

Ветка построена поверх `claude/bankruptcy-ground`: смены по банкротству
названы отдельно.
"""

import argparse
import logging
import sys
from collections import Counter
from datetime import date

from finlib.db import connection, fetch_all
from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import BOND_SOURCES, OWN_FILE, bond_sources, routing_rows

_ROUTE = """
SELECT inn, basket, grounds, as_of FROM routing_day
WHERE as_of = COALESCE(%(as_of)s, (SELECT max(as_of) FROM routing_day))
"""


def cause(after: tuple[str, ...]) -> str:
    """Чем объяснена смена корзины: периметр, банкротство либо иное."""
    if "no_bonds_outstanding" in after:
        return "периметр: нет выпусков в обращении"
    if "bankruptcy_proceedings" in after:
        return "банкротство в карточке"
    return "иное: данные или календарь"


def main() -> int:
    """Печатает список смен корзины и итог по корзинам."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    args = parser.parse_args()
    routing = load_routing()
    names = {basket.code: basket.name for basket in routing.baskets}
    with connection() as conn:
        before = fetch_all(_ROUTE, {"as_of": args.as_of}, conn=conn)
        if not before:
            print("в routing_day нет точек на эту дату", file=sys.stderr)
            return 1
        day = before[0]["as_of"]
        rows, _ = routing_rows(conn, day)
        conn.rollback()
    was = {item["inn"]: item for item in before}
    moved = [
        item for item in sorted(rows, key=lambda entry: (entry.name, entry.inn))
        if item.inn in was and item.verdict.basket != was[item.inn]["basket"]
    ]
    print(f"# Периметр по выпускам: смены корзины на {day:%d.%m.%Y}\n")
    print(f"Было — routing_day {day:%d.%m.%Y}, стало — код ветки на тот же день. "
          f"Эмитентов было {len(was)}, стало {len(rows)}; "
          f"только было {len(was.keys() - {item.inn for item in rows})}, "
          f"только стало {len({item.inn for item in rows} - was.keys())}.\n")
    by_cause = Counter(cause(item.verdict.grounds) for item in moved)
    print("| Причина | Смен корзины |\n|---|---|")
    for said, count in by_cause.most_common():
        print(f"| {said} | {count} |")
    print(f"| **всего** | {len(moved)} |\n")
    print("## Итог по корзинам: было → стало\n")
    pairs = Counter((was[item.inn]["basket"], item.verdict.basket) for item in moved)
    print("| Было | Стало | Эмитентов |\n|---|---|---|")
    for (old, new), count in sorted(pairs.items(), key=lambda entry: -entry[1]):
        print(f"| {names.get(old, old)} | {names.get(new, new)} | {count} |")
    # Выпуски в обращении по каждому источнику — тот же код, что у маршрута
    # (`bond_sources`): число печатается, чтобы «нет выпусков» было видно
    # проверяемым, а не принятым на веру (no-bonds-impact 09.10.2026).
    counted = bond_sources([item.inn for item in rows], day, routing)
    head = " | ".join(BOND_SOURCES)

    def by_source(inn: str) -> str:
        said = counted.get(inn, {})
        return " | ".join(str(said.get(name, 0)) for name in BOND_SOURCES)

    print("\n## Список\n")
    print(f"| Эмитент | ИНН | Было | Стало | Причина | {head} | "
          "Основания было | Основания стало |")
    print("|---|---|---|---|---|" + "---|" * len(BOND_SOURCES) + "---|---|")
    for item in moved:
        old = was[item.inn]
        print(
            f"| {item.name} | {item.inn} | {names.get(old['basket'], old['basket'])} | "
            f"{item.verdict.basket_name} | {cause(item.verdict.grounds)} | "
            f"{by_source(item.inn)} | "
            f"{', '.join(old['grounds']) or '—'} | {', '.join(item.verdict.grounds) or '—'} |"
        )
    # **Источники расходятся** — файл эмитента пуст, другой источник выпуски
    # видит: эмитент по прежним правилам (решение владельца 09.10.2026).
    split = [
        item for item in sorted(rows, key=lambda entry: (entry.name, entry.inn))
        if item.events is not None and item.events.issues_known
        and not counted.get(item.inn, {}).get(OWN_FILE)
        and any(counted.get(item.inn, {}).get(name) for name in BOND_SOURCES
                if name != OWN_FILE)
    ]
    print(f"\n## Источники расходятся: {len(split)}\n")
    print("Файл выпусков эмитента в обращении не показывает ни одного, другой "
          "источник — показывает; эмитент идёт по прежним правилам.\n")
    print(f"| Эмитент | ИНН | Корзина | {head} |")
    print("|---|---|---|" + "---|" * len(BOND_SOURCES))
    for item in split:
        print(f"| {item.name} | {item.inn} | {item.verdict.basket_name} | "
              f"{by_source(item.inn)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
