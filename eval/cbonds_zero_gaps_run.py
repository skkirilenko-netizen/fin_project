"""Перечень к агрегатору: величина есть у него, а у первоисточника прочерк.

    uv run python eval/cbonds_zero_gaps_run.py > data/output/cbonds_gaps.md

**Это разговор с источником, а не вопрос к организации.** Проверено
по исходным выгрузкам ГИР БО: в клетках стоит прочерк, и разбор прочитал его
верно. Значит, расходится с отчётностью агрегатор, а спрашивать организацию
об ошибке третьей стороны незачем — тем более что нераскрытие в полной форме
правомерно само по себе. Перечень собирается для Cbonds: организация, строка,
период, величина.

**Перечень берётся из журнала, а не считается заново.** Записи оставил
загрузчик в момент загрузки (`cbonds_value_for_undisclosed`), и предмет
с величиной лежат в полях записи, а не в прозе: второй разбор той же прозы
разошёлся бы с первым.

Считаются записи **версии кода того комплекта**, к которому относятся: запись
прежнего разбора о нынешней доставке не говорит, а число прежних записей
называется отдельно — молчание о них читалось бы как «журнал чист».
"""

import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.normalize.lines import ReportingType, load_lines  # noqa: E402

logger = logging.getLogger(__name__)

_GAPS = """
SELECT d.inn, coalesce(o.name, d.inn) AS name, s.standard, s.report_year,
       d.report_date, d.line_code, d.new_value, s.unit_code
FROM dq_log d
JOIN src_file s ON s.id = d.src_file_id
LEFT JOIN organization o ON o.inn = d.inn
WHERE d.check_code = 'cbonds_value_for_undisclosed'
  AND d.code_version IS NOT DISTINCT FROM s.code_version
ORDER BY o.name, d.report_date, d.line_code
"""

_OLD = """
SELECT count(*) AS records
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.check_code = 'cbonds_value_for_undisclosed'
  AND d.code_version IS DISTINCT FROM s.code_version
"""

# Нули агрегатора там же: их не столько предъявляют источнику, сколько
# считают — по ним видно, насколько часто ноль у него стоит вместо прочерка.
_ZEROS = """
SELECT count(*) AS records, count(DISTINCT d.inn) AS issuers
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.check_code = 'cbonds_zero_for_undisclosed'
  AND d.code_version IS NOT DISTINCT FROM s.code_version
"""


def main() -> int:
    """Печатает перечень; 1 — если записей нет вовсе."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    catalog = load_lines()
    units = catalog.units
    with connection() as conn:
        rows = fetch_all(_GAPS, {}, conn=conn)
        old = fetch_all(_OLD, {}, conn=conn)[0]
        zeros = fetch_all(_ZEROS, {}, conn=conn)[0]

    print("# Агрегатор даёт величину там, где первоисточник поставил прочерк\n")
    if not rows:
        print(
            "**Записей нет.** Это не «расхождений не найдено»: записи оставляет "
            "загрузчик доставки, и их отсутствие означает, что доставка "
            "не выполнялась либо выполнялась прежней версией разбора."
        )
        return 1

    print(
        "Проверено по исходным выгрузкам ГИР БО: в этих клетках стоит прочерк, "
        "и разбор прочитал его верно. Перечень предъявляется источнику — "
        "к организации вопроса нет, нераскрытие в полной форме правомерно.\n"
    )
    print(
        f"Записей: **{len(rows)}** у **{len({row['inn'] for row in rows})}** "
        f"организаций. Отдельно: нулей агрегатора против прочерка — "
        f"**{zeros['records']}** у {zeros['issuers']} организаций; такой ноль "
        "не записывается вовсе, потому что у источника он означает и "
        "нераскрытие.\n"
    )

    print("| Организация | ИНН | Период | Строка | Величина агрегатора | Единица |")
    print("|---|---|---|---|---|---|")
    by_line: Counter[str] = Counter()
    for row in rows:
        line = catalog.get(row["line_code"] or "", ReportingType.FULL)
        name = f"{row['line_code']} «{line.name}»" if line else str(row["line_code"])
        by_line[name] += 1
        unit = units.name_of(row["unit_code"]) if row["unit_code"] else "не объявлена"
        print(
            f"| {row['name'][:34]} | {row['inn']} | {row['report_date']} | {name} "
            f"| {row['new_value']} | {unit} |"
        )

    print("\n## По строкам\n")
    print("| Строка | Случаев |")
    print("|---|---|")
    for name, count in by_line.most_common():
        print(f"| {name} | {count} |")

    if old["records"]:
        print(
            f"\nЗаписей прежних версий разбора: {old['records']}. В перечень "
            "они не идут — о нынешней доставке они не говорят, — но и молчать "
            "о них нельзя: молчание читалось бы как «журнал чист»."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
