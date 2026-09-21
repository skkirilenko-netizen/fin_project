"""Замер маршрутизации по загруженным данным: у кого считаются величины.

Отвечает на один вопрос: **сколько эмитентов вообще поддаётся решению «нужен
ли человек»**, и по какому полю выпадают остальные. Правил корзин здесь нет:
они методическое решение, и замер их не предугадывает.

    uv run python eval/cbonds_routing_run.py > data/output/cbonds_routing.md

**Замер не считает сам.** Показатели берутся у боевого расчёта по фактам
(`metrics.ifrs_store.compute_from_facts`), а замер считает исходы. Замены,
объявленные справочником сопоставления, применяются к тем величинам, которые
расчёт не собрал: EBITDA источника — с пометкой, что состав её нам неизвестен.

Величины маршрута — пять: долг, EBITDA, капитал, оборотные активы
и краткосрочные обязательства. Первые две дают долговую нагрузку, вторые
три — автономию и текущую ликвидность.
"""

import logging
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.ifrs_store import compute_from_facts  # noqa: E402
from finlib.normalize.cbonds_mapping import load_cbonds_mapping  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402

logger = logging.getLogger(__name__)

# Последний годовой период каждого эмитента с комплектом вне карантина:
# маршрут решается по свежей отчётности, а не по всей истории.
_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
GROUP BY f.inn
"""

# Величина EBITDA, посчитанная самим агрегатором: замена, объявленная
# справочником сопоставления. Ноль заменой не считается — у этого поля он
# означает «не раскрыто».
_REPORTED = """
SELECT s.meta -> 'cbonds' -> 'reported' ->> 'ebitda' AS ebitda
FROM src_file s
WHERE s.inn = %(inn)s AND s.standard = 'ifrs' AND s.source = 'cbonds'
  AND s.report_year = %(year)s
LIMIT 1
"""


_WITHOUT_SET = """
SELECT count(DISTINCT d.inn) AS issuers
FROM dq_log d
WHERE d.check_code = 'cbonds_set_rejected'
  AND d.inn NOT IN (
      SELECT s.inn FROM src_file s
      WHERE s.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
  )
"""


def main() -> int:
    """Печатает замер маршрутизации."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    policy = load_ifrs_metrics()
    mapping = load_cbonds_mapping()
    substitution = mapping.substitutions["ebitda"]
    threshold = max(
        x for x, _ in policy.calibration_points.metrics["net_debt_ebitda"].points
    )

    verdicts: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    substituted = 0
    with connection() as conn:
        # **Знаменатель называется целиком.** Эмитент без комплекта в замер
        # не входит, и доля «считается у 69 %» без этого числа означала бы,
        # что прочие посчитаны и выпали, тогда как их отчётность отклонена
        # на приёме: неконсолидированная, не в рублях, без единицы измерения.
        outside = fetch_all(_WITHOUT_SET, {}, conn=conn)
        latest = fetch_all(_LATEST, {}, conn=conn)
        for row in latest:
            inn, moment = row["inn"], row["report_date"]
            computed = {item.code: item for item in compute_from_facts(inn, moment, conn, policy)}
            absent = _absent(computed)
            if not absent:
                verdicts["величины маршрута считаются"] += 1
                continue
            if absent == {"EBITDA"}:
                reported = fetch_all(
                    _REPORTED, {"inn": inn, "year": moment.year}, conn=conn
                )
                value = _number(reported[0]["ebitda"]) if reported else None
                if value:
                    substituted += 1
                    verdicts["величины маршрута считаются (EBITDA источника)"] += 1
                    continue
                bound = computed.get("net_debt_op_profit")
                if bound is not None and bound.calculable:
                    if bound.value <= threshold:
                        verdicts["вывод по границе: нагрузка ниже порога"] += 1
                    else:
                        verdicts["вывод по границе: выше порога, требует внимания"] += 1
                    continue
            verdicts["к человеку"] += 1
            for name in absent:
                missing[name] += 1

    total = sum(verdicts.values())
    print("# Маршрутизация: у кого считаются величины решения\n")
    aside = int(outside[0]["issuers"]) if outside else 0
    print(
        f"Эмитентов с комплектом вне карантина: **{total}**. Величины маршрута — "
        "долг, EBITDA, капитал, оборотные активы, краткосрочные обязательства; "
        "считаются они боевым расчётом по фактам базы.\n"
    )
    print(
        f"Сверх них **{aside} эмитентов комплекта не имеют вовсе**: их отчётность "
        "отклонена на приёме — неконсолидированная по МСФО, не в рублях либо "
        "без объявленной единицы измерения. В доли ниже они не входят: это "
        "не выпавшие из расчёта, а те, чьей отчётности у нас нет.\n"
    )
    print("| Исход | Эмитентов | Доля |")
    print("|---|---|---|")
    for name, count in verdicts.most_common():
        print(f"| {name} | {count} | {count / total * 100:.0f} % |")

    print(
        f"\nЗамена EBITDA полем источника применена у {substituted} эмитентов "
        f"и объявлена справочником сопоставления: «{substitution.shown_as}». "
        "Ноль заменой не считается — у этого поля он означает «не раскрыто»."
    )
    print(
        f"\nПорог вывода по границе — {threshold}: нижняя опорная точка шкалы "
        "долговой нагрузки. Граница ниже неё означает, что и показатель ниже, "
        "то есть критерий пройден доказанно."
    )

    if missing:
        print("\n## Чем выпадают оставшиеся\n")
        print("| Величина | Эмитентов |")
        print("|---|---|")
        for name, count in missing.most_common():
            print(f"| {name} | {count} |")
    return 0


def _absent(computed: dict) -> set[str]:
    """Величины маршрута, которых расчёт не собрал."""
    absent: set[str] = set()
    if not _ok(computed, "net_debt"):
        absent.add("чистый долг")
    if not _ok(computed, "ebitda"):
        absent.add("EBITDA")
    if not _ok(computed, "equity"):
        absent.add("капитал")
    if not _ok(computed, "cur_liq") and not _ok(computed, "cur_liq_ex_inventories"):
        absent.add("текущая ликвидность")
    if not _ok(computed, "equity_ratio"):
        absent.add("автономия")
    return absent


def _ok(computed: dict, code: str) -> bool:
    """Рассчитан ли показатель с таким кодом."""
    item = computed.get(code)
    return item is not None and item.calculable


def _number(value: object) -> Decimal | None:
    """Величина из `meta` в Decimal; ноль и пустое — не величина."""
    if value in (None, ""):
        return None
    found = Decimal(str(value))
    return found or None


if __name__ == "__main__":
    sys.exit(main())
