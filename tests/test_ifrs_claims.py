"""Тесты правила «одна позиция — одна величина».

Случаи взяты с живых комплектов. У ЛСР «Эмиссионный доход» 26 408 и
«Добавочный капитал» 16 849 легли на один код, словарь оставил последнюю
величину, и капитал не сходился ровно на затёртую. У Норникеля «Прибыль
за год» приходит дважды одной и той же суммой — сложение удвоило бы её.
У ЛСР же дебиторская задолженность 1 410 из внеоборотных активов была
размечена кодом оборотных: правило раздела действовало у автомата
и не действовало у человека.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_claims import Claim, Fold, Refusal, fold
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import DocumentProfile, ReportingKind
from finlib.sources.ifrs_markup import (
    Candidate,
    IssuerMarkup,
    Priority,
    apply_assignment,
    candidates,
    markup_problem,
)
from finlib.sources.ifrs_numbers import Grouping, GroupingDetection

DATES = (date(2024, 12, 31), date(2023, 12, 31))

# Капитал, раскрытый двумя строками одного раздела: случай ЛСР.
TWO_ROWS_ONE_CODE = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Итого оборотные активы                  300 000        280 000
Итого активы                          1 000 000        930 000
Акционерный капитал                      10 000         10 000
Эмиссионный доход                        26 408         26 408
Добавочный капитал                       16 849         16 849
Нераспределённая прибыль                946 743        876 743
Итого капитал                         1 000 000        930 000
"""

# Та же строка, разобранная дважды: случай Норникеля.
REPEATED_ROW = """
Консолидированный отчёт о прибыли или убытке
Выручка                               1 200 000      1 100 000
Себестоимость продаж                   (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Операционная прибыль                    400 000        350 000
Прибыль до налогообложения              400 000        350 000
Прибыль за год                          320 000        280 000
Прибыль за год                          320 000        280 000
"""

# Дебиторская задолженность в обоих разделах баланса: случай ЛСР. Позиции
# теперь две, и различает их раздел.
LONG_TERM_RECEIVABLE = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Торговая и прочая дебиторская задолженность   1 410      2 219
Итого внеоборотные активы               701 410        652 219
Запасы                                  300 000        280 000
Активы по договорам, торговая и прочая дебиторская задолженность  215 664  132 186
Итого оборотные активы                  515 664        412 186
Итого активы                          1 217 074      1 064 405
"""

# Строка внеоборотного раздела, которой в справочнике нет вовсе.
UNKNOWN_NON_CURRENT_ROW = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Средства в банках                         1 410          2 219
Итого внеоборотные активы               701 410        652 219
Запасы                                  300 000        280 000
Итого оборотные активы                  300 000        280 000
Итого активы                          1 001 410        932 219
"""


def issuer_of(text: str, forms: tuple[str, ...]) -> IssuerMarkup:
    """Эмитент с разобранной отчётностью и пустой разметкой."""
    profile = DocumentProfile(
        forms=forms,
        currency="RUB",
        unit_code="385",
        grouping=Grouping.RUSSIAN,
        report_dates=DATES,
        reporting_kind=ReportingKind.FULL,
        grouping_detection=GroupingDetection(Grouping.RUSSIAN),
    )
    return IssuerMarkup(
        "1", Path("1.txt"), profile, extract(text, DATES, profile.grouping)
    )


def candidate_named(issuer: IssuerMarkup, name: str) -> Candidate:
    """Кандидат разметки по наименованию строки."""
    row = next(
        item for item in issuer.extraction.unrecognised if item.source_name == name
    )
    return Candidate(
        inn=issuer.inn,
        form=row.form,
        source_name=row.source_name,
        values=row.values,
        relative_size=None,
        priority=Priority.OTHER,
        index=row.index,
    )


# --- само правило --------------------------------------------------------------


def test_two_rows_of_one_section_are_summed() -> None:
    """Две строки одного раздела складываются, а не затирают друг друга."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.share_premium")
    outcome = fold(
        position,
        [
            Claim("Эмиссионный доход", (Decimal(26408),), position.form, position.section),
            Claim("Добавочный капитал", (Decimal(16849),), position.form, position.section),
        ],
    )
    assert outcome.kind is Fold.SUMMED
    assert outcome.value == Decimal(43257)


def test_the_same_row_twice_is_counted_once() -> None:
    """Повтор строки — не вторая статья: величина берётся один раз."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.profit_for_period")
    claim = Claim("Прибыль за год", (Decimal(2470),), position.form, position.section)
    outcome = fold(position, [claim, claim])
    assert outcome.kind is Fold.REPEATED
    assert outcome.value == Decimal(2470)


