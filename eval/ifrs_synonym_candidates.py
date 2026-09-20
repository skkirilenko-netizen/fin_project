"""Наименования на подъём в справочник МСФО: работа, лежащая без применения.

Человек присвоил строке код на экране разметки; у другого комплекта — своего
или чужого эмитента — та же строка разбором не опознаётся. Переносит
наименование **только справочник**: подтверждение действует у того же
эмитента (`sources/ifrs_confirmed.py`), а у чужого та же формулировка может
означать другое.

Поднимаются только присвоения вида `exact`: детализация входит в позицию,
но позицией не является, а специфическая статья позиции в ядре не имеет —
её место определяется представлением `ifrs_core_candidate`, а не этим
перечнем.

**Неоднозначное наименование отмечается, а не поднимается.** Один и тот же
текст, подтверждённый двумя разными кодами, — это либо двойник, различаемый
разделом («Кредиты и займы» в долгосрочных и в краткосрочных), либо спор
двух эмитентов о смысле. Синонимом такое объявить нельзя: статья ляжет
в позицию, которая встретилась раньше, то есть произвольно.

    uv run python eval/ifrs_synonym_candidates.py
    uv run python eval/ifrs_synonym_candidates.py --grouping 7736216869=english

Ничего не пишет ни в базу, ни на диск: это перечень для человека.
"""

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

from finlib.cli import _load_issuers
from finlib.db import fetch_all
from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.normalize.lines import normalize_name

logger = logging.getLogger(__name__)

_EXACT = """
SELECT inn, source_name, form_code, code
FROM ifrs_line_confirmation
WHERE relation = 'exact' AND source_name <> ''
"""


def main(argv: list[str] | None = None) -> int:
    """Печатает перечень наименований; ноль — прогон состоялся."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=Path("data/raw/ifrs"))
    parser.add_argument(
        "--grouping",
        action="append",
        default=[],
        metavar="ИНН=конвенция",
        help="Разделитель разрядов вручную: ИНН=russian|english|plain",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    manual: dict[str, str] = {}
    for item in args.grouping:
        inn, _, convention = item.partition("=")
        manual[inn.strip()] = convention.strip()

    issuers, _ = _load_issuers(args.path, manual)
    if not issuers:
        # Пустой каталог не даёт нулевого перечня: он говорит, что перечень
        # не составлялся. Печатать ноль значило бы выдать отсутствие данных
        # за результат.
        print(f"в каталоге {args.path} нет документов, прошедших приём")
        return 1

    catalog = load_ifrs_lines()
    confirmed: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
    for row in fetch_all(_EXACT, {}):
        key = (normalize_name(row["source_name"]), row["form_code"])
        confirmed[key].add((row["code"], row["inn"]))

    # Наименование → где оно осталось неопознанным и каким кодом подтверждено.
    gap: dict[tuple[str, str], dict] = {}
    for issuer in issuers:
        for row in issuer.extraction.unrecognised:
            key = (normalize_name(row.source_name), row.form)
            if key not in confirmed:
                continue
            item = gap.setdefault(
                key, {"name": row.source_name, "codes": set(), "own": set(), "other": set()}
            )
            for code, inn in confirmed[key]:
                item["codes"].add(code)
                where = item["own"] if inn == issuer.inn else item["other"]
                where.add(issuer.inn)

    ready = {key: item for key, item in gap.items() if len(item["codes"]) == 1}
    ambiguous = {key: item for key, item in gap.items() if len(item["codes"]) > 1}
    cross = {key: item for key, item in ready.items() if item["other"]}

    print(
        f"наименований на подъём: {len(ready)}; из них не опознаны у чужого "
        f"эмитента {len(cross)}; неоднозначных, поднимать нельзя: {len(ambiguous)}"
    )

    print("\nПОДНИМАТЬ")
    by_code: dict[str, list[dict]] = defaultdict(list)
    for item in ready.values():
        by_code[next(iter(item["codes"]))].append(item)
    for code, items in sorted(by_code.items()):
        position = catalog.get(code)
        print(f"\n  {code} — {position.name if position else 'кода нет в ядре'}")
        for item in sorted(items, key=lambda value: value["name"]):
            mark = " (у чужого эмитента)" if item["other"] else ""
            print(f"      «{item['name']}»{mark}")

    if ambiguous:
        print("\nНЕ ПОДНИМАТЬ: наименование подтверждено разными кодами")
        for item in sorted(ambiguous.values(), key=lambda value: value["name"]):
            print(f"  «{item['name']}» → {', '.join(sorted(item['codes']))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
