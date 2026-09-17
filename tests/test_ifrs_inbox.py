"""Тесты определения параметров документа МСФО (задача 22).

Тип документа проверяется первым и не случайно: годовой отчёт эмитента
финансовой отчётностью не является, но числа в нём есть, они осмысленны,
и любой параметр в нём «определится». Проверено дорого — однажды вместо
отчётности загрузились пять годовых отчётов.
"""

import re
from datetime import date

from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_inbox import (
    DocumentProfile,
    ReportingKind,
    identify,
    limitation_for,
)
from finlib.sources.ifrs_numbers import Grouping

# Шапка настоящего комплекта: две формы, валюта, единица, даты, числа
# с однозначной разметкой.
STATEMENTS = """
Консолидированный отчёт о финансовом положении
по состоянию на 31 декабря 2024 года
(в миллионах российских рублей)

                                    31 декабря 2024    31 декабря 2023
Основные средства                         1 234 567          1 100 000
Запасы                                      663 888            452 110
Итого оборотные активы                    1 000 000            900 000
Итого активы                              2 234 567          2 000 000

Консолидированный отчёт о прибыли или убытке
за год, закончившийся 31 декабря 2024 года

Выручка                                     507 718            469 004
Прибыль на акцию                               12,5               11,3
Рентабельность                                 0,31               0,28
"""


def body(extra: str = "", base: str = STATEMENTS) -> str:
    """Документ нужной длины: порог текстового слоя — две тысячи знаков."""
    padding = "\nПримечания к консолидированной финансовой отчётности.\n" * 40
    return base + extra + padding


# --- порядок проверок ---------------------------------------------------------


def test_annual_report_is_refused_before_anything_else() -> None:
    """Годовой отчёт эмитента отсекается сразу, с внятной причиной.

    В нём есть и числа, и валюта, и единица измерения — всё «определилось»
    бы. Отсекает именно отсутствие форм, и проверка стоит первой.
    """
    annual = body(
        base="""
        Годовой отчёт публичного акционерного общества за 2024 год
        (в миллионах российских рублей)

        Обращение председателя совета директоров
        Стратегия развития и устойчивое развитие
        Выручка группы составила 507 718 млн рублей против 469 004 млн рублей
        Обзор рынка по состоянию на 31 декабря 2024 года и 31 декабря 2023 года
        """
    )
    found = identify(annual)
    assert not found.accepted
    assert found.code is CheckCode.FILE_NOT_STATEMENTS
    assert "не консолидированная финансовая отчётность" in found.reason
    assert "годовой отчёт" in found.reason


def test_scan_is_refused_before_document_kind() -> None:
    """Документ без текстового слоя отклоняется прежде всех прочих проверок.

    У промежуточной отчётности Автодора pdftotext извлекает 26 байт:
    определять в ней тип документа не из чего.
    """
    found = identify("Отчёт о финансовом положении")
    assert not found.accepted
    assert found.code is CheckCode.FILE_TEXT_LAYER_MISSING
    assert "скан" in found.reason


def test_financial_institution_is_refused_before_currency() -> None:
    """Финансовая организация отсекается до разбора остального.

    У неё неклассифицированный баланс и свои показатели; оценивать её
    по методике для нефинансовых нельзя, и притворяться, что можно, — тоже.
    """
    leasing = body(
        "\nЧистые инвестиции в лизинг      1 234 567      1 100 000\n"
    )
    found = identify(leasing)
    assert not found.accepted
    assert found.code is CheckCode.FINANCIAL_INSTITUTION
    assert "не реализована" in found.reason


# --- валюта -------------------------------------------------------------------


def test_foreign_currency_is_out_of_scope() -> None:
    """Отчётность не в рублях — отказ, а не пересчёт по курсу.

    Пересчёт был бы нашим допущением поверх отчётности эмитента.
    """
    in_dollars = body(base=STATEMENTS.replace("российских рублей", "долларах США"))
    found = identify(in_dollars)
    assert not found.accepted
    assert found.code is CheckCode.FILE_CURRENCY_NOT_ROUBLE
    assert found.details["currency"] == "USD"


