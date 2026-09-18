"""Тесты указателя примечаний и перехода по ссылке из строки формы.

Случаи взяты с живых комплектов: у ФосАгро «Амортизация 6, 7» ссылается
на два примечания сразу, у Норникеля слово «амортизация» стоит и в износе
основных средств, и в амортизации дисконта по оценочным обязательствам —
это финансовый расход, и поиск по наименованию по всему документу даёт
не отсутствие числа, а чужое число.
"""

from datetime import date
from decimal import Decimal

from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_notes import find_in_note, index_notes, lines_of, references_in
from finlib.sources.ifrs_numbers import Grouping

DATES = (date(2024, 12, 31), date(2023, 12, 31))

# Примечания с оглавлением: случай Автодора и Сегежи.
DOCUMENT = """
Содержание
6 Себестоимость 20
7 Административные расходы 21
10 Финансовые доходы и расходы 24
12 Основные средства 26
26 Оценочные обязательства 40

6 Себестоимость
Амортизация основных средств 111 52
Материалы 300 280

7 Административные расходы
Амортизация активов в форме права пользования 548 441

10 Финансовые доходы и расходы
Процентный расход по кредитам и облигациям 21 694 14 530
Процентный расход по обязательствам аренды 693 429

12 Основные средства
Накопленная амортизация 5 000 4 600

12 Основные средства (продолжение)
Поступления 700 650

26 Оценочные обязательства
Амортизация дисконта 49 29
"""


def test_headings_are_found_and_counted() -> None:
    """Примечания опознаются по номеру и наименованию."""
    index = index_notes(DOCUMENT)
    assert [item.number for item in index.notes] == [6, 7, 10, 12, 26]
    assert index.get(10) is not None
    assert index.get(11) is None


def test_continuation_does_not_start_a_new_note() -> None:
    """Заголовок, повторённый на следующей странице, — то же примечание."""
    index = index_notes(DOCUMENT)
    assert [item.number for item in index.notes].count(12) == 1
    lines = lines_of(index.get(12), DOCUMENT)
    assert any("Поступления" in line for line in lines)


def test_contents_is_read_and_reconciled() -> None:
    """Оглавление читается и сверяется с найденным: обе стороны названы."""
    index = index_notes(DOCUMENT)
    assert index.has_contents
    assert [item.number for item in index.contents] == [6, 7, 10, 12, 26]
    assert index.missing == ()
    assert index.unexpected == ()


def test_note_declared_but_absent_is_reported() -> None:
    """Примечание, объявленное оглавлением и не найденное, — потеря.

    Ровно так выглядит примечание, попавшее на страницу без текстового слоя:
    в тексте его нет, и узнать о нём можно только из оглавления.
    """
    without = DOCUMENT.replace("10 Финансовые доходы и расходы\n", "", 1)
    index = index_notes(without)
    assert [item.number for item in index.missing] == [10]
    assert "не найдено 1" in index.describe()


def test_value_is_taken_only_from_the_named_note() -> None:
    """Величина берётся только из названного примечания — и ниоткуда больше.

    Слово «амортизация» стоит в документе пять раз и означает разное:
    износ основных средств, амортизацию права пользования, накопленную
    амортизацию и амортизацию дисконта по оценочным обязательствам.
    Поиск по наименованию в пределах документа даёт не отсутствие числа,
    а чужое: у Норникеля амортизация дисконта — финансовый расход.
    """
    index = index_notes(DOCUMENT)
    found = find_in_note(index, 6, ("Амортизация основных средств",), DOCUMENT)
    assert found and "111" in found[0]
    # В примечании об оценочных обязательствах этой строки нет, и модуль
    # не идёт искать её в другом месте документа.
    assert find_in_note(index, 26, ("Амортизация основных средств",), DOCUMENT) == ()
    # Примечания нет вовсе — отказ, а не ноль.
    assert find_in_note(index, 99, ("Амортизация основных средств",), DOCUMENT) == ()


def test_false_heading_is_not_a_note() -> None:
    """Проза, начатая числом, примечанием не становится.

    У Сегежи «30 млн руб. (2024 год: 33 млн руб.)» занимало номер 30,
    и настоящие примечания 28 и 29 после него уже не принимались.
    """
    text = DOCUMENT + "\n30 млн руб. (2024 год: 33 млн руб.)\n"
    index = index_notes(text)
    assert index.get(30) is None


def test_reference_is_read_from_the_form_row() -> None:
    """Номер примечания при наименовании — ссылка, и она не теряется."""
    form = """
Консолидированный отчёт о движении денежных средств
Прибыль до налогообложения 100 000 90 000
Амортизация 6, 7 40 712 36 546
Проценты уплаченные 23 400 18 200
Налог на прибыль уплаченный 12 000 10 000
Чистый денежный поток от операционной деятельности 93 114 80 000
Денежные средства и их эквиваленты на конец периода 14 681 10 398
"""
    found = extract(form, DATES, Grouping.RUSSIAN)
    row = next(
        item
        for item in found.values
        if item.code == "ifrs.depreciation" and item.report_date == DATES[0]
    )
    assert row.value == Decimal(40712)
    assert set(row.note_reference) == {6, 7}
    assert row.source_name == "Амортизация"


def test_reference_in_brackets_is_read() -> None:
    """Ссылка «(прим. 21)» читается наравне с номером при наименовании."""
    assert references_in("Процентный расход по кредитам и облигациям (прим. 21)") == (21,)
    assert references_in("Амортизация 6, 7") == (6, 7)
    assert references_in("Выручка") == ()
