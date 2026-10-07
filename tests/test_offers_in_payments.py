"""Шаг 2 рефинансирования: оферты окна в платежах года, а не второй мерой.

Решения владельца 01.10.2026 и 07.10.2026: у выпуска с офертой в окне
платежи графика — до дня выкупа включительно, к ним прибавляется объём
в обращении на этот день (после погашений графика, по цене оферты, когда
она названа), платежи после выкупа снимаются, а основание об офертах
не выставляется. Выключено по умолчанию — и тогда всё прежнее.
"""

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.scoring import routing_store
from finlib.scoring.routing import Refinance, load_routing, route
from finlib.sources import cbonds_flows
from finlib.sources.cbonds_flows import Payment, Schedule, refinancing

TODAY = date(2026, 9, 22)
KINDS = ("put", "доп. оферта")


@dataclass(frozen=True)
class FakeIssue:
    """Выпуск: столько, сколько нужно расчёту платежей."""

    emission_id: str
    status: str = "в обращении"
    outstanding: Decimal | None = Decimal(1000)


@pytest.fixture()
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Каталог ответов источника на время теста."""
    monkeypatch.setattr(cbonds_flows, "CACHE", tmp_path)
    return tmp_path


def flow(folder: Path, rows: list[tuple[str, str, str]]) -> None:
    """График выпуска «1»: срок, купон и погашение на бумагу номиналом 1 000."""
    items = [
        {
            "date": when,
            "cupon_sum": amount,
            "redemtion": redemption,
            "emission_nominal_price": "1000",
        }
        for when, amount, redemption in rows
    ]
    (folder / "flow_1.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
    )


def offert(folder: Path, records: list[dict]) -> None:
    """Ответ источника об офертах выпуска «1»."""
    (folder / "offert_1.json").write_text(
        json.dumps({"items": records}, ensure_ascii=False), encoding="utf-8"
    )


def plan_of(on: bool, issue: FakeIssue | None = None) -> cbonds_flows.Refinancing:
    """Платежи года по выпуску «1» с шагом 2 либо без него."""
    return refinancing(
        (issue or FakeIssue("1"),), 365, TODAY, KINDS, offers_in_payments=on
    )


def test_flag_off_keeps_the_two_measures(cache: Path) -> None:
    """Выключено — платежи года прежние, оферта во второй мере, доли нет."""
    flow(cache, [("2026-10-05", "10", "0"), ("2026-12-01", "10", "0"),
                 ("2027-06-01", "10", "1000")])
    offert(cache, [{"date": "2026-12-01", "type_rus": "put"}])
    plan = plan_of(False)
    assert plan.scheduled == Decimal(1030)
    assert plan.offered == Decimal(1000)
    assert plan.offers_in_due == 0


def test_offer_replaces_payments_after_the_buyback(cache: Path) -> None:
    """Платежи до выкупа включительно, затем объём; дальше график не платится."""
    flow(cache, [("2026-10-05", "10", "0"), ("2026-12-01", "10", "0"),
                 ("2027-03-01", "10", "0"), ("2027-06-01", "10", "1000")])
    offert(cache, [{"date": "2026-12-01", "date_open": "2026-11-24",
                    "date_close": "2026-11-28", "type_rus": "put"}])
    plan = plan_of(True)
    # Купон дня выкупа в платежах (решение владельца 07.10.2026).
    assert plan.scheduled == Decimal(10) + Decimal(10) + Decimal(1000)
    assert plan.offers_in_due == Decimal(1000)
    # Вторая мера считается по-прежнему — для карточки и замера.
    assert plan.offered == Decimal(1000)


def test_amortisation_before_the_buyback_reduces_the_volume(cache: Path) -> None:
    """Объём на день выкупа — после погашений графика, а не первоначальный."""
    flow(cache, [("2026-10-05", "10", "250"), ("2027-04-01", "5", "250")])
    offert(cache, [{"date": "2027-01-15", "type_rus": "put"}])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(260) + Decimal(750)
    assert plan.offers_in_due == Decimal(750)


def test_redemption_on_the_buyback_day_is_not_counted_twice(cache: Path) -> None:
    """Погашение графика в день выкупа платится графиком, объём после него ноль."""
    flow(cache, [("2026-12-01", "10", "1000")])
    offert(cache, [{"date": "2026-12-01", "type_rus": "put"}])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(1010)
    assert plan.offers_in_due == 0


def test_offer_price_scales_the_volume(cache: Path) -> None:
    """Цена оферты названа — объём по ней, а не по номиналу."""
    flow(cache, [("2026-10-05", "10", "0")])
    offert(cache, [{"date": "2026-12-01", "type_rus": "put", "price": "98.5"}])
    plan = plan_of(True)
    assert plan.offers_in_due == Decimal(985)
    assert plan.scheduled == Decimal(10) + Decimal(985)


def test_presentation_inside_buyback_outside_the_window_counts(cache: Path) -> None:
    """Предъявление в окне, выкуп за краем — оферта учитывается.

    Окно 22.09.2026 — 22.09.2027. Период предъявления 15–20.09.2027 в окне,
    выкуп 05.10.2027 за ним: платежи графика — до края окна, объём —
    на остаток после погашений окна (решение владельца 07.10.2026).
    """
    flow(cache, [("2027-03-01", "10", "0"), ("2027-09-30", "10", "0"),
                 ("2027-10-05", "10", "0")])
    offert(cache, [{"date": "2027-10-05", "date_open": "2027-09-15",
                    "date_close": "2027-09-20", "type_rus": "put"}])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(10) + Decimal(1000)
    assert plan.offers_in_due == Decimal(1000)
    # Вторая мера смотрит на дату опциона и такой оферты не видит.
    assert plan.offered == 0


def test_the_first_offer_of_the_window_is_taken(cache: Path) -> None:
    """Две оферты в окне — выкуп по первой, платежи после неё сняты."""
    flow(cache, [("2026-12-01", "10", "0"), ("2027-04-01", "10", "0"),
                 ("2027-09-01", "10", "0")])
    offert(cache, [{"date": "2027-08-01", "type_rus": "put"},
                   {"date": "2027-02-01", "type_rus": "доп. оферта"}])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(10) + Decimal(1000)


def test_call_and_offers_outside_the_window_change_nothing(cache: Path) -> None:
    """Call — право эмитента; оферта за окном в платежи года не входит."""
    flow(cache, [("2026-12-01", "10", "0"), ("2027-04-01", "10", "0")])
    offert(cache, [{"date": "2026-11-01", "type_rus": "call"},
                   {"date": "2027-12-01", "type_rus": "put"}])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(20)
    assert plan.offers_in_due == 0


def test_missing_offers_answer_keeps_the_schedule(cache: Path) -> None:
    """Ответа об офертах нет — платежи прежние, пробел назван счётчиком."""
    flow(cache, [("2026-12-01", "10", "0")])
    plan = plan_of(True)
    assert plan.scheduled == Decimal(10)
    assert plan.offers_in_due == 0
    assert plan.without_offers == 1


def test_coupon_after_the_buyback_is_not_estimated(cache: Path) -> None:
    """Неустановленный купон после выкупа не оценивается — он снят."""
    flow(cache, [("2026-11-01", "", "0"), ("2027-05-01", "", "0")])
    offert(cache, [{"date": "2027-01-15", "type_rus": "put"}])

    def estimator(
        emission: str, item: Payment, plan: Schedule, today: date
    ) -> tuple[Decimal | None, str, bool]:
        return Decimal(7), "оценка", False

    plan = refinancing(
        (FakeIssue("1"),), 365, TODAY, KINDS, estimator=estimator,
        offers_in_payments=True,
    )
    assert plan.estimated == Decimal(7)
    assert plan.scheduled == Decimal(7) + Decimal(1000)


def test_every_statement_has_an_offers_variant() -> None:
    """У каждой формулировки платежей года объявлен вариант «в т. ч. оферты».

    Без него печать взяла бы `default` и спрятала оферты в сумме.
    """
    templates = load_routing().statements.by_ground["refinancing_gap"]
    plain = [name for name in templates if not name.endswith("offers")]
    assert plain
    for name in plain:
        variant = "offers" if name == "default" else f"{name}_offers"
        assert variant in templates, variant
        assert "{offers}" in templates[variant]


def test_flag_is_off_by_default() -> None:
    """Методика объявляет шаг 2 выключенным: маршрут прежний до решения."""
    assert load_routing().refinancing.offers_in_payments is False


def _verdict(offers_in_due: Decimal | None):  # noqa: ANN202
    """Маршрут по одной мере рефинансирования."""
    plan = Refinance(Decimal(100), Decimal(1), "тыс. руб.", 365,
                     offered=Decimal(80), offers_in_due=offers_in_due)
    return route((), unit=plan.unit, quarantined=False, refinance=plan,
                 latest_annual=date(2025, 12, 31), today=date(2026, 10, 1),
                 routing=load_routing())


def test_without_step_two_both_grounds_stand() -> None:
    """Доли оферт нет — две меры, как до шага 2."""
    verdict = _verdict(None)
    assert "refinancing_gap" in verdict.grounds
    assert "refinancing_offers" in verdict.grounds


def test_step_two_drops_the_offers_ground_and_names_offers() -> None:
    """Шаг 2: основание об офертах уходит, формулировка называет их долю."""
    policy = load_routing()
    verdict = _verdict(Decimal(80))
    assert "refinancing_offers" not in verdict.grounds
    finding = next(item for item in verdict.findings if item.ground == "refinancing_gap")
    assert finding.key == "offers"
    printed = finding.worded(policy, lambda value, unit: (str(value / 1000), "млн руб."))
    assert "платежи по облигациям в окне 0.1, в т. ч. оферты 0.08" in printed
    # Оферт в окне нет — формулировка прежняя, основания об офертах нет.
    quiet = _verdict(Decimal(0))
    assert "refinancing_offers" not in quiet.grounds
    plain = next(item for item in quiet.findings if item.ground == "refinancing_gap")
    assert plain.key == ""


def test_fingerprint_is_unchanged_without_step_two() -> None:
    """Отпечаток прежний, пока шаг 2 выключен: история не меняется «у нас»."""
    plan = Refinance(Decimal(100), Decimal(1), "тыс. руб.", 365, offered=Decimal(80))
    assert routing_store._rendered(plan) == "100:80:1:тыс. руб."
    on = Refinance(Decimal(100), Decimal(1), "тыс. руб.", 365,
                   offered=Decimal(80), offers_in_due=Decimal(80))
    assert routing_store._rendered(on) == "100:80:1:тыс. руб.:offers=80"
