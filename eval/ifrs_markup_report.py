"""Отчёт по разметке МСФО: очередь, сходимость итогов, общность статей.

Отвечает на вопрос ветки: покроет ли справочник новых эмитентов. Расчёт
общности зафиксирован в `finlib.sources.ifrs_commonality` до подведения
итогов — иначе всегда найдётся способ посчитать так, чтобы вышло убедительно.

    uv run python eval/ifrs_markup_report.py
    uv run python eval/ifrs_markup_report.py --grouping 7736216869=english

Ничего не пишет ни в базу, ни на диск: это замер, а не загрузка.
"""

import argparse
import logging
import sys
from collections import Counter
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.quality.totals import TotalVerdict
from finlib.sources.ifrs_commonality import commonality, without_atypical
from finlib.sources.ifrs_markup import candidates, review_saved

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Печатает отчёт по разметке; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument(
        "--grouping",
        action="append",
        default=[],
        metavar="ИНН=конвенция",
        help="Разделитель разрядов вручную: ИНН=russian|english|plain",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    manual: dict[str, str] = {}
    for item in args.grouping:
        inn, _, convention = item.partition("=")
        manual[inn.strip()] = convention.strip()

    issuers, skipped = _load_issuers(args.path, manual)
    if not issuers:
        print(f"в каталоге {args.path} нет документов, прошедших приём")
        return 1

    catalog = load_ifrs_lines()
    print("СОСТАВ РАЗМЕТКИ")
    for issuer in issuers:
        profile = issuer.profile
        print(
            f"  {issuer.inn}: включён — {profile.currency}, "
            f"{profile.grouping.value}, периодов {len(profile.report_dates)}, "
            f"строк {issuer.extraction.rows_total}, "
            f"опознано {issuer.extraction.rows_recognised}"
        )
    for name, reason in skipped:
        print(f"  исключён {name}: {reason}")

    print("\nОЧЕРЕДЬ РАЗМЕТКИ")
    queue = candidates(issuers, catalog)
    overall_queue: Counter[str] = Counter()
    for issuer in issuers:
        rows = candidates([issuer], catalog)
        counts = Counter(item.priority.name for item in rows)
        overall_queue.update(counts)
        breakdown = ", ".join(
            f"{name} {counts[name]}" for name in ("BREAKS_TOTAL", "MATERIAL", "OTHER")
        )
        print(f"  {issuer.inn}: {len(rows)} — {breakdown}")
    print(
        f"  всего {len(queue)} — "
        + ", ".join(
            f"{name} {overall_queue[name]}"
            for name in ("BREAKS_TOTAL", "MATERIAL", "OTHER")
        )
    )

    print("\nСХОДИМОСТЬ ИТОГОВ")
    for issuer in issuers:
        state = issuer.totals_state(catalog)
        matched = [code for code, verdict in state.items() if verdict is TotalVerdict.MATCHED]
        broken = [
            code for code, verdict in state.items() if verdict is TotalVerdict.MISMATCHED
        ]
        print(f"  {issuer.inn}: сошлось {len(matched)}, не сошлось {len(broken)}")
        for code in broken:
            print(f"      не сошёлся {code}")

    print("\nОБЩНОСТЬ СТАТЕЙ")
    found, overall, frequency = commonality(issuers, catalog)
    for item in found:
        print(f"  {item.describe()}")
    print(f"  ВСЕГО: {overall.describe()}")
    print(f"  {frequency.describe()}")

    typical, typical_frequency = without_atypical(found)
    atypical = [item.inn for item in found if item.atypical]
    if atypical:
        print(f"  без нетипичных ({', '.join(atypical)}): {typical.describe()}")
        print(f"  {typical_frequency.describe()}")

    print("\nРАЗМЕТКА ПРОШЛОЙ СЕССИИ")
    try:
        saved = review_saved(issuers)
    except Exception as failure:  # noqa: BLE001 — замер не должен падать из-за базы
        print(f"  журнал подтверждений недоступен: {failure}")
    else:
        if not saved:
            print("  подтверждений прежних сессий нет")
        for item in saved:
            print(f"  {item.describe()}")

    print("\nКОДЫ ПО ЧАСТОТЕ")
    for code, seen in sorted(
        frequency.by_code.items(), key=lambda item: (-item[1], item[0])
    ):
        position = catalog.get(code)
        name = position.name if position is not None else "—"
        print(f"  {seen} из {len(issuers)}  {code:48} {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
