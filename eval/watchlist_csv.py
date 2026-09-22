"""Выгрузка списка наблюдения для рабочего контура: один файл CSV.

    uv run python eval/watchlist_csv.py [--out путь]

**Выгрузка — не второй список, а тот же самый.** Корзины, основания
и величины берутся у боевой маршрутизации (`scoring.routing_store.
routing_rows`) — теми же вызовами, что страница наблюдения: иначе контур
получал бы ответ, расходящийся с нашим, и увидеть это было бы нечем.

**ИНН выгружается строкой, а не числом.** У четверти организаций он
начинается с нуля, и редактор таблиц ноль съедает: «0274051582» становится
«274051582», то есть ИНН другой организации либо никакой. Поэтому поле
обёрнуто в кавычки, а перед ним стоит `﻿`-метка — Excel без неё читает
кириллицу как «РћРћРћ».

**Источник основания берётся из справочника** (`routing.ground_sources`):
корзина без источника ничего не доказывает, а второй экземпляр перечня
разошёлся бы с первым при первом же новом основании.

**Контур объявлен стандартом, а не догадкой.** Третьего стандарта
(`ifrs_standalone`) в проекте нет, поэтому у консолидированной отчётности
контур «консолидированная», и графа честно повторяет то, что известно;
отдельная отчётность управляющей компании от консолидированной в базе пока
не отличима, и это записано в `BACKLOG.md`.
"""

import csv
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import money, ratio  # noqa: E402
from finlib.metrics.ifrs_view import IfrsMetricsView  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402
from finlib.normalize.lines import load_lines  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

# Величины строки: те же, что на странице наблюдения, и в той же разрядности.
VALUES = (
    "net_debt",
    "ebitda",
    "net_debt_ebitda",
    "net_debt_op_profit",
    "equity_ratio",
    "cur_liq",
)

HEADER = (
    "инн",
    "наименование",
    "корзина",
    "код корзины",
    "подгруппа",
    "действие",
    "коды оснований",
    "основания",
    "источники оснований",
    "справочные основания",
    "отчётная дата",
    "стандарт",
    "контур",
    "способ получения",
    "единица",
    "класс по документу",
    "отрасль",
    "группа",
    *VALUES,
    "денежные средства",
    "платежи 12 месяцев",
)


def main() -> int:
    """Пишет CSV; 1 — если выгружать нечего."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    out = Path(f"data/output/watchlist_{today:%Y-%m-%d}.csv")
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    routing = load_routing()
    view = IfrsMetricsView(load_ifrs_metrics())
    units = load_lines().units
    with connection() as conn:
        rows, counts = routing_rows(conn, today)
    if not rows:
        print(
            "выгружать нечего: эмитентов с комплектом вне карантина нет. "
            "Это не пустой список, а отсутствие данных."
        )
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(HEADER)
        for item in sorted(rows, key=lambda entry: (entry.verdict.basket, entry.name)):
            verdict = item.verdict
            unit = units.name_of(item.unit_code) if item.unit_code else ""
            # Величины печатаются той же единой точкой округления, что
            # в документе и на странице: третье представление разошлось бы
            # с первыми двумя.
            values = [
                view.shown(code, value, money=unit or None)
                if (value := item.values.get(code)) is not None
                else ""
                for code in VALUES
            ]
            writer.writerow(
                (
                    # ИНН строкой: ведущий ноль у четверти организаций.
                    f"{item.inn}",
                    item.name,
                    verdict.basket_name,
                    verdict.basket,
                    "; ".join(verdict.subgroup_names),
                    "; ".join(verdict.actions),
                    "; ".join(entry.ground for entry in verdict.findings),
                    " | ".join(entry.text for entry in verdict.findings),
                    "; ".join(
                        dict.fromkeys(
                            routing.source_of(entry.ground)
                            for entry in verdict.findings
                        )
                    ),
                    " | ".join(entry.text for entry in verdict.notes),
                    f"{item.report_date:%Y-%m-%d}",
                    "МСФО",
                    "консолидированная",
                    "; ".join(item.sources),
                    unit,
                    item.assessed_class,
                    item.branch,
                    item.group,
                    *values,
                    money(item.cash) if item.cash is not None else "",
                    money(item.refinance.due)
                    if item.refinance is not None and item.refinance.due is not None
                    else "",
                )
            )
    print(f"{out}: строк {len(rows)}, графа {len(HEADER)}")
    print(
        "ИНН выгружен строкой, разделитель «;», кодировка UTF-8 с меткой: "
        "иначе редактор таблиц съедает ведущий ноль и ломает кириллицу."
    )
    for name, count in sorted(counts.items()):
        print(f"  {name}: {count}")
    # Разрядность величин — та же, что в документе: единая точка округления
    # относится и к выгрузке, иначе контур получит третье представление.
    example = rows[0].values.get("cur_liq")
    if example is not None:
        print(f"  разрядность единой точкой округления, пример: {ratio(example)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
