"""Дата раскрытия ГИР БО в уже загруженные комплекты — из сохранённых ответов.

**Настоящая дата раскрытия старше смоделированной сроком закона.** ГИР БО
её сообщает (`publishedCorrectionDate`: отчётность за 2025 год у пробы
опубликована 27.03.2026 при сроке 31 марта), а комплекты, загруженные
до того, как разбор начал её брать, стоят без неё. Ответы источника лежат
на диске в исходном виде — правило проекта, — и дата берётся оттуда,
а не выдумывается.

**К источнику скрипт не обращается вовсе.** Он читает сохранённое: ГИР БО
не отвечает с 23.09.2026, и ждать его, чтобы проставить дату, которая
уже есть на диске, незачем.

    uv run python scripts/girbo_disclosed_backfill.py          # только показать
    uv run python scripts/girbo_disclosed_backfill.py --write  # записать
"""

import json
import logging
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

from finlib.db import connection, execute, fetch_all
from finlib.scoring.routing import load_routing
from finlib.sources.girbo import parse_report_sets
from finlib.standards import Standard

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
# Каталоги сохранённых ответов: рабочий кэш и пробы, положенные в репозиторий
# как фикстуры. Оба — исходный вид ответа источника.
CACHES = (ROOT / "data" / "raw" / "girbo", ROOT / "data" / "raw" / "probe")

_LOADED = """
SELECT id, inn, report_year, correction_version, meta
FROM src_file WHERE source = 'gir_bo'
ORDER BY inn, report_year, correction_version
"""

_WRITE = """
UPDATE src_file
SET meta = coalesce(meta, '{}'::jsonb) || jsonb_build_object('disclosed_on', %(on)s)
WHERE id = %(id)s
"""


def disclosed() -> dict[tuple[str, int, int], str]:
    """Даты раскрытия из сохранённых ответов: (ИНН, год, корректировка) → дата."""
    found: dict[tuple[str, int, int], str] = {}
    for folder in CACHES:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if "bfo" not in path.name or path.name.endswith(".meta.json"):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                continue
            # ИНН в ответе не лежит — он в имени файла, которым ответ
            # и сохранён. Разбирать имя иначе нечем, и это единственное
            # место, где оно что-то значит.
            digits = "".join(ch for ch in path.stem if ch.isdigit())
            inn = digits[-12:] if len(digits) >= 10 else ""
            if not inn:
                continue
            try:
                sets = parse_report_sets(payload, inn)
            except Exception as failure:  # noqa: BLE001
                logger.warning("%s: разобрать не удалось — %s", path.name, failure)
                continue
            for item in sets:
                if item.disclosed_on is None:
                    continue
                key = (inn, item.report_year, item.correction_version)
                found[key] = f"{item.disclosed_on:%Y-%m-%d}"
    return found


def main() -> int:
    """Проставляет дату раскрытия там, где она есть в сохранённом ответе."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    write = "--write" in sys.argv
    known = disclosed()
    # Срок закона, которым дата моделируется там, где источник о ней молчит:
    # тот же, что в пересчёте истории, и берётся он из методики, а не заводится
    # здесь вторым числом.
    rule = load_routing().history.known_from.days(Standard.RSBU, interim=False)
    print(
        f"Дат раскрытия в сохранённых ответах: {len(known)}. "
        f"Срок закона для годовой РСБУ — {rule} дней.\n"
    )
    filled = missing = already = 0
    drift: list[int] = []
    with connection() as conn:
        rows = fetch_all(_LOADED, {}, conn=conn)
        for row in rows:
            if (row["meta"] or {}).get("disclosed_on"):
                already += 1
                continue
            key = (row["inn"], row["report_year"], row["correction_version"])
            when = known.get(key)
            if when is None:
                missing += 1
                continue
            filled += 1
            # **Насколько настоящая дата расходится со сроком** — это и есть
            # ответ на вопрос, стоит ли её брать. У пробы 7736050003
            # отчётность за 2021 год раскрыта 19.10.2023: модель по сроку
            # объявила бы её известной за два с половиной года до того, как
            # она появилась.
            модель = date(row["report_year"], 12, 31) + timedelta(days=rule)
            факт = date.fromisoformat(when)
            drift.append((факт - модель).days)
            print(
                f"  {row['inn']} за {row['report_year']} "
                f"(корр. {row['correction_version']}): раскрыта {when}, "
                f"срок {модель:%Y-%m-%d}, расхождение {(факт - модель).days:+d} дн."
            )
            if write:
                execute(_WRITE, {"id": row["id"], "on": when}, conn=conn)
    print(
        f"\nКомплектов ГИР БО {len(rows)}: дата уже стояла у {already}, "
        f"проставлена у {filled}, нет в сохранённом ответе у {missing}."
    )
    if drift:
        print(
            f"\n**Расхождение со сроком**: медиана {statistics.median(drift):+.0f} "
            f"дн., от {min(drift):+d} до {max(drift):+d}. Срок — не наблюдение: "
            "отчётность раскрывают и раньше него, и много позже."
        )
    if not write:
        print("\n**Без `--write` ничего не записано.**")
    # Знаменатель важнее самого числа: комплектов ГИР БО одиннадцать из почти
    # пяти тысяч, и даже полностью проставленная дата остаётся исключением.
    return 0


if __name__ == "__main__":
    sys.exit(main())