def test_currency_must_be_declared() -> None:
    """Рубль по умолчанию не принимается: в долларах отчитываются и наши."""
    silent = body(base=STATEMENTS.replace("(в миллионах российских рублей)", ""))
    found = identify(silent)
    assert not found.accepted
    assert found.code is CheckCode.FILE_CURRENCY_NOT_DETERMINED


# --- единица измерения ---------------------------------------------------------


def test_unit_is_taken_from_the_header() -> None:
    """Единица объявлена документом, а не выведена из формы."""
    found = identify(body())
    assert found.accepted
    assert found.unit_code == "385"


def test_unit_must_be_declared() -> None:
    """Без единицы измерения документ не принимается.

    Ошибка в тысячу раз не ловится ни одним контролем сходимости — то же
    основание, что у разделителя разрядов.
    """
    silent = body(base=STATEMENTS.replace("в миллионах российских рублей", "в рублях"))
    found = identify(silent)
    assert not found.accepted
    assert found.code is CheckCode.UNIT_NOT_DETERMINED


# --- периоды -------------------------------------------------------------------


def test_periods_are_taken_from_the_document() -> None:
    """Число периодов переменное и берётся из документа.

    Норникель даёт три периода, остальные разобранные эмитенты два;
    заранее заданное число отбросило бы колонку.
    """
    found = identify(body())
    assert found.accepted
    assert found.report_dates[0] == date(2024, 12, 31)
    assert date(2023, 12, 31) in found.report_dates


def test_three_periods_are_accepted() -> None:
    """Третий период не отбрасывается: у Норникеля их именно три."""
    wide = body(
        base=STATEMENTS.replace(
            "31 декабря 2024    31 декабря 2023",
            "31 декабря 2024    31 декабря 2023    31 декабря 2022",
        )
    )
    found = identify(wide)
    assert found.accepted
    assert len(found.report_dates) >= 3


def test_document_without_dates_is_refused() -> None:
    """Без отчётных дат неизвестно, к какому периоду относятся величины."""
    undated = body(base=re.sub(r"\d{1,2} \w+ \d{4}( года)?", "отчётная дата", STATEMENTS))
    found = identify(undated)
    assert not found.accepted
    assert found.code is CheckCode.FILE_PERIODS_NOT_DETERMINED


# --- вид отчётности ------------------------------------------------------------


def test_full_reporting_is_the_default() -> None:
    """Полная годовая отчётность маркеров не несёт, и умолчание неопасно."""
    found = identify(body())
    assert found.accepted
    assert found.reporting_kind is ReportingKind.FULL
    assert limitation_for(ReportingKind.FULL) is None


def test_interim_reporting_is_recognised_and_limited() -> None:
    """Промежуточная отчётность опознаётся и влечёт обязательную оговорку."""
    interim = body(
        "\nПромежуточная сокращённая консолидированная финансовая отчётность\n"
        "подготовлена в соответствии с МСФО (IAS) 34\n"
    )
    found = identify(interim)
    assert found.accepted
    assert found.reporting_kind is ReportingKind.INTERIM
    note = limitation_for(ReportingKind.INTERIM)
    assert note and "аннуализац" in note


def test_special_purpose_reporting_is_recognised() -> None:
    """Отчётность специального назначения объявляет себя сама."""
    special = body("\nФинансовая отчётность специального назначения\n")
    found = identify(special)
    assert found.accepted
    assert found.reporting_kind is ReportingKind.SPECIAL_PURPOSE
    assert limitation_for(ReportingKind.SPECIAL_PURPOSE)


# --- принятый документ ---------------------------------------------------------


def test_accepted_document_carries_every_parameter() -> None:
    """У принятого документа определены все параметры, ни одного по умолчанию."""
    found = identify(body())
    assert isinstance(found, DocumentProfile)
    assert found.currency == "RUB"
    assert found.unit_code
    assert found.grouping is Grouping.RUSSIAN
    assert found.report_dates
    assert found.reporting_kind
    assert found.grouping_detection.determined
    assert "формы" in found.describe()


def test_grouping_is_determined_after_the_document_kind() -> None:
    """Разделитель разрядов определяется у документа, уже признанного отчётностью.

    Иначе конвенция определялась бы по числам годового отчёта — и определилась
    бы, потому что числа там настоящие.
    """
    found = identify(body())
    assert found.accepted
    assert found.grouping_detection.russian_evidence > 0
