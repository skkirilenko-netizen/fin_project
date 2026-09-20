"""Тесты распределения прибыли между акционерами и НКД (МСФО (IAS) 1.81B).

Строки есть у всех разобранных эмитентов, а позиций у них не было: каждая
размечалась детализацией прибыли — руками, у каждого эмитента и в каждом
комплекте, потому что детализация синонимом не переносится. Заодно
не проверялось тождество: прибыль за период равна сумме распределённого.
"""

from datetime import date
from decimal import Decimal

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.quality.totals import Composition, TotalVerdict, check_total
from finlib.sources.ifrs_claims import Claim, Fold, Refusal, fold
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_numbers import Grouping

DATES = (date(2025, 12, 31), date(2024, 12, 31))

# Распределение, напечатанное один раз: так у Сегежи, ФосАгро, Норникеля,
# Черкизово и Брусники.
SPLIT_ONCE = """
Консолидированный отчёт о прибыли или убытке
Выручка 100 000 90 000
Прибыль до налогообложения 21 000 18 900
Расход по налогу на прибыль (4 000) (3 600)
Прибыль за год 17 000 15 300
Акционерам материнской компании 15 800 14 200
Держателям неконтролирующих долей 1 200 1 100
"""

# То же наименование дважды — под прибылью и под общим совокупным доходом.
# Так у Акрона, и складывать эти строки нельзя: суммы 30 600 нет нигде.
SPLIT_TWICE = """
Консолидированный отчёт о прибыли или убытке
Выручка 100 000 90 000
Прибыль за год 16 000 14 400
Общий совокупный доход за год 15 000 13 900
Собственникам Компании 14 800 13 700
Собственникам Компании 15 800 14 200
"""


def values_of(text: str) -> dict[str, Decimal]:
    """Величины отчётного периода после разбора формы."""
    found = extract(text, DATES, Grouping.RUSSIAN)
    return found.totals(DATES[0])


def test_attribution_is_recognised_by_the_catalog() -> None:
    """Строки распределения опознаются справочником, а не размечаются руками."""
    values = values_of(SPLIT_ONCE)
    assert values["ifrs.profit_attributable_to_owners"] == Decimal(15800)
    assert values["ifrs.profit_attributable_to_nci"] == Decimal(1200)


def test_profit_equals_the_sum_of_its_parts() -> None:
    """Тождество распределения: прибыль равна сумме распределённого."""
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.profit_for_period")
    values = values_of(SPLIT_ONCE)
    found = check_total(
        Composition(position.code, position.split_into),
        values.get,
        lambda code: None,
        lambda amount: abs(amount) / Decimal(1000) + Decimal(1),
        lambda code: 1,
    )
    assert found.verdict is TotalVerdict.MATCHED
    assert found.total == Decimal(17000)
    assert found.computed == Decimal(17000)


def test_split_is_not_a_second_composition() -> None:
    """Распределение не подменяет собой цепочку прибыли.

    Сошедшееся распределение закрыло бы несошедшуюся цепочку, то есть скрыло
    бы дефект вместо того, чтобы его показать. Поэтому состав у прибыли один,
    а распределение объявлено отдельным полем.
    """
    position = load_ifrs_lines().require("ifrs.profit_for_period")
    assert [item.code for item in position.components] == [
        "ifrs.profit_before_tax",
        "ifrs.income_tax",
    ]
    assert position.compositions == (position.components,)
    assert [item.code for item in position.split_into] == [
        "ifrs.profit_attributable_to_owners",
        "ifrs.profit_attributable_to_nci",
    ]


def test_two_rows_of_one_attribution_are_a_dispute() -> None:
    """Одно наименование дважды — спор, а не сумма.

    У Акрона «Собственникам Компании» стоит и под прибылью, и под общим
    совокупным доходом; раздела у таких строк нет — обе ниже всех опознанных
    итогов. Сумма 30 600 не существует ни в отчётности, ни в природе.
    """
    catalog = load_ifrs_lines()
    position = catalog.require("ifrs.profit_attributable_to_owners")
    assert not position.summable

    outcome = fold(
        position,
        [
            Claim("Собственникам Компании", (Decimal(14800),), position.form),
            Claim("Собственникам Компании", (Decimal(15800),), position.form),
        ],
    )
    assert outcome.kind is Fold.CONTESTED
    assert outcome.value is None
    assert {item[1] for item in outcome.refused} == {Refusal.NOT_SUMMABLE}

    # И в разборе формы величина не берётся вовсе: строки уходят на разметку.
    values = values_of(SPLIT_TWICE)
    assert "ifrs.profit_attributable_to_owners" not in values
