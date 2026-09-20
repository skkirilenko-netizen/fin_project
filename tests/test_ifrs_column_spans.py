"""Тесты длительности граф формы: берутся объявленные, а не последние.

Промежуточный отчёт ФосАгро о прибыли или убытке печатает четыре графы —
полугодие 2026, полугодие 2025, квартал 2026, квартал 2025. Брались последние
две, то есть квартальные, и комплект за полугодие собирался из величин
квартала: прибыль 18 432 вместо 18 653, налог −6 466 вместо −6 766.

Ошибка того же рода, что разделитель разрядов: графа согласована сама
с собой, все итоги по ней сходятся, и неверны только сами величины.

Числа набраны английской конвенцией, как в самом документе ФосАгро: при
русской «281 388 298 556 149 929 139 166» неразличимо, четыре это величины
или две, — и разбирать такую вёрстку в четыре графы нечем.
"""

from datetime import date
from decimal import Decimal

from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_extract import extract, split_row
from finlib.sources.ifrs_inbox import identify
from finlib.sources.ifrs_numbers import ColumnLayout, Grouping

# Шапка как в настоящем документе: сначала заголовок формы, где длительность
# упоминается дважды, потом подписи блоков граф и строка годов.
FOUR_COLUMNS = """
Консолидированный промежуточный сокращенный отчет о прибыли или убытке
за три и шесть месяцев, закончившихся 30 июня 2026 года
(в миллионах российских рублей)
Млн руб. Прим.
Шесть месяцев,
закончившихся
30 июня
Три месяца,
закончившихся
30 июня
2026 2025 2026 2025
Выручка 5 281,388 298,556 149,929 139,166
Себестоимость продаж (194,587) (158,932) (100,381) (77,882)
Валовая прибыль 78,095 126,562 45,683 55,619
Административные расходы 7 (27,303) (21,000) (15,419) (10,719)
Операционная прибыль 50,792 105,562 30,264 44,900
Прибыль до налогообложения 25,419 97,472 24,898 37,378
Расход по налогу на прибыль 11 (6,766) (21,930) (6,466) (9,492)
"""

QUARTER_ONLY = """
Консолидированный промежуточный сокращенный отчет о прибыли или убытке
за три месяца, закончившихся 30 июня 2026 года
(в миллионах российских рублей)
Млн руб. Прим.
Три месяца,
закончившихся
30 июня
2026 2025
Выручка 5 149,929 139,166
Себестоимость продаж (100,381) (77,882)
Валовая прибыль 45,683 55,619
Административные расходы 7 (15,419) (10,719)
Операционная прибыль 30,264 44,900
Прибыль до налогообложения 24,898 37,378
"""

BALANCE = """
Консолидированный промежуточный сокращенный отчет о финансовом положении
по состоянию на 30 июня 2026 года
(в миллионах российских рублей)
Млн руб. Прим.
30 июня
2026 года
31 декабря
2025 года
Основные средства 12 700,000 650,000
Нематериальные активы 13 3,666 3,657
Итого внеоборотные активы 703,666 653,657
Запасы 16 200,000 180,000
Денежные средства и их эквиваленты 18 96,334 86,343
Итого оборотные активы 296,334 266,343
Итого активы 1,000,000 920,000
"""


def body(*parts: str) -> str:
    """Документ нужной длины: порог текстового слоя — две тысячи знаков."""
    padding = "\nПримечания к консолидированной финансовой отчётности.\n" * 40
    return "".join(parts) + padding


def accepted(*parts: str):
    """Профиль документа с заданной вручную конвенцией записи чисел."""
    return identify(body(*parts), grouping=Grouping.ENGLISH)


def test_span_of_each_block_is_read_from_the_header() -> None:
    """Шапка объявляет длительность граф, и берутся графы нашей длительности."""
    profile = accepted(FOUR_COLUMNS, BALANCE)
    assert profile.accepted, getattr(profile, "reason", "")

    layout = profile.layout_of("ifrs.statement_of_profit_or_loss")
    assert layout.total == 4
    assert layout.taken == 2
    assert layout.offset == 0
    assert layout.spans == (6, 3)
    assert layout.months == 6

    # У формы, подписанной полными датами, длительность не объявлена вовсе,
    # и граф столько, сколько дат: величины баланса приведены на дату,
    # а не за период.
    balance = profile.layout_of("ifrs.statement_of_financial_position")
    assert (balance.total, balance.spans) == (2, ())


