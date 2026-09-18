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


# --- строки примечаний и отказ вместо суррогата --------------------------------

NOTE_TABLE = """
Консолидированный отчёт о прибыли или убытке
Выручка 500 000 480 000
Себестоимость продаж 300 000 290 000
Валовая прибыль 200 000 190 000
Операционная прибыль 150 000 140 000
Финансовые расходы 2 414 380
Прибыль до налогообложения 138 586 130 000

2 Финансовые доходы и расходы
Проценты по концессионным и долговым инвестиционным соглашениям 11 699 12 151
Проценты по облигационным займам 42 683 54 650
Прочие финансовые расходы 374 408
Общая сумма финансовых расходов 54 756 67 209
"""


def _accrued(text: str, references: tuple[int, ...]):
    """Начисленные проценты по ссылке из строки формы."""
    from finlib.normalize.ifrs_note_lines import load_note_lines
    from finlib.sources.ifrs_notes import value_from_notes

    catalog = load_note_lines()
    line = catalog.get("ifrs.interest_expense_accrued")
    index = index_notes(text)
    return value_from_notes(line, index, references, text, Grouping.RUSSIAN, 2)


def test_note_lines_are_summed_within_the_named_note() -> None:
    """Две строки примечания складываются: взять одну значило бы занизить.

    У Автодора начисленные проценты раскрыты двумя строками — по
    концессионным соглашениям и по облигационным займам.
    """
    found = _accrued(NOTE_TABLE, (2,))
    assert found.found
    assert found.value == Decimal(11699) + Decimal(42683)
    assert found.note == 2
    assert len(found.rows) == 2


def test_refusal_instead_of_the_value_from_the_form() -> None:
    """Нет строки в примечании — отказ, а не величина из формы.

    Правило то же, что в РСБУ при отсутствии амортизации: показатель
    не считается, а не подменяется тем, что лежит рядом.
    """
    without = NOTE_TABLE.replace(
        "Проценты по концессионным и долговым инвестиционным соглашениям 11 699 12 151\n",
        "",
    ).replace("Проценты по облигационным займам 42 683 54 650\n", "")
    found = _accrued(without, (2,))
    assert not found.found
    assert found.value is None
    assert found.refusal is not None


def test_refusal_when_the_note_is_not_found() -> None:
    """Примечания нет — отказ с собственной причиной, не с чужой."""
    from finlib.sources.ifrs_notes import Refusal

    assert _accrued(NOTE_TABLE, (99,)).refusal is Refusal.NOTE_NOT_FOUND
    assert _accrued(NOTE_TABLE, ()).refusal is Refusal.NO_REFERENCE


def test_reference_inside_the_name_does_not_break_recognition() -> None:
    """«(прим. 21)» в наименовании — разметка, а не часть наименования."""
    text = NOTE_TABLE.replace(
        "Проценты по облигационным займам 42 683",
        "Проценты по облигационным займам (прим. 21) 42 683",
    )
    assert _accrued(text, (2,)).value == Decimal(11699) + Decimal(42683)


def test_wrapped_contents_entry_is_read() -> None:
    """Запись оглавления, перенесённая на вторую строку, читается.

    У Автодора «20 Заемные средства и обязательства по долгосрочным
    инвестиционным и» / «концессионным соглашениям 38» без склейки
    не читалась вовсе, и примечание числилось необъявленным — то есть
    мнимая потеря заслоняла бы настоящую.
    """
    text = DOCUMENT.replace(
        "26 Оценочные обязательства 40",
        "26 Оценочные обязательства и обязательства по долгосрочным\n"
        "инвестиционным соглашениям 40",
    )
    index = index_notes(text)
    assert 26 in {item.number for item in index.contents}


def test_source_note_names_the_note_and_its_number() -> None:
    """Оговорка об источнике называет и номер примечания, и наименование."""
    found = _accrued(NOTE_TABLE, (2,))
    text = found.source_note("54 382", "414", "Финансовые расходы")
    assert "примечания 2" in text
    assert "Финансовые доходы и расходы" in text
    assert "54 382" in text and "414" in text


def test_source_note_of_a_refusal_says_the_value_is_not_substituted() -> None:
    """При отказе оговорка говорит, что величина из формы не берётся."""
    text = _accrued(NOTE_TABLE, (99,)).source_note("", "934", "Финансовые расходы")
    assert "не рассчитан" in text
    assert "не берётся" in text
