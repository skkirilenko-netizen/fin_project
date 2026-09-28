"""Пробное базовое заключение по данным агрегатора (уровень 1): один эмитент.

    uv run python eval/level1_conclusion_run.py 7840346335

**Проба, без записи в базу.** Строку маршрута даёт боевой путь на сегодня
(`routing_rows`), транзакция откатывается; заключение собирает
`report.aggregator`, файл кладётся в `data/output/` с пометкой «проба»
в имени и в самом документе. Состав берётся из `report.yaml`
(`aggregator_conclusion`); пока он не утверждён, прогон отказывается.
"""

import logging
import sys
from datetime import date
from pathlib import Path

from finlib.db import connection
from finlib.report.aggregator import build, render
from finlib.report.policy import load_policy
from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import routing_rows

logger = logging.getLogger(__name__)

OUT = Path("data/output")
MARK = "ПРОБА. Документ собран для согласования состава базового заключения и решением не является."


def main() -> int:
    """Собирает пробное заключение по названному ИНН."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    wanted = [item for item in sys.argv[1:] if item.isdigit()]
    if len(wanted) != 1:
        print("назовите один ИНН")
        return 1
    composition = load_policy().aggregator_conclusion
    if composition is None:
        print("состав базового заключения в report.yaml не утверждён — собирать не из чего")
        return 1
    routing = load_routing()
    today = date.today()
    with connection() as conn:
        rows, _ = routing_rows(conn, today)
        found = {row.inn: row for row in rows}
        item = found.get(wanted[0])
        if item is None:
            print(f"{wanted[0]}: в списке нет")
            return 1
        conclusion = build(item, conn, composition, today, routing.basket(item.verdict.basket).name)
        conn.rollback()
    path = render(conclusion, composition, OUT / f"{item.inn}_уровень1_проба.docx", MARK)
    print(f"{path}: {item.name}, разделов {len(conclusion.parts)}")
    for part in conclusion.parts:
        print(f"\n## {part.title}")
        for text in part.paragraphs:
            print(text)
        for line in part.lines:
            print(f"| {line.name} | {line.shown} | {line.code} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
