"""Сравнение двух схем текстовой части на одной подвыборке (задача 18).

Замеры двух схем идут в один журнал `llm_log` и различаются именем промпта,
а не временем: разнесённые по времени прогоны сравнивать хуже — между ними
может измениться код. Поэтому оба прогона делаются подряд, а этот отчёт
собирает их вместе.

**Отчёт обязан содержать `not_in_blocks` по обеим схемам.** Само по себе
обнуление `wrong_anchor` успехом не является: если одновременно вырос
`not_in_blocks`, значит, числа пошли мимо тезисов — модель пишет свои
вместо переданных, — и разворот не удался. Порознь эти две графы читаются
иначе, чем вместе, поэтому печатаются обе и всегда.

**Тексты обеих схем по одной организации приводятся рядом.** Метрики
читаемости не покажут, а решение о переходе принимается и по ней тоже.

    uv run python eval/scheme_compare.py \\
        --free data/output/regression/..._full_free.json \\
        --theses data/output/regression/..._full_theses.json
"""

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from finlib.config import settings
from finlib.db import fetch_all
from finlib.llm.cleanup import strip_identifiers
from finlib.llm.service import PromptScheme
from finlib.llm.verify import Violation, strip_reasoning

logger = logging.getLogger(__name__)

SCHEME_NAMES: dict[PromptScheme, str] = {
    PromptScheme.FREE: "свободная генерация",
    PromptScheme.THESES: "сборка из предписанных тезисов",
}

_FUNNEL = """
SELECT count(*) AS calls,
       count(*) FILTER (WHERE verified) AS accepted,
       count(*) FILTER (WHERE attempt = 1) AS runs,
       count(*) FILTER (WHERE verified AND attempt = 1) AS first_try,
       avg(duration_ms) AS avg_ms,
       sum(duration_ms) AS total_ms,
       count(DISTINCT inn) AS organizations
FROM llm_log
WHERE NOT is_test AND prompt_name = %(scheme)s AND created_at >= %(since)s
  AND inn = ANY(%(inns)s)
"""

_REJECTED = """
SELECT foreign_numbers FROM llm_log
WHERE NOT is_test AND NOT verified AND prompt_name = %(scheme)s
  AND created_at >= %(since)s AND inn = ANY(%(inns)s)
  AND foreign_numbers IS NOT NULL
"""

_BY_ORG = """
SELECT inn,
       count(*) AS calls,
       max(attempt) AS attempts,
       bool_or(verified) AS accepted
FROM llm_log
WHERE NOT is_test AND prompt_name = %(scheme)s AND created_at >= %(since)s
  AND inn = ANY(%(inns)s)
GROUP BY inn ORDER BY inn
"""

# Принятый ответ организации. Берётся последний: попытка, после которой
# постпроверка пропустила текст, — и есть то, что увидел бы пользователь.
_ACCEPTED_TEXT = """
SELECT response_text, attempt, duration_ms FROM llm_log
WHERE NOT is_test AND verified AND prompt_name = %(scheme)s
  AND created_at >= %(since)s AND inn = %(inn)s
ORDER BY id DESC LIMIT 1
"""

_LAST_TEXT = """
SELECT response_text, attempt, duration_ms FROM llm_log
WHERE NOT is_test AND prompt_name = %(scheme)s
  AND created_at >= %(since)s AND inn = %(inn)s
ORDER BY id DESC LIMIT 1
"""

_ORG_NAME = (
    "SELECT coalesce(short_name, name, inn) AS name FROM organization WHERE inn = %(inn)s"
)


@dataclass(frozen=True, slots=True)
class Measurement:
    """Один замер: схема, когда начат, по каким организациям, что вышло."""

    scheme: PromptScheme
    started: datetime
    inns: tuple[str, ...]
    parameters: dict
    metrics: dict
    organizations: tuple[dict, ...]


