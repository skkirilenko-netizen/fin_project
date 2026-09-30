"""Сроки погашения долга: шапка, чтение строки арифметикой, род строки, сверка.

Куски текста — из разведки 30.09.2026 по шести эмитентам «Разбора» с PDF.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.normalize.ifrs_note_lines import DebtMaturity
from finlib.sources.ifrs_debt_note import (
    Check,
    Refusal,
    header_intervals,
    printed_buckets,
    read_debt_note,
    read_tables,
    reconcile_debt,
    stored_facts,
)
from finlib.sources.ifrs_notes import Note, NoteIndex
from finlib.sources.ifrs_numbers import load_parsing_policy

POLICY = load_parsing_policy().maturity_table


def _method() -> DebtMaturity:
    """Состав из теста, а не из справочника: состав сроков на согласовании."""
    return DebtMaturity.model_validate(
        {
            "found_in": ["ifrs.long_term_borrowings", "ifrs.short_term_borrowings"],
            "lease_lines": [
                "ifrs.long_term_lease_liabilities",
                "ifrs.short_term_lease_liabilities",
            ],
            "buckets": [
                {"code": "within_1y", "name": "до 1 года", "from_months": 0, "to_months": 12},
                {"code": "y1_y2", "name": "от 1 до 2 лет", "from_months": 12, "to_months": 24},
                {"code": "y2_y5", "name": "от 2 до 5 лет", "from_months": 24, "to_months": 60},
                {"code": "after_y5", "name": "свыше 5 лет", "from_months": 60},
            ],
            "rows": {
                "debt": [
                    {"name": "Облигации без обеспечения", "seen_at": "Самолёт"},
                    {"name": "Прочие кредиты и займы", "seen_at": "Самолёт"},
                    {"name": "Обеспеченные банковские кредиты", "seen_at": "ЛСР"},
                    {"name": "Проектное финансирование", "seen_at": "ЛСР"},
                    {"name": "Кредиты и займы", "seen_at": "Сегежа"},
                    {"name": "Облигационные займы", "seen_at": "Автодор"},
                ],
                "lease": [
                    {"name": "Обязательства по договорам долгосрочной аренды",
                     "seen_at": "Самолёт"},
                    {"name": "Обязательство по аренде", "seen_at": "Сегежа"},
                ],
                "other": [
                    {"name": "Торговая и прочая кредиторская задолженность",
                     "seen_at": "Сегежа"},
                ],
                "debt_like": [
                    {"name": "Концессионные соглашения", "seen_at": "Автодор"},
                ],
            },
            "storage": {
                "prefixes": {
                    "undiscounted": {"debt": "ifrs.debt_cf_due",
                                     "debt_like": "ifrs.debt_like_cf_due"},
                    "carrying": {"debt": "ifrs.debt_due", "debt_like": "ifrs.debt_like_due"},
                },
                "open_end": "plus",
                "digits": 3,
            },
            "origin": "тест",
        }
    )  # fmt: skip


def _note(text: str, number: int = 23) -> Note:
    """Примечание на весь текст куска."""
    return Note(number, "Справедливая стоимость и управление рисками", 0, len(text))


SAMOLET = """Ниже представлена информация об оставшихся договорных сроках погашения
31 декабря 2025 года   Денежные потоки по договору
млн руб. Балансовая
стоимость Итого По требо-
ванию  0-6 мес. 6-12 мес.  от 1 до 2
лет
от 2 до 10
лет
Непроизводные финансовые
обязательства
Облигации без обеспечения 79 153 108 165 - 31 451 15 656 35 794 25 264
Прочие кредиты и займы 669 993 853 215 - 149 931 145 273 136 944 421 067
Обязательства по договорам
долгосрочной аренды  632 1 948 -  535  825  344  244
867 172 1 101 507 - 242 098 196 041 176 485 486 883
"""

SEGEZHA = """В таблицах ниже приведены сроки погашения финансовой задолженности Группы:
До
востребо-
вания
0 – 30
дней
31 – 365
дней
От 1
до 5 лет
Более
5 лет
Итого,
включая
выплаты по
финансовым
расходам
Балансовая
стоимость
На 31 декабря 2025 года
Кредиты и займы* -  612  32 817  67 889  -  101 318  70 048
Торговая и прочая креди-
торская задолженность -  7 938  6 316  -  -  14 254  14 254
Обязательство по аренде -  191  3 002  10 958  73 232  87 383  11 806
На 31 декабря 2024 года
Кредиты и займы* -  1 684  79 363  108 028  21 904  210 979  152 421
"""

BRUSNIKA = """значительно раньше по времени или в значительно отличающихся суммах.
31 декабря 2025 г.  Балансо-
вая
стоимость
Потоки
денежных
средств по
договору  до 1 года  1-2 года  2-3 года  3 и более млн руб.
Облигации без обеспечения 290 950  417 257  88 292  155 759  54 642  118 564
Обязательство по аренде 5 786  8 764  2 271  2 036  1 871  2 586
"""

LSR = """Ниже представлена информация о договорных сроках погашения финансовых обязательств,
31 декабря
2025 г.  Средняя процентная ставка
В млн руб.  По договору
Эффектив-
ная
Менее
одного
года
от 1 до
5 лет
Свыше
5 лет  Итого
Обеспеченные банковские кредиты
в руб.*  13,90% - 20,95%  14,43%  3 800  14 580  1 402  19 782
в руб.
Ключевая ставка
ЦБ - Ключевая
ставка ЦБ + 6,14%  18,74%  10 335  70 680  1 333  82 348
Проектное финансирование
в руб.*  0,01% - 16,55%  16,58%  766  76 576  -  77 342
"""


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # Брусника: шапка одной строкой, «3 и более» без единицы.
        (
            "договору  до 1 года  1-2 года  2-3 года  3 и более млн руб.",
            [(0, 12), (12, 24), (24, 36), (36, None)],
        ),
        # О'КЕЙ: «до востребования» слито со следующим сроком.
        (
            "До востребо-\nвания и в\nсрок до\n6 месяцев\nОт\n6 до 12\nмесяцев\n"
            "От 1 года\nдо 5 лет\nСвыше\n5 лет",
            [(0, 6), (6, 12), (12, 60), (60, None)],
        ),
        # Автодор: месяцы, затем годы.
        (
            "Менее\n2 мес.  2-12 мес.\nот 1 до 2\nлет\nот 2 до 5\nлет\nСвыше\n5 лет",
            [(0, 2), (2, 12), (12, 24), (24, 60), (60, None)],
        ),
        # Сегежа: дни, переведённые в месяцы.
        (
            "До\nвостребо-\nвания\n0 – 30\nдней\n31 – 365\nдней\nОт 1\nдо 5 лет\nБолее\n5 лет",
            [(0, 0), (0, 1), (1, 12), (12, 60), (60, None)],
        ),
        # ЛСР: числительное словом.
        (
            "Менее\nодного\nгода\nот 1 до\n5 лет\nСвыше\n5 лет  Итого",
            [(0, 12), (12, 60), (60, None)],
        ),
    ],
)
def test_header_intervals_are_read_in_months(header: str, expected: list) -> None:
    """Графы шапки — месяцы от отчётной даты, подряд от нуля."""
    found = header_intervals(header, POLICY)
    assert [(item.start, item.end) for item in found] == expected


def test_intervals_with_a_gap_are_not_a_header() -> None:
    """Графы с разрывом — не шапка сроков: угадывать пропущенную нельзя."""
    assert header_intervals("до 1 года  2-3 года", POLICY) == ()


def test_a_row_is_read_by_its_arithmetic() -> None:
    """Разряды и графы разделены одиночным пробелом: чтение выбирает равенство итога корзинам."""
    method = _method()
    (table,) = read_tables(_note(SAMOLET), SAMOLET, (date(2025, 12, 31),), method.rows, POLICY)
    assert table.report_date == date(2025, 12, 31)
    assert table.layout == "carrying_total_buckets"
    bonds, loans, lease, total = table.rows
    assert (bonds.name, bonds.kind, bonds.carrying, bonds.total) == (
        "Облигации без обеспечения", "debt", Decimal(79153), Decimal(108165),
    )  # fmt: skip
    assert bonds.buckets == (None, Decimal(31451), Decimal(15656), Decimal(35794), Decimal(25264))
    assert loans.carrying == Decimal(669993)
    assert (lease.kind, lease.carrying) == ("lease", Decimal(632))
    # Безымянная строка — итог таблицы, а не наследник рода строки над ней.
    assert total.kind == "total"
    assert table.carrying_of("debt") == Decimal(749146)


def test_the_carrying_column_after_the_total_is_found() -> None:
    """Сегежа: графа балансовой стоимости последняя; новая дата продолжает шапку."""
    method = _method()
    tables = read_tables(
        _note(SEGEZHA, 25), SEGEZHA, (date(2025, 12, 31), date(2024, 12, 31)), method.rows, POLICY
    )
    assert [item.report_date for item in tables] == [date(2025, 12, 31), date(2024, 12, 31)]
    current = tables[0]
    assert current.layout == "buckets_total_carrying"
    assert current.carrying_of("debt") == Decimal(70048)
    # Перенос по слогам склеен, и строка опознана как прочее, а не долг.
    assert [row.kind for row in current.rows] == ["debt", "other", "lease"]


def test_a_rate_only_row_inherits_the_group_kind() -> None:
    """ЛСР: «в руб.» под заголовком группы — строка того же вида долга."""
    method = _method()
    (table,) = read_tables(_note(LSR, 26), LSR, (date(2025, 12, 31),), method.rows, POLICY)
    assert table.layout == "buckets_total"
    assert not table.has_carrying
    assert [(row.name, row.kind, row.total) for row in table.rows] == [
        ("Обеспеченные банковские кредиты", "debt", Decimal(19782)),
        ("Обеспеченные банковские кредиты", "debt", Decimal(82348)),
        ("Проектное финансирование", "debt", Decimal(77342)),
    ]


def test_reconciliation_outcomes() -> None:
    """Сошлось; сошлось только с арендой; не сошлось; графы балансовой нет."""
    method = _method()
    (table,) = read_tables(_note(SAMOLET), SAMOLET, (date(2025, 12, 31),), method.rows, POLICY)
    reference = {
        "ifrs.long_term_borrowings": Decimal(451417),
        "ifrs.short_term_borrowings": Decimal(297729),
        "ifrs.long_term_lease_liabilities": Decimal(400),
        "ifrs.short_term_lease_liabilities": Decimal(232),
    }
    passed = reconcile_debt(table, "385", reference, "385", "данные агрегатора", method)
    assert passed.outcome is Check.PASSED
    # Агрегатор в тысячах: допуск — единица более грубой стороны.
    thousands = {code: value * 1000 + 400 for code, value in reference.items()}
    assert reconcile_debt(table, "385", thousands, "384", "агрегатор", method).passed
    lower = dict(reference, **{"ifrs.short_term_borrowings": Decimal(297097)})
    with_lease = reconcile_debt(table, "385", lower, "385", "баланс документа", method)
    assert with_lease.outcome is Check.WITH_LEASE
    missed = dict(reference, **{"ifrs.short_term_borrowings": Decimal(1)})
    assert reconcile_debt(table, "385", missed, "385", "агрегатор", method).outcome is Check.FAILED
    partial = dict(reference, **{"ifrs.short_term_borrowings": None})
    assert (
        reconcile_debt(table, "385", partial, "385", "агрегатор", method).outcome
        is Check.NO_REFERENCE
    )
    (lsr,) = read_tables(_note(LSR, 26), LSR, (date(2025, 12, 31),), method.rows, POLICY)
    outcome = reconcile_debt(lsr, "385", reference, "385", "баланс", method).outcome
    assert outcome is Check.NO_CARRYING


def test_a_bucket_is_printed_only_when_covered_whole() -> None:
    """Графы, покрывающие корзину, складываются; пересекающая — как напечатана."""
    method = _method()
    (table,) = read_tables(_note(SAMOLET), SAMOLET, (date(2025, 12, 31),), method.rows, POLICY)
    printed = printed_buckets(table, method)
    assert [(item.name, item.value, item.as_printed, item.code) for item in printed] == [
        (
            "до 1 года", Decimal(31451 + 15656 + 149931 + 145273), False,
            "ifrs.debt_cf_due_m000_m012",
        ),
        ("от 1 до 2 лет", Decimal(35794 + 136944), False, "ifrs.debt_cf_due_m012_m024"),
        ("от 2 до 10 лет", Decimal(25264 + 421067), True, "ifrs.debt_cf_due_m024_m120"),
    ]  # fmt: skip
    (partial,) = read_tables(
        _note(BRUSNIKA, 25), BRUSNIKA, (date(2025, 12, 31),), method.rows, POLICY
    )
    names = [item.name for item in printed_buckets(partial, method)]
    # «2–3 года» не выдаётся за «от 2 до 5 лет»: корзина покрыта не целиком.
    assert names == ["до 1 года", "от 1 до 2 лет", "2-3 года", "3 и более"]


AVTODOR = """Ниже представлена информация об оставшихся договорных сроках погашения
млн руб.  Денежные потоки по договору
2025 год Балансовая
стоимость Итого
Менее
2 мес.  2-12 мес.
от 1 до 2
лет
от 2 до 5
лет
Свыше
5 лет
Облигационные займы 798 411 1 216 284 7 734 103 226 64 549 189 625 851 150
Концессионные
соглашения 53 911 135 205 2 194 10 724 7 737 31 179 83 371
"""


def test_graphs_are_stored_as_printed() -> None:
    """Код хранения — основа, род и границы графы; прочерк к хранению не идёт."""
    method = _method()
    (table,) = read_tables(_note(SAMOLET), SAMOLET, (date(2025, 12, 31),), method.rows, POLICY)
    codes = {fact.code: fact.value for fact in stored_facts(table, method)}
    # «По требованию» у обеих строк займов — прочерк: не ноль и не хранится.
    assert "ifrs.debt_cf_due_m000_m000" not in codes
    assert codes["ifrs.debt_cf_due_m000_m006"] == Decimal(31451 + 149931)
    assert codes["ifrs.debt_cf_due_m024_m120"] == Decimal(25264 + 421067)
    assert codes["ifrs.debt_due_m000_plus"] == Decimal(749146)


def test_debt_like_is_a_second_value_and_the_sum_is_reconciled() -> None:
    """Автодор: займы и «займы + долгоподобные»; сверяется сумма обеих."""
    method = _method()
    (table,) = read_tables(
        _note(AVTODOR, 27), AVTODOR, (date(2025, 12, 31),), method.rows, POLICY
    )
    both = {
        "ifrs.long_term_borrowings": Decimal(800000),
        "ifrs.short_term_borrowings": Decimal(52322),
    }
    found = reconcile_debt(table, "385", both, "385", "баланс документа", method)
    assert found.outcome is Check.PASSED
    assert (found.loans, found.table_value) == (Decimal(798411), Decimal(852322))
    assert found.debt_like == ("Концессионные соглашения",)
    # Опора — только займы (агрегатор у Автодора: ровно облигации).
    loans = {
        "ifrs.long_term_borrowings": Decimal(687451),
        "ifrs.short_term_borrowings": Decimal(110960),
    }
    only = reconcile_debt(table, "385", loans, "385", "данные агрегатора", method)
    assert only.outcome is Check.LOANS_ONLY
    assert only.loans_passed and not only.passed
    combined = printed_buckets(table, method, ("debt", "debt_like"))
    assert combined[-1].code == "ifrs.debt_cf_due_m060_plus + ifrs.debt_like_cf_due_m060_plus"
    assert combined[-1].value == Decimal(851150 + 83371)


AVTODOR_NOTE_20 = """20 Заемные средства и обязательства по долгосрочным инвестиционным и
концессионным соглашениям
млн руб.
31 декабря
2025 года
31 декабря
2024 года
Долгосрочные обязательства
Облигационные займы  687 451 778 293
Всего долгосрочных обязательств  748 326 847 678
Всего краткосрочных обязательств  128 880 62 521
Итого заемных средств  877 206 910 199
"""


def test_note_total_is_the_fallback_reference() -> None:
    """Решение 30.09.2026: баланс не прочитан — опора сверки итог примечания,
    опознанного полным наименованием из оглавления."""
    from finlib.normalize.ifrs_note_lines import BalanceFallback
    from finlib.sources.ifrs_debt_note import note_total
    from finlib.sources.ifrs_notes import ContentsEntry
    from finlib.sources.ifrs_numbers import Grouping

    fallback = BalanceFallback.model_validate(
        {
            "note_titles": [
                {"name": "Заемные средства и обязательства по долгосрочным инвестиционным "
                 "и концессионным соглашениям", "seen_at": "Автодор"}
            ],
            "total_names": [{"name": "Итого заемных средств", "seen_at": "Автодор"}],
        }
    )  # fmt: skip
    method = _method().model_copy(update={"balance_fallback": fallback})
    note = Note(
        20, "Заемные средства и обязательства по долгосрочным инвестиционным и",
        0, len(AVTODOR_NOTE_20),
    )  # fmt: skip
    full = ContentsEntry(
        20, "Заемные средства и обязательства по долгосрочным инвестиционным "
        "и концессионным соглашениям", 38,
    )  # fmt: skip
    index = NoteIndex(notes=(note,), contents=(full,))
    found = note_total(AVTODOR_NOTE_20, index, method, Grouping.RUSSIAN, 2)
    assert found is not None
    assert (found[0].number, found[1]) == (20, Decimal(877206))
    # Заголовок из указателя обрезан переносом: без оглавления совпадения нет.
    bare = NoteIndex(notes=(note,))
    assert note_total(AVTODOR_NOTE_20, bare, method, Grouping.RUSSIAN, 2) is None
    # Без запасной опоры в методике — нет и опоры.
    assert note_total(AVTODOR_NOTE_20, index, _method(), Grouping.RUSSIAN, 2) is None

    (table,) = read_tables(
        _note(AVTODOR, 27), AVTODOR, (date(2025, 12, 31),), method.rows, POLICY
    )
    check = reconcile_debt(
        table, "385", {}, "385", "итог примечания 20", method, borrowed=Decimal(852322)
    )
    assert check.outcome is Check.PASSED


def test_without_the_approved_composition_the_reading_refuses() -> None:
    """Состава сроков в методике нет — отказ, а не умолчание."""
    found = read_debt_note(
        "", NoteIndex(), None, date(2025, 12, 31), (), None, POLICY
    )
    assert found.refusal is Refusal.NO_POLICY
    assert found.table is None
