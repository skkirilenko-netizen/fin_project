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
    # Не «непрерывность»: сальдо на начало периода у нас конструктивно совпадает
    # с сальдо на конец предыдущего. Содержательная проверка — пересмотр
    # отчётности прошлых периодов.
    PERIOD_REVISED = "period_revised"
    MANDATORY_FIELDS = "mandatory_fields"
    JUMP_DETECTION = "jump_detection"
    RETAINED_EARNINGS_LINK = "retained_earnings_link"
    # Единица измерения не определена формой комплекта. Ошибка в тысячу раз
    # не ловится ни одним другим контролем: баланс сойдётся, коэффициенты
    # будут верны, а все абсолютные величины окажутся неверны.
    UNIT_NOT_DETERMINED = "unit_not_determined"
    # Правдоподобие абсолютных величин при заявленной единице. Определение
    # единицы по форме — правило, выведенное из состава форм, и оно сломается
    # на источнике, отдающем рубли или миллионы.
    BALANCE_MAGNITUDE = "balance_magnitude"
    PERIOD_MAGNITUDE_SHIFT = "period_magnitude_shift"
    # Записи получения и загрузки (задачи 3 и 4).
    CREDIT_ORGANIZATION = "credit_organization"
    LINE_NOT_RECOGNIZED = "line_not_recognized"
    # Код не привязывается к строке: неполон справочник.
    AMBIGUOUS_LINE_CODE = "ambiguous_line_code"
    # Несколько кодов раскрыли одну укрупнённую строку: аномалия самой отчётности.
    MULTIPLE_SOURCE_CODES = "multiple_source_codes"
    UNKNOWN_LINE_CODE = "unknown_line_code"
    PERIOD_VALUE_MISMATCH = "period_value_mismatch"
    FACT_OVERWRITE = "fact_overwrite"


# Уровень служебных записей получения и загрузки. Строка, не опознанная
# по наименованию, в fact_report не попадает, поэтому запись обязана быть видна
# в сводке качества; блокирующим её делает не факт неопознания, а последующее
# несхождение итога раздела. Кредитная организация — вне периметра методики,
# анализ по РСБУ для неё не проводится вовсе.
LOADER_SEVERITY: dict[CheckCode, Severity] = {
    CheckCode.CREDIT_ORGANIZATION: Severity.BLOCKING,
    CheckCode.LINE_NOT_RECOGNIZED: Severity.WARNING,
    CheckCode.AMBIGUOUS_LINE_CODE: Severity.WARNING,
    CheckCode.MULTIPLE_SOURCE_CODES: Severity.WARNING,
    CheckCode.UNKNOWN_LINE_CODE: Severity.WARNING,
    # Расхождение сравнительного значения с отчётным — признак переклассификации
    # или исправления, содержательный сигнал для заключения.
    CheckCode.PERIOD_VALUE_MISMATCH: Severity.WARNING,
    CheckCode.FACT_OVERWRITE: Severity.INFO,
}

# Семейство кодов, которые контроли качества переписывают при каждом прогоне.
# Записи загрузчика в это семейство не входят: они история, а не снимок.
CHECK_CODES: frozenset[CheckCode] = frozenset(
    {
        CheckCode.BALANCE_EQUALITY,
        CheckCode.SECTION_SUM,
        CheckCode.PROFIT_CHAIN,
        CheckCode.PERIOD_REVISED,
        CheckCode.MANDATORY_FIELDS,
        CheckCode.JUMP_DETECTION,
        CheckCode.RETAINED_EARNINGS_LINK,
        CheckCode.UNIT_NOT_DETERMINED,
        CheckCode.BALANCE_MAGNITUDE,
        CheckCode.PERIOD_MAGNITUDE_SHIFT,
    }
)
