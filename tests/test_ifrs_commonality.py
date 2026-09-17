"""Тесты метрики общности статей.

Расчёт зафиксирован до подведения итогов: иначе всегда найдётся способ
посчитать так, чтобы вышло убедительно. Проверяется именно то, что можно
подсчитать неверно в свою пользу, — что считается статьёй, что итогом
и что из счёта выпадает.
"""

from datetime import date
from decimal import Decimal

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_commonality import ATYPICAL_SHARE, Frequency, commonality
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import DocumentProfile, ReportingKind
from finlib.sources.ifrs_markup import IssuerMarkup
from finlib.sources.ifrs_numbers import Grouping, GroupingDetection

DATES = (date(2024, 12, 31), date(2023, 12, 31))

BALANCE = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  200 000        180 000
Задолженность Принципала                100 000         90 000
Итого оборотные активы                  300 000        270 000
Итого активы                          1 000 000        920 000
"""


def issuer_of(inn: str, text: str) -> IssuerMarkup:
    """Эмитент с разобранной отчётностью и пустой разметкой."""
    profile = DocumentProfile(
        forms=("ifrs.statement_of_financial_position",),
        currency="RUB",
        unit_code="385",
        grouping=Grouping.RUSSIAN,
        report_dates=DATES,
        reporting_kind=ReportingKind.FULL,
        grouping_detection=GroupingDetection(Grouping.RUSSIAN),
    )
    from pathlib import Path

    return IssuerMarkup(inn, Path(f"{inn}.txt"), profile, extract(text, DATES, profile.grouping))


def test_totals_are_counted_apart_from_items() -> None:
    """Итог опознаётся почти всегда, и общность статей он завышал бы.

    Итоговая строка подписана однообразно и стоит в справочнике; смешение
    итогов со статьями поднимало бы общность независимо от того, что мы
    знаем о статьях.
    """
    found, overall, _ = commonality([issuer_of("1", BALANCE)], load_ifrs_lines())
    assert overall.totals >= 3
    assert overall.items >= 3
    assert overall.totals_covered == overall.totals
    assert overall.items_covered < overall.items


def test_uncovered_amount_counts_towards_the_denominator() -> None:
    """Доля по сумме считается от всех статей, а не от опознанных."""
    _, overall, _ = commonality([issuer_of("1", BALANCE)], load_ifrs_lines())
    assert overall.amount > overall.amount_covered
    assert overall.by_amount is not None
    assert Decimal(0) < overall.by_amount < Decimal(1)


def test_atypical_issuer_is_flagged_by_its_own_items() -> None:
    """Эмитент, у которого своя вся отчётность, помечается машинным признаком.

    Основание — Автодор: «Задолженность Принципала» и «Затраты в интересах
    Принципала» составляют около восьмидесяти процентов его активов. Признак
    считается по разбору, а не объявляется: объявленный описывал бы намерение.
    """
    heavy = BALANCE.replace("Задолженность Принципала                100 000         90 000",
                            "Задолженность Принципала                800 000        720 000")
    found, _, _ = commonality([issuer_of("1", heavy)], load_ifrs_lines())
    assert found[0].specific_share is not None
    assert found[0].specific_share >= ATYPICAL_SHARE
    assert found[0].atypical


def test_frequency_threshold_fits_a_small_set() -> None:
    """У набора из двух эмитентов «у большинства» означает «у обоих».

    Порог в четыре наблюдения давал бы ноль общих кодов при любом
    справочнике — то есть измерял бы размер набора, а не общность.
    """
    assert Frequency(issuers=2, by_code={"ifrs.revenue": 2}).many == 2
    assert Frequency(issuers=6, by_code={}).many == 4
    assert Frequency(issuers=2, by_code={"ifrs.revenue": 2}).at_least(2) == 1
