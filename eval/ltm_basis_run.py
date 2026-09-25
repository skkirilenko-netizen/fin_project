"""Замер фазы 5-бис: что изменила LTM-база маршрута — было/стало.

    uv run python eval/ltm_basis_run.py --before history_before.csv

`--before` — выгрузка `routing_history` до перехода на LTM-базу (снятая
`\\copy (select inn, as_of, kind, standard, basket, subgroup, grounds,
grounds_all, inputs, report_date from routing_history) to … csv header`):
история переписывается пересчётом, и прежнего её вида в базе после него
не остаётся, а сравнивать надо с ним.

**Замер не считает сам**: корзины и величины дала боевая маршрутизация
и записала в историю; здесь только сравнение двух записанных состояний.

Что печатается:

1. точка сегодняшнего прогона — матрица переходов корзин и поимённо
   сменившие корзину, сколько эмитентов сменили базу;
2. дребезг — смены корзины в пересчёте за год, было и стало;
3. сезонность баланса — ключевые отношения на 30.06 (LTM) против 31.12
   у одних и тех же эмитентов: медиана и p10/p90, парный сдвиг;
4. критерий «качество» — эмитенты с событием, стоявшие «Без внимания»
   в последний день перед событием.
"""

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_lead_run import events  # noqa: E402

from finlib.db import connection, fetch_all  # noqa: E402

_HISTORY = """
SELECT inn, as_of, kind, standard, basket, grounds_all, inputs, report_date
FROM routing_history
"""

# Ключевые отношения и их имена по стандартам: у РСБУ долговой нагрузки нет,
# вывод делается границей — чистым долгом к прибыли от продаж.
RATIOS = {
    "cur_liq": "текущая ликвидность",
    "equity_ratio": "автономия",
    "net_debt_ebitda": "чистый долг / EBITDA (МСФО)",
    "debt_to_op_profit": "чистый долг / прибыль от продаж (РСБУ)",
}


def _load_before(path: Path) -> dict[tuple[str, date, str], dict]:
    """Прежняя история: ключ «ИНН, дата, род точки»."""
    found: dict[tuple[str, date, str], dict] = {}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            row["inputs"] = json.loads(row["inputs"]) if row["inputs"] else {}
            row["report_date"] = (
                date.fromisoformat(row["report_date"]) if row["report_date"] else None
            )
            found[(row["inn"], date.fromisoformat(row["as_of"]), row["kind"])] = row
    return found


def _load_after() -> dict[tuple[str, date, str], dict]:
    """Нынешняя история из базы тем же ключом."""
    with connection() as conn:
        rows = fetch_all(_HISTORY, {}, conn=conn)
    return {(row["inn"], row["as_of"], row["kind"]): dict(row) for row in rows}


def _quantiles(values: list[Decimal]) -> str:
    """Медиана и p10/p90; пусто — прочерк."""
    if not values:
        return "—"
    ordered = sorted(values)
    cut = statistics.quantiles([float(item) for item in ordered], n=10)
    return f"{statistics.median(ordered):.2f} ({cut[0]:.2f} … {cut[-1]:.2f})"


def _today(before: dict, after: dict) -> None:
    """Точка сегодняшнего прогона: переходы корзин и смена базы."""
    day = max(key[1] for key in after if key[2] == "run")
    was = {key[0]: row for key, row in before.items() if key[1] == day and key[2] == "run"}
    now = {key[0]: row for key, row in after.items() if key[1] == day and key[2] == "run"}
    common = sorted(set(was) & set(now))
    print(f"## 1. Точка прогона {day:%d.%m.%Y}\n")
    moved = Counter((was[inn]["basket"], now[inn]["basket"]) for inn in common)
    baskets = sorted({pair for items in moved for pair in items})
    print("| было \\ стало | " + " | ".join(baskets) + " |")
    print("|---|" + "---|" * len(baskets))
    for left in baskets:
        print(
            f"| {left} | "
            + " | ".join(str(moved.get((left, right), 0)) for right in baskets)
            + " |"
        )
    based = [
        inn
        for inn in common
        if now[inn]["report_date"] is not None
        and was[inn]["report_date"] is not None
        and now[inn]["report_date"] > was[inn]["report_date"]
    ]
    changed = [inn for inn in common if was[inn]["basket"] != now[inn]["basket"]]
    print(
        f"\nЭмитентов в обеих точках {len(common)}; базу сменили **{len(based)}**; "
        f"корзину сменили **{len(changed)}**, из них при смене базы "
        f"{sum(1 for inn in changed if inn in set(based))}.\n"
    )
    if changed:
        print("| ИНН | было | стало | база стала | основания стали |")
        print("|---|---|---|---|---|")
        for inn in changed:
            print(
                f"| {inn} | {was[inn]['basket']} | {now[inn]['basket']} "
                f"| {now[inn]['report_date'] or '—'} "
                f"| {', '.join(now[inn]['grounds_all'] or []) or '—'} |"
            )


_LAST_GRID = """
SELECT DISTINCT as_of FROM routing_history
WHERE kind = 'backfill'
  AND run_id = (SELECT max(id) FROM routing_run WHERE kind = 'backfill')
"""


