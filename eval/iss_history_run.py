"""Что доставлено с ISS: глубина истории торгов, поля и цена на диске.

    uv run python eval/iss_history_run.py > data/output/iss_history_summary.md

**Читает диск, не сеть.** Доставка — `scripts/moex_market_fetch.py`; этот
замер только рассказывает, что она принесла, и ни одного запроса не делает.

**Три вопроса, и у каждого знаменатель.** Сколько выпусков с историей —
против скольких выпусков в обращении у наших эмитентов; какая глубина —
не «два года», а сколько дней у каждого выпуска на самом деле; какие поля
пришли — не перечень колонок, а доля строк, в которых поле не пусто. Колонка,
стоящая в ответе и пустая у всех, — это отсутствующее поле, и путать его
с присутствующим нельзя.

**Поля названы по родам**, потому что вопросы к ним разные: цена и доходность
нужны спреду, оборот и число сделок — ликвидному ядру, дюрация и срочность —
сравнимости. Поле, не попавшее ни в один род, печатается отдельно: перечень
родов наш, а колонки — источника, и новая колонка обязана быть замечена.
"""

import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402
from finlib.sources.moex import CACHE  # noqa: E402

logger = logging.getLogger(__name__)

# Рода полей: вопрос к ним разный, и сводка обязана их различать.
KINDS: dict[str, tuple[str, ...]] = {
    "цена": (
        "CLOSE",
        "LEGALCLOSEPRICE",
        "WAPRICE",
        "OPEN",
        "LOW",
        "HIGH",
        "MARKETPRICE2",
        "MARKETPRICE3",
        "ADMITTEDQUOTE",
        "CBRCLOSE",
    ),
    "доходность": (
        "YIELDCLOSE",
        "YIELDATWAP",
        "YIELDTOOFFER",
        "YIELDLASTCOUPON",
        "CALLOPTIONYIELD",
        "ZSPREAD",
        "ZSPREADATWAPRICE",
        "IRICPICLOSE",
        "BEICLOSE",
    ),
    "ликвидность": (
        "NUMTRADES",
        "VALUE",
        "VOLUME",
        "MP2VALTRD",
        "MARKETPRICE3TRADESVALUE",
        "ADMITTEDVALUE",
    ),
    "срочность": (
        "DURATION",
        "MATDATE",
        "OFFERDATE",
        "BUYBACKDATE",
        "CALLOPTIONDATE",
        "PUTOPTIONDATE",
        "CALLOPTIONDURATION",
        "LASTTRADEDATE",
        "DATEYIELDFROMISSUER",
    ),
    "устройство выпуска": (
        "COUPONPERCENT",
        "COUPONVALUE",
        "FACEVALUE",
        "FACEUNIT",
        "FACEVALUE_TYPE",
        "CURRENCYID",
        "BONDTYPE",
        "BONDSUBTYPE",
        "COUPON_DETAILS",
        "ACCINT",
    ),
}


def ours() -> tuple[set[str], set[str]]:
    """ISIN наших выпусков: в обращении и все, включая погашенные.

    **Знаменателя здесь два, и они отвечают на разные вопросы.** Выпуски
    в обращении — то, о чём маршрут спрашивает сегодня; все выпуски —
    то, по чему у истории вообще могут быть торги: погашенный выпуск
    торговался, пока был жив, и его ряд для калибровки годится наравне.
    """
    alive: set[str] = set()
    every: set[str] = set()
    for inn in bond_issuers():
        issues, known = issues_of(inn)
        if not known:
            continue
        for item in issues:
            if not item.isin:
                continue
            every.add(item.isin)
            if item.status == "в обращении":
                alive.add(item.isin)
    return alive, every


