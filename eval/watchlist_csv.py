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
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.definitions import Unit  # noqa: E402
from finlib.metrics.display import foreign_units, format_metric, ratio  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

# Величины строки: те же, что на странице наблюдения, и в той же разрядности.
# **Графы объявлены по обоим стандартам сразу.** У эмитента, маршрут которого
# построен по РСБУ, долговой нагрузки нет вовсе, а граница зовётся
# `debt_to_op_profit`; графа под чужим кодом стояла бы пустой при известной
# величине. Пустая графа здесь означает «величины нет у этого эмитента»,
# а не «стандарт другой»: стандарт назван своей графой.
VALUES = (
    "net_debt",
    "ebitda",
    "net_debt_ebitda",
    "net_debt_op_profit",
    "debt_to_op_profit",
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


def _sum(value: Decimal | None, unit: str) -> str:
    """Денежная величина графы вместе с единицей комплекта; None — пусто."""
    if value is None:
        return ""
    return format_metric(value, Unit.THOUSAND_RUB, money=unit)


def main() -> int:
    """Пишет CSV; 1 — если выгружать нечего."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    out = Path(f"data/output/watchlist_{today:%Y-%m-%d}.csv")
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    routing = load_routing()
    with connection() as conn:
        rows, counts = routing_rows(conn, today)
    if not rows:
        print(
            "выгружать нечего: эмитентов с комплектом вне карантина нет. "
            "Это не пустой список, а отсутствие данных."
        )
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    verified = 0
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(HEADER)
        for item in sorted(rows, key=lambda entry: (entry.verdict.basket, entry.name)):
            verdict = item.verdict
            # Единица берётся у строки: набранная здесь во второй раз,
            # она разошлась бы с той, которую печатают основания.
            unit = item.unit
            # Величины печатаются той же единой точкой округления, что
            # в документе и на странице, и набраны они один раз — маршрутом,
            # который знает справочник своего стандарта. Третий набор
            # разошёлся бы с первыми двумя.
            printed_by_code = {
                code: shown for code, _, shown in item.shown_values
            }
            values = [printed_by_code.get(code, "") for code in VALUES]
            # **Единица сверяется у каждой строки той же функцией, что
            # у документа.** Выгрузка печатала мимо проверки, и контур получал
            # миллионы, подписанные тысячами.
            printed = " ".join(
                [unit, *values]
                + [entry.text for entry in verdict.findings]
                + [entry.text for entry in verdict.notes]
            )
            wrong = foreign_units(printed, unit)
            if wrong:
                raise ValueError(
                    f"{item.name} ({item.inn}): напечатана единица "
                    f"«{', '.join(wrong)}», а комплект составлен в «{unit}»"
                )
            verified += 1
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
                    (
                        f"{item.report_date:%Y-%m-%d}"
                        if item.report_date is not None
                        else ""
                    ),
                    # **Стандарт и контур берутся у строки.** Прежде они были
                    # написаны здесь словами и говорили «МСФО ·
                    # консолидированная» у каждой строки; теперь маршрут
                    # строится и по отчётности юридического лица, и по одним
                    # событиям, и графа обязана это различать.
                    item.standard.value if item.standard is not None else "",
                    item.basis,
                    "; ".join(item.sources),
                    unit,
                    item.assessed_class,
                    item.branch,
                    item.group,
                    *values,
                    # **Денежная графа называет свою единицу сама.** Графа
                    # «единица» стоит рядом, но читатель берёт из выгрузки
                    # столбец, а не строку, и число без единицы в нём читается
                    # в тех единицах, которые он предположит.
                    _sum(item.cash, unit),
                    _sum(
                        item.refinance.due if item.refinance is not None else None,
                        unit,
                    ),
                )
            )
    print(f"{out}: строк {len(rows)}, графа {len(HEADER)}")
    # Знаменатель проверки единицы: ноль расхождений при неизвестном числе
    # сверенных строк не означает ничего.
    print(f"  единица сверена у строк: {verified}, расхождений 0")
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
