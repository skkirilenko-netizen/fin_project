"""Прогон контролей качества, запись результатов и карантин комплекта."""

import logging
from collections import Counter
from dataclasses import dataclass, field

from finlib.db import PgConnection, execute, fetch_all
from finlib.normalize.lines import LinesCatalog
from finlib.quality.checks import ALL_CHECKS, CheckOutcome
from finlib.quality.codes import CHECK_CODES, CheckCode, CheckStatus, Severity
from finlib.quality.context import ReportContext, build_context
from finlib.quality.journal import CheckRecord, log_records
from finlib.quality.thresholds import Thresholds
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_CLEAR_PREVIOUS = """
DELETE FROM dq_log WHERE src_file_id = %(id)s AND check_code = ANY(%(codes)s)
"""

_SET_STATUS = """
UPDATE src_file SET status = %(status)s, quarantine_reason = %(reason)s WHERE id = %(id)s
"""

# Стандарт входит в отбор наравне с организацией: ряды по РСБУ и по МСФО
# несопоставимы, и комплект в карантине по одному стандарту о другом
# не говорит ничего. Запрет смешения действует не только в показателях.
_QUARANTINED = """
SELECT id FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND status = 'quarantine'
"""

# Блокирующие записи журнала, которые оставил не прогон контролей, а загрузчик.
# Карантин ставится по блокирующему провалу, откуда бы он ни пришёл: строка,
# не опознанная по наименованию и при этом несущая значение, — такая же
# остановка, как несошедшийся итог.
_LOADER_BLOCKING = """
SELECT check_code, count(*) AS hits
FROM dq_log
WHERE src_file_id = %(id)s AND severity = 'blocking' AND status = 'fail'
  AND NOT (check_code = ANY(%(codes)s))
GROUP BY check_code
ORDER BY check_code
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
    if context.standard is not Standard.RSBU:
        # Контроли этого модуля построены на формах и кодах строк РСБУ:
        # равенство 1600 = 1700, состав разделов, цепочка прибыли. К комплекту
        # МСФО они неприменимы, и прогнать их значило бы получить полтора
        # десятка ложных провалов. Контроли МСФО выполняются на экране сверки
        # (`sources/ifrs_review.py`) и пишутся в журнал при загрузке.
        #
        # Пропуск объявляется записью, а не молчанием: комплект без записей
        # в журнале неотличим от проверенного и чистого.
        skipped = _skipped_for_standard(context)
        log_records([skipped], conn=conn)
        logger.info(
            "контроли РСБУ к комплекту %s не применяются: стандарт %s",
            src_file_id,
            context.standard,
        )
        return QualityReport(src_file_id=src_file_id, inn=context.inn)

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

    reason = _reason(report.blocking_failures, _loader_blocking(src_file_id, conn))
    if reason is not None:
        report.quarantined = True
        report.quarantine_reason = reason
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


def _skipped_for_standard(context: ReportContext) -> CheckRecord:
    """Запись о том, что контроли РСБУ к комплекту другого стандарта не шли.

    Молчание здесь было бы тем же «ноль срабатываний»: комплект без записей
    в журнале выглядит проверенным и чистым.
    """
    return CheckRecord(
        inn=context.inn,
        check_code=CheckCode.LINE_MAPPING,
        status=CheckStatus.INFO,
        severity=Severity.INFO,
        message=(
            f"Контроли РСБУ не выполнялись: комплект стандарта "
            f"{context.standard.value}. Контроли этого стандарта выполняются "
            "при приёме документа и на экране сверки"
        ),
        src_file_id=context.src_file_id,
        details={"standard": context.standard.value},
    )


def _reason(failures: list[CheckOutcome], loader: dict[str, int]) -> str | None:
    """Короткая причина карантина; None — блокирующих провалов нет."""
    by_check = Counter(outcome.check_code.value for outcome in failures)
    by_check.update(loader)
    if not by_check:
        return None
    parts = [f"{code} ({count})" for code, count in sorted(by_check.items())]
    return "Провалены блокирующие контроли: " + ", ".join(parts)


def _loader_blocking(src_file_id: int, conn: PgConnection) -> dict[str, int]:
    """Блокирующие записи журнала, оставленные загрузчиком до прогона контролей."""
    rows = fetch_all(
        _LOADER_BLOCKING,
        {"id": src_file_id, "codes": [code.value for code in CHECK_CODES]},
        conn=conn,
    )
    return {row["check_code"]: int(row["hits"]) for row in rows}


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


def quarantined_src_files(
    inn: str,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> set[int]:
    """Комплекты организации, отправленные в карантин: их факты в расчёт не идут.

    Стандарт задаётся явно: комплект МСФО в карантине к расчёту по РСБУ
    отношения не имеет, и смешивать их нельзя — это то же правило, по
    которому показатель не считается из величин двух стандартов.
    """
    return {
        row["id"]
        for row in fetch_all(
            _QUARANTINED, {"inn": inn, "standard": standard.value}, conn=conn
        )
    }
