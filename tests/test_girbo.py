"""Тесты разбора ответа ГИР БО на реальных сохранённых пробах."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from probes import FULL_BFO, SEARCH_EMPTY, SEARCH_FOUND, SIMPLIFIED_BFO, read_probe

from finlib.normalize.lines import ReportingType
from finlib.sources.cache import RawCache
from finlib.sources.errors import CreditOrganizationError, OrganizationNotFoundError
from finlib.sources.girbo import (
    FORM_PERIOD_DEPTH,
    GirboSource,
    ReportSet,
    is_credit_organization,
    parse_organization,
    parse_report_sets,
    strip_highlight,
)
from finlib.sources.http import PoliteClient
from finlib.utils import json_loads_decimal

FULL_INN = "7736050003"
SIMPLIFIED_INN = "2100010824"


@pytest.fixture(scope="module")
def full_sets() -> list[ReportSet]:
    """Комплекты полной отчётности из пробы."""
    return parse_report_sets(json_loads_decimal(read_probe(FULL_BFO)), FULL_INN)


@pytest.fixture(scope="module")
def simplified_sets() -> list[ReportSet]:
    """Комплекты упрощённой отчётности из пробы."""
    return parse_report_sets(json_loads_decimal(read_probe(SIMPLIFIED_BFO)), SIMPLIFIED_INN)


def latest(sets: list[ReportSet]) -> ReportSet:
    """Самый свежий актуальный комплект."""
    return next(s for s in sets if s.is_actual)


# --- разбор реквизитов ------------------------------------------------------


def test_strip_highlight() -> None:
    """Подсветка совпадений снимается: источник оборачивает найденное в <strong>."""
    assert strip_highlight("<strong>7736050003</strong>") == "7736050003"
    assert strip_highlight(None) is None


def test_parse_organization_from_search() -> None:
    """Реквизиты собираются из строки поиска без остатков разметки."""
    payload = json_loads_decimal(read_probe(SEARCH_FOUND))
    org = parse_organization(payload["content"][0])
    assert org.inn == FULL_INN
    assert org.girbo_id == 6622458
    assert "<strong>" not in (org.short_name or "")
    assert org.ogrn == "1027700070518"


# --- формы и периоды --------------------------------------------------------


def test_forms_arrive_as_separate_blocks(full_sets: list[ReportSet]) -> None:
    """Баланс, ОФР и ОДДС разбираются в отдельные формы со своими ОКУД."""
    assert latest(full_sets).form_codes == ("0710001", "0710002", "0710005")


def test_balance_totals_are_distinct_keys(full_sets: list[ReportSet]) -> None:
    """1600 и 1700 различаются кодом: одинаковое наименование «Баланс» роли не играет."""
    balance = latest(full_sets).forms["0710001"]
    current = balance.values[date(2025, 12, 31)]
    assert current["1600"] == current["1700"] == Decimal("25736328136")
    assert current["1100"] + current["1200"] == current["1600"]
    assert current["1300"] + current["1400"] + current["1500"] == current["1700"]


def test_history_depth_differs_by_form(full_sets: list[ReportSet]) -> None:
    """Третий период есть только у баланса — глубина истории по формам разная."""
    report = latest(full_sets)
    assert report.report_dates("0710001") == (
        date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31),
    )
    assert report.report_dates("0710002") == (date(2025, 12, 31), date(2024, 12, 31))
    assert report.report_dates("0710005") == (date(2025, 12, 31), date(2024, 12, 31))
    for form_code, depth in FORM_PERIOD_DEPTH.items():
        assert report.forms[form_code].depth == depth


# --- знаки и арифметика -----------------------------------------------------


def test_expenses_arrive_positive(full_sets: list[ReportSet]) -> None:
    """Расходные строки приходят положительными, вычитание задаёт справочник."""
    ofr = latest(full_sets).forms["0710002"].values[date(2025, 12, 31)]
    assert ofr["2120"] > 0
    assert ofr["2110"] - ofr["2120"] == ofr["2100"]
    assert ofr["2100"] - ofr["2210"] - ofr["2220"] == ofr["2200"]


def test_lines_with_sign_any_can_be_negative(full_sets: list[ReportSet]) -> None:
    """Строки, помеченные в справочнике sign: any, действительно приходят отрицательными."""
    report = latest(full_sets)
    ofr = report.forms["0710002"].values[date(2025, 12, 31)]
    odds = report.forms["0710005"].values[date(2025, 12, 31)]
    assert ofr["2460"] < 0
    assert odds["4490"] < 0
    assert odds["4450"] + odds["4400"] + odds["4490"] == odds["4500"]


def test_values_are_decimal_not_float(full_sets: list[ReportSet]) -> None:
    """Суммы приходят Decimal: дробные значения не проходят через float."""
    ofr = latest(full_sets).forms["0710002"].values[date(2025, 12, 31)]
    assert isinstance(ofr["2110"], Decimal)
    assert ofr["2900"] == Decimal("0.48")


def test_missing_value_is_none_not_zero(full_sets: list[ReportSet]) -> None:
    """Нераскрытое значение приходит как None, а не как ноль."""
    balance = latest(full_sets).forms["0710001"].values[date(2025, 12, 31)]
    assert balance["1120"] is None
    assert balance["1110"] is not None


# --- упрощённая отчётность --------------------------------------------------


def test_simplified_is_detected_by_knd(simplified_sets: list[ReportSet]) -> None:
    """Тип отчётности определяется по КНД, а не по коду формы."""
    report = latest(simplified_sets)
    assert report.knd == "0710096"
    assert report.reporting_type is ReportingType.SIMPLIFIED
    # ОКУД форм при этом тот же, что у полной отчётности.
    assert "0710001" in report.form_codes


def test_full_is_detected_by_knd(full_sets: list[ReportSet]) -> None:
    """Полная отчётность опознаётся по КНД 0710099."""
    assert latest(full_sets).knd == "0710099"
    assert latest(full_sets).reporting_type is ReportingType.FULL


def test_simplified_uses_largest_share_code(simplified_sets: list[ReportSet]) -> None:
    """Укрупнённая строка пришла под кодом показателя с наибольшим удельным весом."""
    balance = latest(simplified_sets).forms["0710001"].values[date(2024, 12, 31)]
    filled = {code for code, value in balance.items() if value is not None}
    # Дебиторская задолженность вместо канонического кода 1240 упрощённого набора.
    assert "1230" in filled
    assert "1240" not in filled
    assert balance["1230"] + balance["1250"] == balance["1600"]


def test_simplified_profit_formula(simplified_sets: list[ReportSet]) -> None:
    """Формула 2400 упрощённого набора сходится на реальных данных."""
    ofr = latest(simplified_sets).forms["0710002"].values[date(2024, 12, 31)]
    computed = (
        ofr["2110"]
        - ofr["2120"]
        - (ofr.get("2330") or 0)
        + (ofr.get("2340") or 0)
        - (ofr.get("2350") or 0)
        - (ofr.get("2410") or 0)
    )
    assert computed == ofr["2400"]


def test_simplified_negative_equity(simplified_sets: list[ReportSet]) -> None:
    """Капитал и резервы приходят отрицательными — справочник допускает такой знак."""
    balance = latest(simplified_sets).forms["0710001"].values[date(2024, 12, 31)]
    assert balance["1300"] < 0


# --- корректировки ----------------------------------------------------------


def test_correction_version_and_actuality(full_sets: list[ReportSet]) -> None:
    """У каждого комплекта есть номер корректировки и признак актуальности."""
    for report in full_sets:
        assert report.correction_version >= 0
        assert isinstance(report.is_actual, bool)
    years = {r.report_year for r in full_sets if r.is_actual}
    assert years == {r.report_year for r in full_sets}, "у каждого года есть актуальная версия"


def test_sets_are_sorted_newest_first(full_sets: list[ReportSet]) -> None:
    """Комплекты отсортированы от свежих к старым."""
    years = [r.report_year for r in full_sets]
    assert years == sorted(years, reverse=True)


# --- отбраковка и ошибки ----------------------------------------------------


def test_credit_organization_flag() -> None:
    """Признак кредитной организации читается из ответа."""
    assert not is_credit_organization(json_loads_decimal(read_probe(FULL_BFO)))
    assert is_credit_organization([{"isCb": True}])


def test_credit_organization_is_rejected(tmp_path: Path) -> None:
    """Кредитная организация отбраковывается на входе, а не анализируется по РСБУ."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "search" in request.url.path:
            return httpx.Response(200, content=read_probe(SEARCH_FOUND))
        return httpx.Response(200, content=b'[{"id": 1, "period": "2024", "isCb": true}]')

    with _source(handler, tmp_path) as source, pytest.raises(CreditOrganizationError):
        source.fetch_report_sets(FULL_INN)


