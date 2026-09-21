"""Опись доступа к методам Cbonds: что открыто, что закрыто (замер 21.09.2026).

**Зачем.** Спецификация объявляет 211 методов в десяти разделах, а подписка
открывает часть; догадываться по названию нельзя — `get_emitents` открыт,
а `get_emission_guarantors` закрыт, и предположить это было неоткуда.

Проба сделана по одному запросу на метод с пустым отбором, ответы разобраны
на исходы. **Ошибка запроса и отказ доступа — разные вещи**, и они разведены:
отказ говорит `invalid resource name` либо `invalid operation name`, ошибка
запроса — что-то иное. На этом замере ошибок запроса не случилось ни одной:
пустой отбор приняли все открытые методы.

    uv run python eval/cbonds_access.py > data/output/cbonds_access.md

В сеть не ходит: читает сохранённый ответ пробы `data/raw/cbonds/
access_probe.json`. Сама проба стоила 211 запросов — 2,1 % суточной нормы
(10 000), которую источник объявляет в `meta` каждого ответа.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROBE = Path("data/raw/cbonds/access_probe.json")
REFUSED = "отказ доступа"


def main() -> int:
    """Печатает опись; 1 — если сохранённой пробы на диске нет."""
    if not PROBE.exists():
        print(f"нет {PROBE}: проба доступа не сохранена, измерения не было")
        return 1
    found = json.loads(PROBE.read_text(encoding="utf-8"))
    outcomes = Counter(item["outcome"] for item in found.values())
    open_methods = sum(count for name, count in outcomes.items() if name != REFUSED)

    print("# Cbonds: опись доступа к методам спецификации\n")
    print(
        f"Проверено методов: **{len(found)}**. Открыто **{open_methods}**, "
        f"отказано в доступе **{outcomes[REFUSED]}**.\n"
    )
    print("| Исход | Методов |")
    print("|---|---|")
    for name, count in outcomes.most_common():
        print(f"| {name} | {count} |")

    by_section: dict[str, Counter] = defaultdict(Counter)
    for item in found.values():
        by_section[item["section"]][item["outcome"]] += 1
    print("\n## По разделам\n")
    print("| Раздел | Открыто | Отказ | Всего |")
    print("|---|---|---|---|")
    for section in sorted(by_section, key=lambda name: -sum(by_section[name].values())):
        counts = by_section[section]
        total = sum(counts.values())
        print(f"| {section} | {total - counts[REFUSED]} | {counts[REFUSED]} | {total} |")

    print("\n## Открытые методы\n")
    for section in sorted(by_section):
        rows = sorted(
            (name, item)
            for name, item in found.items()
            if item["section"] == section and item["outcome"] != REFUSED
        )
        if not rows:
            continue
        print(f"### {section} — {len(rows)}\n")
        for name, item in rows:
            print(f"- `{name}` — {item['summary']}")
        print()

    print("## Отказано в доступе — перечень для запроса о расширении\n")
    for section in sorted(by_section, key=lambda name: -by_section[name][REFUSED]):
        rows = sorted(
            (name, item)
            for name, item in found.items()
            if item["section"] == section and item["outcome"] == REFUSED
        )
        if not rows:
            continue
        print(f"### {section} — {len(rows)}\n")
        for name, item in rows:
            print(f"- `{name}` — {item['summary']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
