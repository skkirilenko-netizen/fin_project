"""Проверка форвардной оценки купона флоатера: форвард, горизонт и парное сравнение."""

import sys
from datetime import date
from decimal import Decimal

from finlib.config import settings
from finlib.scoring.routing import load_routing
from finlib.sources import floating
from finlib.sources.cbonds_flows import Payment, Schedule
from finlib.sources.market import curve_of

sys.path.insert(0, str(settings.base_dir / "eval"))
import forward_index_check as check  # noqa: E402

KEY = {
    "floating_rate": "1",
    "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
    "cupon_rus": "Ключевая ставка ЦБ РФ + 2%",
    "margin": "2",
}


def _points(*pairs: tuple[str, str]) -> list[tuple[Decimal, Decimal]]:
    """Точки кривой «годы, доходность» из строк."""
    return [(Decimal(years), Decimal(value)) for years, value in pairs]


def test_months_before_keeps_the_day_or_takes_the_last_of_the_month() -> None:
    """31 марта минус месяц — 28 февраля; через год назад — тот же день."""
    assert check.months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert check.months_before(date(2026, 1, 15), 3) == date(2025, 10, 15)
    assert check.months_before(date(2026, 10, 8), 24) == date(2024, 10, 8)
    assert check.months_before(date(2026, 10, 8), 0) == date(2026, 10, 8)


def test_forward_of_a_flat_curve_is_the_curve() -> None:
    """Плоская кривая: форвард на любой отрезок равен её уровню."""
    points = _points(("0.25", "10"), ("1", "10"), ("5", "10"))
    found = check.forward_rate(points, Decimal("0.5"), Decimal("2"))
    assert found is not None and abs(found - 10) < Decimal("1e-12")


def test_forward_of_a_rising_curve_by_annual_compounding() -> None:
    """Форвард 1→2 года: (1,14² / 1,12) − 1 при точках 12 % и 14 %."""
    points = _points(("0.25", "10"), ("1", "12"), ("2", "14"))
    found = check.forward_rate(points, Decimal(1), Decimal(2))
    expected = (Decimal("1.14") ** 2 / Decimal("1.12") - 1) * 100
    assert found is not None and abs(found - expected) < Decimal("1e-12")
    assert check.forward_rate(points, Decimal(2), Decimal(1)) is None


def test_a_fixed_coupon_is_compared_by_both_methods_on_the_same_day(monkeypatch) -> None:
    """Купон 15.07–15.10.2025 на горизонтах 0 и 6: пары, отказы и направление ставки."""
    key = ((date(2025, 1, 1), Decimal(20)), (date(2025, 7, 1), Decimal(16)))
    monkeypatch.setattr(floating, "key_rate", lambda: key)
    monkeypatch.setattr(floating, "ruonia", lambda: ())
    curve = [{"period": years, "value": value} for years, value in ((0.25, 19), (1, 17), (2, 15))]
    monkeypatch.setattr(
        floating,
        "curves",
        lambda: {"2025-01-14": curve_of(curve), "2025-07-14": curve_of(curve)},
    )
    plan = Schedule(
        emission_id="1",
        nominal=Decimal(1000),
        payments=(
            Payment(
                due=date(2025, 1, 10), coupon=Decimal("52.93"), redemption=Decimal(0),
                start=date(2024, 10, 10), rate=Decimal(21), number=1,
            ),
            Payment(
                due=date(2025, 10, 15), coupon=Decimal("45.37"), redemption=Decimal(0),
                start=date(2025, 7, 15), rate=Decimal(18), number=2,
            ),
            Payment(
                due=date(2026, 1, 15), coupon=Decimal(0), redemption=Decimal(0),
                coupon_known=False, start=date(2025, 10, 15), number=3,
            ),
        ),
    )
    unparsed = KEY | {"cupon_rus": "Ключевая ставка ЦБ РФ × 1,5"}
    rules = load_routing().refinancing.floating_coupons
    series = check.Series(int(rules["rate_stale_days"]))
    rows, refused, counts = check.check(
        [(KEY, plan), (unparsed, plan), ({"floating_rate": "0"}, plan)],
        rules, series, (0, 6), 60,
    )
    assert counts["флоатеров"] == 2 and counts["графиков с записью выпуска"] == 3
    # Первый купон: на день начала ключевой на диске нет, за полгода — выпуска нет.
    assert refused["h=0: индекса на дату нет"] == 1
    assert refused["h=6: выпуска на дату ещё не было"] == 1
    assert refused["выпуск: формула не разобрана (lower_bound)"] == 1
    by_horizon = {row.horizon: row for row in rows}
    assert set(by_horizon) == {0, 6}
    late = by_horizon[6]
    # Текущий индекс на 15.01.2025 — 20 % + 2; ставка затем снизилась до 16 %.
    assert late.current == Decimal(22) and late.actual == Decimal(18)
    assert late.error(check.METHODS[0]) == Decimal(4)
    assert late.move == "снижение"
    # Форвард убывающей кривой ниже текущей точки, базис 19 − 20 = −1.
    t1 = Decimal((date(2025, 7, 15) - date(2025, 1, 15)).days) / 365
    t2 = Decimal((date(2025, 10, 15) - date(2025, 1, 15)).days) / 365
    expected = check.forward_rate(curve_of(curve), t1, t2) + 1 + 2
    assert late.forward == expected and late.forward < late.current
    assert by_horizon[0].current == Decimal(18) and by_horizon[0].move == "без изменения"