def main() -> int:
    """Печатает сводку доставленной истории торгов; 1 — доставки нет вовсе."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    files = sorted(CACHE.glob("xsec_*.json"))
    days = [item for item in files if "_p" not in item.name]
    if not days:
        print("срезов на диске нет: доставка не выполнялась")
        return 1

    by_isin: dict[str, list[date]] = {}
    filled: Counter[str] = Counter()
    boards: Counter[str] = Counter()
    columns: set[str] = set()
    rows = 0
    size = sum(item.stat().st_size for item in files)
    for path in days:
        found = json.loads(path.read_text(encoding="utf-8")).get("history") or []
        for row in found:
            rows += 1
            columns |= set(row)
            code = str(row.get("SECID") or "")
            when = str(row.get("TRADEDATE") or "")
            if code and when:
                by_isin.setdefault(code, []).append(date.fromisoformat(when))
            boards[str(row.get("BOARDID") or "")] += 1
            for name, value in row.items():
                if value is not None and value != "":
                    filled[name] += 1

    alive, mine = ours()
    covered = {code for code in by_isin if code in mine}
    covered_alive = {code for code in by_isin if code in alive}
    print("# История торгов ISS: что доставлено\n")
    print(
        f"Дней со срезом **{len(days)}**, строк **{rows}**, на диске "
        f"**{size / 1024 / 1024:.0f} МБ** (включая незавершённые страницы). "
        f"Глубина: {min(min(v) for v in by_isin.values()):%d.%m.%Y} — "
        f"{max(max(v) for v in by_isin.values()):%d.%m.%Y}.\n"
    )
    print(
        f"Выпусков с историей **{len(by_isin)}**, из них наших "
        f"**{len(covered)}** — включая погашенные, которых у эмитентов списка "
        f"{len(mine)} с ISIN. Выпусков **в обращении** история накрывает "
        f"**{len(covered_alive)}** из {len(alive)}: остальные за окно доставки "
        "не торговались ни дня. Срез берётся по рынку целиком — ориентир "
        "фазы 3 есть перцентиль ликвидного ядра, и по одним своим он считался "
        "бы по перечню, который мы сами и задали.\n"
    )

    print("## Глубина по выпускам\n")
    depth = sorted(len(set(v)) for v in by_isin.values())
    mine_depth = sorted(len(set(by_isin[code])) for code in covered)
    print(
        f"У всех выпусков медиана **{depth[len(depth) // 2]}** дней, "
        f"от {depth[0]} до {depth[-1]}. У наших медиана "
        f"**{mine_depth[len(mine_depth) // 2] if mine_depth else 0}** дней.\n"
    )
    print("| Дней истории | Выпусков всего | Из них наших |")
    print("|---|---|---|")
    edges = ((1, 10), (11, 50), (51, 150), (151, 300), (301, 10_000))
    for low, high in edges:
        total = sum(1 for item in depth if low <= item <= high)
        own = sum(1 for item in mine_depth if low <= item <= high)
        print(f"| {low}–{high if high < 10_000 else '…'} | {total} | {own} |")

    print("\n## Режимы торгов\n")
    print("| Режим | Строк |")
    print("|---|---|")
    for name, count in boards.most_common(8):
        print(f"| {name} | {count} |")

    print("\n## Какие поля пришли\n")
    print(
        "Доля — от строк среза. Колонка, стоящая в ответе и пустая у всех, "
        "это отсутствующее поле: путать его с присутствующим нельзя.\n"
    )
    for kind, names in KINDS.items():
        print(f"\n**{kind}**\n")
        print("| Поле | Заполнено | Доля |")
        print("|---|---|---|")
        for name in names:
            share = filled.get(name, 0) / rows * 100 if rows else 0
            print(f"| {name} | {filled.get(name, 0)} | {share:.1f} % |")
    rest = sorted(columns - {name for names in KINDS.values() for name in names})
    print(
        f"\nКолонок вне объявленных родов {len(rest)}: "
        + (", ".join(rest) if rest else "нет")
        + ". Перечень родов наш, а колонки — источника, и новая обязана быть "
        "замечена, а не попасть в сводку молча.\n"
    )

    curves = CACHE / "zcyc_by_day.json"
    if curves.exists():
        found = json.loads(curves.read_text(encoding="utf-8"))
        marks = sorted(found)
        print("## Кривая бескупонной доходности ОФЗ\n")
        points = sum(1 for item in found.values() if item.get("yearyields"))
        print(
            f"Кривых на диске **{len(found)}**, с {marks[0]} по {marks[-1]}; "
            f"с опубликованными опорными точками **{points}**. У каждой — "
            "параметры на закрытие дня (B1–B3, T1, G1–G9) и сама кривая "
            "в одиннадцати точках: по параметрам она считается, точками счёт "
            "проверяется, и фаза 3 обязана сверить формулу именно ими.\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
