"""Запись в журнал контролей качества dq_log."""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.db import PgConnection, execute, execute_many
from finlib.quality.codes import LOADER_SEVERITY, CheckCode, CheckStatus, Severity

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO dq_log (
    src_file_id, inn, report_date, form_code, line_code, check_code,
    status, severity, message, previous_value, new_value, details, code_version
) VALUES (
    %(src_file_id)s, %(inn)s, %(report_date)s, %(form_code)s, %(line_code)s, %(check_code)s,
    %(status)s, %(severity)s, %(message)s, %(previous_value)s, %(new_value)s, %(details)s,
    %(code_version)s
)
"""


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """Одна запись журнала качества."""

    inn: str
    check_code: CheckCode
    status: CheckStatus
    severity: Severity | None = None
    message: str | None = None
    src_file_id: int | None = None
    report_date: date | None = None
    form_code: str | None = None
    line_code: str | None = None
    previous_value: Decimal | None = None
    new_value: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _resolve_severity(record: CheckRecord) -> Severity:
    """Уровень записи: заданный явно либо умолчание из словаря кодов."""
    level = record.severity if record.severity is not None else LOADER_SEVERITY.get(
        record.check_code
    )
    if level is None:
        raise ValueError(f"для контроля {record.check_code} не задан уровень severity")
    return level


def _as_params(record: CheckRecord) -> dict[str, Any]:
    """Превращает запись в параметры запроса.

    **Версия кода пишется у каждой записи.** Журнал — доказательная база,
    и удалять из него нельзя; но запись, порождённая разбором, которого больше
    нет, о комплекте уже не говорит: у ЛСР так остались 18 записей
    «расхождение сравнительных данных», из которых 12 знаковые, а 6 — следы
    наших же исправлений справочника. Сводка считает записи версии, которой
    комплект загружен, прочие называет отдельно.
    """
    from finlib.version import code_version

    return {
        "code_version": code_version(),
        "src_file_id": record.src_file_id,
        "inn": record.inn,
        "report_date": record.report_date,
        "form_code": record.form_code,
        "line_code": record.line_code,
        "check_code": record.check_code.value,
        "status": record.status.value,
        "severity": _resolve_severity(record).value,
        "message": record.message,
        "previous_value": record.previous_value,
        "new_value": record.new_value,
        "details": json.dumps(record.details, ensure_ascii=False, default=str)
        if record.details
        else None,
    }


def log_records(records: Sequence[CheckRecord], conn: PgConnection | None = None) -> int:
    """Пишет пачку записей журнала; при переданном соединении — в его транзакции."""
    if not records:
        return 0
    execute_many(_INSERT, [_as_params(record) for record in records], conn=conn)
    logger.info("dq_log: записано %d событий", len(records))
    return len(records)


def log_check(
    inn: str,
    check_code: CheckCode,
    status: CheckStatus,
    *,
    conn: PgConnection | None = None,
    severity: Severity | None = None,
    message: str | None = None,
    src_file_id: int | None = None,
    report_date: date | None = None,
    form_code: str | None = None,
    line_code: str | None = None,
    previous_value: Decimal | None = None,
    new_value: Decimal | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Пишет одну запись в dq_log; уровень по умолчанию берётся из словаря кодов."""
    record = CheckRecord(
        inn=inn,
        check_code=check_code,
        status=status,
        severity=severity,
        message=message,
        src_file_id=src_file_id,
        report_date=report_date,
        form_code=form_code,
        line_code=line_code,
        previous_value=previous_value,
        new_value=new_value,
        details=details or {},
    )
    execute(_INSERT, _as_params(record), conn=conn)
    logger.info(
        "dq_log: %s %s %s (%s)",
        inn,
        check_code.value,
        status.value,
        _resolve_severity(record).value,
    )
