"""Охват списка наблюдения: кого он видит, чем маршрутизирован и кого не видит.

    uv run python eval/watchlist_coverage_run.py     # make watchlist-coverage

**Вопрос фазы 1: видит ли список эмитента вообще.** Рынок уточняет тех, кого
список уже видит; охват отвечает на предыдущий вопрос. Прежде ответ был:
из 702 эмитентов с выпусками в обращении список видел 206, потому что
универсум собирался из доставок МСФО, а 403 раскрывают только отчётность
юридического лица. Отсутствие их не было видно даже как отсутствие.

Замер отвечает числами на три вопроса подряд:

1. **Сколько эмитентов с долгом в круге и чем каждый маршрутизирован** —
   консолидированной отчётностью, отчётностью юридического лица либо одними
   событиями и рейтингами. Знаменатель печатается всегда: «маршрутизировано
   660» без «из 702» выглядит полнотой.
2. **Как распределены корзины внутри каждой группы.** Доля «Без внимания»
   у маршрута по одним событиям и у маршрута по отчётности означает разное,
   и одна общая доля скрывала бы это.
3. **Почему отчётности нет** у тех, у кого её нет: отвергнута приёмом
   (валюта, неконсолидированная, единица, период), стоит в карантине,
   не спрашивалась вовсе либо отсутствует у источника.

**Строки без выпусков в обращении — отдельным разделом.** Маршрут спрашивает,
нужен ли человек, а нужен он там, где есть долг; эмитент, долг которого
погашен, из списка молча не исчезает, но в сводные доли не идёт — иначе доли
мерили бы состав списка, а не охват рынка.

**Замер не считает сам.** Корзины и состав берутся у боевой маршрутизации
(`scoring.routing_store.routing_rows`) — теми же вызовами, что страница
наблюдения и выгрузка. Универсумы читаются с диска: в сеть прогон не идёт,
и отсутствующий файл он называет, а не подменяет нулём.
"""

import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")

# Сохранённые перечни источника. Каждый — ответ целиком по стране либо
# по виду отчёта: отбирать по наименованию источник не умеет, и перечень
# забирается полностью.
UNIVERSES = {
    "ifrs": (
        "msfo_real_universe.json",
        "нормализованная отчётность по МСФО (get_report_msfo_real)",
    ),
    "rsbu": (
        "rsbu_balance_universe.json",
        "нормализованный баланс РСБУ (get_report_rsbu_balance)",
    ),
}

# Отказы приёма комплекта: строка, не ставшая комплектом, не молчит —
# причина названа кодом контроля и словами.
_REJECTED = """
SELECT inn, message FROM dq_log
WHERE check_code = 'cbonds_set_rejected'
"""

# Комплекты в карантине по стандарту: отчётность есть и отбракована нами.
_QUARANTINED = """
SELECT DISTINCT inn, standard FROM src_file WHERE status = 'quarantine'
"""

_LOADED = """
SELECT DISTINCT inn, standard FROM src_file
WHERE is_actual AND status <> 'quarantine'
"""


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


def _reason_of(message: str) -> str:
    """Краткое имя причины отказа приёма: первые слова сообщения.

    Сообщение пишет загрузчик, и оно содержательно целиком; для сводки нужна
    не проза, а вид причины. Вид берётся из самого сообщения, а не из второго
    перечня видов: перечень разошёлся бы с сообщениями при первой же правке.
    """
    head = message.split(":")[0].strip().lower()
    return head or "причина не названа"