def test_total_disclosed_twice_gives_no_value() -> None:
    """Итог не складывается из двух итогов: величина не берётся вовсе."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.total_assets")
    outcome = fold(
        position,
        [
            Claim("Итого активы", (Decimal(1000),), position.form, position.section),
            Claim("Всего активов", (Decimal(1200),), position.form, position.section),
        ],
    )
    assert outcome.kind is Fold.CONTESTED
    assert outcome.value is None


def test_row_of_another_form_is_refused() -> None:
    """Строка другой формы на позицию не претендует."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.finance_costs")
    outcome = fold(
        position,
        [
            Claim(
                "Процентные расходы, отраженные в прибылях и убытках",
                (Decimal(25488),),
                "ifrs.statement_of_cash_flows",
                "operating_cash_flow",
            )
        ],
    )
    assert outcome.kind is Fold.NONE
    assert outcome.refused[0][1] is Refusal.FOREIGN_FORM
    assert outcome.value is None


def test_row_of_another_section_is_refused() -> None:
    """Строка другого раздела на позицию не претендует."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.trade_receivables")
    outcome = fold(
        position,
        [
            Claim(
                "Торговая и прочая дебиторская задолженность",
                (Decimal(1410),),
                position.form,
                "non_current_assets",
            )
        ],
    )
    assert outcome.kind is Fold.NONE
    assert outcome.refused[0][1] is Refusal.FOREIGN_SECTION


def test_row_without_section_is_judged_by_form_alone() -> None:
    """Раздела нет — правило раздела не применяется, а не совпадает с любым."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.inventories")
    outcome = fold(
        position, [Claim("Запасы", (Decimal(10),), position.form, None)]
    )
    assert outcome.kind is Fold.SINGLE
    assert outcome.value == Decimal(10)


# --- правило в разборе ---------------------------------------------------------


def test_extraction_sums_two_rows_of_one_code() -> None:
    """В разборе величина складывается, и сложение объявлено, а не молчит."""
    issuer = issuer_of(TWO_ROWS_ONE_CODE, ("ifrs.statement_of_financial_position",))
    values = issuer.extraction.totals(DATES[0])
    assert values["ifrs.share_premium"] == Decimal(43257)
    assert [code for code, _, _ in issuer.extraction.merged] == ["ifrs.share_premium"]


def test_extraction_counts_repeated_row_once() -> None:
    """Повтор строки в разборе не удваивает величину."""
    issuer = issuer_of(REPEATED_ROW, ("ifrs.statement_of_profit_or_loss",))
    values = issuer.extraction.totals(DATES[0])
    assert values["ifrs.profit_for_period"] == Decimal(320000)
    assert [kind for _, _, kind in issuer.extraction.merged] == [Fold.REPEATED.value]


# --- правило в разметке --------------------------------------------------------


def test_receivables_of_both_sections_keep_their_own_positions() -> None:
    """Дебиторская задолженность разных разделов — две позиции, не одна.

    Прежде позиция была одна, оборотная, и строка внеоборотного раздела
    не опознавалась вовсе: у ЛСР итог внеоборотных активов не сходился
    ровно на 1 410, а размеченная кодом оборотной она завышала оборотные
    активы на ту же величину.
    """
    issuer = issuer_of(
        LONG_TERM_RECEIVABLE, ("ifrs.statement_of_financial_position",)
    )
    values = issuer.extraction.totals(DATES[0])
    assert values["ifrs.long_term_trade_receivables"] == Decimal(1410)
    # Оборотная позиция при этом свободна: строка внеоборотного раздела
    # её не занимает, а собственная строка ЛСР названа иначе и размечается.
    assert "ifrs.trade_receivables" not in values


def test_markup_of_another_section_is_refused() -> None:
    """Человеку правило раздела предъявляется так же, как автомату."""
    issuer = issuer_of(
        UNKNOWN_NON_CURRENT_ROW, ("ifrs.statement_of_financial_position",)
    )
    candidate = candidate_named(issuer, "Средства в банках")
    catalog = load_ifrs_lines()
    problem = markup_problem(issuer, candidate, "ifrs.inventories", catalog)
    assert problem is not None
    assert "разделе" in problem
    with pytest.raises(ValueError):
        apply_assignment(issuer, candidate, "ifrs.inventories", catalog)


def test_refused_markup_returns_the_row_to_the_queue() -> None:
    """Отклонённое присвоение не разметка: строка снова в очереди."""
    issuer = issuer_of(
        UNKNOWN_NON_CURRENT_ROW, ("ifrs.statement_of_financial_position",)
    )
    catalog = load_ifrs_lines()
    candidate = candidate_named(issuer, "Средства в банках")
    issuer.assignments[candidate.key] = "ifrs.inventories"
    assert candidate.key in issuer.rejects(catalog)
    assert issuer.values(catalog).get("ifrs.inventories") == Decimal(300000)
    assert any(
        item.key == candidate.key for item in candidates([issuer], catalog)
    )
