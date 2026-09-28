"""Пороги варианта калибровки: вопрос «а если порог другой» задаётся маршруту.

Калибровка фазы 6 спрашивает историю о других порогах, и отвечать обязан
боевой маршрут: пересчёт основания по записанным величинам в замере был бы
вторым путём к вердикту — без гашения стоп-фактором, без типа эмитента
и полосы у края шкалы. Замена порогов — довод замера, как `blind`.
"""

from datetime import date
from decimal import Decimal

from finlib.metrics.ifrs import MetricValue
from finlib.scoring.routing import Overrides, Refinance, route
from finlib.scoring.routing_catalogue import catalogue_for
from finlib.standards import Standard

UNIT = "тыс. руб."


def _metric(code: str, value: str) -> MetricValue:
    """Рассчитанный показатель для маршрута."""
    return MetricValue(
        code=code, name=code, group="debt", in_scoring=True, value=Decimal(value)
    )


def _values(debt: str = "1.0", equity: str = "0.6", liquidity: str = "2.5") -> tuple:
    """Величины МСФО: по умолчанию здоровые."""
    return (
        _metric("net_debt_ebitda", debt),
        _metric("equity_ratio", equity),
        _metric("cur_liq", liquidity),
    )


def _grounds(computed: tuple, **kwargs) -> set[tuple[str, str]]:
    """Основания маршрута парами «код, предмет»."""
    verdict = route(
        computed,
        unit=UNIT,
        quarantined=False,
        today=date(2026, 5, 1),
        latest_annual=date(2025, 12, 31),
        **kwargs,
    )
    return {(item.ground, item.subject) for item in verdict.findings}


def test_no_override_is_the_live_route() -> None:
    """Пустая замена — тот же вердикт, что без неё: вариант «прежний» и есть боевой."""
    computed = _values(debt="4.0", equity="0.10", liquidity="0.9")
    assert _grounds(computed, thresholds=Overrides()) == _grounds(computed)


def test_review_override_moves_the_edge_in_the_bad_direction() -> None:
    """Долговая нагрузка 4,0: по шкале нижняя часть, с отсечкой 3,8 — за концом."""
    computed = _values(debt="4.0")
    assert ("metric_in_lower_band", "net_debt_ebitda") in _grounds(computed)
    moved = Overrides(review={"net_debt_ebitda": Decimal("3.8")})
    assert ("level_off_scale", "net_debt_ebitda") in _grounds(
        computed, thresholds=moved
    )


def test_low_side_metric_is_judged_from_below() -> None:
    """Ликвидность хуже снизу: отсечка 1,2 ставит 1,1 за конец шкалы, а 1,3 — нет."""
    moved = Overrides(review={"cur_liq": Decimal("1.2")})
    assert ("level_off_scale", "cur_liq") in _grounds(
        _values(liquidity="1.1"), thresholds=moved
    )
    assert not any(
        subject == "cur_liq"
        for _, subject in _grounds(_values(liquidity="1.3"), thresholds=moved)
    )


def test_attention_override_replaces_the_lower_band() -> None:
    """Автономия 0,30 по шкале здорова; с отсечкой нижней части 0,35 — нет."""
    moved = Overrides(attention={"equity_ratio": Decimal("0.35")})
    assert ("metric_in_lower_band", "equity_ratio") in _grounds(
        _values(equity="0.30"), thresholds=moved
    )
    assert ("metric_in_lower_band", "equity_ratio") not in _grounds(
        _values(equity="0.30")
    )


def test_override_applies_only_to_its_standard_and_branch() -> None:
    """Замена для РСБУ не трогает МСФО; замена для лизинга — прочие отрасли."""
    computed = _values(equity="0.30")
    rsbu_only = Overrides(
        attention={"equity_ratio": Decimal("0.35")}, standard=Standard.RSBU
    )
    assert _grounds(
        computed, thresholds=rsbu_only, catalogue=catalogue_for(Standard.IFRS)
    ) == _grounds(computed)
    leasing = Overrides(
        attention={"equity_ratio": Decimal("0.35")},
        branch_in=frozenset({"Лизинг и аренда"}),
    )
    assert _grounds(computed, thresholds=leasing, branch="Нефтегаз") == _grounds(
        computed, branch="Нефтегаз"
    )
    assert ("metric_in_lower_band", "equity_ratio") in _grounds(
        computed, thresholds=leasing, branch="Лизинг и аренда"
    )


def test_cover_override_moves_the_refinancing_cut() -> None:
    """Платежи 150 при деньгах 100: при отсечке 1 не хватает, при 2 — хватает."""
    money = Refinance(
        due=Decimal(150), offered=Decimal(0), cash=Decimal(100), unit=UNIT, days=365
    )
    computed = _values()
    assert ("refinancing_gap", "refinancing") in _grounds(computed, refinance=money)
    wider = Overrides(cover={"refinancing_gap": Decimal(2)})
    assert ("refinancing_gap", "refinancing") not in _grounds(
        computed, refinance=money, thresholds=wider
    )