def main() -> int:
    """Печатает охват; 1 — если перечни на диске неполны."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    try:
        universes = {key: _inns(name) for key, (name, _) in UNIVERSES.items()}
    except FileNotFoundError as error:
        print(error)
        return 1

    bonds = set(bond_issuers())
    ifrs, rsbu = universes["ifrs"], universes["rsbu"]
    routing = load_routing()
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
        rejected: dict[str, set[str]] = defaultdict(set)
        for row in fetch_all(_REJECTED, {}, conn=conn):
            rejected[_reason_of(row["message"])].add(row["inn"])
        quarantined = {
            (row["inn"], row["standard"])
            for row in fetch_all(_QUARANTINED, {}, conn=conn)
        }
        loaded = {
            (row["inn"], row["standard"])
            for row in fetch_all(_LOADED, {}, conn=conn)
        }

    listed = {item.inn: item for item in rows}
    with_bonds = [item for item in rows if item.has_bonds]
    idle = [item for item in rows if not item.has_bonds]
    absent = bonds - set(listed)

    print("# Охват списка наблюдения\n")
    print(f"Эмитентов с выпусками в обращении у источника: **{len(bonds)}**.")
    print(f"Из них в списке **{len(with_bonds)}**, нет в списке **{len(absent)}**.")
    if absent:
        print(
            "Нет в списке — значит, эмитент вышел из него с записью в журнале "
            "исключений: ликвидирован, преемник известен."
        )
    print(
        f"\nВсего строк в списке {len(rows)}: к ним добавлены {len(idle)} "
        "эмитентов **без выпусков в обращении** — их отчётность у нас есть, "
        "и молча они из списка не исчезают."
    )

    # --- чем маршрутизирован каждый ----------------------------------------
    print("\n## Чем построен маршрут\n")
    print(
        "Маршрут спрашивает одно и то же у обоих стандартов, а базу выбирает "
        "порядком предпочтения (`standards.yaml`): консолидированная "
        "отчётность описывает периметр деятельности, отчётность юридического "
        "лица — то, чем долг привлечён. Отчётности нет ни по одному стандарту — "
        "маршрут строится по событиям и рейтингам: они от стандарта не зависят.\n"
    )
    ordered = [item.code for item in routing.ordered()]
    names = {item.code: item.name for item in routing.baskets}
    by_basis: dict[str, list] = defaultdict(list)
    for item in with_bonds:
        by_basis[item.basis].append(item)
    print("| Чем построен | Эмитентов | " + " | ".join(names[code] for code in ordered) + " |")
    print("|---|---|" + "---|" * len(ordered))
    for basis, group in sorted(
        by_basis.items(), key=lambda pair: -len(pair[1])
    ):
        baskets = Counter(entry.verdict.basket for entry in group)
        print(
            f"| {basis} | {len(group)} | "
            + " | ".join(str(baskets.get(code, 0)) for code in ordered)
            + " |"
        )
    total = Counter(entry.verdict.basket for entry in with_bonds)
    print(
        f"| **итого** | **{len(with_bonds)}** | "
        + " | ".join(f"**{total.get(code, 0)}**" for code in ordered)
        + " |"
    )

    # --- строки без выпусков ------------------------------------------------
    print("\n## Строки без выпусков в обращении\n")
    print(
        f"Их {len(idle)}, и в доли охвата они не идут: маршрут спрашивает, "
        "нужен ли человек, а нужен он там, где есть долг. Из списка они "
        "не исчезают — отчётность у нас загружена, и молчание о них читалось "
        "бы как «такого эмитента нет».\n"
    )
    idle_baskets = Counter(entry.verdict.basket for entry in idle)
    print("| Корзина | Эмитентов |")
    print("|---|---|")
    for code in ordered:
        print(f"| {names[code]} | {idle_baskets.get(code, 0)} |")

    # --- почему отчётности нет ---------------------------------------------
    no_reporting = [item for item in with_bonds if item.standard is None]
    print("\n## Почему отчётности нет\n")
    print(
        f"Эмитентов с долгом и без отчётности {len(no_reporting)} из "
        f"{len(with_bonds)}. Причины считаются по тому, что записано: отказ "
        "приёма — журналом контролей, карантин — состоянием комплекта, "
        "остальное — наличием эмитента в перечнях источника.\n"
    )
    print("| Почему | Эмитентов |")
    print("|---|---|")
    silent = set(entry.inn for entry in no_reporting)
    named: set[str] = set()
    for reason, who in sorted(rejected.items(), key=lambda pair: -len(pair[1])):
        hit = who & silent
        if not hit:
            continue
        named |= hit
        print(f"| отказ приёма: {reason} | {len(hit)} |")
    in_quarantine = {inn for inn, _ in quarantined} & silent - named
    if in_quarantine:
        named |= in_quarantine
        print(f"| комплекты в карантине | {len(in_quarantine)} |")
    nothing = {inn for inn in silent - named if inn not in ifrs and inn not in rsbu}
    print(f"| у источника нет ни МСФО, ни РСБУ | {len(nothing)} |")
    rest = silent - named - nothing
    print(f"| отчётность у источника есть, у нас не загружена | {len(rest)} |")

    # --- отчётность по МСФО есть у источника, а маршрут не по ней -----------
    print("\n## Отчётность по МСФО есть у источника, а маршрут построен иначе\n")
    other = [
        item
        for item in with_bonds
        if item.inn in ifrs and item.basis != "МСФО · консолидированная"
    ]
    print(
        f"Таких {len(other)}. Вопрос содержательный: у эмитента есть "
        "консолидированная отчётность, а маршрут построен по отчётности "
        "юридического лица либо по одним событиям, то есть по более узкому "
        "основанию.\n"
    )
    print("| Почему | Эмитентов |")
    print("|---|---|")
    left = {item.inn for item in other}
    shown: set[str] = set()
    for reason, who in sorted(rejected.items(), key=lambda pair: -len(pair[1])):
        hit = who & left
        if not hit:
            continue
        shown |= hit
        print(f"| отказ приёма: {reason} | {len(hit)} |")
    ifrs_quarantine = {inn for inn, standard in quarantined if standard == "ifrs"}
    hit = (ifrs_quarantine & left) - shown
    if hit:
        shown |= hit
        print(f"| комплект МСФО в карантине | {len(hit)} |")
    print(f"| не загружено, причина не записана | {len(left - shown)} |")

    # --- чего замер не говорит ---------------------------------------------
    print("\n## Чего замер не говорит\n")
    print(
        "- раскрывает ли эмитент МСФО **в своём годовом отчёте**: у агрегатора "
        f"отчётности нет, а PDF эмитента мог бы быть. Универсум МСФО источника "
        f"— {len(ifrs)} эмитентов, баланса РСБУ — {len(rsbu)};"
    )
    print(
        "- полон ли перечень выпусков: он взят у одного источника, и второго "
        "у нас нет;"
    )
    print(
        "- верна ли корзина: замер мерит охват, а не качество решения. "
        "Качество мерится на календаре событий."
    )
    print(
        f"\nКомплектов вне карантина в базе: {len(loaded)} пар «эмитент — "
        f"стандарт». Корзин посчитано: {counts['эмитентов']}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