def test_unknown_inn_raises(tmp_path: Path) -> None:
    """ИНН, которого нет в источнике, даёт внятную ошибку, а не пустой результат."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_probe(SEARCH_EMPTY))

    with (
        _source(handler, tmp_path) as source,
        pytest.raises(OrganizationNotFoundError, match="7707083893"),
    ):
        source.find_organization("7707083893")


def test_end_to_end_on_probe(tmp_path: Path) -> None:
    """Полный путь от поиска до комплектов работает на сохранённой пробе."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "search" in request.url.path:
            return httpx.Response(200, content=read_probe(SEARCH_FOUND))
        return httpx.Response(200, content=read_probe(FULL_BFO))

    with _source(handler, tmp_path) as source:
        organization, sets = source.fetch_report_sets(FULL_INN)
    assert organization.inn == FULL_INN
    assert len(sets) == 4
    assert latest(sets).forms["0710001"].values[date(2025, 12, 31)]["1600"] > 0


def _source(handler, tmp_path: Path) -> GirboSource:
    """Источник поверх поддельного транспорта и временного кэша."""
    client = PoliteClient(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(handler),
        backoff_s=0.0,
        min_interval_s=0.0,
    )
    return GirboSource(client=client, cache=RawCache("girbo", root=tmp_path), journal=False)
