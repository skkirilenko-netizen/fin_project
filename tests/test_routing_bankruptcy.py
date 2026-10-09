"""Банкротство в карточке — основание «Разбора», старше очереди статуса и «Внимания».

Решение владельца 09.10.2026 по диагностике `eval/audit_diag.py` (card-status):
у шести эмитентов со статусом «идёт процедура банкротства» маршрут статуса
не читал вовсе — четверо стояли в очереди статуса с действием «установить,
не начато ли банкротство», двое во «Внимании». Случаи ниже — синтетика
тех же шести сочетаний оснований, без данных эмитентов.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.scoring.routing import RoutingPolicy, load_routing, route
from finlib.scoring.routing_catalogue import catalogue_for
from finlib.scoring.routing_store import _bankruptcy
from finlib.sources.cbonds_events import DefaultRecord, Issue, IssuerEvents
from finlib.standards import Standard

UNIT = "тыс. руб."
TODAY = date(2026, 10, 9)
FRESH = date(2025, 12, 31)
# Годовая отчётность старше двух циклов раскрытия: очередь статуса.
STALE = date(2022, 12, 31)


def _metric(code: str, value: str) -> MetricValue:
    """Рассчитанный показатель маршрута."""
    return MetricValue(code=code, name=code, group="debt", in_scoring=True, value=Decimal(value))


def _healthy() -> tuple[MetricValue, ...]:
    """Величины без оснований."""
    return (
        _metric("net_debt_ebitda", "1.0"),
        _metric("equity_ratio", "0.6"),
        _metric("cur_liq", "2.5"),
    )


def _type(code: str):  # noqa: ANN202
    """Тип эмитента по коду из справочника."""
    return next(kind for kind in load_routing().issuer_types if kind.code == code)


def _stale_default() -> IssuerEvents:
    """Неурегулированный дефолт по погашению старше трёх лет."""
    issue = Issue(
        emission_id="1", name="Выпуск 01", isin="RU0000000001",
        status="дефолт по погашению", default=True, unsettled=True,
        maturity=date(2019, 6, 1), offer=None, outstanding=None, updated=date(2019, 6, 1),
    )
    record = DefaultRecord(
        emission_id="1", kind="Погашение", status="Дефолт", due=date(2019, 6, 1),
        when=date(2019, 6, 1), announced=None, met=None, amount=None,
    )
    return IssuerEvents(
        inn="1", issues=(issue,), issues_known=True, records=(record,), records_known=True
    )


# Шесть случаев раздела а): доводы маршрута и корзина без банкротства.
CASES = {
    "очередь статуса: давность отчётности": (
        dict(latest_annual=STALE), "status_unknown"),
    "очередь статуса: давность и SPV без поручителя в списке": (
        dict(latest_annual=STALE, issuer_type=_type("financing"),
             guarantor="Поручитель", guarantor_inns="7700000000"), "status_unknown"),
    "очередь статуса: давность и дефолт по погашению": (
        dict(latest_annual=STALE, events=_stale_default()), "status_unknown"),
    "очередь статуса: давность и тяжёлый стоп-фактор": (
        dict(latest_annual=STALE, stop_factors=("negative_equity",)), "status_unknown"),
    "внимание: стоп-фактор с ограничением средним": (
        dict(latest_annual=FRESH, stop_factors=("weak_coverage",)), "attention"),
    "внимание: давний неурегулированный дефолт": (
        dict(latest_annual=FRESH, events=_stale_default()), "attention"),
}


def _routed(**given: object):  # noqa: ANN202
    """Вердикт по РСБУ на день разбора при здоровых величинах."""
    return route(
        _healthy(), unit=UNIT, quarantined=False, today=TODAY,
        catalogue=catalogue_for(Standard.RSBU), **given,
    )


@pytest.mark.parametrize("case", list(CASES))
def test_bankruptcy_names_review_over_the_status_queue_and_attention(case: str) -> None:
    """Без банкротства — прежняя корзина; с ним — «Разбор» в событийной подгруппе.

    Порядок оснований внутри корзины — порядок объявления, старшинства у них
    нет (`_verdict`): дефолт по выпуску объявлен раньше и стоит раньше.
    """
    given, before = CASES[case]
    assert _routed(**given).basket == before
    verdict = _routed(**given, bankruptcy="идёт процедура банкротства",
                      bankruptcy_updated="16.09.2026")
    assert verdict.basket == "review"
    assert "bankruptcy_proceedings" in verdict.grounds
    assert verdict.subgroups[0] == "event_risk"
    # Прежние обстоятельства не исчезают: они в перечне сработавших.
    was = {item.ground for item in _routed(**given).findings}
    assert was <= {item.ground for item in verdict.findings}
    said = next(item for item in verdict.findings if item.ground == "bankruptcy_proceedings")
    assert "идёт процедура банкротства" in said.text and "16.09.2026" in said.text


def test_the_card_status_gives_bankruptcy_only_for_declared_statuses() -> None:
    """Довод берётся у карточки по объявленным статусам; прочие — пусто."""
    routing = load_routing()
    universe = routing.universe
    code = universe.bankruptcy_statuses[0]
    card = {"emitent_statuses_id": code, "updating_date": "2026-09-16T00:00:00"}
    assert _bankruptcy(card, routing) == {
        "bankruptcy": universe.status_of(code),
        "bankruptcy_updated": "16.09.2026",
    }
    # Дата, которую не разобрать, печатается как есть, а не пропадает.
    odd = {"emitent_statuses_id": code, "updating_date": "не дата"}
    assert _bankruptcy(odd, routing)["bankruptcy_updated"] == "не дата"
    active = next(key for key, name in universe.statuses.items() if name == "действующая")
    assert _bankruptcy({"emitent_statuses_id": active}, routing) == {}
    assert _bankruptcy({}, routing) == {}


def test_an_undeclared_bankruptcy_status_does_not_load() -> None:
    """Статус банкротства вне объявленных статусов — справочник не грузится."""
    raw = load_routing().model_dump()
    raw["universe"]["bankruptcy_statuses"] = ["99"]
    with pytest.raises(ValueError, match="банкротства"):
        RoutingPolicy.model_validate(raw)
