"""Раскладка непрозрачных эмитентов Cbonds по видам (замер 21.09.2026).

**Зачем.** Доля «прочих» выше половины у 138 рублёвых эмитентов из 455 — это
30 % универсума, и если все они пойдут к человеку по признаку непрозрачности,
скрининг не сэкономит внимания. Вопрос замера: кто эти 138.

**Вид определяется данными, а не наименованием.** «…Финанс» в названии —
примета, и по ней в один вид попадали бы Русбонд-Удобрения и Коршуновский ГОК.
Справочник эмитентов Cbonds (`get_emitents`) даёт признак SPV, отрасль
и категорию МСП — этим и раскладываем; наименование в отчёте остаётся, чтобы
раскладку можно было проверить глазами.

    uv run python eval/cbonds_opaque.py > data/output/cbonds_opaque.md

В базу не пишет и в сеть не ходит: читает сохранённые ответы
`data/raw/cbonds/`. Карточки эмитентов собираются отдельно и лежат
в `emitents_opaque.json` — 138 запросов по одному на ИНН, потому что отбор
`in` источник не поддерживает (проверено: по трём ИНН возвращает ноль записей).
"""

import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

CACHE = Path("data/raw/cbonds")
UNIVERSE = CACHE / "msfo_real_universe.json"
CARDS = CACHE / "emitents_opaque.json"

# Отсечка непрозрачности: доля «прочих» в активах либо в обязательствах.
# Величина обсуждается как методическое решение; здесь она параметр замера.
THRESHOLD = Decimal(50)

# Отрасли, у которых «прочие» — устройство дела, а не непрозрачность:
# у финансовой компании выданные займы и стоят в прочих активах.
FINANCIAL = frozenset(
    {
        "Лизинг и аренда",
        "Микрофинансирование",
        "Прочие финансовые институты",
        "Холдинги",
        "Банки",
        "Страхование",
        "Инвестиционные компании и фонды",
        "Финансовые институты",
    }
)

SIZE = {"1": "микро", "3": "малое", "5": "среднее", "7": "не МСП", "0": "—"}
UNITS = {1000: "тыс.", 1000000: "млн", 1000000000: "млрд"}

# Выручка ниже этой доли активов означает, что операционной деятельности
# у эмитента по существу нет: структурный признак сильнее наименования.
OPERATING_MIN = Decimal(5)


def number(value) -> Decimal | None:
    """Величина Cbonds в Decimal; пустое значение остаётся None."""
    return None if value in (None, "") else Decimal(str(value))


def latest_annual(items: list[dict]) -> dict[str, dict]:
    """Самая свежая годовая строка каждого рублёвого эмитента."""
    found: dict[str, dict] = {}
    for row in items:
        inn = row.get("emitent_inn")
        if not inn or not row["date"].endswith("12-31") or row.get("ln104") != "RUB":
            continue
        if inn not in found or row["date"] > found[inn]["date"]:
            found[inn] = row
    return found


def kind_of(branch: str, spv: bool, revenue: Decimal | None, assets: Decimal) -> str:
    """Вид эмитента: признак источника, отрасль, затем структурный признак."""
    if spv:
        return "финансирующая структура (SPV по данным Cbonds)"
    if branch in FINANCIAL:
        return "финансовый сектор и холдинги"
    if revenue is None or revenue / assets * 100 < OPERATING_MIN:
        return "без операционной деятельности (SPV не помечен)"
    return "операционная компания"


