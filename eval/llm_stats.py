"""Статистика обращений к модели по журналу llm_log.

Нужна для решения по модели: сколько попыток уходит на принятый ответ и на
чём именно проверка спотыкается. Тестовые записи в выборку не входят —
считается только то, что происходило в рабочих прогонах.

Записи прежних версий кода тоже не входят. Правка инструкции, постпроверки
или состава блоков меняет поведение текстового слоя целиком, и среднее число
попыток по смеси версий описывает историю разработки, а не систему. Сколько
записей отброшено и каких версий — печатается: молча сузить выборку нельзя,
иначе сводка выглядит полной, не будучи ею.

    make llm-stats
    uv run python eval/llm_stats.py --all-versions
"""

import argparse
import sys
from collections import Counter

from finlib.db import fetch_all
from finlib.version import code_version

# Боевые записи: тестовые в статистику не идут, иначе она описывает не работу
# системы, а состав тестового набора.
_REAL = "NOT is_test"

# Записи текущей версии кода. NULL версии не равен ничему, в том числе самому
# себе: прогоны до появления колонки в выборку не попадают, и это верно —
# их происхождение неизвестно.
_CURRENT = "code_version = %(version)s"

_TOTALS = """
SELECT count(*) AS calls,
       count(*) FILTER (WHERE verified) AS accepted,
       count(*) FILTER (WHERE NOT verified) AS rejected,
       count(DISTINCT inn) AS organizations,
       round(avg(duration_ms) / 1000.0, 1) AS avg_seconds
FROM llm_log WHERE {scope}
"""

# Попытка, на которой ответ приняли, по каждому завершённому прогону.
# Прогон опознаётся по организации и отчётной дате: внутри него попытки
# нумеруются с единицы.
_ACCEPTED = """
SELECT inn, report_date, attempt
FROM llm_log
WHERE {scope} AND verified
ORDER BY inn, report_date, id
"""

_REJECTIONS = """
SELECT foreign_numbers FROM llm_log
WHERE {scope} AND NOT verified AND foreign_numbers IS NOT NULL
"""

_BY_ORG = """
SELECT l.inn, coalesce(o.short_name, '—') AS name,
       count(*) AS calls,
       count(*) FILTER (WHERE l.verified) AS accepted,
       max(l.attempt) AS max_attempt
FROM llm_log l LEFT JOIN organization o ON o.inn = l.inn
WHERE {scope}
GROUP BY l.inn, o.short_name
ORDER BY l.inn
"""

# Что осталось за выборкой: прогоны прежних версий кода и те, что сделаны
# до появления самой колонки.
_OTHER_VERSIONS = """
SELECT coalesce(code_version, '—') AS version, count(*) AS calls,
       max(created_at)::date AS last_call
FROM llm_log
WHERE {real} AND code_version IS DISTINCT FROM %(version)s
GROUP BY code_version
ORDER BY last_call DESC, version
""".replace("{real}", _REAL)

# Соответствие раздела журнала виду нарушения.
SECTIONS: dict[str, str] = {
    "numbers": "числа",
    "wordings": "формулировки о нормативах",
    "claims": "ложный нерасчёт",
    "verdicts": "расхождение с оценкой",
    "statements": "утверждения текста",
}


def collect(*, all_versions: bool = False, conn=None) -> dict:
    """Собирает сводку по журналу за текущую версию кода.

    all_versions снимает отбор по версии: нужно, когда разбирают историю
    прогонов целиком, а не поведение нынешнего кода. conn задаёт соединение —
    им пользуется тест, чтобы считать отбор в своей транзакции, не касаясь
    боевых записей журнала.
    """
    version = code_version()
    params = {"version": version}
    scope = _REAL if all_versions else f"{_REAL} AND {_CURRENT}"

    totals = fetch_all(_TOTALS.format(scope=scope), params, conn=conn)[0]
    accepted = fetch_all(_ACCEPTED.format(scope=scope), params, conn=conn)
    attempts = [row["attempt"] for row in accepted]

    violations: Counter[str] = Counter()
    sections: Counter[str] = Counter()
    for row in fetch_all(_REJECTIONS.format(scope=scope), params, conn=conn):
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
        "by_org": fetch_all(_BY_ORG.format(scope=scope), params, conn=conn),
        "version": version,
        "all_versions": all_versions,
        "other_versions": []
        if all_versions
        else fetch_all(_OTHER_VERSIONS, params, conn=conn),
    }


def render(summary: dict) -> str:
    """Сводка в виде текста."""
    totals = summary["totals"]
    attempts = summary["attempts"]
    lines = [
        "=== Версия кода ===",
        f"  текущая:              {summary['version']}",
    ]
    if summary["all_versions"]:
        lines.append("  выборка:              все версии, отбор снят")
    else:
        lines.append("  выборка:              только текущая версия")
        discarded = sum(row["calls"] for row in summary["other_versions"])
        lines.append(f"  отброшено записей:    {discarded}")
        for row in summary["other_versions"]:
            lines.append(
                f"    {row['version']:<16} {row['calls']:>4} "
                f"(последний прогон {row['last_call']})"
            )
    lines.extend(
        [
            "",
            "=== Обращения к модели (тестовые записи исключены) ===",
            f"  обращений:            {totals['calls']}",
            f"  принято:              {totals['accepted']}",
            f"  отклонено:            {totals['rejected']}",
            f"  организаций:          {totals['organizations']}",
            f"  среднее время ответа: {totals['avg_seconds'] or '—'} с",
            "",
        ]
    )
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


def main(argv: list[str] | None = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-versions",
        action="store_true",
        help="считать записи всех версий кода, а не только текущей",
    )
    args = parser.parse_args(argv)
    print(render(collect(all_versions=args.all_versions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
