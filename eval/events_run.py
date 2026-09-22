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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds_events import latest_snapshot, unknown_scales  # noqa: E402

logger = logging.getLogger(__name__)

def main() -> int:
    """Печатает разбор событийного слоя; 1 — если событий читать нечем."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    routing = load_routing()
    on, snapshot = latest_snapshot()
    with connection() as conn:
        rows, counts = routing_rows(conn, today)

    known = [item for item in rows if item.events and item.events.issues_known]
    print("# Событийный слой: дефолты и рейтинги\n")
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
        "**Правило давности принято 22.09.2026: три года, четыре исхода.** "
        "Неурегулированный до трёх лет — разбор; старше — внимание с вопросом "
        "о статусе урегулирования; урегулированный до трёх лет — внимание как "
        "кредитная история; урегулированный старше — справочное основание, "
        "корзины не называющее. Порог остаётся предварительным: он назван "
        "человеком, а не измерен.\n"
    )
    print(
        "**Дата события источником не приводится и графиком платежей "
        "не восстанавливается.** Поле `actual_payment_date` метода "
        "`get_flow_new` — срок, сдвинутый на рабочий день: у Кириллицы купон "
        "со сроком 07.10.2023 (суббота) стоит с «фактом» 09.10.2023, "
        "а у ЕвроТранса заполнены платежи 2027 года. По 93 выпускам с признаком "
        "дефолта неуплаченным не оказался ни один. Опора — дата погашения "
        "выпуска, и выпуск выбирается строением записи: статус «дефолт "
        "по погашению» называет сам предмет, у закрытого иначе дата погашения — "
        "только верхняя граница.\n"
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

    # --- 3. рефинансирование: вынесено, и вот почему -------------------------
    # **Две меры одной величины разошлись бы, и увидеть это было бы нечем.**
    # Здесь объём к погашению считался как сумма выпусков, у которых погашение
    # либо оферта приходятся на окно, — то есть весь остаток целиком. График
    # платежей отвечает точнее: купоны и амортизация в окне, а оферта порознь,
    # потому что предъявление — право владельца. Мера оставлена одна
    # (`eval/refinancing_run.py`, `sources/cbonds_flows.py`), а здесь названа
    # ссылка: удалённая мера иначе не отличается от забытой.
    print("## Рефинансирование\n")
    print(
        "Вынесено в `eval/refinancing_run.py`: платежи считаются по графику "
        "(`get_flow_new`), а не по остатку выпуска целиком, и оферта стоит "
        "порознь от купонов. Две меры одной величины расходились бы, и какая "
        "из них попала в отчёт, зависело бы от того, кто спросил.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
