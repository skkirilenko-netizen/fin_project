"""Словарь кодов журнала качества: коды контролей, статусы и уровни."""

from enum import StrEnum


class CheckStatus(StrEnum):
    """Статус записи в dq_log; значения совпадают с CHECK в схеме БД."""

    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    INFO = "info"


class Severity(StrEnum):
    """Уровень записи в dq_log; blocking отправляет отчётность в карантин."""

    BLOCKING = "blocking"
    WARNING = "warning"
    INFO = "info"


class CheckCode(StrEnum):
    """Коды контролей качества и служебных записей загрузки."""

    # Контроли качества (задача 5).
    BALANCE_EQUALITY = "balance_equality"
    SECTION_SUM = "section_sum"
    PROFIT_CHAIN = "profit_chain"
    PERIOD_CONTINUITY = "period_continuity"
    MANDATORY_FIELDS = "mandatory_fields"
    JUMP_DETECTION = "jump_detection"
    # Записи загрузки (задача 4).
    LINE_NOT_RECOGNIZED = "line_not_recognized"
    UNKNOWN_LINE_CODE = "unknown_line_code"
    FACT_OVERWRITE = "fact_overwrite"


# Уровень служебных записей загрузки. Строка, не опознанная по наименованию,
# в fact_report не попадает, поэтому запись обязана быть видна в сводке
# качества; блокирующим её делает не факт неопознания, а последующее
# несхождение итога раздела.
LOADER_SEVERITY: dict[CheckCode, Severity] = {
    CheckCode.LINE_NOT_RECOGNIZED: Severity.WARNING,
    CheckCode.UNKNOWN_LINE_CODE: Severity.WARNING,
    CheckCode.FACT_OVERWRITE: Severity.INFO,
}