def test_values_are_taken_from_the_declared_span() -> None:
    """В комплект идут шестимесячные величины, а не квартальные."""
    profile = accepted(FOUR_COLUMNS, BALANCE)
    found = extract(
        body(FOUR_COLUMNS, BALANCE),
        profile.dates_by_form,
        profile.grouping,
        layouts=profile.columns_by_form,
    )
    assert found.value_of("ifrs.revenue", date(2026, 6, 30)) == Decimal(281388)
    assert found.value_of("ifrs.revenue", date(2025, 6, 30)) == Decimal(298556)
    assert found.value_of("ifrs.income_tax", date(2026, 6, 30)) == Decimal(-6766)
    assert found.value_of("ifrs.gross_profit", date(2026, 6, 30)) == Decimal(78095)
    # Квартальных величин в комплекте нет вовсе: они не наш период.
    assert Decimal(149929) not in [item.value for item in found.values]
    # И потерей это не считается: отброшенные графы объявлены шапкой.
    assert found.dropped_values == []


def test_name_keeps_nothing_of_the_other_block() -> None:
    """Наименование кончается там, где начинается первая графа величин.

    Прежде оно кончалось перед **нашей** графой, и величины чужой длительности
    оставались в наименовании: «Расход по налогу на прибыль 11 (6,766)
    (21,930)» справочник не узнавал вовсе.
    """
    layout = ColumnLayout(total=4, taken=2, offset=0, spans=(6, 3), months=6)
    name, values, _, reference, dropped = split_row(
        "Расход по налогу на прибыль 11 (6,766) (21,930) (6,466) (9,492)",
        Grouping.ENGLISH,
        2,
        layout,
    )
    assert name == "Расход по налогу на прибыль"
    assert values == (Decimal(-6766), Decimal(-21930))
    assert reference == (11,)
    assert dropped == ()


def test_quarter_only_form_is_refused() -> None:
    """Графы чужой длительности — отказ, а не выбор.

    Форма, приведённая только за квартал, при отчётной дате 30 июня описывает
    другой период. Взять её величины значило бы выдать квартал за полугодие:
    ошибка не ловится ни одним контролем сходимости.
    """
    found = accepted(QUARTER_ONLY, BALANCE)
    assert not getattr(found, "accepted", False)
    assert found.code is CheckCode.FILE_COLUMN_SPAN_MISMATCH
    # Отказ называет обе длительности, с которыми сверялись графы: и данную
    # отчётной датой, и принятую по виду отчётности. Графы объявили три
    # месяца, а комплект на 30 июня — шесть.
    assert found.details["report_month"] == 6
    assert found.details["forms"][0]["spans"] == [3]


def test_dropped_value_is_named_when_the_header_is_silent() -> None:
    """Граф больше, чем дат, а длительность не объявлена — это нарушение.

    Молча отбросить графу нельзя: отброшенной может оказаться как раз та,
    величины которой нужны. Строка при этом называется вместе с числами —
    по счётчику нарушений не понять, что именно потеряно.
    """
    name, values, _, _, dropped = split_row(
        "Выручка 281,388 298,556 149,929 139,166", Grouping.ENGLISH, 2
    )
    assert name == "Выручка"
    assert values == (Decimal(149929), Decimal(139166))
    assert dropped == (Decimal(281388), Decimal(298556))


def test_note_number_is_not_a_dropped_value() -> None:
    """Номер примечания отброшенной величиной не считается: он ссылка."""
    name, values, _, reference, dropped = split_row(
        "Выручка 5 281,388 298,556", Grouping.ENGLISH, 2
    )
    assert (name, values, reference, dropped) == (
        "Выручка",
        (Decimal(281388), Decimal(298556)),
        (5,),
        (),
    )
