"""Статистика обращений к модели по журналу llm_log.

Нужна для решения по модели: сколько попыток уходит на принятый ответ и на
чём именно проверка спотыкается. Тестовые записи в выборку не входят —
считается только то, что происходило в рабочих прогонах.

    make llm-stats
"""

import sys
from collections import Counter

from finlib.db import fetch_all

# Боевые записи: тестовые в статистику не идут, иначе она описывает не работу
# системы, а состав тестового набора.
_REAL = "NOT is_test"

_TOTALS = f"""
SELECT count(*) AS calls,
       count(*) FILTER (WHERE verified) AS accepted,
       count(*) FILTER (WHERE NOT verified) AS rejected,
       count(DISTINCT inn) AS organizations,
       round(avg(duration_ms) / 1000.0, 1) AS avg_seconds
FROM llm_log WHERE {_REAL}
"""

# Попытка, на которой ответ приняли, по каждому завершённому прогону.
# Прогон опознаётся по организации и отчётной дате: внутри него попытки
# нумеруются с единицы.
_ACCEPTED = f"""
SELECT inn, report_date, attempt
FROM llm_log
WHERE {_REAL} AND verified
ORDER BY inn, report_date, id
"""

_REJECTIONS = f"""
SELECT foreign_numbers FROM llm_log
WHERE {_REAL} AND NOT verified AND foreign_numbers IS NOT NULL
"""

_BY_ORG = f"""
SELECT l.inn, coalesce(o.short_name, '—') AS name,
       count(*) AS calls,
       count(*) FILTER (WHERE l.verified) AS accepted,
       max(l.attempt) AS max_attempt
FROM llm_log l LEFT JOIN organization o ON o.inn = l.inn
WHERE {_REAL}
GROUP BY l.inn, o.short_name
ORDER BY l.inn
"""

# Соответствие раздела журнала виду нарушения.
SECTIONS: dict[str, str] = {
    "numbers": "числа",
    "wordings": "формулировки о нормативах",
    "claims": "ложный нерасчёт",
    "verdicts": "расхождение с оценкой",
}


def collect() -> dict:
    """Собирает сводку по журналу."""
    totals = fetch_all(_TOTALS)[0]
    accepted = fetch_all(_ACCEPTED)
    attempts = [row["attempt"] for row in accepted]

    violations: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    for row in fetch_all(_REJECTIONS):
        payload = row["foreign_numbers"]
        if not isinstance(payload, dict):
            continue  # записи старого формата: список чисел без разделов
        for section, items in payload.items():
            if not items:
                continue
            sections[SECTIONS.get(section, section)] += len(items)
            for item in items:
                kind = item.get("violation") or SECTIONS.get(section, section)
                violations[kind] += len(item) and 1
    return {
        "totals": totals,
        "attempts": attempts,
        "violations": violations,
        "sections": sections,
        "by_org": fetch_all(_BY_ORG),
    }


def render(summary: dict) -> str:
    """Сводка в виде текста."""
    totals = summary["totals"]
    attempts = summary["attempts"]
    lines = [
        "=== Обращения к модели (тестовые записи исключены) ===",
        f"  обращений:            {totals['calls']}",
        f"  принято:              {totals['accepted']}",
        f"  отклонено:            {totals['rejected']}",
        f"  организаций:          {totals['organizations']}",
        f"  среднее время ответа: {totals['avg_seconds']} с",
        "",
    ]
    if attempts:
        average = sum(attempts) / len(attempts)
        lines.extend(
            [
                "=== Попыток до принятия ===",
                f"  принятых заключений: {len(attempts)}",
                f"  в среднем попыток:   {average:.2f}",
                f"  с первой попытки:    {attempts.count(1)}",
                f"  максимум:            {max(attempts)}",
                "",
            ]
        )
    else:
        lines.extend(["=== Попыток до принятия ===", "  принятых заключений нет", ""])

    if summary["sections"]:
        lines.append("=== Замечания по видам ===")
        for name, count in summary["sections"].most_common():
            lines.append(f"  {count:>4}  {name}")
        lines.append("")
    if summary["violations"]:
        lines.append("=== Нарушения по кодам ===")
        for name, count in summary["violations"].most_common():
            lines.append(f"  {count:>4}  {name}")
        lines.append("")

    lines.append("=== По организациям ===")
    for row in summary["by_org"]:
        lines.append(
            f"  {row['inn']}  {row['name']:<22} обращений {row['calls']:>3}, "
            f"принято {row['accepted']:>2}, попыток до {row['max_attempt']}"
        )
    return "\n".join(lines)


def main() -> int:
    """Точка входа."""
    print(render(collect()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
