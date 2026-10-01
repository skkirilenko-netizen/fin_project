"""Долг с обязательствами по аренде (МСФО (IFRS) 16): рядом с прежним, не вместо.

EBITDA и FFO считаются «после аренды», а долг был из одних займов. Новый
состав считается отдельными показателями; балл и маршрут остаются на прежнем,
пока переключение не решит владелец. Нераскрытая аренда — не ноль: отношение
без неё печатается оценкой снизу, а оценка сверху не считается вовсе.
"""

from datetime import date
from decimal import Decimal

from finlib.metrics.ifrs import Inputs, MetricValue, compute_all
from finlib.scoring.routing import Overrides, route

UNIT = "тыс. руб."

# Росинтер, 2025 год, тыс. руб. (разбор 30.09.2026): займы 3 008 795,
# аренда 4 421 747; денежные средства и прибыль подобраны так, чтобы
# отношения были круглыми.
BASE = {
    "ifrs.long_term_borrowings": Decimal("2000000"),
    "ifrs.short_term_borrowings": Decimal("1000000"),
    "ifrs.cash": Decimal("500000"),
    "ifrs.operating_profit": Decimal("700000"),
    "ifrs.depreciation": Decimal("-300000"),
}
LEASES = {
    "ifrs.long_term_lease_liabilities": Decimal("3500000"),
    "ifrs.short_term_lease_liabilities": Decimal("500000"),
}


def _by_code(values: dict[str, Decimal]) -> dict[str, MetricValue]:
    """Показатели МСФО по коду."""
    return {item.code: item for item in compute_all(Inputs(values, {}))}


def test_leases_enter_the_new_debt_and_not_the_old() -> None:
    """Чистый долг 2 500 000 без аренды и 6 500 000 с ней; EBITDA 1 000 000."""
    found = _by_code(BASE | LEASES)
    assert found["net_debt_ebitda"].value == Decimal("2.5")
    assert found["net_debt_ebitda_leases"].value == Decimal("6.5")
    assert found["debt_ebitda_leases"].value == Decimal("7")
    assert found["net_debt_with_leases"].value == Decimal("6500000")
    # Точная величина есть — границ рядом с ней нет.
    assert "net_debt_ebitda_leases_floor" not in found
    assert "net_debt_op_profit_leases" not in found


def test_one_lease_line_is_enough_and_the_other_is_zero() -> None:
    """Раскрыта одна строка аренды — вторая нулём, как у заёмных средств."""
    values = BASE | {"ifrs.short_term_lease_liabilities": Decimal("500000")}
    assert _by_code(values)["net_debt_with_leases"].value == Decimal("3000000")


def test_undisclosed_leases_give_a_floor_not_a_zero() -> None:
    """Аренды нет в отчётности — точной величины нет, есть «не ниже 2,5»."""
    found = _by_code(BASE)
    assert not found["net_debt_ebitda_leases"].calculable
    assert not found["debt_with_leases"].calculable
    floor = found["net_debt_ebitda_leases_floor"]
    assert floor.value == Decimal("2.5")
    assert floor.shown.startswith("не ниже 2.500")
    assert "аренда не раскрыта" in floor.shown


def test_undisclosed_leases_give_no_upper_bound() -> None:
    """Амортизации и аренды нет: оценки сверху через прибыль нет вовсе."""
    values = {key: value for key, value in BASE.items() if key != "ifrs.depreciation"}
    found = _by_code(values)
    assert not found["net_debt_op_profit_leases"].calculable
    # Прежняя граница при этом считается как считалась.
    assert found["net_debt_op_profit"].calculable


def _route(computed: tuple[MetricValue, ...], over: Overrides | None = None) -> set:
    """Основания маршрута МСФО парами «код, предмет»."""
    verdict = route(
        computed,
        unit=UNIT,
        quarantined=False,
        today=date(2026, 5, 1),
        latest_annual=date(2025, 12, 31),
        thresholds=over,
    )
    return {(item.ground, item.subject) for item in verdict.findings}


VARIANT = Overrides(
    metrics={
        "net_debt_ebitda": "net_debt_ebitda_leases",
        "net_debt_op_profit": "net_debt_op_profit_leases",
    },
    floors={"net_debt_ebitda": "net_debt_ebitda_leases_floor"},
)


def _computed(values: dict[str, Decimal]) -> tuple[MetricValue, ...]:
    """Показатели МСФО с здоровыми автономией и ликвидностью."""
    return compute_all(
        Inputs(
            values
            | {
                "ifrs.total_equity": Decimal("6"),
                "ifrs.total_assets": Decimal("10"),
                "ifrs.total_current_assets": Decimal("25"),
                "ifrs.total_current_liabilities": Decimal("10"),
            },
            {},
        )
    )


def test_live_route_keeps_the_old_definition() -> None:
    """Боевой маршрут о новом составе не знает: 2,5 — без оснований по нагрузке."""
    grounds = _route(_computed(BASE | LEASES))
    assert not any(subject == "net_debt_ebitda" for _, subject in grounds)


def test_variant_routes_by_debt_with_leases() -> None:
    """Вариант замера: 6,5 с арендой — за концом шкалы 5,0."""
    grounds = _route(_computed(BASE | LEASES), VARIANT)
    assert ("level_off_scale", "net_debt_ebitda") in grounds


def test_floor_that_proves_nothing_is_absence() -> None:
    """Аренда не раскрыта, без неё 2,5: благополучия это не доказывает."""
    grounds = _route(_computed(BASE), VARIANT)
    assert not any(
        ground in {"level_off_scale", "metric_in_lower_band"}
        and subject == "net_debt_ebitda"
        for ground, subject in grounds
    )
    assert any(ground == "data_insufficient" for ground, _ in grounds)


def test_floor_that_proves_the_burden_stands() -> None:
    """Аренда не раскрыта, без неё 6,0: нагрузка за концом шкалы доказанно."""
    values = BASE | {"ifrs.long_term_borrowings": Decimal("5500000")}
    grounds = _route(_computed(values), VARIANT)
    assert ("level_off_scale", "net_debt_ebitda") in grounds