def opaque_rows() -> list[dict]:
    """Эмитенты с долей «прочих» не ниже отсечки, с карточкой источника."""
    items = json.loads(UNIVERSE.read_text(encoding="utf-8"))["items"]
    cards = json.loads(CARDS.read_text(encoding="utf-8")) if CARDS.exists() else {}
    rows: list[dict] = []
    for inn, row in latest_annual(items).items():
        assets, equity = number(row.get("ln11")), number(row.get("ln20"))
        if not assets or equity is None:
            continue
        liabilities = assets - equity
        others_assets = (number(row.get("ln5")) or 0) + (number(row.get("ln9")) or 0)
        others_liabs = (number(row.get("ln15")) or 0) + (number(row.get("ln18")) or 0)
        share_assets = others_assets / assets * 100
        share_liabs = others_liabs / liabilities * 100 if liabilities else Decimal(0)
        if max(share_assets, share_liabs) < THRESHOLD:
            continue
        card = cards.get(inn) or {}
        branch = card.get("branch_name_rus") or "отрасль не названа"
        spv = str(card.get("emitent_spv")) == "1"
        revenue = number(row.get("ln23"))
        rows.append(
            {
                "inn": inn,
                "name": (card.get("full_name_rus") or row.get("emitent_name_rus") or "").strip(),
                "year": row["date"][:4],
                "assets": assets,
                "unit": int(row.get("ln105") or 0),
                "share_assets": share_assets,
                "share_liabs": share_liabs,
                "revenue": revenue,
                "equity": equity,
                "branch": branch,
                "size": SIZE.get(str(card.get("emitent_categories_id")), "—"),
                "spv": spv,
                "kind": kind_of(branch, spv, revenue, assets),
                "revenue_share": (revenue / assets * 100) if revenue is not None else None,
            }
        )
    return rows


def main() -> int:
    """Печатает раскладку; 1 — если сохранённых ответов нет."""
    if not UNIVERSE.exists():
        print(f"нет {UNIVERSE}: сохранённого ответа Cbonds на диске не найдено")
        return 1
    rows = opaque_rows()
    measured = len(latest_annual(json.loads(UNIVERSE.read_text(encoding="utf-8"))["items"]))
    if not rows:
        print("эмитентов сверх отсечки не нашлось — измерение состоялось, вид пуст")
        return 0

    print("# Непрозрачные эмитенты Cbonds: раскладка по видам\n")
    print(
        "Отобраны рублёвые эмитенты, у которых доля «прочих» в активах либо "
        f"в обязательствах не ниже {THRESHOLD} %: **{len(rows)} из {measured}**. "
        "Вид определяется данными справочника эмитентов Cbonds — признаком SPV, "
        "отраслью и категорией МСП, — а не наименованием: «…Финанс» в названии "
        "примета, а не признак.\n"
    )
    counts = Counter(item["kind"] for item in rows)
    print("| Вид | Эмитентов | Доля | Отрицательный капитал | Выручки нет |")
    print("|---|---|---|---|---|")
    for name, count in counts.most_common():
        group = [item for item in rows if item["kind"] == name]
        negative = sum(1 for item in group if item["equity"] < 0)
        no_revenue = sum(1 for item in group if not item["revenue"])
        print(
            f"| {name} | {count} | {count / len(rows) * 100:.0f} % "
            f"| {negative} | {no_revenue} |"
        )

    print("\n## Отрасли\n")
    print("| Отрасль | Эмитентов |")
    print("|---|---|")
    for name, count in Counter(item["branch"] for item in rows).most_common():
        print(f"| {name} | {count} |")

    for name, _ in counts.most_common():
        group = sorted(
            (item for item in rows if item["kind"] == name),
            key=lambda item: -max(item["share_assets"], item["share_liabs"]),
        )
        print(f"\n## {name} — {len(group)}\n")
        print(
            "| Эмитент | ИНН | Год | Активы | Ед. | Прочие А | Прочие О "
            "| Выручка/Активы | SPV | Отрасль | Размер | Капитал < 0 |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for item in group:
            revenue = (
                f"{item['revenue_share']:.1f} %"
                if item["revenue_share"] is not None
                else "нет"
            )
            assets = f"{item['assets']:,.0f}".replace(",", " ")
            print(
                f"| {item['name'][:44]} | {item['inn']} | {item['year']} | {assets} "
                f"| {UNITS.get(item['unit'], item['unit'])} | {item['share_assets']:.0f} % "
                f"| {item['share_liabs']:.0f} % | {revenue} "
                f"| {'да' if item['spv'] else ''} | {item['branch'][:28]} | {item['size']} "
                f"| {'да' if item['equity'] < 0 else ''} |"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
