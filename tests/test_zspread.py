"""Z-спред: надбавка к кривой, сводящая поток к цене, и отказы потока."""

from datetime import date
from decimal import Decimal

from finlib.sources.zspread import Flow, cash_flow, settlement, z_spread


def _price(pairs: list[tuple[Decimal, Decimal]], rate: Decimal, extra: Decimal) -> Decimal:
    """Цена потока при плоской кривой и надбавке, годовой компаундинг."""
    level = 1 + rate / 100 + extra / 10000
    return sum((amount / level**years for years, amount in pairs), Decimal(0))


def test_a_bond_priced_on_the_curve_has_zero_z() -> None:
    """Цена по самой кривой — Z ноль; цена по кривой плюс 100 б. п. — Z сто."""
    pairs = [(Decimal(1), Decimal(100)), (Decimal(2), Decimal(1100))]
    rates = [Decimal(15), Decimal(15)]
    assert abs(z_spread(pairs, rates, _price(pairs, Decimal(15), Decimal(0)))) < Decimal("0.01")
    found = z_spread(pairs, rates, _price(pairs, Decimal(15), Decimal(100)))
    assert found is not None and abs(found - 100) < Decimal("0.01")


def test_continuous_and_annual_conventions_differ() -> None:
    """Одна цена, две конвенции — два ответа: проверка на ОФЗ выбирает между ними."""
    pairs = [(Decimal(3), Decimal(1000))]
    rates = [Decimal(15)]
    price = _price(pairs, Decimal(15), Decimal(0))
    annual = z_spread(pairs, rates, price, "annual")
    continuous = z_spread(pairs, rates, price, "continuous")
    assert annual is not None and continuous is not None
    assert abs(annual) < 1 and continuous < -100


def test_an_unknown_coupon_refuses_and_an_offer_cuts_the_flow() -> None:
    """Купон не объявлен — Z нет; оферта обрезает поток и выкупает остаток."""
    settle = settlement(date(2026, 9, 30))
    flows = [
        Flow(date(2027, 3, 1), Decimal(50), Decimal(0)),
        Flow(date(2027, 9, 1), None, Decimal(0)),
        Flow(date(2028, 9, 1), Decimal(50), Decimal(1000)),
    ]
    pairs, why = cash_flow(flows, settle, Decimal(1000), None, Decimal(100))
    assert not pairs and why == "будущий купон не объявлен"
    pairs, why = cash_flow(flows, settle, Decimal(1000), date(2027, 3, 1), Decimal(100))
    assert not why and [amount for _, amount in pairs] == [Decimal(50), Decimal(1000)]


def test_redemptions_must_meet_the_face_value() -> None:
    """Погашения графика не сходятся с номиналом биржи — поток не тот, Z нет."""
    flows = [Flow(date(2027, 9, 1), Decimal(50), Decimal(500))]
    _, why = cash_flow(flows, date(2026, 10, 1), Decimal(1000), None, Decimal(100))
    assert why == "погашения графика не сходятся с номиналом биржи"


def test_settlement_skips_the_weekend() -> None:
    """Расчёты — следующий рабочий день: пятница → понедельник."""
    assert settlement(date(2026, 10, 2)) == date(2026, 10, 5)
