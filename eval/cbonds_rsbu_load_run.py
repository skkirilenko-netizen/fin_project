"""Загрузка отчётности РСБУ из Cbonds: прогон и счётчики.

Отвечает на вопрос, для которого сверка была только подготовкой: **что даёт
доставка агрегатора там, где ГИР БО недоступен**. Сверка сравнивала величины
и в базу не писала; здесь комплекты записываются боевым путём
(`pipeline.accept_cbonds_report`), со всеми проверками и контролями.

    uv run python eval/cbonds_rsbu_load_run.py > data/output/cbonds_rsbu_load.md

**По умолчанию прогон не ходит в сеть.** Ответ, которого нет на диске,
не запрашивается, а организация называется пропущенной: обращение к источнику
тратит суточную норму запросов, и решать об этом должен человек. `--fetch`
включает запросы, `--inn` сужает перечень.

**Замер не считает сам.** Комплекты пишет цикл, проверки выполняют контроли,
а прогон считает исходы: комплектов, фактов, кодов вне справочника, расхождений
с первоисточником, соглашений о знаке, нераскрытых строк против величин
агрегатора и карантинов.
"""

import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.config import settings  # noqa: E402
from finlib.db import connection, fetch_all  # noqa: E402
from finlib.normalize.cbonds_mapping import load_cbonds_mapping  # noqa: E402
from finlib.pipeline import accept_cbonds_report  # noqa: E402
from finlib.quality.codes import check_name  # noqa: E402
from finlib.sources.cbonds import CACHE, CbondsUnavailableError  # noqa: E402
from finlib.version import code_version  # noqa: E402

logger = logging.getLogger(__name__)

REPORT = "report_rsbu"

# Организации, о которых есть смысл спрашивать: те, что уже заведены у нас.
# Перечень берётся из базы, а не из файла: файл устарел бы в день правки набора.
# **Графа называет то, что считает**: фактов РСБУ **первоисточника**, а не всех.
# Считая все, она включала бы величины самого агрегатора, и при повторном
# прогоне графа «у нас» росла бы от наших же загрузок.
_ORGANIZATIONS = """
SELECT o.inn, o.name,
       count(f.id) FILTER (
           WHERE f.standard = 'rsbu' AND f.recognition IS DISTINCT FROM 'cbonds'
       ) AS rsbu_facts
FROM organization o LEFT JOIN fact_report f ON f.inn = o.inn
GROUP BY o.inn, o.name
ORDER BY o.inn
"""


_SETS = """
SELECT count(*) FILTER (WHERE status = 'quarantine') AS quarantine,
       count(*) AS total
FROM src_file
WHERE standard = 'rsbu' AND source = 'cbonds' AND code_version = %(version)s
"""

_CHECK_OUTCOMES = """
SELECT d.check_code,
       count(*) FILTER (WHERE d.status = 'fail') AS failed,
       count(*) FILTER (WHERE d.status = 'pass') AS passed
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE s.standard = 'rsbu' AND s.source = 'cbonds'
  AND d.code_version = s.code_version
  AND d.check_code NOT LIKE 'cbonds%%'
GROUP BY d.check_code
"""


def cached(inn: str, report: object) -> bool:
    """Есть ли на диске ответ хотя бы одной доставки этой организации.

    Доставок три, и отчёт о движении денежных средств отбирается
    по идентификатору эмитента: его имя на диске заранее неизвестно. Поэтому
    достаточным считается ответ первой доставки — по нему идентификатор
    и находится.
    """
    first = report.deliveries[0]  # type: ignore[attr-defined]
    return (CACHE / f"{first.cache}_{inn}.json").exists()


