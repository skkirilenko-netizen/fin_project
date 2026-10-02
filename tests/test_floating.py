"""Неустановленные купоны: правило оценки по записи выпуска и сумма купона."""

from datetime import date
from decimal import Decimal

from finlib.scoring.routing import load_routing
from finlib.sources import floating


def _rules() -> dict:
    """Боевой блок методики: проверяется он сам, а не копия."""
    rules = load_routing().refinancing.floating_coupons
    assert rules, "блока refinancing.floating_coupons в методике нет"
    return rules


def test_index_plus_spread_is_estimated() -> None:
    """«Ключевая ставка + 2 %» — оценка по ставке на дату плюс спред условий."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "1-12 купоны - Ключевая ставка ЦБ РФ + 2%",
        "margin": "2",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.ESTIMATE and terms.spread == Decimal(2)


def test_a_multiplier_outside_max_min_is_a_lower_bound() -> None:
    """Множитель вне MAX/MIN под шаблон «индекс + спред» не подходит — «не менее»."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "Ключевая ставка ЦБ РФ × 1,5",
        "margin": "1",
    }
    assert floating.terms_of(record, _rules(), None).kind == floating.LOWER


def test_a_cap_is_computed_at_the_current_index() -> None:
    """Потолок — при текущей ставке: не связывает — индекс + спред, связывает — он (3.2)."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "1-48 купоны: C(i)=Cr+3,5%, но не более 18 %",
        "margin": "3.5",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.ESTIMATE and terms.cap == Decimal(18)
    found = floating.estimate(terms, Decimal(1000), 365, date(2026, 10, 1), 7)
    rate, _ = floating.rate_on(terms, date(2026, 10, 1), 7)
    expected = min(rate + Decimal("3.5"), Decimal(18))
    assert found.amount == Decimal(1000) * expected / 100
    assert ("потолок" in found.basis) == (rate + Decimal("3.5") > 18)


def test_max_with_a_divided_index_binds_the_floor() -> None:
    """MAX(Cri/2; 4,75 %) — формула внутри MAX линейна по индексу, пол связывает при низкой."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "1-29 купоны - Ci = max (Cri/2; 4.75%), где Cri Ключевая ставка",
        "margin": "0",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.ESTIMATE
    assert terms.factor == Decimal("0.5") and terms.floor == Decimal("4.75")
    low = floating.Terms(
        floating.ESTIMATE, index="key_rate", factor=Decimal("0.5"), floor=Decimal("4.75")
    )
    rate, _ = floating.rate_on(low, date(2026, 10, 1), 7)
    found = floating.estimate(low, Decimal(100), 365, date(2026, 10, 1), 7)
    assert found.amount == max(rate / 2, Decimal("4.75"))


def test_min_with_spread_inside() -> None:
    """MIN(Cr+5,75%; 15,75%) — потолок числом, спред внутри формулы."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "купоны 19-60: C(i)=MIN(Cr+5,75%; 15,75%), где MIN(А;Б) - меньшее",
        "margin": "5.75",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.ESTIMATE
    assert terms.spread == Decimal("5.75") and terms.cap == Decimal("15.75")


def test_a_key_rate_floor_of_a_hundredth_is_not_a_bound() -> None:
    """«R + 1,8 %, но не менее 0,01 %» — оценка по индексу, пол при нём не связывает."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "2-40 купоны: Сi = R + 1.8%, где Сi - ставка, но не менее 0,01% годовых",
        "margin": "1.8",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.ESTIMATE and terms.floor == Decimal("0.01")


def test_rate_written_in_the_terms_is_data() -> None:
    """Ставка, прописанная для диапазона купонов, — данные «по условиям выпуска» (3.1)."""
    record = {
        "floating_rate": "0",
        "cupon_rus": "1-9 купоны - 6% годовых, 10-129 купоны - 10.5% годовых",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.fixed == ((1, 9, Decimal(6)), (10, 129, Decimal("10.5")))
    found = floating.estimate(terms, Decimal(1000), 365, date(2026, 10, 1), 7, number=12)
    assert found.amount == Decimal(105) and found.data
    assert found.basis == "по условиям выпуска"
    # Формула после номера купона ставкой не считается.
    assert floating.fixed_rates("2-136 купоны - Кi = (CPI - 100%) + 1%") == ()
    assert floating.fixed_rates("5-6 купон - ставка рефинансирования + 3% годовых") == ()


def test_a_floor_bounds_from_below_by_number() -> None:
    """Пол в условиях — граница снизу по полу, числом."""
    record = {
        "floating_rate": "1",
        "reference_rate_name_rus": "Инфляция (Россия)",
        "cupon_rus": "ИПЦ + 1%, но не менее 4,75% годовых",
    }
    terms = floating.terms_of(record, _rules(), None)
    assert terms.kind == floating.FLOOR and terms.floor == Decimal("4.75")


def test_issuer_set_coupon_uses_the_last_known() -> None:
    """Купон, который определяет эмитент, — по последнему известному, с пометкой."""
    record = {
        "floating_rate": "0",
        "cupon_rus": "1-4 купоны - 18% годовых, 5-8 купоны - ставку определяет эмитент",
    }
    terms = floating.terms_of(record, _rules(), Decimal(18))
    assert terms.kind == floating.LAST_KNOWN
    found = floating.estimate(terms, Decimal(1000), 91, date(2026, 10, 1), 7)
    assert found.amount is not None
    assert abs(found.amount - Decimal(1000) * Decimal("0.18") * 91 / 365) < Decimal("0.0001")
    assert found.basis == "последний известный купон"


def test_the_ground_names_its_estimate_and_its_gap() -> None:
    """Формулировка называет оценку с основанием и неполноту границы."""
    routing = load_routing()
    text = routing.say(
        "refinancing_gap",
        "estimated_lower_bound",
        due="120,0",
        estimated="20,0",
        basis="ключевая ставка 14,00 % на 01.10.2026 + спред условий",
        cash="50,0",
        unit="млн руб.",
        unknown=2,
    )
    assert "не менее 120,0" in text and "из них 20,0" in text
    assert "ключевая ставка 14,00 %" in text and "основание неполное" in text
