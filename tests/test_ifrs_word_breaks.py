"""Тесты склейки слова, разорванного извлекателем.

У ФосАгро pypdf отдаёт «Г руппы» одним куском вместе с пробелом: разрыв
внутри куска, а не между кусками, поэтому координаты здесь не помогают.
Наименование «Себестоимость реализованной продукции Г руппы» не опознаётся
ни справочником, ни ранее подтверждённым, а это 194 587 — двадцать восемь
процентов валюты баланса.

Склеивается только то, что словом не бывает: перечень однобуквенных слов
закрыт и объявлен методикой. Испорченное наименование хуже неопознанного —
оно опознаётся неверно, и потому «В составе» остаётся как есть.
"""

from datetime import date
from decimal import Decimal

from finlib.sources.ifrs_extract import extract, glue_word_breaks
from finlib.sources.ifrs_numbers import Grouping, load_parsing_policy

DATES = (date(2025, 12, 31), date(2024, 12, 31))


def letters() -> frozenset[str]:
    """Однобуквенные слова методики."""
    return frozenset(
        item.casefold()
        for item in load_parsing_policy().word_breaks.single_letter_words
    )


def test_lone_letter_is_glued_to_the_word() -> None:
    """Буква, которой нет как слова, склеивается со следующим словом."""
    assert glue_word_breaks("Г руппы", letters()) == "Группы"
    assert (
        glue_word_breaks("Себестоимость реализованной продукции Г руппы", letters())
        == "Себестоимость реализованной продукции Группы"
    )


def test_single_letter_word_is_never_glued() -> None:
    """Предлог и союз словом являются, и склеивать их нельзя.

    Отличить разрыв от предлога нечем, а испорченное наименование хуже
    неопознанного: «Всоставе расходов» не опознается никогда и человеку
    покажется бессмыслицей.
    """
    for name in (
        "В составе расходов на персонал",
        "С тавка налога на прибыль",
        "Прочие доходы и расходы",
        "Расходы по аренде и обслуживанию",
    ):
        assert glue_word_breaks(name, letters()) == name


def test_digits_are_not_a_word_break() -> None:
    """Цифра словом не бывает и разрывом слова не считается."""
    assert glue_word_breaks("Выручка 5", letters()) == "Выручка 5"
    assert glue_word_breaks("За 6 месяцев", letters()) == "За 6 месяцев"


def test_broken_name_is_recognised_after_the_glue() -> None:
    """Разбор склеивает разрыв, и статья опознаётся справочником."""
    text = """
Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка 1 200 000 1 100 000
Себестоимость реализованной п родукции (800 000) (750 000)
Валовая прибыль 400 000 350 000
Операционная прибыль 300 000 260 000
Прибыль до налогообложения 260 000 220 000
"""
    found = extract(text, DATES, Grouping.RUSSIAN)
    assert found.value_of("ifrs.cost_of_sales", DATES[0]) == Decimal(-800000)
    assert not [row.source_name for row in found.unrecognised]
