"""Доверие к периоду: проверялся ли он блокирующими контролями."""

import logging
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from finlib.db import PgConnection, fetch_all
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_SELECT = """
SELECT report_date, has_own_report, own_report_quarantined, own_src_file_id,
       lines_total, lines_disclosed, confidence
FROM period_quality
WHERE inn = %(inn)s AND standard = %(standard)s
ORDER BY report_date DESC
"""


class PeriodConfidence(StrEnum):
    """Доверие к периоду; значения совпадают с CHECK в metric_value."""

    VERIFIED = "verified"
    COMPARATIVE_ONLY = "comparative_only"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class PeriodQuality:
    """Происхождение периода и вытекающее из него доверие."""

    report_date: date
    confidence: PeriodConfidence
    has_own_report: bool
    own_report_quarantined: bool
    own_src_file_id: int | None
    lines_total: int
    lines_disclosed: int

    @property
    def is_usable(self) -> bool:
        """Можно ли считать по периоду показатели."""
        return self.confidence is not PeriodConfidence.QUARANTINED

    @property
    def limitation(self) -> str | None:
        """Текст оговорки для раздела «Ограничения анализа»."""
        if self.confidence is PeriodConfidence.COMPARATIVE_ONLY:
            # **«Не загружен», а не «в источнике нет».** Своего комплекта у нас
            # нет — но это наше состояние, а не свойство источника: у ФосАгро
            # годовая отчётность за 2024 год существует, мы её не загружали.
            # Утверждение об источнике мы проверить не можем, о своей базе —
            # можем, и говорить надо о ней.
            return (
                f"Период {self.report_date:%d.%m.%Y} восстановлен по сравнительным колонкам "
                "более поздней отчётности: собственный комплект за этот период не загружен, "
                "блокирующие контроли качества по нему не выполнялись. Показатели за этот "
                "период приведены с пониженным доверием"
            )
        if self.confidence is PeriodConfidence.QUARANTINED:
            return (
                f"Отчётность за период {self.report_date:%d.%m.%Y} не прошла контроли качества "
                "и в расчёт не включена"
            )
        return None


def period_quality(
    inn: str,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> dict[date, PeriodQuality]:
    """Доверие ко всем периодам организации в пределах одного стандарта."""
    result: dict[date, PeriodQuality] = {}
    for row in fetch_all(_SELECT, {"inn": inn, "standard": standard.value}, conn=conn):
        result[row["report_date"]] = PeriodQuality(
            report_date=row["report_date"],
            confidence=PeriodConfidence(row["confidence"]),
            has_own_report=row["has_own_report"],
            own_report_quarantined=row["own_report_quarantined"],
            own_src_file_id=row["own_src_file_id"],
            lines_total=row["lines_total"],
            lines_disclosed=row["lines_disclosed"],
        )
    return result


def limitations(
    inn: str,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> list[str]:
    """Оговорки по периодам для раздела «Ограничения анализа» заключения.

    **Оговорка о периоде, показателей за который нет, — не ограничение,
    а лишнее утверждение.** «Показатели за этот период приведены с пониженным
    доверием» верно только там, где они приведены; у периода, за который
    не посчитано ни одного, доверие понижать не у чего, и фраза говорит
    о величинах, которых в документе нет.
    """
    quality = period_quality(inn, conn, standard)
    computed = _periods_with_metrics(inn, conn, standard)
    notes = [
        item.limitation
        for item in sorted(quality.values(), key=lambda p: p.report_date)
        # Период в карантине оговорку сохраняет: она говорит не о доверии
        # к показателям, а о том, что комплект в расчёт не вошёл вовсе.
        if item.limitation is not None
        and (item.report_date in computed or item.own_report_quarantined)
    ]
    return notes


_WITH_METRICS = """
SELECT DISTINCT report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND status = 'ok'
"""


def _periods_with_metrics(
    inn: str, conn: PgConnection | None, standard: Standard
) -> set[date]:
    """Периоды, за которые есть хотя бы один рассчитанный показатель."""
    return {
        row["report_date"]
        for row in fetch_all(
            _WITH_METRICS, {"inn": inn, "standard": standard.value}, conn=conn
        )
    }