def _churn(before: dict, after: dict) -> None:
    """Дребезг: смены корзины в пересчёте за год.

    **Сравниваются одни и те же даты — сетки последнего пересчёта, бывшие
    и в прежней истории.** В таблице лежат и точки прежних пересчётов с другой
    привязкой недели; вставленные в ряд, они давали бы смены, которых
    не было ни в одном пересчёте.
    """
    with connection() as conn:
        last = {row["as_of"] for row in fetch_all(_LAST_GRID, {}, conn=conn)}
    days = last & {day for (_, day, kind) in before if kind == "backfill"}
    print("\n## 2. Дребезг пересчёта за год\n")
    print(f"Даты: сетка последнего пересчёта, бывшие и в прежней истории, — {len(days)}.\n")
    for name, rows in (("было", before), ("стало", after)):
        series: dict[str, list[tuple[date, str]]] = defaultdict(list)
        for (inn, day, kind), row in rows.items():
            if kind == "backfill" and day in days:
                series[inn].append((day, row["basket"]))
        moves = {
            inn: sum(
                1
                for (_, left), (_, right) in zip(
                    sorted(items)[:-1], sorted(items)[1:], strict=True
                )
                if left != right
            )
            for inn, items in series.items()
        }
        print(
            f"- {name}: смен корзины **{sum(moves.values())}**, "
            f"сменили хотя бы раз {sum(1 for value in moves.values() if value)} "
            f"из {len(moves)}"
        )


def _seasonality(before: dict, after: dict) -> None:
    """Сезонность баланса: отношения на 30.06 против 31.12 у тех же эмитентов."""
    day = max(key[1] for key in after if key[2] == "run")
    print("\n## 3. Сезонность баланса: 30.06 (LTM) против 31.12\n")
    print(
        "Одни и те же эмитенты: у кого на сегодня база — LTM на 30.06, "
        "а прежде была годовая на 31.12. Парный сдвиг — медиана разностей "
        "«стало − было» и доля эмитентов, у которых отношение выросло.\n"
    )
    print(
        "| Отношение | Эмитентов | 31.12, медиана (p10 … p90) "
        "| 30.06, медиана (p10 … p90) | Парный сдвиг, медиана | Выросло |"
    )
    print("|---|---|---|---|---|---|")
    for code, name in RATIOS.items():
        was: list[Decimal] = []
        now: list[Decimal] = []
        for (inn, when, kind), row in after.items():
            if kind != "run" or when != day:
                continue
            old = before.get((inn, when, kind))
            if old is None or row["report_date"] is None or old["report_date"] is None:
                continue
            if (row["report_date"].month, old["report_date"].month) != (6, 12):
                continue
            left = (old["inputs"].get("metrics") or {}).get(code)
            right = ((row["inputs"] or {}).get("metrics") or {}).get(code)
            if left is None or right is None:
                continue
            was.append(Decimal(left))
            now.append(Decimal(right))
        shifts = [right - left for left, right in zip(was, now, strict=True)]
        grew = sum(1 for item in shifts if item > 0)
        print(
            f"| {name} | {len(was)} | {_quantiles(was)} | {_quantiles(now)} "
            f"| {statistics.median(shifts):.2f} | "
            f"{grew} из {len(shifts)} |"
            if shifts
            else f"| {name} | 0 | — | — | — | — |"
        )


def _quality(before: dict, after: dict) -> None:
    """Критерий «качество»: эмитент с событием стоял «Без внимания» накануне."""
    print("\n## 4. Критерий «качество»: «Без внимания» накануне события\n")
    happened = events()
    for name, rows in (("было", before), ("стало", after)):
        series: dict[str, list[tuple[date, str]]] = defaultdict(list)
        for (inn, day, kind), row in rows.items():
            if kind == "backfill":
                series[inn].append((day, row["basket"]))
        first = min((day for items in series.values() for day, _ in items), default=None)
        missed: list[str] = []
        seen = 0
        for inn, moment in happened.items():
            if first is None or moment <= first or inn not in series:
                continue
            prior = [basket for day, basket in sorted(series[inn]) if day < moment]
            if not prior:
                continue
            seen += 1
            if prior[-1] == "clear":
                missed.append(f"{inn} ({moment:%d.%m.%Y})")
        print(
            f"- {name}: событий в окне истории {seen}, «Без внимания» накануне "
            f"**{len(missed)}**" + (f": {', '.join(missed)}" if missed else "")
        )


def main() -> int:
    """Печатает замер; 1 — если прежней истории не передано."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    args = parser.parse_args()
    if not args.before.exists():
        print(f"прежней истории нет: {args.before}")
        return 1
    before = _load_before(args.before)
    after = _load_after()
    print("# Фаза 5-бис: LTM-база маршрута — было/стало\n")
    _today(before, after)
    _churn(before, after)
    _seasonality(before, after)
    _quality(before, after)
    print(
        "\nУпреждение, прирост и выявляемость слоёв, в том числе рефинансирование "
        "отдельной строкой, — `make layer-matrix` и `make market-lead` до и после."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
