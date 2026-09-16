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
    # Отказ разобрать поданный вручную файл. Организацию, период и единицу
    # измерения определяет содержимое файла: имя файла ничего не значит,
    # его может дать кто угодно. Не определилось — комплекта не возникает
    # вовсе, и в расчёт попасть нечему.
    FILE_INN_NOT_DETERMINED = "file_inn_not_determined"
    FILE_PERIOD_NOT_DETERMINED = "file_period_not_determined"
    FILE_REPORTING_TYPE_UNKNOWN = "file_reporting_type_unknown"
    FILE_NOT_PARSED = "file_not_parsed"
    # Код не привязывается к строке: неполон справочник.
    AMBIGUOUS_LINE_CODE = "ambiguous_line_code"
    # Несколько кодов раскрыли одну укрупнённую строку: аномалия самой отчётности.
    MULTIPLE_SOURCE_CODES = "multiple_source_codes"
    UNKNOWN_LINE_CODE = "unknown_line_code"
    PERIOD_VALUE_MISMATCH = "period_value_mismatch"
    FACT_OVERWRITE = "fact_overwrite"


# Уровень служебных записей получения и загрузки. Строка, не опознанная
# по наименованию, в fact_report не попадает, поэтому запись обязана быть видна
# в сводке качества. Уровень у неё не один: пустая строка — пробел справочника
# и повод его пополнить, строка с ненулевым значением — тихая потеря данных,
# и она блокирующая. Уровень в таком случае передаётся записью явно, здесь
# стоит умолчание. Кредитная организация — вне периметра методики, анализ
# по РСБУ для неё не проводится вовсе. Файл, из которого не определить
# организацию, период или тип отчётности, комплектом не становится.
LOADER_SEVERITY: dict[CheckCode, Severity] = {
    CheckCode.CREDIT_ORGANIZATION: Severity.BLOCKING,
    CheckCode.LINE_NOT_RECOGNIZED: Severity.WARNING,
    CheckCode.FILE_INN_NOT_DETERMINED: Severity.BLOCKING,
    CheckCode.FILE_PERIOD_NOT_DETERMINED: Severity.BLOCKING,
    CheckCode.FILE_REPORTING_TYPE_UNKNOWN: Severity.BLOCKING,
    CheckCode.FILE_NOT_PARSED: Severity.BLOCKING,
    CheckCode.AMBIGUOUS_LINE_CODE: Severity.WARNING,
    CheckCode.MULTIPLE_SOURCE_CODES: Severity.WARNING,
    CheckCode.UNKNOWN_LINE_CODE: Severity.WARNING,
    # Расхождение сравнительного значения с отчётным — признак переклассификации
    # или исправления, содержательный сигнал для заключения.
    CheckCode.PERIOD_VALUE_MISMATCH: Severity.WARNING,
    CheckCode.FACT_OVERWRITE: Severity.INFO,
}

# Наименования контролей для документа. Код — механизм, а не часть заключения:
# в «Ключевом выводе» читатель видит наименование, а код остаётся в приложении
# и в журнале. Словарь здесь, а не в сборке документа: свободных строк
# с кодами контролей в коде быть не должно.
CHECK_NAMES: dict[CheckCode, str] = {
    CheckCode.BALANCE_EQUALITY: "равенство актива и пассива",
    CheckCode.SECTION_SUM: "сходимость итога раздела",
    CheckCode.PROFIT_CHAIN: "сходимость цепочки финансового результата",
    CheckCode.PERIOD_REVISED: "пересмотр отчётности прошлых периодов",
    CheckCode.MANDATORY_FIELDS: "раскрытие обязательных строк",
    CheckCode.JUMP_DETECTION: "скачок величины между периодами",
    CheckCode.RETAINED_EARNINGS_LINK: "связь нераспределённой прибыли с результатом",
    CheckCode.UNIT_NOT_DETERMINED: "определение единицы измерения по форме",
    CheckCode.BALANCE_MAGNITUDE: "правдоподобие валюты баланса",
    CheckCode.PERIOD_MAGNITUDE_SHIFT: "кратное тысяче изменение величин",
    CheckCode.CREDIT_ORGANIZATION: "организация вне периметра методики",
    CheckCode.LINE_NOT_RECOGNIZED: "опознание строки по наименованию",
    CheckCode.FILE_INN_NOT_DETERMINED: "определение организации по содержимому файла",
    CheckCode.FILE_PERIOD_NOT_DETERMINED: "определение отчётного периода по содержимому файла",
    CheckCode.FILE_REPORTING_TYPE_UNKNOWN: "определение типа отчётности по содержимому файла",
    CheckCode.FILE_NOT_PARSED: "разбор поданного файла отчётности",
    CheckCode.AMBIGUOUS_LINE_CODE: "неоднозначность кода строки",
    CheckCode.MULTIPLE_SOURCE_CODES: "строка раскрыта несколькими кодами",
    CheckCode.UNKNOWN_LINE_CODE: "код строки отсутствует в справочнике",
    CheckCode.PERIOD_VALUE_MISMATCH: "расхождение сравнительного значения с отчётным",
    CheckCode.FACT_OVERWRITE: "перезапись ранее загруженного значения",
}


def check_name(code: str) -> str:
    """Наименование контроля по коду; неизвестный код возвращается как есть."""
    try:
        return CHECK_NAMES[CheckCode(code)]
    except (ValueError, KeyError):  # pragma: no cover — код вне справочника
        return code


# Записи загрузчика о сопоставлении строк со справочником. Они не событие,
# а состояние комплекта: «эта строка не опознана», «этот код неизвестен».
# Повторная загрузка того же комплекта даёт то же состояние, поэтому прежние
# записи затираются — иначе повторные прогоны множили бы одинаковые строки
# журнала, а исправленный справочник не снимал бы карантин, поставленный
# по устаревшей записи. Перезапись значения и расхождение периодов сюда
# не входят: это события, и они история.
MAPPING_CODES: frozenset[CheckCode] = frozenset(
    {
        CheckCode.LINE_NOT_RECOGNIZED,
        CheckCode.UNKNOWN_LINE_CODE,
        CheckCode.AMBIGUOUS_LINE_CODE,
        CheckCode.MULTIPLE_SOURCE_CODES,
    }
)

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
