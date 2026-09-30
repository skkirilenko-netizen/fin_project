"""Базовые заключения уровня 1 пакетом: все эмитенты «Разбора» с МСФО у агрегатора.

    uv run python eval/level1_batch_run.py

**Прогон без записи в базу.** Строки маршрута даёт боевой путь на сегодня
(`routing_rows`), транзакция откатывается; заключения собирает
`report.aggregator` по составу `report.yaml` (`aggregator_conclusion`),
файлы кладутся в `data/output/level1/`. Отбор — корзина «Разбор», стандарт
МСФО, величины только агрегатора; эмитент «Разбора» по МСФО, у которого
в величинах есть документ, уровнем 1 не описывается и считается отказом
с причиной, а не исчезает молча.

В конце — сводка: сколько собрано, сколько отказов и почему, и пять
документов на просмотр: два из подгруппы «события», два «рынок», один
«величины».
"""

import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from finlib.db import connection
from finlib.report.aggregator import build, render
from finlib.report.policy import load_policy
from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import routing_rows

logger = logging.getLogger(__name__)

OUT = Path("data/output/level1")
REVIEW = "review"
# Подгруппы «Разбора», из которых берутся документы на просмотр, и сколько.
SAMPLE = (("события", 2), ("рынок", 2), ("величин", 1))


def main() -> int:
    """Собирает заключения и печатает сводку."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    composition = load_policy().aggregator_conclusion
    if composition is None:
        print("состав базового заключения в report.yaml не утверждён — собирать не из чего")
        return 1
    routing = load_routing()
    today = date.today()
    built: list[tuple[object, Path]] = []
    refused: list[tuple[object, str]] = []
    with connection() as conn:
        rows, _ = routing_rows(conn, today)
        chosen = [
            item
            for item in rows
            if item.verdict.basket == REVIEW
            and item.standard is not None
            and item.standard.value == "ifrs"
        ]
        for item in sorted(chosen, key=lambda row: row.inn):
            try:
                conclusion = build(
                    item, conn, composition, today, routing.basket(item.verdict.basket).name
                )
            except Exception as error:  # noqa: BLE001 — отказ называется, а не роняет пакет
                refused.append((item, f"{type(error).__name__}: {error}"))
                conn.rollback()
                continue
            path = render(conclusion, composition, OUT / f"{item.inn}_уровень1.docx")
            built.append((item, path))
        conn.rollback()
    aggregator_only = sum(1 for item in chosen if tuple(item.sources) == ("Cbonds",))
    print("# Уровень 1 пакетом\n")
    print(
        f"«Разбор» по МСФО: {len(chosen)} эмитентов, из них величины только "
        f"агрегатора — {aggregator_only}.\n"
    )
    print(f"Собрано {len(built)}, отказов {len(refused)}.\n")
    reasons = Counter(reason.split(":", 1)[0] for _, reason in refused)
    for reason, count in reasons.most_common():
        print(f"- {reason}: {count}")
    for item, reason in refused:
        print(f"  - {item.inn} {item.name}: {reason}")  # type: ignore[attr-defined]
    groups = Counter(
        name for item, _ in built for name in item.verdict.subgroup_names  # type: ignore[attr-defined]
    )
    print("\n## Подгруппы собранных\n")
    for name, count in groups.most_common():
        print(f"- {name}: {count}")
    print("\n## На просмотр\n")
    taken: set[str] = set()
    for marker, count in SAMPLE:
        # Сперва эмитенты только этой подгруппы: в документе видна она одна.
        fitting = [
            (item, path)
            for item, path in built
            if any(marker in name for name in item.verdict.subgroup_names)  # type: ignore[attr-defined]
            and item.inn not in taken  # type: ignore[attr-defined]
        ]
        fitting.sort(key=lambda pair: (len(pair[0].verdict.subgroup_names), pair[0].inn))  # type: ignore[attr-defined]
        for item, path in fitting[:count]:
            taken.add(item.inn)  # type: ignore[attr-defined]
            print(
                f"- {marker}: {item.inn} {item.name} — "  # type: ignore[attr-defined]
                f"{', '.join(item.verdict.subgroup_names)} — {path}"  # type: ignore[attr-defined]
            )
        if len(fitting) < count:
            print(f"- {marker}: собрано только {len(fitting)} из {count} нужных")
    return 0


if __name__ == "__main__":
    sys.exit(main())
