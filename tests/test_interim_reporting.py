"""Промежуточная отчётность: приведение потока и признаки изменения."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.interim import (
    last_annual_before,
    rolling_flow,
    same_ytd_year_before,
)
from finlib.scoring.interim import (
    Observation,
    change,
    cutoffs,
    distribution,
    findings,
    load_interim,
    percentile,
)
from finlib.standards import Standard


def _flows() -> dict[date, Decimal | None]:
    """Выручка зимнего бизнеса: год 1000, полугодие 200, прошлое полугодие 150."""
    return {
        date(2024, 12, 31): Decimal(1000),
        date(2024, 6, 30): Decimal(150),
        date(2025, 6, 30): Decimal(200),
    }


def test_the_flow_is_not_doubled() -> None:
    """Скользящий год считается тождеством, а не умножением полугодия на два.

    У зимнего бизнеса полугодие вдвое даёт 400 при настоящих 1050 — ошибка
    не в данных, а в мере, и ни один контроль сходимости её не ловит.
    """
    said = rolling_flow(_flows(), date(2025, 6, 30))
    assert said.value == Decimal(1050)
    assert said.annual == date(2024, 12, 31)
    assert said.previous == date(2024, 6, 30)


def test_the_annual_period_is_taken_as_it_is() -> None:
    """Годовой поток к двенадцати месяцам приводить не к чему."""
    said = rolling_flow(_flows(), date(2024, 12, 31))
    assert said.value == Decimal(1000)


def test_a_missing_part_refuses_the_whole() -> None:
    """Нехватка любой из трёх величин — отказ, а не приближение."""
    values = _flows()
    del values[date(2024, 6, 30)]
    said = rolling_flow(values, date(2025, 6, 30))
    assert said.value is None
    assert "прошлогоднего" in said.reason


def test_the_segment_must_match() -> None:
    """Полугодие вычитается из полугодия, а не из девяти месяцев."""
    assert same_ytd_year_before(date(2025, 9, 30)) == date(2024, 9, 30)
    assert last_annual_before(date(2025, 9, 30), set(_flows())) == date(2024, 12, 31)


def test_the_statement_says_the_reporting_is_unaudited() -> None:
    """Оговорка о неаудированности входит в саму формулировку основания."""
    policy = load_interim()
    for feature in policy.features:
        assert "{note}" in feature.statement
    said = policy.confidence.said("interim", date(2026, 6, 30))
    assert "неаудированной" in said and "30.06.2026" in said


def test_no_feature_goes_into_the_route_yet() -> None:
    """Признаки объявлены недействующими вместе с причиной.

    Объявить их действующими, не измерив прироста и упреждения, значило бы
    завести правило по двум наблюдениям.
    """
    policy = load_interim()
    assert policy.status.in_route is False
    assert policy.status.in_route_origin


def _observation(moment: date, cash: int, debt: int, kind: str = "interim") -> Observation:
    """Наблюдение с величинами признаков; скользящий год здесь не нужен."""
    from finlib.metrics.interim import Rolling

    return Observation(
        moment=moment,
        kind=kind,
        unit_code="384",
        values={
            "cash": Decimal(cash),
            "short_debt": Decimal(debt),
            "operating": Decimal(10),
        },
        standard=Standard.RSBU,
        rolling={
            name: Rolling(Decimal(10)) for name in ("cash", "short_debt", "operating")
        },
    )


def test_the_direction_is_declared_not_guessed() -> None:
    """Падение денежных средств и рост долга считаются каждый в свою сторону."""
    policy = load_interim()
    was = _observation(date(2025, 3, 31), cash=100, debt=100)
    now = _observation(date(2025, 6, 30), cash=40, debt=180)
    by_code = {item.code: item for item in policy.features}
    assert change(policy, by_code["interim_cash_drop"], was, now) == Decimal("0.6")
    assert change(policy, by_code["interim_short_debt_growth"], was, now) == Decimal(
        "0.8"
    )


def test_a_non_positive_previous_value_is_not_measured() -> None:
    """Прежняя величина, равная нулю, доли не даёт и в распределение не идёт."""
    policy = load_interim()
    was = _observation(date(2025, 3, 31), cash=0, debt=100)
    now = _observation(date(2025, 6, 30), cash=40, debt=180)
    by_code = {item.code: item for item in policy.features}
    assert change(policy, by_code["interim_cash_drop"], was, now) is None
    spread, counts = distribution(policy, {"1": (was, now)})
    assert counts["interim_cash_drop"]["мерить нечем"] == 1
    assert counts["interim_cash_drop"]["измерено"] == 0
    assert spread["interim_cash_drop"] == []


def test_the_cutoff_comes_from_the_distribution() -> None:
    """Отсечка берётся перцентилем, а не назначается числом.

    Признак без распределения отсечки не получает и не срабатывает: назначить
    её здесь значило бы вернуть магическую величину в другом файле.
    """
    policy = load_interim()
    moving = [item.code for item in policy.features if item.frozen is None]
    assert moving, "хоть один признак считается перцентилем"
    assert percentile([], 95) is None
    empty = cutoffs(policy, {item.code: [] for item in policy.features})
    assert not set(empty) & set(moving)
    values = [Decimal(number) / 100 for number in range(101)]
    edges = cutoffs(policy, {item.code: values for item in policy.features})
    assert all(edges[code] == Decimal("0.95") for code in moving)


def test_the_last_pair_decides_not_the_whole_series() -> None:
    """Признак говорит о перемене, а не о том, что когда-то срабатывало.

    Мера «сработал хотя бы раз» мерит длину ряда — то же правило, что
    у рыночного слоя.
    """
    policy = load_interim()
    edges = {item.code: Decimal("0.5") for item in policy.features}
    dropped = _observation(date(2025, 3, 31), cash=10, debt=100)
    ordered = (
        _observation(date(2024, 12, 31), cash=100, debt=100, kind="full"),
        dropped,
        _observation(date(2025, 6, 30), cash=10, debt=100),
    )
    early = findings(policy, ordered[:2], edges, date(2025, 3, 31))
    assert [item.code for item in early] == ["interim_cash_drop"]
    assert early[0].since == dropped.moment
    assert early[0].kind == "interim"
    late = findings(policy, ordered, edges, date(2025, 6, 30))
    assert late == ()


def test_a_single_set_gives_no_change() -> None:
    """Об изменении по одному комплекту сказать нечего."""
    policy = load_interim()
    one = (_observation(date(2025, 6, 30), cash=10, debt=100),)
    assert findings(policy, one, {"interim_cash_drop": Decimal("0.1")}, date(2025, 7, 1)) == ()


@pytest.mark.parametrize("kind", ["interim", "full"])
def test_the_kind_of_the_set_travels_with_the_finding(kind: str) -> None:
    """Вид комплекта хранится рядом с признаком: оговорка берётся у него."""
    policy = load_interim()
    ordered = (
        _observation(date(2024, 12, 31), cash=100, debt=100, kind="full"),
        _observation(date(2025, 6, 30), cash=10, debt=100, kind=kind),
    )
    said = findings(policy, ordered, {"interim_cash_drop": Decimal("0.5")}, date(2025, 7, 1))
    assert said[0].kind == kind


def test_the_cash_drop_cutoff_is_frozen_on_the_day_of_measurement() -> None:
    """Отсечка падения денежных средств не едет вместе с распределением.

    Решение владельца 28.09.2026: заморожена днём замера 27.09.2026, в маршрут
    признак не идёт, в карточке — справочно.
    """
    policy = load_interim()
    by_code = {item.code: item for item in policy.features}
    cash = by_code["interim_cash_drop"]
    assert cash.frozen is not None and cash.frozen.measured_on == date(2026, 9, 27)
    assert cash.decision is not None
    assert not cash.decision.in_route and cash.decision.card == "reference"
    # Какое бы распределение ни пришло, отсечка — замороженная.
    edges = cutoffs(policy, {"interim_cash_drop": [Decimal("0.1")] * 100})
    assert edges["interim_cash_drop"] == cash.frozen.value
    # Незамороженные признаки по-прежнему считаются перцентилем.
    assert by_code["interim_short_debt_growth"].frozen is None
