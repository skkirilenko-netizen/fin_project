"""Расчёт показателей по периодам. Все вычисления в Decimal."""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.db import PgConnection, fetch_all
from finlib.metrics.definitions import MetricDef, MetricsCatalog, load_metrics
from finlib.metrics.formula import (
    NotCalculableReason,
    ZeroDenominatorError,
    average_codes,
    denominator_of,
    describe,
    evaluate,
    line_codes,
)
from finlib.normalize.lines import ReportingType
from finlib.quality.periods import PeriodConfidence, period_quality
from finlib.quality.thresholds import Thresholds, load_thresholds
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_SELECT_FACTS = """
SELECT f.report_date, f.line_code, f.value
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND s.status <> 'quarantine'
ORDER BY f.report_date DESC
"""

_SELECT_REPORTING_TYPE = """
SELECT DISTINCT s.reporting_type
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(d)s
  AND f.period_role = 'current'
"""

_SELECT_ANY_TYPE = """
SELECT s.reporting_type, count(*) AS n
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
GROUP BY s.reporting_type ORDER BY n DESC LIMIT 1
"""


class MetricStatus(StrEnum):
    """Статус рассчитанного показателя."""

    OK = "ok"
    NOT_CALCULABLE = "not_calculable"


@dataclass(frozen=True, slots=True)
class MetricResult:
    """Результат расчёта одного показателя за один период."""

    metric_code: str
    report_date: date
    value: Decimal | None
    status: MetricStatus
    confidence: PeriodConfidence
    reason: str | None = None
    reason_code: str | None = None

    @property
    def is_ok(self) -> bool:
        """Рассчитан ли показатель."""
        return self.status is MetricStatus.OK


@dataclass
class PeriodValues:
    """Значения строк одного периода, плоско по кодам."""

    report_date: date
    values: dict[str, Decimal | None]


def load_period_values(
    inn: str, conn: PgConnection | None = None, standard: Standard = Standard.RSBU
) -> dict[date, PeriodValues]:
    """Читает значения строк организации по периодам, минуя карантин."""
    periods: dict[date, PeriodValues] = {}
    for row in fetch_all(_SELECT_FACTS, {"inn": inn, "standard": standard.value}, conn=conn):
        period = periods.setdefault(
            row["report_date"], PeriodValues(row["report_date"], {})
        )
        period.values[row["line_code"]] = row["value"]
    return periods


def reporting_type_of(
    inn: str,
    report_date: date,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> ReportingType:
    """Набор строк, по которому сдан период; при отсутствии — преобладающий у организации."""
    params = {"inn": inn, "standard": standard.value, "d": report_date}
    rows = fetch_all(_SELECT_REPORTING_TYPE, params, conn=conn)
    if rows:
        return ReportingType(rows[0]["reporting_type"])
    fallback = fetch_all(_SELECT_ANY_TYPE, {"inn": inn, "standard": standard.value}, conn=conn)
    if not fallback:
        raise ValueError(f"для ИНН {inn} нет загруженной отчётности")
    return ReportingType(fallback[0]["reporting_type"])


def compute_metric(
    metric: MetricDef,
    reporting_type: ReportingType,
    report_date: date,
    current: dict[str, Decimal | None],
    previous: dict[str, Decimal | None] | None,
    confidence: PeriodConfidence,
    thresholds: Thresholds,
) -> MetricResult | None:
    """Считает один показатель за один период.

    Если хотя бы один нужный код не раскрыт, показатель не рассчитывается
    и получает not_calculable с перечнем отсутствующих кодов. Подстановка
    приближений запрещена. Показатель, неприменимый к набору отчётности,
    не рассчитывается вовсе и результата не даёт.
    """
    tree = metric.tree_for(reporting_type)
    if tree is None:
        return None

    needs_previous = bool(average_codes(tree))
    if needs_previous and previous is None:
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason=(
                "Нет данных на начало периода: средняя балансовая величина требует "
                "двух точек, подстановка значения на конец периода запрещена"
            ),
            reason_code=NotCalculableReason.NO_PREVIOUS_PERIOD.value,
        )

    missing = _missing_codes(tree, current, previous)
    if missing:
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason="Не раскрыты строки: " + ", ".join(missing),
            reason_code=NotCalculableReason.MISSING_LINES.value,
        )

    if metric.denominator_must_be_positive:
        denominator = denominator_of(tree)
        if denominator is not None:
            computed = evaluate(denominator, current, previous, thresholds.constants)
            if computed < 0:
                return MetricResult(
                    metric.code,
                    report_date,
                    None,
                    MetricStatus.NOT_CALCULABLE,
                    confidence,
                    reason=(
                        "Знаменатель отрицателен, коэффициент не интерпретируется: "
                        f"{describe(denominator)} = {computed}"
                    ),
                    reason_code=NotCalculableReason.NEGATIVE_DENOMINATOR.value,
                )

    try:
        value = evaluate(tree, current, previous, thresholds.constants)
    except ZeroDenominatorError as exc:
        reason = f"Коэффициент не определён: {exc.expression} равен нулю"
        if metric.zero_denominator_note:
            reason = f"{reason}. {metric.zero_denominator_note.strip()}"
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason=reason,
            reason_code=NotCalculableReason.ZERO_DENOMINATOR.value,
        )

    return MetricResult(metric.code, report_date, value, MetricStatus.OK, confidence)


def _missing_codes(
    tree, current: dict[str, Decimal | None], previous: dict[str, Decimal | None] | None
) -> list[str]:
    """Коды, без которых показатель не посчитать; порядок устойчив."""
    missing: set[str] = set()
    averages = average_codes(tree)
    for code in line_codes(tree):
        if current.get(code) is None:
            missing.add(code)
            continue
        if code not in averages:
            continue
        # Средняя величина требует значения и на начало периода.
        if previous is None or previous.get(code) is None:
            missing.add(code)
    return sorted(missing)


def compute_all(
    inn: str,
    conn: PgConnection | None = None,
    *,
    catalog: MetricsCatalog | None = None,
    thresholds: Thresholds | None = None,
    standard: Standard = Standard.RSBU,
) -> list[MetricResult]:
    """Считает все применимые показатели по всем периодам организации.

    Расчёт всегда идёт в пределах одного стандарта: ряды по РСБУ и по МСФО
    несопоставимы и смешению не подлежат.
    """
    catalog = catalog if catalog is not None else load_metrics()
    thresholds = thresholds if thresholds is not None else load_thresholds()

    periods = load_period_values(inn, conn, standard)
    quality = period_quality(inn, conn, standard)
    ordered = sorted(periods, reverse=True)

    results: list[MetricResult] = []
    for report_date in ordered:
        info = quality.get(report_date)
        if info is not None and not info.is_usable:
            logger.info("период %s в карантине, показатели не считаются", report_date)
            continue
        confidence = info.confidence if info is not None else PeriodConfidence.VERIFIED
        reporting_type = reporting_type_of(inn, report_date, conn, standard)
        previous_date = _previous_usable(report_date, ordered, quality)
        previous = periods[previous_date].values if previous_date is not None else None

        for metric in catalog.for_type(reporting_type):
            result = compute_metric(
                metric,
                reporting_type,
                report_date,
                periods[report_date].values,
                previous,
                confidence,
                thresholds,
            )
            if result is not None:
                results.append(result)
    return results


def _previous_usable(report_date: date, ordered: list[date], quality: dict) -> date | None:
    """Ближайший предыдущий период, пригодный как база для средней величины."""
    for candidate in ordered:
        if candidate >= report_date:
            continue
        info = quality.get(candidate)
        if info is None or info.is_usable:
            return candidate
    return None
