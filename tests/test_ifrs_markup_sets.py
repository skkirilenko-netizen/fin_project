"""Разметка принадлежит комплекту, а не эмитенту.

У эмитента комплектов сколько угодно: годовой и промежуточный лежат в одной
папке и различаются только тем, что прочитано из документа. Ключ «форма
и место в ней» у другого комплекта означает **другую строку**, поэтому
решение человека обязано нести отчётную дату — иначе оно применится к чужому
комплекту и запишется под его датой. Так и вышло у Сегежи: три присвоения
промежуточного комплекта легли на годовой, а одно затёрло запись годового
по ключу (код, ИНН, дата, наименование).
"""

from datetime import date
from pathlib import Path

from finlib.normalize.ifrs_lines import load_ifrs_lines
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import DocumentProfile, ReportingKind
from finlib.sources.ifrs_markup import IssuerMarkup, candidates
from finlib.sources.ifrs_numbers import Grouping, GroupingDetection

ANNUAL = (date(2025, 12, 31), date(2024, 12, 31))
INTERIM = (date(2026, 6, 30), date(2025, 6, 30))

FORM = """
Консолидированный отчёт о финансовом положении
Основные средства                       700 000        650 000
Задолженность Принципала                100 000         90 000
Итого внеоборотные активы               800 000        740 000
Запасы                                  200 000        180 000
Итого оборотные активы                  200 000        180 000
Итого активы                          1 000 000        920 000
"""


def issuer_of(inn: str, dates: tuple[date, ...], kind: ReportingKind) -> IssuerMarkup:
    """Комплект одного эмитента с пустой разметкой."""
    profile = DocumentProfile(
        forms=("ifrs.statement_of_financial_position",),
        currency="RUB",
        unit_code="385",
        grouping=Grouping.RUSSIAN,
        report_dates=dates,
        reporting_kind=kind,
        grouping_detection=GroupingDetection(Grouping.RUSSIAN),
    )
    return IssuerMarkup(
        inn,
        Path(f"{inn}-{dates[0]}.txt"),
        profile,
        extract(FORM, dates, profile.grouping),
    )


def test_candidate_names_its_set() -> None:
    """Кандидат несёт отчётную дату комплекта, а не только ИНН."""
    issuer = issuer_of("1", INTERIM, ReportingKind.INTERIM)
    queue = candidates([issuer], load_ifrs_lines())
    assert queue
    assert all(item.report_date == date(2026, 6, 30) for item in queue)


def test_same_row_of_two_sets_has_two_keys() -> None:
    """Одна и та же строка двух комплектов различается ключом комплекта.

    Ключ строки у них совпадает — форма и место те же, — и без отчётной даты
    решение по одному комплекту применилось бы к другому.
    """
    catalog = load_ifrs_lines()
    annual = issuer_of("1", ANNUAL, ReportingKind.FULL)
    interim = issuer_of("1", INTERIM, ReportingKind.INTERIM)
    queue = candidates([annual, interim], catalog)

    by_set: dict[tuple, list] = {}
    for item in queue:
        by_set.setdefault(item.issuer_key, []).append(item)

    assert set(by_set) == {("1", date(2025, 12, 31)), ("1", date(2026, 6, 30))}
    first, second = by_set[("1", date(2025, 12, 31))], by_set[("1", date(2026, 6, 30))]
    assert {item.key for item in first} == {item.key for item in second}
