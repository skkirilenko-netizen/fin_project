"""Запись в журнал контролей качества dq_log."""

import json
import logging
from decimal import Decimal
from typing import Any

from finlib.db import execute
from finlib.quality.codes import LOADER_SEVERITY, CheckCode, CheckStatus, Severity

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO dq_log (
    src_file_id, inn, report_date, form_code, line_code, check_code,
    status, severity, message, previous_value, new_value, details
) VALUES (
    %(src_file_id)s, %(inn)s, %(report_date)s, %(form_code)s, %(line_code)s, %(check_code)s,
    %(status)s, %(severity)s, %(message)s, %(previous_value)s, %(new_value)s, %(details)s
)
"""


def log_check(
    inn: str,
    check_code: CheckCode,
    status: CheckStatus,
    *,
    severity: Severity | None = None,
    message: str | None = None,
    src_file_id: int | None = None,
    report_date: str | None = None,
    form_code: str | None = None,
    line_code: str | None = None,
    previous_value: Decimal | None = None,
    new_value: Decimal | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Пишет одну запись в dq_log; уровень по умолчанию берётся из словаря кодов."""
    level = severity if severity is not None else LOADER_SEVERITY.get(check_code)
    if level is None:
        raise ValueError(f"для контроля {check_code} не задан уровень severity")
    execute(
        _INSERT,
        {
            "src_file_id": src_file_id,
            "inn": inn,
            "report_date": report_date,
            "form_code": form_code,
            "line_code": line_code,
            "check_code": check_code.value,
            "status": status.value,
            "severity": level.value,
            "message": message,
            "previous_value": previous_value,
            "new_value": new_value,
            "details": json.dumps(details, ensure_ascii=False, default=str)
            if details is not None
            else None,
        },
    )
    logger.info("dq_log: %s %s %s (%s)", inn, check_code.value, status.value, level.value)