def read_report(path: Path) -> Measurement:
    """Читает машиночитаемый отчёт прогона."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    declared = payload["parameters"].get("схема текстовой части", "")
    scheme = (
        PromptScheme.THESES if declared.startswith("theses") else PromptScheme.FREE
    )
    return Measurement(
        scheme=scheme,
        started=datetime.fromisoformat(payload["started"]),
        inns=tuple(item["inn"] for item in payload["organizations"]),
        parameters=payload["parameters"],
        metrics=payload.get("metrics", {}),
        organizations=tuple(payload["organizations"]),
    )


def funnel(measurement: Measurement) -> dict:
    """Воронка обращений к модели: сколько сделано, сколько принято."""
    params = {
        "scheme": measurement.scheme.value,
        "since": measurement.started,
        "inns": list(measurement.inns),
    }
    rows = fetch_all(_FUNNEL, params)
    row = rows[0] if rows else {}
    calls = int(row.get("calls") or 0)
    accepted = int(row.get("accepted") or 0)
    runs = int(row.get("runs") or 0)
    total_ms = int(row.get("total_ms") or 0)
    return {
        "обращений к модели": calls,
        "принято постпроверкой": accepted,
        "отклонено": calls - accepted,
        "прогонов": runs,
        "прогонов без документа": runs - accepted,
        "принято с первой попытки": int(row.get("first_try") or 0),
        "среднее время ответа, с": round(float(row.get("avg_ms") or 0) / 1000, 1),
        "машинного времени всего, мин": round(total_ms / 60000, 1),
    }


def violations(measurement: Measurement) -> dict[str, int]:
    """Замечания постпроверки по видам, включая нулевые.

    Нулевая графа — не то же, что отсутствующая: отсутствие читается как
    «не измерялось», а сравнение схем держится ровно на этих числах.
    """
    found: dict[str, int] = {item.value: 0 for item in Violation}
    params = {
        "scheme": measurement.scheme.value,
        "since": measurement.started,
        "inns": list(measurement.inns),
    }
    for row in fetch_all(_REJECTED, params):
        payload = row["foreign_numbers"]
        if not isinstance(payload, dict):
            continue
        for section, items in payload.items():
            for item in items or ():
                kind = item.get("violation") or item.get("rule") or section
                found[kind] = found.get(kind, 0) + 1
    return found


def by_organization(measurement: Measurement) -> dict[str, dict]:
    """Итог каждой организации в этом замере."""
    params = {
        "scheme": measurement.scheme.value,
        "since": measurement.started,
        "inns": list(measurement.inns),
    }
    return {row["inn"]: dict(row) for row in fetch_all(_BY_ORG, params)}


def text_of(measurement: Measurement, inn: str) -> tuple[str, bool]:
    """Текст ответа по организации и признак того, что он принят.

    Отклонённый ответ приводится тоже: сравнение читаемости иначе опиралось
    бы только на схему, которой повезло, а отклонённый текст показывает,
    чем именно схема не справилась.
    """
    params = {
        "scheme": measurement.scheme.value,
        "since": measurement.started,
        "inn": inn,
    }
    rows = fetch_all(_ACCEPTED_TEXT, params)
    accepted = bool(rows)
    if not rows:
        rows = fetch_all(_LAST_TEXT, params)
    if not rows or not rows[0]["response_text"]:
        return "", False
    # Разметка снимается так же, как перед сборкой документа: сравнивать
    # надо то, что увидел бы читатель, а не размеченный текст.
    return strip_identifiers(strip_reasoning(rows[0]["response_text"])), accepted


def _name_of(inn: str) -> str:
    """Наименование организации по базе."""
    rows = fetch_all(_ORG_NAME, {"inn": inn})
    return rows[0]["name"] if rows else inn


def pick_organization(
    free: Measurement, theses: Measurement, requested: str | None
) -> str | None:
    """Организация, тексты по которой приводятся рядом.

    По умолчанию берётся та, по которой обе схемы дали принятый ответ:
    сравнивать принятый текст с отклонённым — сравнивать разные вещи.
    Если такой нет, берётся первая общая, и это оговаривается.
    """
    if requested:
        return requested
    left, right = by_organization(free), by_organization(theses)
    common = [item for item in free.inns if item in right]
    both = [
        item
        for item in common
        if left.get(item, {}).get("accepted") and right.get(item, {}).get("accepted")
    ]
    return (both or common or [None])[0]


def render(free: Measurement, theses: Measurement, text_inn: str | None) -> str:
    """Отчёт сравнения двух схем."""
    lines = [
        "# Сравнение схем текстовой части",
        "",
        "Замеры сделаны подряд, одной версией кода и одной моделью: "
        "разнесённые по времени прогоны сравнивать хуже — между ними может "
        "измениться код.",
        "",
        "## Параметры замеров",
        "",
        "| Параметр | free | theses |",
        "|---|---|---|",
    ]
    keys = list(dict.fromkeys(list(free.parameters) + list(theses.parameters)))
    for key in keys:
        left, right = free.parameters.get(key), theses.parameters.get(key)
        if isinstance(left, dict) or isinstance(right, dict):
            left = ", ".join(f"{k} {v}" for k, v in (left or {}).items())
            right = ", ".join(f"{k} {v}" for k, v in (right or {}).items())
        mark = "" if left == right else " ⚠"
        lines.append(f"| {key}{mark} | {left} | {right} |")

    lines += ["", "## Воронка", "", "| Величина | free | theses |", "|---|---|---|"]
    left_funnel, right_funnel = funnel(free), funnel(theses)
    for key in left_funnel:
        lines.append(f"| {key} | {left_funnel[key]} | {right_funnel[key]} |")

    lines += [
        "",
        "## Замечания постпроверки",
        "",
        "`not_in_blocks` приводится по обеим схемам обязательно: обнуление "
        "`wrong_anchor` при его росте означает, что числа пошли мимо тезисов, "
        "и разворот не удался.",
        "",
        "| Замечание | free | theses |",
        "|---|---|---|",
    ]
    left_violations, right_violations = violations(free), violations(theses)
    for key in dict.fromkeys(list(left_violations) + list(right_violations)):
        lines.append(
            f"| `{key}` | {left_violations.get(key, 0)} | {right_violations.get(key, 0)} |"
        )

    lines += [
        "",
        "## По организациям",
        "",
        "| ИНН | Организация | free: попыток | free: принят | "
        "theses: попыток | theses: принят |",
        "|---|---|---|---|---|---|",
    ]
    left_orgs, right_orgs = by_organization(free), by_organization(theses)
    for inn in free.inns:
        left = left_orgs.get(inn, {})
        right = right_orgs.get(inn, {})
        lines.append(
            f"| {inn} | {_name_of(inn)} | {left.get('attempts', '—')} | "
            f"{'да' if left.get('accepted') else 'нет'} | "
            f"{right.get('attempts', '—')} | "
            f"{'да' if right.get('accepted') else 'нет'} |"
        )

    if text_inn:
        lines += _texts(free, theses, text_inn)
    return "\n".join(lines)


def _texts(free: Measurement, theses: Measurement, inn: str) -> list[str]:
    """Тексты обеих схем по одной организации, одну за другой."""
    name = _name_of(inn)
    lines = [
        "",
        f"## Тексты по одной организации: {name} ({inn})",
        "",
        "Метрики читаемости не показывают, и решение о переходе принимается "
        "в том числе по этим двум текстам.",
    ]
    for measurement in (free, theses):
        text, accepted = text_of(measurement, inn)
        mark = "принят постпроверкой" if accepted else "отклонён постпроверкой"
        lines += [
            "",
            f"### Схема {measurement.scheme.name.lower()}: "
            f"{SCHEME_NAMES[measurement.scheme]} — {mark}",
            "",
            text or "_ответа в журнале нет_",
        ]
    return lines


def output_path(started: datetime) -> Path:
    """Куда ложится отчёт сравнения."""
    target = settings.output_dir / "regression"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"{started:%Y-%m-%d_%H%M}_scheme_compare.md"


def main(argv: list[str] | None = None) -> int:
    """Точка входа."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--free", required=True, type=Path, help="отчёт прогона free")
    parser.add_argument(
        "--theses", required=True, type=Path, help="отчёт прогона theses"
    )
    parser.add_argument(
        "--text-inn", help="организация, тексты по которой приводятся рядом"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    first, second = read_report(args.free), read_report(args.theses)
    if first.scheme is second.scheme:
        parser.error("оба отчёта сделаны по одной схеме: сравнивать нечего")
    # Схема берётся из самого отчёта, а не из имени флага: перепутанные
    # местами файлы дали бы отчёт, в котором графы подписаны наоборот.
    free = first if first.scheme is PromptScheme.FREE else second
    theses = second if first.scheme is PromptScheme.FREE else first
    text_inn = pick_organization(free, theses, args.text_inn)
    rendered = render(free, theses, text_inn)
    path = output_path(theses.started)
    path.write_text(rendered, encoding="utf-8")
    print(rendered)
    print(f"\nОтчёт сравнения: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
