"""Долговая нагрузка ЛСР: из двух доставок порознь, чтобы закрыть вопрос о разборе.

**Странное число объясняется двояко: либо эмитент так отчитался, либо так
прочитали мы.** Второе вероятнее и проверяется первым — но проверяется, а не
предполагается. Долговая нагрузка ЛСР трижды называлась «вероятной ошибкой
распознавания», и закрыть это можно только числом: тот же показатель,
посчитанный **без единой страницы PDF** — по нормализованным данным
агрегатора, — против посчитанного по разобранному документу.

Совпали — распознавание ни при чём, и вопрос закрыт навсегда: причина в шкале,
а не в разборе. Разошлись — разбор виноват, и разбирать надо его.

    uv run python eval/lsr_debt_run.py > data/output/lsr_debt.md

**Замер не считает сам**: величины берёт боевой расчёт по фактам
(`metrics.ifrs_store.compute_from_facts`), а способ получения называет доводом.
Своей арифметики здесь нет вовсе — сравниваются два ответа одного кода.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.ifrs_store import (  # noqa: E402
    IfrsPeriodMissingError,
    compute_from_facts,
    periods_of,
)
from finlib.normalize.facts import unit_name_of  # noqa: E402
from finlib.scoring.routing_catalogue import catalogue_for  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

INN = "7838360491"
NAME = "ЛСР"

# Способы получения, которые сравниваются. Наименования — те же, что в базе:
# «file» — разобранный документ эмитента, «cbonds» — нормализованные данные
# агрегатора, у которых страницы PDF нет вовсе.
WAYS = (("file", "разобранный документ"), ("cbonds", "данные агрегатора"))

# Величины, по которым идёт спор. Чистый долг и EBITDA называются порознь:
# отношение одно, а разойтись могут обе его части, и по отношению их
# не различить.
SHOWN = ("debt_total", "net_debt", "ebitda", "net_debt_ebitda")


def _value(found: tuple, code: str, unit: str) -> str:
    """Величина показателя словами; не рассчитан — причина, а не пусто.

    Печатает её справочник своего стандарта той же единой точкой округления,
    что список и документ: второй набор правил печати разошёлся бы с первым.
    """
    item = next((entry for entry in found if entry.code == code), None)
    if item is None:
        return "нет в справочнике"
    if not item.calculable:
        return f"не рассчитан: {item.reason.value if item.reason else 'без причины'}"
    return catalogue_for(Standard.IFRS).shown(code, item.value, unit)


def main() -> int:
    """Печатает долговую нагрузку ЛСР по каждой доставке порознь."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    with connection() as conn:
        moments = periods_of(INN, conn)
        if not moments:
            print(
                f"# {NAME}: комплектов по МСФО в базе нет\n\n"
                "Сравнивать нечего, и ноль здесь означал бы совпадение, "
                "которого не проверяли."
            )
            return 1
        # Единица берётся у комплекта самого свежего периода: она свойство
        # комплекта, а не стандарта, и «тыс. руб.» у миллионов — ошибка
        # в тысячу раз, которую не ловит ни один контроль сходимости.
        unit = unit_name_of(INN, moments[0], conn)
        print(f"# {NAME} ({INN}): долговая нагрузка по каждой доставке порознь\n")
        print(
            "Вопрос один: **посчитана ли она из разобранного документа так же, "
            "как из нормализованных данных агрегатора**, у которых страницы PDF "
            "нет вовсе. Совпали — распознавание ни при чём.\n"
        )
        print(f"Единица комплекта — {unit}.\n")
        agreed = 0
        compared = 0
        for moment in moments:
            print(f"\n## {moment:%d.%m.%Y}\n")
            print("| Величина | " + " | ".join(name for _, name in WAYS) + " | обе |")
            print("|---|" + "---|" * (len(WAYS) + 1))
            answers: dict[str, dict[str, str]] = {}
            for source, _ in WAYS:
                try:
                    found = compute_from_facts(INN, moment, conn, source=source)
                except IfrsPeriodMissingError:
                    answers[source] = dict.fromkeys(SHOWN, "доставки нет")
                    continue
                answers[source] = {
                    code: _value(found, code, unit) for code in SHOWN
                }
            both = compute_from_facts(INN, moment, conn)
            for code in SHOWN:
                cells = " | ".join(answers[source][code] for source, _ in WAYS)
                print(f"| {code} | {cells} | {_value(both, code, unit)} |")
            # **Сходимость считается только там, где ответили обе доставки.**
            # «Совпало ноль из нуля» и «совпало ноль из четырёх» — разные
            # сведения, и знаменатель обязан стоять рядом.
            for code in SHOWN:
                said = [answers[source][code] for source, _ in WAYS]
                if any(item in ("доставки нет",) for item in said):
                    continue
                compared += 1
                agreed += int(said[0] == said[1])
        print(
            f"\n**Сошлось {agreed} величин из {compared} сравнённых.** "
            "Знаменатель здесь и есть ответ: «расхождений нет» при неизвестном "
            "числе сверок не означает ничего.\n"
        )
        _line_level(conn)
    return 0


# **Сверка идёт на уровне строк, а не показателя, и это сильнее.** Величина
# агрегатора за период, который уже загружен из документа, в факты
# не записывается — первоисточник старше, — но перед этим она **сравнивается**,
# и расхождение уходит в журнал кодом `cbonds_value_mismatch`. Значит, ответ
# на вопрос «прочитали ли мы документ так же, как агрегатор» лежит в журнале
# целиком: сошлись все строки, кроме названных.
_OFFERED = """
SELECT DISTINCT report_date, message
FROM dq_log
WHERE inn = %(inn)s AND check_code = 'cbonds_field_mapping'
  AND src_file_id IN (
      SELECT id FROM src_file
      WHERE inn = %(inn)s AND standard = 'ifrs' AND source = 'cbonds'
  )
ORDER BY report_date DESC
"""

_DIVERGED = """
SELECT DISTINCT report_date, message
FROM dq_log
WHERE inn = %(inn)s AND check_code = 'cbonds_value_mismatch'
ORDER BY report_date DESC
"""


def _line_level(conn) -> None:  # noqa: ANN001
    """Печатает построчную сверку документа с агрегатором по журналу."""
    offered = fetch_all(_OFFERED, {"inn": INN}, conn=conn)
    diverged = fetch_all(_DIVERGED, {"inn": INN}, conn=conn)
    print("\n## Построчная сверка документа с агрегатором\n")
    print(
        "**Это сильнее сравнения показателя.** Величина агрегатора за период, "
        "уже загруженный из документа, в факты не записывается — первоисточник "
        "старше, — но перед этим она сравнивается, и расхождение уходит "
        "в журнал. Значит, ответ лежит в журнале целиком: сошлось всё, кроме "
        "названного ниже.\n"
    )
    if not offered:
        print(
            "Записей о доставке агрегатора нет: сверять было нечего, "
            "и молчание здесь не «совпало».\n"
        )
    for row in offered:
        print(f"- {row['report_date']:%d.%m.%Y}: {row['message']}")
    print(f"\n**Расхождений {len(diverged)}:**\n")
    if not diverged:
        print("ни одного.\n")
    for row in diverged:
        print(f"- {row['report_date']:%d.%m.%Y}: {row['message']}")


if __name__ == "__main__":
    sys.exit(main())
