"""Тесты разбора колонок по координатам и разведения строк по разделам.

Все случаи найдены сверкой с Cbonds по пяти эмитентам: величины основных
форм у внешнего источника и у нас должны совпадать, а расхождение — иметь
названную причину. Четыре расхождения из семи оказались нашими ошибками,
и здесь они закреплены.
"""

from datetime import date
from decimal import Decimal

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.pdf_text import PdfDocument, PdfPage, TextPiece

DATES = (date(2025, 12, 31), date(2024, 12, 31))

# Отчёт о движении денежных средств Автодора: колонки отделены промежутком,
# а в плоском тексте промежуток тот же, что между разрядами.
FLOWS = """
Консолидированный отчет о движении денежных средств
млн руб. 2025 год 2024 год
Прибыль до налогообложения 10 660 9 048
Амортизация 737 562
Проценты уплаченные 45 668 38 511
Налог на прибыль уплаченный 3 742 4 743
Приобретение основных средств 162 471
Проценты полученные 829 909
"""


def page_with_columns() -> PdfDocument:
    """Страница, у которой колонки стоят на своих местах по координатам."""
    unit = 5.0
    rows = [
        ("млн руб.", ("2025 год", "2024 год")),
        ("Прибыль до налогообложения", ("10 660", "9 048")),
        ("Амортизация", ("737", "562")),
        ("Проценты уплаченные", ("45 668", "38 511")),
        ("Налог на прибыль уплаченный", ("3 742", "4 743")),
        ("Приобретение основных средств", ("162", "471")),
        ("Проценты полученные", ("829", "909")),
    ]
    pieces: list[TextPiece] = []
    for number, (name, values) in enumerate(rows):
        top = 700.0 - number * 14
        pieces.append(TextPiece(name, 70.0, top))
        # Правый край колонки один и тот же, левый зависит от ширины числа.
        for column, value in enumerate(values):
            right = 460.0 + column * 80
            pieces.append(TextPiece(value, right - len(value) * unit, top))
    body = "\n".join(f"{name} {' '.join(values)}" for name, values in rows)
    text = "Консолидированный отчет о движении денежных средств\n" + body
    return PdfDocument((PdfPage(1, text, tuple(pieces)),), extractor="проба")


def test_columns_decide_where_a_value_ends() -> None:
    """Координаты разводят «737 562» на две величины, а плоский текст нет.

    В плоском тексте разделитель разрядов и разделитель колонок — один
    и тот же пробел. У Автодора «Амортизация 737 562» читалось как семьсот
    тридцать семь тысяч, то есть амортизация в шестьдесят процентов активов;
    Cbonds даёт 737, и координаты говорят то же: между «737» и «562»
    семьдесят три пункта, а внутри «737» девять.
    """
    document = page_with_columns()
    found = extract(document.text, DATES, Grouping.RUSSIAN, columns=document.columns_of)
    flows = found.forms["ifrs.statement_of_cash_flows"]
    values = {
        (item.source_name, item.report_date): item.value for item in flows.values
    }
    assert values.get(("Амортизация", DATES[0])) == Decimal(737)
    assert values.get(("Амортизация", DATES[1])) == Decimal(562)


def test_without_coordinates_the_rules_still_apply() -> None:
    """Без координат работает разбор по строению числа: выгрузка их не имеет."""
    found = extract(FLOWS, DATES, Grouping.RUSSIAN)
    flows = found.forms["ifrs.statement_of_cash_flows"]
    assert any(item.code == "ifrs.depreciation" for item in flows.values)


