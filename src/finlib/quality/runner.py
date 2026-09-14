"""Прогон контролей качества, запись результатов и карантин комплекта."""

import logging
from collections import Counter
from dataclasses import dataclass, field

from finlib.db import PgConnection, execute, fetch_all
from finlib.normalize.lines import LinesCatalog
from finlib.quality.checks import ALL_CHECKS, CheckOutcome
from finlib.quality.codes import CHECK_CODES, CheckStatus, Severity
from finlib.quality.context import ReportContext, build_context
from finlib.quality.journal import CheckRecord, log_records
from finlib.quality.thresholds import Thresholds

logger = logging.getLogger(__name__)

_CLEAR_PREVIOUS = """
DELETE FROM dq_log WHERE src_file_id = %(id)s AND check_code = ANY(%(codes)s)
"""

_SET_STATUS = """
UPDATE src_file SET status = %(status)s, quarantine_reason = %(reason)s WHERE id = %(id)s
"""

_QUARANTINED = """
SELECT id FROM src_file WHERE inn = %(inn)s AND status = 'quarantine'
"""


@dataclass
class QualityReport:
    """Итог прогона контролей по одному комплекту."""

    src_file_id: int
    inn: str
    outcomes: list[CheckOutcome] = field(default_factory=list)
    quarantined: bool = False
    quarantine_reason: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        """Число результатов по статусам."""
        return dict(Counter(outcome.status.value for outcome in self.outcomes))

    @property
    def blocking_failures(self) -> list[CheckOutcome]:
        """Провалы, останавливающие расчёт."""
        return [outcome for outcome in self.outcomes if outcome.is_blocking_failure]

    @property
    def warnings(self) -> list[CheckOutcome]:
        """Предупреждения, включая невыполнимые контроли."""
        return [
            outcome
            for outcome in self.outcomes
            if outcome.severity is Severity.WARNING
            and outcome.status in (CheckStatus.WARNING, CheckStatus.INFO, CheckStatus.FAIL)
        ]

    @property
    def not_verifiable(self) -> list[CheckOutcome]:
        """Контроли, которые не удалось выполнить из-за незагруженных строк."""
        return [
            outcome
            for outcome in self.outcomes
            if outcome.status is CheckStatus.INFO and outcome.severity is Severity.WARNING
        ]

    def summary(self) -> str:
        """Однострочная сводка для CLI."""
        counts = self.counts
        parts = [
            f"ИНН {self.inn}, комплект {self.src_file_id}",
            f"пройдено {counts.get('pass', 0)}",
        ]
        if counts.get("fail"):
            parts.append(f"провалов {counts['fail']}")
        if counts.get("warning"):
            parts.append(f"предупреждений {counts['warning']}")
        if self.not_verifiable:
            parts.append(f"не проверено {len(self.not_verifiable)}")
        parts.append("КАРАНТИН" if self.quarantined else "расчёт разрешён")
        return "; ".join(parts)


def run_checks(
    src_file_id: int,
    conn: PgConnection,
    *,
    catalog: LinesCatalog | None = None,
    thresholds: Thresholds | None = None,
) -> QualityReport:
    """Выполняет все контроли по комплекту и записывает результат одной транзакцией.

    Прежние результаты контролей затираются: они снимок состояния. Записи
    загрузчика (перезаписи, неизвестные коды, расхождения периодов) не
    трогаются — это история.
    """
    context = build_context(src_file_id, conn, catalog=catalog, thresholds=thresholds)
    _warn_on_unit_mismatch(context)

    outcomes: list[CheckOutcome] = []
    for check in ALL_CHECKS:
        outcomes.extend(check(context))

    report = QualityReport(src_file_id=src_file_id, inn=context.inn, outcomes=outcomes)

    execute(
        _CLEAR_PREVIOUS,
        {"id": src_file_id, "codes": [code.value for code in CHECK_CODES]},
        conn=conn,
    )
    log_records([_to_record(context, outcome) for outcome in outcomes], conn=conn)

    failures = report.blocking_failures
    if failures:
        report.quarantined = True
        report.quarantine_reason = _reason(failures)
        execute(
            _SET_STATUS,
            {"id": src_file_id, "status": "quarantine", "reason": report.quarantine_reason},
            conn=conn,
        )
    elif context.status == "quarantine":
        # Повторный прогон после исправления снимает карантин.
        execute(_SET_STATUS, {"id": src_file_id, "status": "loaded", "reason": None}, conn=conn)

    logger.info("контроли: %s", report.summary())
    return report


def _reason(failures: list[CheckOutcome]) -> str:
    """Короткая причина карантина из провалившихся контролей."""
    by_check = Counter(outcome.check_code.value for outcome in failures)
    parts = [f"{code} ({count})" for code, count in sorted(by_check.items())]
    return "Провалены блокирующие контроли: " + ", ".join(parts)


def _warn_on_unit_mismatch(context: ReportContext) -> None:
    """Пороги заданы в конкретной единице измерения; иная единица их обесценивает."""
    if context.unit_code != context.thresholds.unit_code:
        logger.warning(
            "единица измерения комплекта %s не совпадает с единицей порогов %s: "
            "допуски на округление неприменимы",
            context.unit_code,
            context.thresholds.unit_code,
        )


def _to_record(context: ReportContext, outcome: CheckOutcome) -> CheckRecord:
    """Превращает результат контроля в запись журнала."""
    return CheckRecord(
        inn=context.inn,
        check_code=outcome.check_code,
        status=outcome.status,
        severity=outcome.severity,
        message=outcome.message,
        src_file_id=context.src_file_id,
        report_date=outcome.report_date,
        form_code=outcome.form_code,
        line_code=outcome.line_code,
        details=outcome.details,
    )


def quarantined_src_files(inn: str, conn: PgConnection | None = None) -> set[int]:
    """Комплекты организации, отправленные в карантин: их факты в расчёт не идут."""
    return {row["id"] for row in fetch_all(_QUARANTINED, {"inn": inn}, conn=conn)}
