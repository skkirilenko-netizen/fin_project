"""Охват списка наблюдения: кого он видит и кого не видит по построению.

    uv run python eval/watchlist_coverage_run.py     # make watchlist-coverage

**Вопрос не праздный: графа «МСФО · консолидированная» стоит у каждой строки
списка, и прочесть её можно двумя способами.** Либо так вышло у всех, кого мы
загрузили, — либо список **по построению** не может содержать никого другого.
Верно второе, и в двух местах разом:

- выборка списка называет стандарт (`routing_store._LATEST`, `standard =
  'ifrs'`): эмитент, раскрывающий только РСБУ, в список не попадает вовсе;
- сам универсум собран из доставок МСФО (`cbonds.msfo_universe`): карточки
  эмитента, у которого у агрегатора нет отчётности по МСФО, у нас нет —
  то есть слепое пятно шире списка.

Замер отвечает числами: сколько эмитентов с выпусками в обращении есть
у источника, сколько из них в списке, и сколько из отсутствующих раскрывают
**только** РСБУ. Знаменатель печатается всегда: «не в списке 542» без «всего
702» не означает ничего.

**Замер не считает сам.** Корзины и состав списка берутся у боевой
маршрутизации (`scoring.routing_store.routing_rows`) — теми же вызовами, что
страница наблюдения и выгрузка. Универсумы читаются из сохранённых ответов
источника: в сеть прогон не идёт, и отсутствующий файл он называет, а не
подменяет нулём.
"""

import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")

# Сохранённые перечни источника. Каждый — ответ целиком по стране либо
# по виду отчёта: отбирать по наименованию источник не умеет, и перечень
# забирается полностью.
UNIVERSES = {
    "outstanding": (
        "emissions_ru_outstanding.json",
        "выпуски в обращении, Россия (get_emissions, status_id=192)",
    ),
    "ifrs": (
        "msfo_real_universe.json",
        "нормализованная отчётность по МСФО (get_report_msfo_real)",
    ),
    "rsbu": (
        "rsbu_balance_universe.json",
        "нормализованный баланс РСБУ (get_report_rsbu_balance)",
    ),
}


def _inns(name: str) -> set[str]:
    """ИНН из сохранённого перечня источника; отсутствие файла — отказ."""
    path = CACHE / name
    if not path.exists():
        raise FileNotFoundError(
            f"перечня {path} на диске нет. Замер в сеть не ходит: пустой охват "
            "означал бы, что эмитентов у источника нет, а он означает, "
            "что мы не спрашивали"
        )
    items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    return {(row.get("emitent_inn") or "").strip() for row in items} - {""}


def coverage(listed: set[str]) -> dict[str, int] | None:
    """Числа охвата по сохранённым перечням; None — перечней на диске нет.

    Считает их одно место: те же числа печатает страница наблюдения в строке
    охвата, и второй их набор разошёлся бы с первым.
    """
    try:
        universes = {key: _inns(name) for key, (name, _) in UNIVERSES.items()}
    except FileNotFoundError:
        return None
    outstanding, ifrs, rsbu = (
        universes["outstanding"],
        universes["ifrs"],
        universes["rsbu"],
    )
    absent = outstanding - listed
    return {
        "с выпусками в обращении": len(outstanding),
        "из них в списке": len(listed & outstanding),
        "из них нет в списке": len(absent),
        "нет в списке, МСФО у источника есть": len(absent & ifrs),
        "нет в списке, только РСБУ": len(absent & rsbu - ifrs),
        "нет в списке, отчётности у источника нет": len(absent - ifrs - rsbu),
        "в списке без выпусков в обращении": len(listed - outstanding),
        "у источника только РСБУ": len(rsbu - ifrs),
        "универсум МСФО": len(ifrs),
        "универсум РСБУ": len(rsbu),
    }


def main() -> int:
    """Печатает охват; 1 — если перечни на диске неполны."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    try:
        universes = {key: _inns(name) for key, (name, _) in UNIVERSES.items()}
    except FileNotFoundError as error:
        print(error)
        return 1

    outstanding = universes["outstanding"]
    ifrs, rsbu = universes["ifrs"], universes["rsbu"]
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
    listed = {item.inn for item in rows}

    print("# Охват списка наблюдения\n")
    for key, (name, what) in UNIVERSES.items():
        print(f"- {what}: эмитентов {len(universes[key])} ({name})")
    print(f"- список наблюдения: эмитентов {len(listed)}\n")

    covered = listed & outstanding
    absent = outstanding - listed
    print("## Эмитенты с выпусками в обращении\n")
    print(f"Всего у источника {len(outstanding)}, в списке {len(covered)}, "
          f"нет в списке {len(absent)}.\n")
    print("| Почему нет в списке | Эмитентов |")
    print("|---|---|")
    print(f"| у источника есть МСФО — комплект не загружен или в карантине "
          f"| {len(absent & ifrs)} |")
    print(f"| **раскрывают только РСБУ** — список их не видит по построению "
          f"| {len(absent & rsbu - ifrs)} |")
    print(f"| ни МСФО, ни РСБУ у источника нет | {len(absent - ifrs - rsbu)} |")

    # **Обратная сторона охвата.** Список, в котором больше половины строк —
    # эмитенты без долга в обращении, отвечает не на тот вопрос: маршрут
    # спрашивает, нужен ли человек, а человек нужен там, где есть долг.
    idle = listed - outstanding
    print(
        f"\nВ списке при этом {len(idle)} эмитентов **без выпусков "
        f"в обращении** из {len(listed)}: их долг погашен либо выпуски "
        "аннулированы. Это не дефект охвата, а вопрос о составе списка."
    )

    print("\n## Слепое пятно шире списка\n")
    print(
        f"У источника {len(rsbu - ifrs)} эмитентов с балансом РСБУ и без "
        f"отчётности по МСФО, и {len((rsbu - ifrs) & outstanding)} из них "
        "имеют выпуски в обращении. Карточек этих эмитентов у нас нет вовсе: "
        "универсум собран из доставок МСФО, поэтому отсутствие их в списке "
        "не видно даже как отсутствие."
    )
    print(
        "\n**Чего замер не говорит.** Он не говорит, раскрывает ли эмитент "
        "МСФО в своём годовом отчёте: у агрегатора отчётности нет, а PDF "
        "эмитента мог бы быть. Проверить это можно только документом, "
        "и по 403 эмитентам вручную — нельзя."
    )
    print(f"\nКорзин в списке посчитано: {counts['эмитентов']}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
