"""Периметр по выпускам: без выпусков в обращении — «Вне периметра методики».

Решение владельца 09.10.2026 по диагностике `eval/audit_diag.py` (market-gaps):
204 эмитента без выпусков в обращении стояли во всех корзинах. Выпуск есть
и у размещаемого, и у планируемого; банкротство, неурегулированный дефолт
и решение человека удерживают эмитента по прежним правилам; нет ответа
источника о выпусках — не «нет выпусков». Случаи — синтетика.
"""

import sys
from datetime import date
from decimal import Decimal

import pytest

from finlib.config import settings
from finlib.metrics.ifrs import MetricValue
from finlib.scoring.routing import ManualFloor, RoutingPolicy, load_routing, route
from finlib.sources.cbonds_events import DefaultRecord, Issue, IssuerEvents

sys.path.insert(0, str(settings.base_dir / "eval"))

import change_report_run as report  # noqa: E402

UNIT = "тыс. руб."
TODAY = date(2026, 10, 9)


def _healthy() -> tuple[MetricValue, ...]:
    """Величины без оснований."""
    return tuple(
        MetricValue(code=code, name=code, group="debt", in_scoring=True, value=Decimal(value))
        for code, value in (("net_debt_ebitda", "1.0"), ("equity_ratio", "0.6"),
                            ("cur_liq", "2.5"))
    )


def _issue(status: str, *, unsettled: bool = False, maturity: date = date(2030, 1, 1)) -> Issue:
    """Выпуск в названном статусе."""
    return Issue(
        emission_id=status, name=status, isin=f"RU{len(status)}", status=status,
        default=unsettled, unsettled=unsettled, maturity=maturity, offer=None,
        outstanding=None, updated=date(2026, 9, 1),
    )


def _events(*issues: Issue, known: bool = True, records: tuple = ()) -> IssuerEvents:
    """События эмитента: выпуски и, по желанию, события дефолта."""
    return IssuerEvents(inn="1", issues=issues, issues_known=known,
                        records=records, records_known=bool(records))


def _routed(events: IssuerEvents | None, **given: object):  # noqa: ANN202
    """Вердикт при здоровых величинах и свежей отчётности."""
    return route(_healthy(), unit=UNIT, quarantined=False, events=events,
                 latest_annual=date(2025, 12, 31), today=TODAY, **given)


def test_without_bonds_outstanding_the_issuer_is_out_of_scope() -> None:
    """Все выпуски погашены либо выпусков нет вовсе — «Вне периметра», основание названо."""
    verdict = _routed(_events(_issue("погашена"), _issue("досрочно погашена")))
    assert verdict.basket == "out_of_scope"
    assert verdict.grounds == ("no_bonds_outstanding",)
    said = next(item for item in verdict.findings if item.ground == "no_bonds_outstanding")
    assert "выпусков у источника 2" in said.text and "погашена 1" in said.text
    assert _routed(_events()).basket == "out_of_scope"


@pytest.mark.parametrize("status", ["в обращении", "размещается", "планируется"])
def test_an_issue_in_circulation_placing_or_planned_keeps_the_route(status: str) -> None:
    """Выпуск в обращении, размещаемый или планируемый — выпуск есть."""
    verdict = _routed(_events(_issue("погашена"), _issue(status)))
    assert verdict.basket == "clear"


def test_no_answer_about_issues_is_not_no_issues() -> None:
    """Нет ответа источника о выпусках — прежние правила, а не «Вне периметра»."""
    assert _routed(_events(known=False)).basket == "clear"
    assert _routed(None).basket == "clear"


def test_default_bankruptcy_and_human_decision_keep_the_route() -> None:
    """Неурегулированный дефолт, банкротство и решение человека удерживают эмитента."""
    fresh = _issue("дефолт по погашению", unsettled=True, maturity=date(2026, 6, 1))
    record = DefaultRecord(emission_id=fresh.emission_id, kind="Погашение", status="Дефолт",
                           due=date(2026, 6, 1), when=date(2026, 6, 1), announced=None,
                           met=None, amount=None)
    verdict = _routed(_events(fresh, records=(record,)))
    assert verdict.basket == "review" and "emission_default" in verdict.grounds
    stale = _issue("дефолт по погашению", unsettled=True, maturity=date(2019, 6, 1))
    old = DefaultRecord(emission_id=stale.emission_id, kind="Погашение", status="Дефолт",
                        due=date(2019, 6, 1), when=date(2019, 6, 1), announced=None,
                        met=None, amount=None)
    verdict = _routed(_events(stale, records=(old,)))
    assert verdict.basket == "attention" and "default_unsettled_stale" in verdict.grounds
    verdict = _routed(_events(_issue("погашена")), bankruptcy="идёт процедура банкротства")
    assert verdict.basket == "review" and "bankruptcy_proceedings" in verdict.grounds
    floor = ManualFloor(basket="attention", author="владелец", reason="наблюдение",
                        decided_on=date(2026, 9, 22), valid_until=date(2027, 4, 22))
    verdict = _routed(_events(_issue("погашена")), manual_floor=floor)
    assert verdict.basket == "attention" and "manual_floor" in verdict.grounds


def test_the_perimeter_is_older_than_the_status_queue_and_attention() -> None:
    """Давность отчётности и стоп-фактор без выпусков не удерживают: остаются в перечне."""
    verdict = route(_healthy(), unit=UNIT, quarantined=False, events=_events(_issue("погашена")),
                    latest_annual=date(2022, 12, 31), today=TODAY,
                    stop_factors=("negative_nwc",))
    assert verdict.basket == "out_of_scope"
    assert {"reporting_two_cycles_old", "stop_factor_capped"} <= {
        item.ground for item in verdict.findings
    }


def test_a_stray_keeping_ground_does_not_load() -> None:
    """Удерживающее основание, которого нет, — справочник не грузится."""
    raw = load_routing().model_dump()
    raw["perimeter"]["keeps_route"] = [*raw["perimeter"]["keeps_route"], "нет_такого"]
    with pytest.raises(ValueError, match="периметр"):
        RoutingPolicy.model_validate(raw)


def test_the_change_report_names_who_left_for_no_bonds() -> None:
    """Отчёт изменений называет вышедших по периметру отдельно."""
    was = {"1": {"grounds": ["stop_factor_capped"]}, "2": {"grounds": []},
           "3": {"grounds": ["no_bonds_outstanding"]}}
    now = {"1": {"grounds": ["no_bonds_outstanding"]}, "2": {"grounds": []},
           "3": {"grounds": ["no_bonds_outstanding"]}, "4": {"grounds": ["no_bonds_outstanding"]}}
    assert report.no_bonds_left(was, now) == ["1"]


def test_the_impact_names_the_cause_of_a_move() -> None:
    """Замер влияния называет причину смены, а не приписывает всё периметру."""
    import no_bonds_impact as impact

    assert impact.cause(("no_bonds_outstanding",)) == "периметр: нет выпусков в обращении"
    assert impact.cause(("bankruptcy_proceedings", "emission_default")) == "банкротство в карточке"
    assert impact.cause(("stop_factor_capped",)) == "иное: данные или календарь"
