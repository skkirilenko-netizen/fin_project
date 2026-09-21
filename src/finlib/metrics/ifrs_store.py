"""Расчёт показателей МСФО по фактам базы: вход собирается из `fact_report`.

**Два пути к одному числу расходятся, и расхождения не видно, пока их
не сравнить.** Замер задачи 27 считает по разобранному документу, а заключение
обязано считаться по фактам базы — иначе документ утверждает одно, а прогон
другое. Поэтому вход расчёта собирается здесь, а сама арифметика остаётся
в `metrics/ifrs.py`: сверка ФосАгро показала, что расчётное ядро на двух путях
даёт одно и то же, расходился только вход.

Три силы опознания идут в расчёт наравне и различаются графой `recognition`:
величина строки, опознанной справочником, принятая по подтверждению человека
и взятая из примечания. Доверие к ним разное, участие одинаковое, а вот
**место разное**: величина примечания в состав итогов формы не входит —
у неё свой код.

Комплекты в карантине не читаются: инвариант 6 действует и здесь.
"""

import logging
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, fetch_all
from finlib.metrics.ifrs import Inputs, MetricValue, compute_all, months_of
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, load_ifrs_metrics
from finlib.normalize.ifrs_note_lines import load_note_lines
from finlib.quality.periods import PeriodConfidence
from finlib.sources.ifrs_notes import accrued_interest
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Величины комплекта вне карантина: только актуальная версия года и только
# отчётный период. Сравнительные колонки в расчёт показателей МСФО пока
# не идут — динамика по МСФО появится с вторым годом одного эмитента.
_FACTS = """
SELECT f.line_code, f.value, f.recognition, f.note_source_name,
       s.reporting_kind, s.meta
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(date)s
  AND s.status <> 'quarantine' AND s.is_actual
"""

# Периоды, за которые есть величины: и отчётные, и сравнительные. **Роль
# периода определяет доверие, а не участие в расчёте.** Сравнительная колонка
# загружена фактами, и показатели по ней считаются — они нужны читателю
# документа; блокирующими контролями такой период не проверялся, и это
# отражается признаком доверия, как в РСБУ.
_PERIODS = """
SELECT f.report_date, min(f.period_role) AS best_role
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
  AND s.status <> 'quarantine' AND s.is_actual
GROUP BY f.report_date
ORDER BY f.report_date DESC
"""


class IfrsPeriodMissingError(RuntimeError):
    """Нет ни одного комплекта МСФО вне карантина: считать нечего."""


def periods_of(inn: str, conn: PgConnection | None = None) -> tuple[date, ...]:
    """Периоды МСФО вне карантина, свежий первым: отчётные и сравнительные."""
    rows = fetch_all(
        _PERIODS, {"inn": inn, "standard": Standard.IFRS.value}, conn=conn
    )
    return tuple(row["report_date"] for row in rows)


def confidence_of(
    inn: str, conn: PgConnection | None = None
) -> dict[date, PeriodConfidence]:
    """Доверие к каждому периоду: проверен своим комплектом или восстановлен.

    Правило то же, что в РСБУ: период, существующий только сравнительной
    колонкой, блокирующими контролями не проверялся, и показатели по нему
    приводятся с пониженным доверием. Признак объявляется, а не подразумевается:
    без него ряд из трёх точек выглядит одинаково достоверным.
    """
    rows = fetch_all(
        _PERIODS, {"inn": inn, "standard": Standard.IFRS.value}, conn=conn
    )
    return {
        row["report_date"]: (
            PeriodConfidence.VERIFIED
            if row["best_role"] == "current"
            else PeriodConfidence.COMPARATIVE_ONLY
        )
        for row in rows
    }


def inputs_of(
    inn: str,
    report_date: date,
    conn: PgConnection | None = None,
    policy: IfrsMetricsPolicy | None = None,
) -> Inputs:
    """Собирает вход расчёта из фактов комплекта: величины, примечания, обстановка.

    Величины примечаний отделены от величин форм по графе `recognition`,
    а не по коду: перечень кодов примечаний в двух местах разошёлся бы
    с самим справочником примечаний.

    Число месяцев берётся из вида отчётности комплекта правилом методики,
    а не задаётся снаружи: величина, которую можно передать, однажды
    передаётся неверной и об этом не сообщает.
    """
    policy = policy or load_ifrs_metrics()
    rows = fetch_all(
        _FACTS,
        {"inn": inn, "standard": Standard.IFRS.value, "date": report_date},
        conn=conn,
    )
    if not rows:
        raise IfrsPeriodMissingError(
            f"по МСФО за {report_date:%d.%m.%Y} нет фактов вне карантина: "
            "комплект либо не загружен, либо отбракован экраном сверки"
        )

    note_codes = {item.code for item in load_note_lines().lines}
    values: dict[str, Decimal] = {}
    notes: dict[str, Decimal] = {}
    note_rows: dict[str, tuple[str, ...]] = {}
    for row in rows:
        if row["recognition"] == "note" or row["line_code"] in note_codes:
            notes[row["line_code"]] = row["value"]
            note_rows[row["line_code"]] = tuple(
                (row["note_source_name"] or "").split("; ")
            )
            continue
        values[row["line_code"]] = row["value"]

    # Знаменатель покрытия процентов собирается тем же правилом, что в замере:
    # расход плюс капитализированные, а при объявленной очистке от них
    # и отсутствии величины — отказ.
    accrued = accrued_interest(notes, rows_by_code=note_rows)
    if accrued is not None:
        notes["interest_accrued"] = accrued

    meta = rows[0]["meta"] or {}
    issuer_type = meta.get("issuer_type") or "corporate"
    if not meta.get("issuer_type"):
        # Комплект загружен до того, как тип стал записываться. Умолчание
        # объявлено методикой, но молчать о нём нельзя: поправка показателя
        # по типу тогда не применяется, и это не свойство эмитента, а наш
        # пробел — комплект нужно перезагрузить.
        logger.warning(
            "%s за %s: тип эмитента в комплекте не записан, принят corporate",
            inn,
            report_date,
        )
    return Inputs(
        values,
        notes,
        issuer_type,
        months=months_of(report_date, rows[0]["reporting_kind"], policy),
    )


def compute_from_facts(
    inn: str,
    report_date: date,
    conn: PgConnection | None = None,
    policy: IfrsMetricsPolicy | None = None,
) -> tuple[MetricValue, ...]:
    """Показатели МСФО за период по фактам базы — той же арифметикой, что замер."""
    policy = policy or load_ifrs_metrics()
    found = compute_all(inputs_of(inn, report_date, conn, policy), policy)
    logger.info(
        "%s за %s: показателей рассчитано %d из %d",
        inn,
        report_date,
        sum(1 for item in found if item.calculable),
        len(found),
    )
    return found
