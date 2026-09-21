"""Тесты ранее подтверждённого опознания на экране сверки.

Справочник и подтверждение — утверждения разной силы. Справочник: строка
с таким наименованием означает это у любого эмитента. Подтверждение: у этого
эмитента эта строка означает это. Второго довольно для повторного комплекта
того же эмитента — человек уже смотрел ту же строку в той же форме той же
организации, — и это не послабление условия «машина знает, что перед ней»,
а другой источник того же знания.

Границы проверяются здесь же: чужой эмитент и другое наименование знанием
не являются, а вид отчётности и потерянная страница остаются блокирующими.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.db import execute
from finlib.sources.ifrs_confirmed import Confirmed, load_confirmed
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import ReportingKind, identify
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.ifrs_review import ReviewReason, review

INN = "7736050003"
OTHER_INN = "7707083893"
DATES = (date(2025, 12, 31), date(2024, 12, 31))

# Комплект, у которого всё опознано справочником, кроме одной статьи
# в 160 000 — это 10,7 % валюты баланса, то есть сверх порога
# существенности. Без подтверждения такой комплект человеку отдаётся
# по двум основаниям сразу: строка не опознана и статья существенна.
BALANCE = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2025 года      31 декабря 2024 года
Основные средства                       540 000        500 000
Задолженность Принципала                160 000        150 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      500 000        430 000
Итого оборотные активы                  800 000        710 000
Итого активы                          1 500 000      1 360 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                200 000        160 000
Итого капитал                           600 000        560 000
Долгосрочные кредиты и займы            500 000        500 000
Итого долгосрочные обязательства        500 000        500 000
Краткосрочные кредиты и займы           400 000        300 000
Итого краткосрочные обязательства       400 000        300 000
Итого обязательства                     900 000        800 000
Итого капитал и обязательства         1 500 000      1 360 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 200 000      1 100 000
Себестоимость продаж                    (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Коммерческие расходы                     (40 000)       (35 000)
Административные расходы                 (60 000)       (55 000)
Операционная прибыль                    300 000        260 000
Финансовые доходы                        10 000          8 000
Финансовые расходы                       (50 000)       (48 000)
Прибыль до налогообложения              260 000        220 000
Расход по налогу на прибыль              (52 000)       (44 000)
Прибыль за период                       208 000        176 000
"""

HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2025 года и 31 декабря 2024 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM ifrs_line_confirmation WHERE inn = ANY(%(inns)s)",
        {"inns": [INN, OTHER_INN]},
        conn=db_conn,
    )
    for inn in (INN, OTHER_INN):
        execute(
            "INSERT INTO organization (inn) VALUES (%(i)s) ON CONFLICT DO NOTHING",
            {"i": inn},
            conn=db_conn,
        )
    return db_conn


def prepared():
    """Документ, проведённый через приём и разбор."""
    profile = identify(BALANCE + HEADER)
    assert profile.accepted, getattr(profile, "reason", "")
    return extract(BALANCE, DATES, Grouping.RUSSIAN), profile


def confirm(conn, inn: str, name: str, code: str, relation: str = "specific") -> None:
    """Подтверждение человека о строке этого эмитента."""
    execute(
        "INSERT INTO ifrs_line_confirmation (code, inn, report_date, source_name, "
        "form_code, value, materiality_share, confirmed_by, relation) VALUES "
        "(%(code)s, %(inn)s, %(date)s, %(name)s, %(form)s, %(value)s, %(share)s, "
        "%(who)s, %(relation)s)",
        {
            "code": code,
            "inn": inn,
            "date": date(2024, 12, 31),
            "name": name,
            "form": "ifrs.statement_of_financial_position",
            "value": Decimal(150000),
            "share": Decimal("0.11"),
            "who": "тест",
            "relation": relation,
        },
        conn=conn,
    )


