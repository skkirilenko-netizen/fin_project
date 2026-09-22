"""Событийный слой: что он показывает и что остаётся решить человеку.

    uv run python eval/events_run.py > data/output/cbonds_events_layer.md

Три вопроса, поставленные экспертной проверкой:

1. **Текущий дефолт против исторического.** У ДВМП признак дефолта стоит
   по еврооблигациям, погашенным около десяти лет назад. Прогон печатает даты
   по всем эмитентам с признаком, чтобы отличить одно от другого, и называет
   правило, которым их различает сейчас.
2. **Рейтинги** — что даёт справочник категорий и кто попадает под градации.
3. **Рефинансирование** — объём к погашению и оферте ближайших 12 месяцев
   против денежных средств. Порога нет: распределение показано, решение
   за человеком.

**Замер не считает сам**: строки и вердикты берёт боевая маршрутизация
(`scoring.routing_store.routing_rows`), события — `sources.cbonds_events`.
"""

import logging
import sys
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.display import money  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds_events import (  # noqa: E402
    in_unit,
    latest_snapshot,
    unknown_scales,
)

logger = logging.getLogger(__name__)

# Денежные средства отчётного периода: знаменатель рефинансирования.
_CASH = """
SELECT f.value FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'ifrs' AND f.report_date = %(d)s
  AND f.line_code = 'ifrs.cash' AND s.is_actual AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""


def main() -> int:
    """Печатает разбор событийного слоя; 1 — если событий читать нечем."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    routing = load_routing()
    on, snapshot = latest_snapshot()
    with connection() as conn:
        rows, counts = routing_rows(conn, today)
        cash = {}
        for item in rows:
            found = fetch_all(
                _CASH, {"inn": item.inn, "d": item.report_date}, conn=conn
            )
            if found:
                cash[item.inn] = Decimal(found[0]["value"])

    known = [item for item in rows if item.events and item.events.issues_known]
    print("# Событийный слой: дефолты, рейтинги, рефинансирование\n")
    if not known:
        print(
            "**Выпусков не прочитано ни у одного эмитента.** Это не «событий "
            "нет»: ответы источника собирает `scripts/emissions_fetch.py`, "
            "и без них слой пуст."
        )
        return 1
    print(
        f"Эмитентов в списке {counts['эмитентов']}, выпуски прочитаны "
        f"у {len(known)}, снимок рейтингов на {on or 'нет'} "
        f"({sum(1 for item in rows if item.events and item.events.ratings_known)} "
        "эмитентов в нём).\n"
    )

    # --- 1. дефолты: текущий против исторического ---------------------------
    print("## Дефолты: текущий против исторического\n")
    print(
        "**Различает их признак неурегулированности и статус выпуска.** "
        "`has_default` сам по себе говорит о прошлом: у ДВМП он стоит "
        "по еврооблигациям, погашенным около десяти лет назад. Даты события "
        "источник не приводит вовсе — опорой служит дата погашения (день, "
        "когда платёж был должен состояться) и дата обновления записи.\n"
    )
    print(
        "| Эмитент | ИНН | Корзина | Выпуск | Статус | Неулажен | "
        "Погашение | Запись обновлена |"
    )
    print("|---|---|---|---|---|---|---|---|")
    current = historical = 0
    for item in sorted(rows, key=lambda row: row.name):
        events = item.events
        if events is None:
            continue
        for issue in (*events.defaulted, *events.settled):
            mark = "да" if issue.unsettled else "нет"
            current += 1 if issue.defaulted else 0
            historical += 0 if issue.defaulted else 1
            print(
                f"| {item.name[:26]} | {item.inn} | {item.verdict.basket_name} "
                f"| {issue.name[:34]} | {issue.status} | {mark} "
                f"| {issue.maturity or '—'} | {issue.updated or '—'} |"
            )
    print(
        f"\nВыпусков с текущим дефолтом **{current}**, с урегулированным "
        f"в прошлом **{historical}**. В разбор идут только текущие.\n"
    )
    print(
        "**Признак неурегулированности давности не мерит, и на ДВМП это видно.** "
        "У ДВМП дефолт по погашению выпусков БО-01 и БО-02 датирован 2018 годом, "
        "а признак неурегулированности стоит до сих пор: восемь лет спустя он "
        "отправляет эмитента в разбор наравне со свежим дефолтом. Различить "
        "текущее от прошлого по самому признаку нельзя — он бессрочен.\n"
    )
    print(
        "**Предложение правила давности (решение за человеком).** Опора — дата "
        "погашения выпуска, по которому объявлен дефолт: это день, когда платёж "
        "был должен состояться. Дефолт считается текущим, если эта дата "
        "не старше трёх лет либо выпуск ещё в обращении; старше — обстоятельство "
        "внимания со словами «дефолт в прошлом», а не разбора. Три года взяты "
        "не из данных: за такой срок сменяется весь набор отчётности, попадающий "
        "в оценку, и число подлежит вашему решению, а не подгонке.\n"
    )

    # --- 2. рейтинги --------------------------------------------------------
    print("## Рейтинги\n")
    print(
        "Категория берётся из справочника точек шкал источника "
        f"(`get_rating_scale_points`). Разбор: {', '.join(routing.events.review_categories)}. "
        f"Внимание: {', '.join(routing.events.attention_categories)} "
        f"при прогнозе {', '.join(routing.events.attention_outlooks)}.\n"
    )
    by_category: Counter[str] = Counter()
    withdrawn = 0
    for item in rows:
        if item.events is None:
            continue
        if item.events.ratings and not item.events.live:
            withdrawn += 1
        for rating in item.events.live:
            by_category[rating.category] += 1
    print("| Категория | Рейтингов |")
    print("|---|---|")
    for name, count in by_category.most_common():
        print(f"| {name} | {count} |")
    unknown = unknown_scales(snapshot)
    if unknown:
        print("\nШкалы вне перечня кредитных (в градацию не идут):\n")
        print("| Шкала | Записей |")
        print("|---|---|")
        for name, count in sorted(unknown.items(), key=lambda row: -row[1]):
            print(f"| {name} | {count} |")
    print(
        f"\nЭмитентов, у которых все рейтинги отозваны, — **{withdrawn}**. "
        "Отзыв — точка шкалы, а не признак, и прежнего значения метод "
        "`…_maxdate` не хранит: история берётся из снимков, начиная "
        f"с {on or 'первого'}.\n"
    )

    # --- 3. рефинансирование ------------------------------------------------
    print("## Рефинансирование: погашения 12 месяцев против денежных средств\n")
    print(
        "Порога нет: показано распределение. Объём к погашению — сумма выпусков "
        "в обращении, у которых погашение либо оферта приходятся на ближайшие "
        "12 месяцев; знаменатель — денежные средства отчётного периода.\n"
    )
    ratios: list[tuple[str, str, Decimal, Decimal, Decimal]] = []
    unknown_unit = 0
    for item in rows:
        if item.events is None or not item.events.issues_known:
            continue
        # **Объём выпуска приходит в рублях, отчётность бывает в миллионах.**
        # Без приведения к единице комплекта отношение ошибалось бы в тысячу
        # раз — тот же класс дефекта, что единица измерения комплекта.
        due = in_unit(item.events.due(12, today), item.unit_code)
        if due is None:
            unknown_unit += 1 if item.events.due(12, today) else 0
            continue
        if due == 0:
            continue
        have = cash.get(item.inn)
        if have is None or have <= 0:
            ratios.append((item.name, item.inn, due, Decimal(0), Decimal(-1)))
            continue
        ratios.append((item.name, item.inn, due, have, due / have))
    ratios.sort(key=lambda row: row[4], reverse=True)
    print(f"Эмитентов с погашениями в ближайшие 12 месяцев: **{len(ratios)}**.\n")
    print("| Эмитент | ИНН | К погашению | Денежные средства | Отношение |")
    print("|---|---|---|---|---|")
    for name, inn, due, have, ratio in ratios[:40]:
        shown = "денежных средств нет" if ratio < 0 else f"{ratio:.2f}"
        print(
            f"| {name[:26]} | {inn} | {money(due)} | "
            f"{money(have) if have else '—'} | {shown} |"
        )
    if len(ratios) > 40:
        print(f"| …и ещё {len(ratios) - 40} | | | | |")
    inside = [row for row in ratios if 0 <= row[4] <= 1]
    print(
        f"\nУкладываются в денежные средства **{len(inside)}** из {len(ratios)}; "
        f"у {sum(1 for row in ratios if row[4] < 0)} денежных средств нет вовсе. "
        "**Единица у выпусков и у отчётности разная** — объём выпуска источник "
        "отдаёт в рублях, отчётность бывает в миллионах, — и отношение "
        "поэтому считается только там, где обе величины приведены к одной "
        "единице комплекта по коду ОКЕИ: иначе получилась бы ошибка в тысячу "
        f"раз. Эмитентов с погашениями, у которых единица комплекта неизвестна, "
        f"— {unknown_unit}.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
