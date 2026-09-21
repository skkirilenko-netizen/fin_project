"""Загрузка универсума МСФО из нормализованных данных Cbonds (задача 31).

Отвечает на вопрос: сколько эмитентов вообще попадает в базу нормализованными
данными и сколько из них проходит проверки нуля. Без этого маршрутизация
опирается на четыре комплекта, разобранных из PDF.

    uv run python eval/cbonds_universe_run.py            # без записи, только счёт
    uv run python eval/cbonds_universe_run.py --write    # с записью в базу

**Запросов к источнику не делает вовсе.** Справочник отдаёт весь массив
записей одним ответом, и он уже сохранён (`data/raw/cbonds/
msfo_real_universe.json`): спрашивать источник по каждому ИНН значило бы
тратить суточную норму на то, что лежит на диске.

**Замер не считает сам**: строки идут в `pipeline.accept_cbonds_report`,
а прогон считает исходы. Своего разбора полей, своих проверок и своего
сопоставления здесь нет — они в справочнике и в загрузчике.
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.pipeline import accept_cbonds_report  # noqa: E402
from finlib.quality.codes import CheckCode  # noqa: E402
from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

# Предел запросов ночи: источник объявляет 10 000 в сутки, и прогон обязан
# останавливаться раньше, а не выяснять предел отказом.
REQUEST_BUDGET = 3000


def by_issuer(rows: list[dict]) -> dict[str, list[dict]]:
    """Строки источника по ИНН: у эмитента их несколько, по периодам."""
    found: dict[str, list[dict]] = {}
    for row in rows:
        inn = (row.get("emitent_inn") or "").strip()
        if inn:
            found.setdefault(inn, []).append(row)
    return found


def main(argv: list[str] | None = None) -> int:
    """Прогоняет универсум через загрузчик; печатает отчёт."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="писать комплекты в базу")
    parser.add_argument("--limit", type=int, default=0, help="ограничить число эмитентов")
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s %(name)s: %(message)s",
    )

    rows = cbonds.msfo_universe()
    grouped = by_issuer(rows)
    issuers = sorted(grouped)
    if args.limit:
        issuers = issuers[: args.limit]
    print("# Загрузка универсума МСФО из Cbonds\n")
    print(f"- записей в справочнике: {len(rows)}")
    print(f"- эмитентов: {len(grouped)}, в прогоне {len(issuers)}")
    print(f"- запросов к источнику: {cbonds.pace.requested} (норма ночи {REQUEST_BUDGET})")
    if not args.write:
        print(
            "\n**Прогон без записи.** Строки разбираются, комплекты не пишутся: "
            "запись включается ключом `--write`."
        )

    outcomes: list[object] = []
    verdicts: Counter[str] = Counter()
    rejections: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    if args.write:
        # **Транзакция на эмитента, а не на прогон.** Загрузка комплекта одной
        # транзакцией — правило проекта; одна транзакция на весь универсум
        # означала бы, что сбой на пятисотом эмитенте уносит четыреста
        # девяносто девять загруженных.
        for n, inn in enumerate(issuers, 1):
            outcomes.extend(accept_cbonds_report(inn, rows=grouped[inn]))
            if n % 100 == 0:
                print(f"  ...{n} из {len(issuers)}", flush=True)
    else:
        # Без записи прогон считает то же, но в откатываемой транзакции:
        # второго пути к тому же ответу здесь нет.
        with connection() as conn:
            for inn in issuers:
                found = accept_cbonds_report(inn, rows=grouped[inn], conn=conn)
                outcomes.extend(found)
            conn.rollback()

    for item in outcomes:
        if not item.accepted:
            verdicts["строка не принята"] += 1
            rejections[_reason_kind(item.rejection.reason)] += 1
            continue
        verdicts["комплект в карантине" if item.quarantined else "комплект принят"] += 1
        for code, _ in item.failures:
            failures[code] += 1

    print("\n## Исходы строк\n")
    print("| Исход | Строк |")
    print("|---|---|")
    total = sum(verdicts.values())
    for name, count in verdicts.most_common():
        print(f"| {name} | {count} |")
    print(f"| **всего строк** | **{total}** |")

    if rejections:
        print("\n## Почему строка не принята\n")
        print("| Причина | Строк |")
        print("|---|---|")
        for name, count in rejections.most_common():
            print(f"| {name} | {count} |")

    print("\n## Проверки нуля и сверки\n")
    print("| Контроль | Сработал у строк |")
    print("|---|---|")
    checks = (
        CheckCode.CBONDS_IDENTITY_MISMATCH,
        CheckCode.CBONDS_SECTIONS_MISMATCH,
        CheckCode.CBONDS_ZERO_TOTAL,
        CheckCode.CBONDS_DEBT_SPLIT_MISMATCH,
    )
    accepted = verdicts["комплект принят"] + verdicts["комплект в карантине"]
    for code in checks:
        print(f"| {code.value} | {failures.get(code.value, 0)} |")
    print(f"| **проверено комплектов** | **{accepted}** |")

    mismatched = [item for item in outcomes if getattr(item, "mismatches", ())]
    print(
        f"\nРасхождений с первоисточником: {sum(len(item.mismatches) for item in mismatched)} "
        f"у {len(mismatched)} комплектов — величина документа не затирается, "
        "расхождение идёт в журнал."
    )
    facts = sum(getattr(item, "facts", 0) for item in outcomes)
    print(f"Величин записано: {facts}.")
    return 0


def _reason_kind(reason: str) -> str:
    """Причина отказа коротким видом: для сводки, а не для журнала."""
    for mark, name in (
        ("валюта", "валюта не рублёвая"),
        ("единица", "единица измерения не объявлена"),
        ("неконсолидированная", "отчётность неконсолидированная"),
        ("стандарт", "стандарт не опознан"),
        ("не годовой", "период не годовой"),
        ("ИНН", "нет ИНН"),
    ):
        if mark in reason:
            return name
    return "прочее"


if __name__ == "__main__":
    sys.exit(main())