def main() -> int:
    """Печатает итог загрузки; 1 — если не загружено ни одного комплекта."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    fetch = "--fetch" in sys.argv
    only = {
        item for index, item in enumerate(sys.argv) if index and item.isdigit()
    }
    report = load_cbonds_mapping().report(REPORT)

    counts: Counter[str] = Counter()
    rows_out: list[str] = []
    skipped: list[str] = []
    quarantined: list[str] = []
    with connection() as conn:
        organizations = fetch_all(_ORGANIZATIONS, {}, conn=conn)
        wanted = [
            item
            for item in organizations
            if not only or item["inn"] in only
        ]
        for item in wanted:
            inn = item["inn"]
            if not fetch and not cached(inn, report):
                skipped.append(f"{(item['name'] or inn)[:30]} ({inn})")
                continue
            try:
                outcomes = accept_cbonds_report(
                    inn, report=REPORT, conn=conn, refresh=False
                )
            except CbondsUnavailableError as failure:
                skipped.append(f"{(item['name'] or inn)[:30]} ({inn}): {failure}")
                continue
            accepted = [entry for entry in outcomes if entry.accepted]
            if not accepted:
                counts["без годовых строк"] += 1
                continue
            counts["организаций"] += 1
            counts["комплектов"] += len(accepted)
            facts = sum(entry.facts for entry in accepted)
            counts["фактов записано"] += facts
            counts["величин предъявлено"] += sum(
                entry.presented for entry in accepted
            )
            counts["вне справочника"] += sum(
                entry.outside_catalog for entry in accepted
            )
            counts["расхождений с первоисточником"] += sum(
                len(entry.mismatches) for entry in accepted
            )
            counts["соглашений о знаке"] += sum(
                len(entry.sign_conventions) for entry in accepted
            )
            # **Два исхода нераскрытой строки считаются порознь.** Ноль
            # агрегатора не пишется никогда, ненулевая величина решается
            # правилом приоритета — и одно число на оба случая скрывало бы,
            # чего именно мы не берём.
            counts["нераскрыто у нас, есть у агрегатора"] += sum(
                len(entry.undisclosed) for entry in accepted
            )
            counts["из них ноль не записан"] += sum(
                1
                for entry in accepted
                for _, note in entry.undisclosed
                if "ноль не записан" in note
            )
            counts["из них величина записана"] += sum(
                1
                for entry in accepted
                for _, note in entry.undisclosed
                if "величина записана" in note
            )
            counts["не сошлось сверок"] += sum(
                len(entry.failures) for entry in accepted
            )
            for entry in accepted:
                if entry.quarantined:
                    quarantined.append(
                        f"{(item['name'] or inn)[:30]} ({inn}) "
                        f"{entry.report_date}: "
                        + "; ".join(message for _, message in entry.failures)
                    )
            rows_out.append(
                f"| {(item['name'] or inn)[:30]} | {inn} | {len(accepted)} | "
                f"{facts} | {item['rsbu_facts']} | "
                f"{sum(len(entry.mismatches) for entry in accepted)} | "
                f"{sum(len(entry.undisclosed) for entry in accepted)} |"
            )

    with connection() as conn:
        # Итоговое состояние комплектов и исходы контролей качества: решение
        # о карантине принимают они, а не загрузчик, и спрашивать о нём надо
        # базу. Считаются записи версии кода этих комплектов — записи прежних
        # версий о нынешней загрузке не говорят.
        sets = fetch_all(_SETS, {"version": code_version()}, conn=conn)[0]
        outcomes_rows = fetch_all(_CHECK_OUTCOMES, {}, conn=conn)
    failures = {
        row["check_code"]: int(row["failed"]) for row in outcomes_rows if row["failed"]
    }
    passes = {row["check_code"]: int(row["passed"]) for row in outcomes_rows}

    print("# Загрузка отчётности РСБУ из Cbonds\n")
    if not counts["комплектов"]:
        print(
            "**Ни одного комплекта не загружено.** Это не нулевая доля, "
            "а отсутствие измерения: ответов на диске нет, а запросы "
            "не разрешены (`--fetch`).\n"
        )
        if skipped:
            print(f"Пропущено организаций: {len(skipped)}.")
        return 1

    print(
        f"Доставок у вида отчёта {len(report.deliveries)}: "
        + ", ".join(item.method for item in report.deliveries)
        + ". Комплект сводится по отчётной дате.\n"
    )
    print(
        f"Загружено организаций **{counts['организаций']}**, комплектов "
        f"**{counts['комплектов']}**, величин предъявлено "
        f"**{counts['величин предъявлено']}**, из них записано "
        f"**{counts['фактов записано']}** (остальные уже стоят в базе теми же "
        "числами либо уступили правилу приоритета). "
        f"Пропущено без ответа на диске **{len(skipped)}**"
        + (" (запросы разрешены)" if fetch else " (запросы не разрешены)")
        + f". Сеть: запросов {0 if not fetch else 'по необходимости'}, "
        f"логин задан: {'да' if settings.cbonds_ready else 'нет'}.\n"
    )

    print("| Организация | ИНН | Комплектов | Фактов | Фактов РСБУ у нас "
          "| Расхождений | Нераскрыто у нас |")
    print("|---|---|---|---|---|---|---|")
    for line in rows_out:
        print(line)

    print("\n## Счётчики\n")
    print("| Что | Сколько |")
    print("|---|---|")
    for name, count in counts.most_common():
        print(f"| {name} | {count} |")
    print(
        "\n**Коды вне справочника — решение, а не пробел.** Агрегатор "
        "раскрывает детализацию отчёта о движении денежных средств и справочный "
        "раздел отчёта о финансовых результатах; ни один показатель методики "
        "на них не опирается, и заводить их решено не сейчас. Число стоит "
        "рядом с записанными величинами, чтобы «неизвестных кодов нет» "
        "не подтверждалось молчанием.\n"
    )

    print("\n## Карантин\n")
    print(
        "**Проверки загрузчика и контроли качества — разные проверки, и графа "
        "обязана называть, о чьём решении речь.** Загрузчик проверяет три "
        "признака нуля; контроли РСБУ проверяют то же, что у выгрузки ГИР БО, "
        "и по комплекту агрегатора это независимая проверка его данных.\n"
    )
    print(
        f"- по проверкам нуля загрузчика: **{len(quarantined)}**\n"
        f"- по контролям качества (итоговое состояние комплекта): "
        f"**{sets['quarantine']}** из {sets['total']}"
    )
    for line in quarantined[:10]:
        print(f"  - {line}")
    if failures:
        print("\n| Контроль | Провалов | Пройдено |")
        print("|---|---|---|")
        for code, count in sorted(failures.items(), key=lambda item: -item[1]):
            print(f"| {check_name(code)} | {count} | {passes.get(code, 0)} |")
        print(
            "\nЭто не дефект доставки и не дефект контролей: контроли сходимости "
            "применены к данным агрегатора впервые, и провал означает, что "
            "у этих комплектов итог не сходится с составом. Комплект в карантине "
            "в расчёт не идёт.\n"
        )

    if skipped:
        print(f"\n## Пропущено без ответа на диске: {len(skipped)}\n")
        for line in skipped[:30]:
            print(f"- {line}")
        if len(skipped) > 30:
            print(f"- …и ещё {len(skipped) - 30}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
