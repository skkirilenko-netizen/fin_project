"""Запись рассчитанных показателей в metric_value и чтение рядов."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, execute_many, fetch_all
from finlib.metrics.definitions import load_metrics
from finlib.metrics.engine import MetricResult, MetricStatus
from finlib.quality.periods import PeriodConfidence

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO metric_value (
    inn, report_date, metric_code, value, status, confidence, reason, reason_code,
    methodology_version
) VALUES (
    %(inn)s, %(report_date)s, %(metric_code)s, %(value)s, %(status)s, %(confidence)s,
    %(reason)s, %(reason_code)s, %(methodology_version)s
)
ON CONFLICT (inn, report_date, metric_code) DO UPDATE SET
    value = EXCLUDED.value,
    status = EXCLUDED.status,
    confidence = EXCLUDED.confidence,
    reason = EXCLUDED.reason,
    reason_code = EXCLUDED.reason_code,
    methodology_version = EXCLUDED.methodology_version,
    computed_at = now()
"""

_SELECT_SERIES = """
SELECT report_date, value, status, confidence, reason, reason_code
FROM metric_value
WHERE inn = %(inn)s AND metric_code = %(code)s
ORDER BY report_date DESC
"""


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    """Точка ряда показателя."""

    report_date: date
    value: Decimal | None
    status: MetricStatus
    confidence: PeriodConfidence
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MetricSeries:
    """Ряд значений показателя по периодам, от свежего к старому."""

    metric_code: str
    points: tuple[SeriesPoint, ...]

    @property
    def calculated(self) -> tuple[SeriesPoint, ...]:
        """Точки, по которым показатель рассчитан."""
        return tuple(item for item in self.points if item.status is MetricStatus.OK)

    @property
    def is_trend_possible(self) -> bool:
        """Есть ли хотя бы две рассчитанные точки: без них динамики нет."""
        return len(self.calculated) >= 2

    @property
    def has_unverified_points(self) -> bool:
        """Опирается ли ряд на периоды, не проверенные блокирующими контролями."""
        return any(
            item.confidence is not PeriodConfidence.VERIFIED for item in self.calculated
        )

    def trend_note(self) -> str | None:
        """Оговорка о достоверности динамики для раздела «Ограничения анализа»."""
        if not self.is_trend_possible:
            return (
                f"Показатель «{self.metric_code}» рассчитан менее чем за два периода: "
                "динамику оценить нельзя"
            )
        if self.has_unverified_points:
            unverified = [
                f"{item.report_date:%d.%m.%Y}"
                for item in self.calculated
                if item.confidence is not PeriodConfidence.VERIFIED
            ]
            return (
                f"Динамика показателя «{self.metric_code}» частично опирается на периоды, "
                f"не проверенные блокирующими контролями: {', '.join(unverified)}"
            )
        return None


def save_results(
    inn: str, results: Sequence[MetricResult], conn: PgConnection | None = None
) -> int:
    """Пишет результаты расчёта; повторный расчёт не создаёт дублей."""
    if not results:
        return 0
    version = load_metrics().version
    execute_many(
        _UPSERT,
        [
            {
                "inn": inn,
                "report_date": item.report_date,
                "metric_code": item.metric_code,
                "value": item.value,
                "status": item.status.value,
                "confidence": item.confidence.value,
                "reason": item.reason,
                "reason_code": item.reason_code,
                "methodology_version": version,
            }
            for item in results
        ],
        conn=conn,
    )
    logger.info("показатели: записано %d значений по ИНН %s", len(results), inn)
    return len(results)


def load_series(inn: str, metric_code: str, conn: PgConnection | None = None) -> MetricSeries:
    """Читает ряд показателя вместе с доверием к каждому периоду."""
    points = tuple(
        SeriesPoint(
            report_date=row["report_date"],
            value=row["value"],
            status=MetricStatus(row["status"]),
            confidence=PeriodConfidence(row["confidence"]),
            reason=row["reason"],
        )
        for row in fetch_all(_SELECT_SERIES, {"inn": inn, "code": metric_code}, conn=conn)
    )
    return MetricSeries(metric_code=metric_code, points=points)