def test_without_confirmation_the_screen_asks_the_human() -> None:
    """Неопознанная существенная статья — два основания подтверждения."""
    extraction, profile = prepared()
    decision = review(extraction, profile)
    assert not decision.automatic
    assert ReviewReason.UNRECOGNISED_POSITION in decision.reasons
    assert ReviewReason.MATERIAL_SPECIFIC_ITEM in decision.reasons


def test_prior_confirmation_of_the_same_issuer_is_knowledge(clean) -> None:
    """Подтверждение того же эмитента снимает оба основания.

    Комплект следующего года: строка та же, место в таблице другое, поэтому
    подтверждение переносится наименованием, а не индексом строки.
    """
    confirm(clean, INN, "Задолженность Принципала", "ifrs.principal_receivable")
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    assert len(known.rows) == 1
    decision = review(extraction, profile, confirmed=known)
    assert decision.automatic, decision.problems
    assert decision.rows_confirmed == tuple(known.rows)
    # Опознание двух сил считается порознь: доверие к ним разное.
    assert decision.rows_recognised < decision.rows_total
    assert decision.confirmed_from == ("31.12.2024",)


def test_confirmed_row_carries_its_value_to_the_facts(clean) -> None:
    """У подтверждённой статьи величина готова лечь в факты — с её формой.

    Форма берётся у строки, а не у позиции: один код в двух формах правомерен,
    и одна и та же позиция в балансе и в потоке — два разных факта.
    """
    confirm(clean, INN, "Задолженность Принципала", "ifrs.principal_receivable")
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    assert len(known.facts) == 1
    fact = known.facts[0]
    assert fact.code == "ifrs.principal_receivable"
    assert fact.form == "ifrs.statement_of_financial_position"
    assert fact.source_name == "Задолженность Принципала"
    assert fact.values == (Decimal(160_000), Decimal(150_000))


def test_detail_row_does_not_become_a_fact(clean) -> None:
    """Детализация фактом не становится: её величина уже внутри своей позиции.

    Записать её отдельным фактом того же кода значило бы подменить величину
    позиции частью её же — и итог раздела сошёлся бы только случайно.
    """
    confirm(
        clean,
        INN,
        "Задолженность Принципала",
        "ifrs.long_term_trade_receivables",
        relation="part_of",
    )
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    assert known.rows, "подтверждение не применилось — проверять нечего"
    assert known.facts == ()


def test_confirmation_of_another_issuer_is_not_knowledge(clean) -> None:
    """У чужого эмитента то же наименование может означать другое."""
    confirm(clean, OTHER_INN, "Задолженность Принципала", "ifrs.principal_receivable")
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    assert known.rows == frozenset()
    assert not review(extraction, profile, confirmed=known).automatic


def test_another_name_is_not_knowledge(clean) -> None:
    """Подтверждается наименование, а не смысл: другое написание не годится."""
    confirm(clean, INN, "Задолженность концедента", "ifrs.principal_receivable")
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    assert known.rows == frozenset()


def test_reporting_kind_and_lost_page_stay_blocking(clean) -> None:
    """Подтверждением строки состав раскрытий и потеря страницы не снимаются.

    Это не опознание: у промежуточной отчётности уже состав примечаний,
    а страницы без текстового слоя мы не видим вовсе — и что на ней стояло,
    подтверждение прошлого года не говорит.
    """
    from dataclasses import replace

    confirm(clean, INN, "Задолженность Принципала", "ifrs.principal_receivable")
    extraction, profile = prepared()
    known = load_confirmed(INN, extraction, profile, conn=clean)

    interim = replace(profile, reporting_kind=ReportingKind.INTERIM)
    assert ReviewReason.REPORTING_KIND in review(
        extraction, interim, confirmed=known
    ).reasons

    lost = replace(profile, pages_without_text=(6,))
    assert ReviewReason.LOST_PAGE in review(extraction, lost, confirmed=known).reasons


def test_empty_confirmation_changes_nothing() -> None:
    """Пустое подтверждение — то же, что его отсутствие."""
    extraction, profile = prepared()
    assert (
        review(extraction, profile, confirmed=Confirmed()).reasons
        == review(extraction, profile).reasons
    )
