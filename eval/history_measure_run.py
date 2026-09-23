"""Что показала пересчитанная история: движение корзин и его дребезг.

Четыре вопроса, заданные к итогам обратного пересчёта:

1. сколько смен корзин в среднем за неделю и каков разброс;
2. сколько из них возвраты внутри окна;
3. как выглядит отчёт обычного дня;
4. сколько эмитентов сменили корзину хотя бы раз за год, а сколько
   не менялись ни разу.

Последнее — отдельный вопрос о предмете мониторинга: если девять из десяти
не двигались ни разу, наблюдение сводится к узкому кругу, и это надо знать.

**Окно показывает дребезг, а не гасит его** (решение человека 23.09.2026):
возврат в прежнюю корзину внутри окна печатается отдельной строкой вместе
с прежней корзиной и датой. Гасить нечего, пока неизвестно, сколько его.

    uv run python eval/history_measure_run.py > data/output/history_measure.md

**Замер не считает сам**: корзины он читает из истории, записанной боевой
маршрутизацией.
"""

import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402

logger = logging.getLogger(__name__)

_HISTORY = """
SELECT inn, as_of, basket, grounds FROM routing_history
WHERE kind = %(kind)s ORDER BY inn, as_of
"""


def main() -> int:
    """Печатает движение корзин по пересчитанной истории."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    routing = load_routing()
    window = routing.history.window_days
    with connection() as conn:
        rows = fetch_all(_HISTORY, {"kind": "backfill"}, conn=conn)
    if not rows:
        print(
            "# Движение корзин\n\nИстории нет: пересчёт не выполнялся. "
            "Это не «движения нет» — это отсутствие данных.\n"
        )
        return 1

    series: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for row in rows:
        series[row["inn"]].append((row["as_of"], row["basket"]))
    dates = sorted({row["as_of"] for row in rows})
    bonds = set(bond_issuers())

    # Смены корзины: пара соседних точек, у которых корзина разная.
    moves: list[tuple[str, date, str, str]] = []
    for inn, points in series.items():
        for (_, before), (when, after) in zip(points, points[1:], strict=False):
            if before != after:
                moves.append((inn, when, before, after))

    # **Возврат — смена, отменённая обратно внутри окна.** Он не гасится:
    # печатается отдельной строкой вместе с прежней корзиной и датой.
    returns: list[tuple[str, date, str, date]] = []
    by_issuer: dict[str, list[tuple[str, date, str, str]]] = defaultdict(list)
    for item in moves:
        by_issuer[item[0]].append(item)
    for inn, own in by_issuer.items():
        for first, second in zip(own, own[1:], strict=False):
            if (
                second[3] == first[2]
                and (second[1] - first[1]).days <= window
            ):
                returns.append((inn, second[1], first[3], first[1]))

    names = {basket.code: basket.name for basket in routing.baskets}
    print("# Движение корзин по пересчитанной истории\n")
    print(
        f"Точек {len(dates)}, эмитентов {len(series)}, глубина "
        f"{(dates[-1] - dates[0]).days} дней. **Все изменения здесь — "
        "от данных**: код и методика на всём протяжении пересчёта одни, "
        "и другого источника изменений у него нет.\n"
    )

    print("## Сколько смен и каков разброс\n")
    weeks: Counter[date] = Counter()
    for _, when, _, _ in moves:
        weeks[when - timedelta(days=when.weekday())] += 1
    counts = [weeks.get(item, 0) for item in _weeks_between(dates[0], dates[-1])]
    print(
        f"Смен корзины всего **{len(moves)}** за "
        f"{(dates[-1] - dates[0]).days // 7} недель. "
        f"В среднем за неделю **{statistics.mean(counts):.1f}**, медиана "
        f"{statistics.median(counts):.0f}, от {min(counts)} до {max(counts)}."
    )
    busiest = weeks.most_common(3)
    if busiest:
        print(
            "\nСамые шумные недели: "
            + ", ".join(f"{item:%d.%m.%Y} — {n}" for item, n in busiest)
            + ". Разброс здесь и есть ответ: среднее без него сказало бы,"
            " что каждую неделю происходит одно и то же.\n"
        )

    print(f"## Возвраты внутри окна ({window} дней)\n")
    print(
        f"Смен, отменённых обратно внутри окна, **{len(returns)}** "
        f"из {len(moves)} — это "
        f"{len(returns) / len(moves) * 100:.1f} % всех смен.\n"
    )
    for inn, when, basket, first in returns[:10]:
        print(
            f"- {inn}: {when:%d.%m.%Y} вернулся, был "
            f"«{names.get(basket, basket)}» {first:%d.%m.%Y}"
        )
    if not returns:
        print("ни одного — дребезга в пересчитанной истории нет.\n")

    print("\n## Кто двигался, а кто нет\n")
    moved = {item[0] for item in moves}
    never = len(series) - len(moved)
    with_bonds = {inn for inn in moved if inn in bonds}
    print(
        f"Сменили корзину хотя бы раз **{len(moved)} из {len(series)}** "
        f"({len(moved) / len(series) * 100:.0f} %), не менялись ни разу "
        f"**{never}**. Из двигавшихся с выпусками в обращении "
        f"{len(with_bonds)}.\n"
    )
    spread = Counter(len(by_issuer[inn]) for inn in moved)
    print("| Смен за год | Эмитентов |")
    print("|---|---|")
    for times, count in sorted(spread.items()):
        print(f"| {times} | {count} |")
    print(f"| ни одной | {never} |")
    print(
        "\n**Это и есть предмет наблюдения.** Если подавляющее большинство "
        "не двигалось ни разу, ежедневный отчёт говорит о узком круге, "
        "и знать это надо до того, как его начнут читать каждый день.\n"
    )

    # **Пересчёт, не сказавший о своей неполноте, выдаёт её за наблюдение.**
    # Основание, построенное на признаке без истории, говорит о сегодняшнем
    # знании, а не о наблюдении того дня.
    restored = Counter()
    for row in rows:
        for ground in row["grounds"]:
            restored[routing.restored(ground)] += 1
    dated, current = restored["dated"], restored["current"]
    whole = dated + current or 1
    print("## Чем восстановлена история\n")
    print(
        f"Оснований за весь пересчёт **{dated + current}**, из них "
        f"восстановлено своими датами **{dated}** "
        f"({dated / whole * 100:.1f} %), взято нынешними данными "
        f"**{current}** ({current / whole * 100:.1f} %).\n"
    )
    print(
        "**Второе — не наблюдение, а сегодняшнее знание.** Признак карточки, "
        "статус выпуска, наш карантин и наша оценка истории не имеют вовсе, "
        "и основание, на них построенное, говорит о том, что известно "
        "сегодня.\n"
    )

    print("## Куда двигались\n")
    print("| Откуда | Куда | Смен |")
    print("|---|---|---|")
    pairs = Counter((before, after) for _, _, before, after in moves)
    for (before, after), count in pairs.most_common(12):
        print(f"| {names.get(before, before)} | {names.get(after, after)} | {count} |")
    return 0


def _weeks_between(first: date, last: date) -> list[date]:
    """Понедельники всех недель отрезка: знаменатель среднего за неделю.

    Считать среднее по неделям, в которых что-то было, значило бы ответить
    на другой вопрос — «сколько смен в шумную неделю».
    """
    start = first - timedelta(days=first.weekday())
    found: list[date] = []
    while start <= last:
        found.append(start)
        start += timedelta(days=7)
    return found


if __name__ == "__main__":
    sys.exit(main())
