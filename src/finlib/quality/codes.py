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
    # Документ без текстового слоя: разбирать нечего, нужен OCR.
    FILE_TEXT_LAYER_MISSING = "file_text_layer_missing"
    # Подан не комплект отчётности, а другой документ — чаще всего годовой
    # отчёт эмитента. Проверяется первым: в годовом отчёте есть и числа,
    # и упоминания отчётности, и любой параметр в нём «определится».
    FILE_NOT_STATEMENTS = "file_not_statements"
    # Финансовая организация: неклассифицированный баланс, свои показатели,
    # отдельная методика не реализована.
    FINANCIAL_INSTITUTION = "financial_institution"
    FILE_CURRENCY_NOT_DETERMINED = "file_currency_not_determined"
    # Отчётность не в рублях: методика рублёвая, пересчёт по курсу был бы
    # нашим допущением поверх отчётности эмитента.
    FILE_CURRENCY_NOT_ROUBLE = "file_currency_not_rouble"
    FILE_PERIODS_NOT_DETERMINED = "file_periods_not_determined"
    # Конвенция записи чисел документа МСФО не определена. Отказ, а не выбор
    # по умолчанию: прочтения различаются в тысячу раз, и ни один контроль
    # сходимости ошибки не поймает — сойдётся всё, кроме самих величин.
    DIGIT_GROUPING_NOT_DETERMINED = "digit_grouping_not_determined"
    # Разобранные величины не согласуются между собой так, как согласуются
    # величины одной конвенции: признак того, что конвенция выбрана неверно.
    DIGIT_GROUPING_IMPLAUSIBLE = "digit_grouping_implausible"
    # Графы формы приведены за период иной длительности, чем период комплекта:
    # у промежуточного ФосАгро рядом с полугодием стоит квартал. Отказ, а не
    # выбор: величины квартала, взятые за полугодие, согласованы сами с собой,
    # и ни один контроль сходимости этого не покажет.
    FILE_COLUMN_SPAN_MISMATCH = "file_column_span_mismatch"
    # Граф с величинами больше, чем отчётных дат, а длительность их шапка
    # не объявила: лишние отброшены вслепую. Нарушение, а не норма —
    # отброшенной может оказаться как раз та графа, которая нужна.
    EXTRA_COLUMNS_DROPPED = "extra_columns_dropped"
    # Код не привязывается к строке: неполон справочник.
    AMBIGUOUS_LINE_CODE = "ambiguous_line_code"
    # Несколько кодов раскрыли одну укрупнённую строку: аномалия самой отчётности.
    MULTIPLE_SOURCE_CODES = "multiple_source_codes"
    UNKNOWN_LINE_CODE = "unknown_line_code"
    # Сводка судеб кодов источника: сколько сопоставлено, сколько игнорируется
    # осознанно, сколько неприменимо к набору форм. Без неё нули по трём
    # предыдущим кодам ничем не подтверждены — журнал молчит и когда коды
    # разобраны все, и когда разбор не выполнялся вовсе.
    LINE_MAPPING = "line_mapping"
    PERIOD_VALUE_MISMATCH = "period_value_mismatch"
    # Величина та же, знак обратный. Это расхождение соглашения о печати
    # знака, а не пересмотр отчётности эмитентом: расходная статья
    # печатается то в скобках, то без них, и одна организация делает это
    # в разные годы по-разному. В `period_value_mismatch` такой записи
    # не место — по нему считается интенсивность пересмотра, и сигнал
    # мерил бы нас, а не эмитента.
    SIGN_CONVENTION_MISMATCH = "sign_convention_mismatch"
    # Сводка столкновений периодов: сколько входящих величин встретили
    # уже загруженную, сколько совпало, сколько отклонено приоритетом.
    # Без знаменателя ноль отклонений неотличим от отсутствия столкновений,
    # а правило приоритета выглядит работающим, ни разу не сработав.
    PERIOD_PRIORITY = "period_priority"
    FACT_OVERWRITE = "fact_overwrite"
    # --- аудиторское заключение (задача 25) ---------------------------------
    # Мнение аудитора модифицировано: оговорка, отрицательное мнение либо
    # отказ от выражения мнения. Относится к самой отчётности, на которой
    # построен расчёт, и потому идёт в журнал комплекта.
    AUDIT_OPINION_MODIFIED = "audit_opinion_modified"
    # Существенная неопределённость в отношении непрерывности деятельности:
    # объявляется отдельным разделом и мнения не модифицирует. Признак
    # независимый и по тяжести старше вида мнения.
    AUDIT_GOING_CONCERN = "audit_going_concern"
    # Аудитор обратил внимание на пересмотр ранее выпущенной отчётности.
    AUDIT_STATEMENTS_RESTATED = "audit_statements_restated"
    # Заключение в документе есть, но прочесть его нельзя: страницы без
    # текстового слоя. Не то же самое, что отсутствие оговорок.
    AUDIT_REPORT_NOT_READABLE = "audit_report_not_readable"
    # Заключения в документе нет вовсе — третье состояние, со своим смыслом.
    AUDIT_REPORT_ABSENT = "audit_report_absent"
    # Отчётность прошла обзорную проверку, а не аудит: объём процедур меньше,
    # мнения о достоверности аудитор не выражает.
    AUDIT_REVIEW_ENGAGEMENT = "audit_review_engagement"
    # Величина, объявленная в примечании, не извлечена: ссылки из формы нет,
    # примечание не найдено, строки в нём нет. **Это отказ, а не отсутствие
    # факта**: у Норникеля капитализированные проценты раскрыты прозой,
    # и показатель, которому величины не хватило, обязан назвать причину.
    NOTE_VALUE_NOT_EXTRACTED = "note_value_not_extracted"
    # Сводка величин примечаний: сколько взято, сколько отказов. Счётчик
    # проверенного рядом со счётчиком сработавшего.
    NOTE_VALUES = "note_values"


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
    # Документ, числа которого прочесть нельзя, комплектом не становится.
    CheckCode.DIGIT_GROUPING_NOT_DETERMINED: Severity.BLOCKING,
    CheckCode.DIGIT_GROUPING_IMPLAUSIBLE: Severity.BLOCKING,
    # Графы чужой длительности: комплекта из такого документа не возникает.
    CheckCode.FILE_COLUMN_SPAN_MISMATCH: Severity.BLOCKING,
    # Отброшенная вслепую графа — потеря величины, а не мелочь вёрстки.
    CheckCode.EXTRA_COLUMNS_DROPPED: Severity.BLOCKING,
    # Документ, который не является отчётностью либо не поддаётся разбору,
    # комплектом не становится вовсе — фактов из него не пишется.
    CheckCode.FILE_TEXT_LAYER_MISSING: Severity.BLOCKING,
    CheckCode.FILE_NOT_STATEMENTS: Severity.BLOCKING,
    CheckCode.FINANCIAL_INSTITUTION: Severity.BLOCKING,
    CheckCode.FILE_CURRENCY_NOT_DETERMINED: Severity.BLOCKING,
    CheckCode.FILE_CURRENCY_NOT_ROUBLE: Severity.BLOCKING,
    CheckCode.FILE_PERIODS_NOT_DETERMINED: Severity.BLOCKING,
    CheckCode.AMBIGUOUS_LINE_CODE: Severity.WARNING,
    CheckCode.MULTIPLE_SOURCE_CODES: Severity.WARNING,
    CheckCode.UNKNOWN_LINE_CODE: Severity.WARNING,
    # Сводка — не нарушение, а счётчик проверенного.
    CheckCode.LINE_MAPPING: Severity.INFO,
    # Расхождение сравнительного значения с отчётным — признак переклассификации
    # или исправления, содержательный сигнал для заключения.
    CheckCode.PERIOD_VALUE_MISMATCH: Severity.WARNING,
    # Расхождение знака при равной величине — наш дефект, а не сведение
    # об эмитенте: величина не пересмотрена, расходится способ её печати.
    # Уровень предупреждения, потому что знак в базе от этого зависит.
    CheckCode.SIGN_CONVENTION_MISMATCH: Severity.WARNING,
    # Сводка столкновений — не нарушение, а счётчик проверенного.
    CheckCode.PERIOD_PRIORITY: Severity.INFO,
    CheckCode.FACT_OVERWRITE: Severity.INFO,
    # Сведения из аудиторского заключения. Ни одно из них не отменяет расчёт:
    # отчётность с оговоркой остаётся отчётностью, а нечитаемое заключение
    # ничего не говорит о самой отчётности. Но в заключение они обязаны
    # попасть, поэтому уровень — предупреждение, а не сведение к сведению.
    CheckCode.AUDIT_OPINION_MODIFIED: Severity.WARNING,
    CheckCode.AUDIT_GOING_CONCERN: Severity.WARNING,
    CheckCode.AUDIT_STATEMENTS_RESTATED: Severity.WARNING,
    CheckCode.AUDIT_REPORT_NOT_READABLE: Severity.WARNING,
    CheckCode.AUDIT_REPORT_ABSENT: Severity.WARNING,
    CheckCode.AUDIT_REVIEW_ENGAGEMENT: Severity.WARNING,
    # Отказ извлечения из примечания карантина не вызывает: это наш пробел
    # либо способ раскрытия эмитента, а не дефект отчётности. Но и молчать
    # о нём нельзя — показатель без величины обязан назвать причину.
    CheckCode.NOTE_VALUE_NOT_EXTRACTED: Severity.WARNING,
    CheckCode.NOTE_VALUES: Severity.INFO,
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
    CheckCode.DIGIT_GROUPING_NOT_DETERMINED: "определение разделителя разрядов",
    CheckCode.DIGIT_GROUPING_IMPLAUSIBLE: "правдоподобие разделителя разрядов",
    CheckCode.FILE_COLUMN_SPAN_MISMATCH: "длительность граф формы",
    CheckCode.EXTRA_COLUMNS_DROPPED: "полнота прочтения граф формы",
    CheckCode.FILE_TEXT_LAYER_MISSING: "наличие текстового слоя в документе",
    CheckCode.FILE_NOT_STATEMENTS: "документ является финансовой отчётностью",
    CheckCode.FINANCIAL_INSTITUTION: "организация в периметре методики",
    CheckCode.FILE_CURRENCY_NOT_DETERMINED: "определение валюты отчётности",
    CheckCode.FILE_CURRENCY_NOT_ROUBLE: "валюта отчётности в периметре методики",
    CheckCode.FILE_PERIODS_NOT_DETERMINED: "определение отчётных дат",
    CheckCode.AMBIGUOUS_LINE_CODE: "неоднозначность кода строки",
    CheckCode.MULTIPLE_SOURCE_CODES: "строка раскрыта несколькими кодами",
    CheckCode.UNKNOWN_LINE_CODE: "код строки отсутствует в справочнике",
    CheckCode.LINE_MAPPING: "сопоставление кодов источника со справочником",
    CheckCode.PERIOD_VALUE_MISMATCH: "расхождение сравнительного значения с отчётным",
    CheckCode.SIGN_CONVENTION_MISMATCH: "соглашение о знаке при равной величине",
    CheckCode.PERIOD_PRIORITY: "приоритет отчётного значения над сравнительным",
    CheckCode.FACT_OVERWRITE: "перезапись ранее загруженного значения",
    CheckCode.AUDIT_OPINION_MODIFIED: "модификация мнения аудитора",
    CheckCode.AUDIT_GOING_CONCERN: "существенная неопределённость о непрерывности",
    CheckCode.AUDIT_STATEMENTS_RESTATED: "пересмотр ранее выпущенной отчётности",
    CheckCode.AUDIT_REPORT_NOT_READABLE: "чтение аудиторского заключения",
    CheckCode.AUDIT_REPORT_ABSENT: "наличие аудиторского заключения",
    CheckCode.AUDIT_REVIEW_ENGAGEMENT: "тип аудиторского задания",
    CheckCode.NOTE_VALUE_NOT_EXTRACTED: "извлечение величины из примечания",
    CheckCode.NOTE_VALUES: "величины, взятые из примечаний",
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
        # Сводка судеб кодов — то же состояние комплекта, что и записи выше,
        # и переписывается вместе с ними: иначе повторная загрузка множила бы
        # счётчики, и число разобранных кодов росло бы от прогона к прогону.
        CheckCode.LINE_MAPPING,
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
