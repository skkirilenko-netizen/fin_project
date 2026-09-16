"""Вырожденная отчётность: нули там, где обычно стоят величины.

Одна ошибка нулевого знаменателя, ушедшая мимо перехвата, нашлась на живых
данных: у организации в конкурсном производстве валюта баланса равна нулю,
и прогон падал целиком. Дефекты такого рода в одиночку не ходят, поэтому
здесь синтетический набор: нулевая валюта баланса, нулевая выручка, нулевой
собственный капитал и все три сразу.

Проверяется не значение показателя, а то, что расчётный слой доходит
до конца: показатель либо посчитан, либо объявлен нерассчитанным с причиной.
Исключение наружу — дефект, каким бы ни была отчётность.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.db import execute
from finlib.metrics.engine import compute_all
from finlib.metrics.store import save_results
from finlib.normalize.lines import LineDef, LinesCatalog, ReportingType, load_lines
from finlib.normalize.loader import load_report_set
from finlib.scoring.engine import assess
from finlib.scoring.store import save_assessment
from finlib.sources.model import FormData, Organization, ReportSet

INN = "5001000018"
YEARS = (2024, 2025)
BASE = Decimal(1000)


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute("DELETE FROM organization WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute("DELETE FROM dq_log WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute("DELETE FROM metric_value WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    return db_conn


def _lines(catalog: LinesCatalog) -> tuple[LineDef, ...]:
    """Строки полного набора всех форм методики."""
    return tuple(
        line
        for form in catalog.forms_of(ReportingType.FULL)
        for line in catalog.for_form(form, ReportingType.FULL)
    )


def _consistent(catalog: LinesCatalog) -> dict[str, Decimal]:
    """Согласованная отчётность: слагаемые одинаковы, итоги сходятся.

    Итоги считаются по составу справочника, а не задаются руками: иначе
    вырожденный случай проверял бы заодно и несходимость, и понять, что
    именно сломалось, стало бы нельзя.
    """
    lines = _lines(catalog)
    values = {line.code: BASE for line in lines if not line.is_total}
    for _ in range(len(lines)):
        changed = False
        for line in lines:
            if not line.is_total:
                continue
            parts = [values.get(item.code) for item in line.components]
            if any(part is None for part in parts):
                continue
            total = sum(
                part if item.op == "+" else -part
                for item, part in zip(line.components, parts, strict=True)
                if part is not None
            )
            if values.get(line.code) != total:
                values[line.code] = Decimal(total)
                changed = True
        if not changed:
            break
    return values


def _report(values: dict[str, Decimal], year: int, catalog: LinesCatalog) -> ReportSet:
    """Комплект за год: те же значения в отчётном и сравнительном периодах."""
    forms: dict[str, FormData] = {}
    for form_code in catalog.forms_of(ReportingType.FULL):
        codes = [line.code for line in catalog.for_form(form_code, ReportingType.FULL)]
        forms[form_code] = FormData(
            form_code=form_code,
            values={
                date(year - offset, 12, 31): {
                    code: values[code] for code in codes if code in values
                }
                for offset in (0, 1)
            },
        )
    return ReportSet(
        inn=INN,
        report_year=year,
        report_date=date(year, 12, 31),
        knd="0710099",
        reporting_type=ReportingType.FULL,
        correction_version=0,
        is_actual=True,
        forms=forms,
    )


def _load(values: dict[str, Decimal], conn) -> None:
    """Загружает два комплекта подряд: динамика требует двух точек."""
    catalog = load_lines()
    organization = Organization(inn=INN, full_name="ТЕСТ")
    for year in YEARS:
        load_report_set(_report(values, year, catalog), organization, conn, catalog=catalog)


ZERO_CASES = {
    # Организация в конкурсном производстве: баланс сведён к нулю.
    "нулевая валюта баланса": ("1100", "1200", "1300", "1400", "1500", "1600", "1700"),
    # Холдинг или СФО: выручки нет, цепочка прибыли от неё не строится.
    "нулевая выручка": ("2110", "2100", "2200"),
    # Капитал съеден убытком ровно в ноль: знаменатель автономии обращается в ноль.
    "нулевой собственный капитал": ("1300", "1370"),
}
ALL_AT_ONCE = tuple(code for codes in ZERO_CASES.values() for code in codes)


def _with_zeros(codes: tuple[str, ...]) -> dict[str, Decimal]:
    """Согласованная отчётность, в которой названные строки обнулены."""
    values = _consistent(load_lines())
    for code in codes:
        if code in values:
            values[code] = Decimal(0)
    return values


@pytest.mark.parametrize(
    "codes",
    [*ZERO_CASES.values(), ALL_AT_ONCE],
    ids=[*ZERO_CASES, "все три сразу"],
)
def test_zero_values_do_not_break_the_calculation(codes: tuple[str, ...], clean) -> None:
    """Расчётный слой доходит до конца на вырожденной отчётности."""
    _load(_with_zeros(codes), clean)

    results = compute_all(INN, clean)
    assert results, "показатели обязаны быть перечислены, пусть и нерассчитанными"
    for item in results:
        if not item.is_ok:
            assert item.reason, f"{item.metric_code}: причина отказа обязана быть названа"

    save_results(INN, results, clean)
    assessment = assess(INN, clean)
    assert assessment is not None
    save_assessment(assessment, clean)
    # Класс может не присваиваться — это штатный исход, но причина обязана быть.
    assert assessment.class_code or assessment.no_class_reason


def test_calculation_survives_missing_previous_period(clean) -> None:
    """Единственный период: показатели по средним величинам не считаются.

    Отдельный случай к вырожденным значениям: при одном периоде полусумма
    не определена, и это отказ методики, а не ошибка расчёта.
    """
    catalog = load_lines()
    values = _consistent(catalog)
    load_report_set(
        _report(values, YEARS[0], catalog),
        Organization(inn=INN, full_name="ТЕСТ"),
        clean,
        catalog=catalog,
    )
    results = compute_all(INN, clean)
    assert results
    assert any(item.is_ok for item in results), "точечные показатели считаются и так"