def test_same_name_in_two_sections_is_split_by_section() -> None:
    """«Кредиты и займы» в балансе дважды, и различает их раздел.

    Прежде обе строки опознавались одной позицией, и краткосрочный долг
    затирал долгосрочный: у ЛСР вместо 328 256 в расчёт шло 35 876, и то же
    у Норникеля и Сегежи. Сверка с Cbonds показала это у трёх эмитентов
    из пяти.
    """
    balance = """
Консолидированный отчёт о финансовом положении
Основные средства 83 589 56 128
Итого внеоборотные активы 83 589 56 128
Запасы 310 977 297 715
Итого оборотные активы 310 977 297 715
Итого активы 394 566 353 843
Долгосрочные обязательства
Кредиты и займы 328 256 300 000
Итого долгосрочные обязательства 328 256 300 000
Краткосрочные обязательства
Кредиты и займы 35 876 30 000
Итого краткосрочные обязательства 35 876 30 000
"""
    found = extract(balance, DATES, Grouping.RUSSIAN)
    assert found.value_of("ifrs.long_term_borrowings", DATES[0]) == Decimal(328_256)
    assert found.value_of("ifrs.short_term_borrowings", DATES[0]) == Decimal(35_876)


def test_position_outside_its_section_is_not_recognised() -> None:
    """Статья оборотных активов не опознаётся в разделе внеоборотных.

    У ЛСР «Торговая и прочая дебиторская задолженность» стоит и во
    внеоборотных активах, и в оборотных под другим наименованием; справочник
    знает её оборотной, и в расчёт уходило 1 410 вместо 215 664.
    """
    balance = """
Консолидированный отчёт о финансовом положении
Основные средства 83 589 56 128
Нематериальные активы 3 657 3 644
Торговая и прочая дебиторская задолженность 1 410 2 219
Итого внеоборотные активы 88 656 61 991
Запасы 310 977 297 715
Денежные средства и их эквиваленты 27 724 46 307
Итого оборотные активы 338 701 344 022
Итого активы 427 357 406 013
"""
    found = extract(balance, DATES, Grouping.RUSSIAN)
    assert found.value_of("ifrs.trade_receivables", DATES[0]) is None
    assert any(
        "дебиторская" in row.source_name.lower() for row in found.unrecognised
    )


def test_intermediate_subtotal_is_not_named_by_a_catalog_total() -> None:
    """Промежуточный итог, которого нет в справочнике, чистой прибылью не станет.

    У Автодора «Финансовые доходы (нетто)» равны сумме двух предшествующих
    строк, и структурное правило называло их чистой прибылью: 11 373 вместо
    7 636. Равенства суммы мало — итог обязан узнавать свой состав.
    """
    profit = """
Консолидированный отчёт о прибыли или убытке
Выручка 7 303 7 582
Себестоимость продаж (3 792) (3 949)
Валовая прибыль 3 511 3 633
Административные расходы (4 300) (4 030)
Операционная прибыль (713) 2 272
Финансовые доходы 11 787 7 222
Финансовые расходы (414) (446)
Финансовые доходы (нетто) 11 373 6 776
Прибыль до налогообложения 10 660 9 048
"""
    found = extract(profit, DATES, Grouping.RUSSIAN)
    assert load_ifrs_lines().get("ifrs.profit_for_period") is not None
    assert found.value_of("ifrs.profit_for_period", DATES[0]) is None
    assert any(
        "нетто" in row.source_name for row in found.unrecognised
    )


def test_page_without_text_layer_inside_the_forms_is_reported() -> None:
    """Страница без текстового слоя внутри форм называется, а не молчит.

    У Автодора баланс занимает страницы 8 и 9, слой есть только у восьмой,
    и разбор кончался на «Всего активов» — вся сторона пассива терялась.
    Актив при этом сходился сам с собой, и ни один контроль пропажи
    не замечал.
    """
    document = PdfDocument(
        (
            PdfPage(1, "Консолидированный отчёт о финансовом положении\nЗапасы 1 2"),
            PdfPage(2, "   "),
            PdfPage(3, "Итого активы 3 4"),
        ),
        extractor="проба",
    )
    assert document.pages_without_text == (2,)
    assert document.page_at(0) == 1
