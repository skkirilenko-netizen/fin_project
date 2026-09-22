"""Календарь событий и замер маршрута **без** событийного правила.

    uv run python eval/event_measure_run.py > data/output/event_measure.md

**Правило, проверенное на тех же событиях, на которых построено, всегда
выходит идеальным.** Дефолт по выпуску — факт, и маршрут обязан его называть;
но замер качества на нём отвечает «выявляемость единица» и не говорит ничего.
Вопрос, который стоит задавать, другой: **видели ли эмитента отчётность,
рейтинги, группы и поручители до события** — то есть без правила о дефолте.

Поэтому календарь событий стоит отдельно от правил: он не участвует
в маршруте вовсе, а маршрут пересчитывается со скрытыми событиями дефолта
(`routing_rows(blind={"defaults"})`). Прячутся признаки, а не выпуски:
рефинансирование и объём долга — это срочность и размер, а не события.

**Упреждение считается от отчётной даты комплекта**, по которому маршрут
судит, до даты события. Отрицательное упреждение означает, что событие
случилось **до** отчётности, и предсказанием такой ответ не является —
об этом сказано числом, а не умолчанием.

**Вердиктов прошлого у нас нет**, и это ограничение объявлено: маршрут
считается по нынешним данным, а не по тем, что были известны в день события.
Верхняя оценка выявляемости отсюда завышена, и снять эту оговорку может
только история снимков.
"""

import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import money  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

# Окно свежести события: год. Событие 2009 года о нынешнем эмитенте
# не говорит, и мерить по нему выявляемость значило бы мерить прошлое.
FRESH_DAYS = 365


def calendar(rows) -> list[dict]:
    """Календарь событий: дефолты по выпускам и рейтинговые действия.

    **Календарь в маршруте не участвует.** Он собирается из тех же данных,
    но отдельно: правило, проверяемое на своих же событиях, идеально
    по устройству.
    """
    routing = load_routing()
    found: list[dict] = []
    for item in rows:
        events = item.events
        if events is None:
            continue
        for entry in events.records:
            if entry.moment is None:
                continue
            found.append(
                {
                    "inn": item.inn,
                    "name": item.name,
                    "when": entry.moment,
                    "kind": f"дефолт: {entry.kind.lower()}, {entry.status.lower()}",
                    "amount": entry.amount,
                    "settled": entry.settled,
                    "report_date": item.report_date,
                }
            )
        for rating in events.live:
            if (
                rating.category in routing.events.review_categories
                and rating.assigned is not None
            ):
                found.append(
                    {
                        "inn": item.inn,
                        "name": item.name,
                        "when": rating.assigned,
                        "kind": f"рейтинг {rating.point} ({rating.agency})",
                        "amount": None,
                        "settled": False,
                        "report_date": item.report_date,
                    }
                )
    return sorted(found, key=lambda entry: entry["when"], reverse=True)


