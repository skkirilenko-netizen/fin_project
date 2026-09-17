"""Тесты определения параметров документа МСФО (задача 22).

Тип документа проверяется первым и не случайно: годовой отчёт эмитента
финансовой отчётностью не является, но числа в нём есть, они осмысленны,
и любой параметр в нём «определится». Проверено дорого — однажды вместо
отчётности загрузились пять годовых отчётов.
"""

import re
from datetime import date

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_inbox import (
    DocumentProfile,
    ReportingKind,
    form_blocks,
    form_headings,
    identify,
    limitation_for,
    load_parsing_policy,
)
from finlib.sources.ifrs_numbers import Grouping

# Шапка настоящего комплекта: две формы, валюта, единица, даты, числа
# с однозначной разметкой.
# Шапка формы устроена как в настоящей отчётности: под заголовком стоит
# единица измерения, следом заголовки колонок, и только потом статьи.
# Первая редакция держала единицу в конце документа — на живых файлах
# так не бывает, и приём её там не ищет.
STATEMENTS = """
Консолидированный отчёт о финансовом положении
по состоянию на 31 декабря 2024 года
(в миллионах российских рублей)
                                    31 декабря 2024    31 декабря 2023
Основные средства                         1 234 567          1 100 000
Запасы                                      663 888            452 110
Дебиторская задолженность                   120 500            110 300
Денежные средства и их эквиваленты          200 100            180 200
Итого оборотные активы                    1 000 000            900 000
Итого активы                              2 234 567          2 000 000

Консолидированный отчёт о прибыли или убытке
за год, закончившийся 31 декабря 2024 года
(в миллионах российских рублей)
Выручка                                     507 718            469 004
Себестоимость продаж                       (400 100)          (380 200)
Валовая прибыль                             107 618             88 804
Административные расходы                    (20 300)           (18 100)
Операционная прибыль                         87 318             70 704
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


# --- границы форм в живой вёрстке ---------------------------------------------

# Оглавление, разорванный переносом заголовок и разрыв страницы посреди
# формы — три случая, на которых разбор ошибался молча. Документ собран
# по вёрстке Сегежи и ЛСР: у первой заголовки набраны двумя строками
# и оглавление стоит теми же словами, у второй баланс занимает две страницы.
VERSO = """
Содержание
Консолидированный отчет о финансовом положении 6
Консолидированный отчет о прибыли или убытке 7-8
Примечания к консолидированной финансовой отчетности 9

КОНСОЛИДИРОВАННЫЙ ОТЧЕТ О ФИНАНСОВОМ
ПОЛОЖЕНИИ ПО СОСТОЯНИЮ НА 31 ДЕКАБРЯ 2024 ГОДА
(в миллионах российских рублей)
Прим.
31 декабря
2024 года
31 декабря
2023 года
АКТИВЫ
Основные средства 11 1 234 567 1 100 000
Запасы 12 663 888 452 110
Итого активы 2 234 567 2 000 000
ПАО «Пример»
Консолидированный отчет о финансовом положении по состоянию на 31 декабря 2024 г.
7
Данные раскрываемого консолидированного отчета о финансовом положении должны
рассматриваться в совокупности с пояснениями на стр. 9-75, которые являются
неотъемлемой частью данной отчетности.

В млн руб. Прим. 2024 г. 2023 г.
КАПИТАЛ И ОБЯЗАТЕЛЬСТВА
Акционерный капитал 21 500 000 500 000
Итого капитал и обязательства 2 234 567 2 000 000

КОНСОЛИДИРОВАННЫЙ ОТЧЕТ О ПРИБЫЛИ ИЛИ УБЫТКЕ
ЗА ГОД, ЗАКОНЧИВШИЙСЯ 31 ДЕКАБРЯ 2024 ГОДА
(в миллионах российских рублей)
Выручка 4 507 718 469 004
Себестоимость продаж (400 100) (380 200)
Валовая прибыль 107 618 88 804
Операционная прибыль 87 318 70 704
Прибыль за год 60 000 50 000
"""


def blocks_of(text: str) -> dict[str, list[str]]:
    """Строки таблиц каждой формы для собранного документа."""
    policy = load_parsing_policy()
    headings = form_headings(text, load_ifrs_lines(), policy)
    return form_blocks(text, headings, policy)


def test_wrapped_heading_is_found() -> None:
    """Заголовок, разорванный переносом, опознаётся ядром наименования.

    У Сегежи «О ФИНАНСОВОМ \nПОЛОЖЕНИИ» и «О ДВИЖЕНИИ ДЕНЕЖНЫХ \nСРЕДСТВ»:
    по одной строке такой заголовок не находится вовсе.
    """
    found = blocks_of(body(base=VERSO))
    assert "ifrs.statement_of_financial_position" in found
    balance = found["ifrs.statement_of_financial_position"]
    assert any("Основные средства" in line for line in balance)


def test_table_of_contents_is_not_a_form() -> None:
    """Строка оглавления формой не становится, хотя слова те же.

    Отличает её то, что идёт следом: у формы таблица начинается сразу,
    у оглавления — другие строки оглавления. Номер страницы, записанный
    диапазоном («7-8»), за две величины не считается.
    """
    found = blocks_of(body(base=VERSO))
    for lines in found.values():
        assert not any("Содержание" in line for line in lines)
        assert not any(line.strip().endswith("7-8") for line in lines)


def test_page_break_does_not_end_the_form() -> None:
    """Разрыв страницы посреди формы таблицу не кончает.

    На переломе стоят колонтитул, номер страницы и надпись о пояснениях —
    больше строк без величин, чем допускает разрыв. У ЛСР блок баланса
    обрывался на «Итого активы», и сторона капитала терялась молча.
    """
    lines = blocks_of(body(base=VERSO))["ifrs.statement_of_financial_position"]
    assert any("Акционерный капитал" in line for line in lines)
    assert any("Итого капитал и обязательства" in line for line in lines)


def test_form_block_stops_before_the_next_form() -> None:
    """Блок формы не захватывает следующую форму."""
    lines = blocks_of(body(base=VERSO))["ifrs.statement_of_financial_position"]
    assert not any("Выручка" in line for line in lines)