def main() -> int:
    """Печатает календарь и замер; 1 — если событий не нашлось вовсе."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    with connection() as conn:
        rows, counts = routing_rows(conn, today)
        blind, _ = routing_rows(conn, today, blind=frozenset({"defaults"}))
    seen = {item.inn: item for item in rows}
    without = {item.inn: item for item in blind}
    events = calendar(rows)

    print("# Календарь событий и замер маршрута без событийного правила\n")
    if not events:
        print(
            "**Событий нет ни одного.** Это не «эмитенты чисты», а отсутствие "
            "данных: события дефолта забираются `scripts/events_fetch.py`, "
            "рейтинги — ежедневным снимком."
        )
        return 1
    fresh = [item for item in events if (today - item["when"]).days <= FRESH_DAYS]
    print(
        f"Эмитентов в списке {counts['эмитентов']}. Событий с датой — "
        f"**{len(events)}** у {len({item['inn'] for item in events})} эмитентов, "
        f"из них за последний год — **{len(fresh)}** "
        f"у {len({item['inn'] for item in fresh})}.\n"
    )
    print(
        "**Календарь в маршруте не участвует.** Он собран из тех же данных, "
        "но отдельно: правило, проверяемое на своих же событиях, идеально "
        "по устройству и о качестве маршрута не говорит ничего.\n"
    )

    print("## Календарь: события последнего года\n")
    # **Графа называет свою единицу.** Неисполненная сумма приходит от источника
    # в рублях — не в единице комплекта, — и число без единицы читается
    # в тех единицах, которые предположит читатель.
    print(
        "| Дата | Эмитент | ИНН | Событие | Не исполнено, руб. | Улажено "
        "| Отчётная дата |"
    )
    print("|---|---|---|---|---|---|---|")
    for item in fresh[:60]:
        print(
            f"| {item['when']} | {item['name'][:26]} | {item['inn']} | {item['kind']} "
            f"| {money(item['amount']) if item['amount'] is not None else '—'} "
            f"| {'да' if item['settled'] else 'нет'} "
            f"| {item['report_date'] or 'отчётности нет'} |"
        )
    if len(fresh) > 60:
        print(f"| …и ещё {len(fresh) - 60} | | | | | | |")

    # --- замер: что видел маршрут без событий -------------------------------
    print("\n## Замер маршрута без событийного правила\n")
    print(
        "События дефолта скрыты от маршрута, рейтинги, группы, поручители "
        "и отчётность оставлены. Вопрос: **видели ли они эмитента до события**.\n"
    )
    with_event = {item["inn"] for item in fresh}
    base = len(with_event) / len(without) if without else 0
    print(
        f"Базовая частота: событие за год есть у **{len(with_event)}** эмитентов "
        f"из {len(without)} — {base:.1%}.\n"
    )
    print("| Корзина без событий | Эмитентов | С событием | Доля | Прирост к базовой |")
    print("|---|---|---|---|---|")
    by_basket: Counter[str] = Counter()
    hits: Counter[str] = Counter()
    for inn, item in without.items():
        by_basket[item.verdict.basket_name] += 1
        if inn in with_event:
            hits[item.verdict.basket_name] += 1
    for name, total in by_basket.most_common():
        share = hits[name] / total if total else 0
        lift = share / base if base else 0
        print(
            f"| {name} | {total} | {hits[name]} | {share:.1%} "
            f"| {lift:.2f}× |"
        )

    caught = sum(
        1
        for inn in with_event
        if inn in without and without[inn].verdict.basket != "clear"
    )
    print(
        f"\n**Выявляемость: {caught} из {len(with_event)}** "
        f"({caught / len(with_event):.0%}) — столько эмитентов с событием "
        "маршрут не оставил в «Без внимания», даже не зная о событии.\n"
    )

    print("### Кого не увидел никто\n")
    print("| Эмитент | ИНН | Событие | Дата | Корзина с событиями |")
    print("|---|---|---|---|---|")
    missed = 0
    for item in fresh:
        inn = item["inn"]
        if inn not in without or without[inn].verdict.basket != "clear":
            continue
        missed += 1
        print(
            f"| {item['name'][:26]} | {inn} | {item['kind']} | {item['when']} "
            f"| {seen[inn].verdict.basket_name} |"
        )
    if not missed:
        print("| — | — | — | — | — |")

    # --- упреждение ---------------------------------------------------------
    print("\n### Упреждение: от отчётной даты до события\n")
    print(
        "Отрицательное упреждение означает, что событие случилось **до** "
        "отчётности, по которой маршрут судит: предсказанием такой ответ "
        "не является, и молчать об этом нельзя.\n"
    )
    ahead = [
        (item["when"] - item["report_date"]).days
        for item in fresh
        if item["report_date"] is not None
    ]
    after = [days for days in ahead if days > 0]
    print(
        f"Событий за год {len(ahead)}; случились **после** отчётной даты "
        f"{len(after)}, до неё {len(ahead) - len(after)}.\n"
    )
    if after:
        after.sort()
        print(
            f"Упреждение по тем, что после: медиана **{after[len(after) // 2]}** "
            f"дней, от {after[0]} до {after[-1]}.\n"
        )
    print(
        "**Вердиктов прошлого у нас нет**, и оговорка эта не формальная: "
        "маршрут посчитан по нынешним данным, а не по тем, что были известны "
        "в день события. Выявляемость отсюда — верхняя оценка, и снять "
        "оговорку может только история снимков.\n"
    )
    horizon = today - timedelta(days=FRESH_DAYS)
    print(
        f"Окно свежести события — {FRESH_DAYS} дней (с {horizon}): событие "
        "2009 года о нынешнем эмитенте не говорит, и мерить им выявляемость "
        "значило бы мерить прошлое."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
