"""Неустановленный купон флоатера в потоке к PV: оценка v2 за параметром, синтетика.

Решение владельца 08.10.2026: та же оценка, что в рефинансировании (индекс +
спред при индексе дня торгов, пол и потолок, ставка по условиям, последний
известный купон); пол без индекса — нет; «по номиналу» — только где оценки
нет. В маршрут не включается без замера: `distress_zone.pv_floating: false`.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from finlib.scoring.routing import load_routing
from finlib.sources import cbonds, cbonds_flows, floating
from finlib.sources.market import Distress, Point, _of_day, _read, _written, load_market
from finlib.sources.zspread import price_to_pv

TRADE = date(2090, 1, 2)
CURVE = Decimal(15)
KEY = Decimal(16)
ON_KEY = (
    {
        "floating_rate": "1",
        "reference_rate_name_rus": "Ключевая ставка ЦБ РФ",
        "cupon_rus": "1-12 купоны - Ключевая ставка ЦБ РФ + 2%",
        "margin": "2",
    }
)
DUES = [date(2090 + number, 1, 10) for number in range(4)]


def _rules() -> dict:
    """Боевой блок правил оценки: проверяется он сам, а не копия."""
    rules = load_routing().refinancing.floating_coupons
    assert rules
    return rules


def _graph(root: Path, emission: str, coupons: list[str]) -> None:
    """График на одну бумагу номиналом 1000: первый купон объявлен, ставка 15 %."""
    items = [
        {
            "date": f"{due}",
            "start_date": f"{date(due.year - 1, 1, 10)}",
            "coupon_num": str(number + 1),
            "cupon_sum": coupon,
            "cupon_rate": "0.15" if coupon else "",
            "redemtion": "1000" if number == len(DUES) - 1 else "0",
            "emission_nominal_price": "1000",
        }
        for number, (due, coupon) in enumerate(zip(DUES, coupons, strict=True))
    ]
    (root / f"flow_{emission}.json").write_text(json.dumps({"items": items}), encoding="utf-8")


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    """Графики на диске и записи выпусков: у флоатера будущие купоны не объявлены."""
    monkeypatch.setattr(cbonds, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds_flows, "CACHE", tmp_path)
    monkeypatch.setattr(floating, "key_rate", lambda: ((date(2089, 12, 1), KEY),))
    records: dict[str, dict] = {}
    monkeypatch.setattr(floating, "record_of", lambda emission: records.get(emission))
    for emission in ("1", "2", "3", "4"):
        _graph(tmp_path, emission, ["150", "", "", ""])
    return records


def _pv(emission: str, coupons=None):  # noqa: ANN001, ANN202
    """Отношение к PV бумаги `RU<выпуск>` по кривой 15 % и цене 90 %."""
    return price_to_pv(
        {"SECID": f"RU{emission}", "FACEVALUE": "1000", "ACCINT": "0"},
        TRADE, lambda years: CURVE, Decimal(90), {f"RU{emission}": emission}, coupons,
    )


def _amount(rate: Decimal, due: date) -> str:
    """Купон на одну бумагу правилом рефинансирования: ставка × номинал × дни / 365."""
    days = (due - date(due.year - 1, 1, 10)).days
    return str(Decimal(1000) * rate / 100 * days / 365)


def test_without_the_parameter_an_unset_coupon_leaves_no_flow(disk: dict) -> None:
    """Без оценки — как прежде: «будущий купон не объявлен», отношения нет."""
    disk["1"] = ON_KEY
    got = _pv("1")
    assert got.ratio is None and got.why == "будущий купон не объявлен"


def test_index_plus_spread_at_the_trade_day_enters_the_flow(
    disk: dict, tmp_path: Path
) -> None:
    """Ключевая 16 % + 2 % — купоны по 18 %, PV тот же, что у графика с этими суммами."""
    disk["1"] = ON_KEY
    got = _pv("1", floating.pv_coupons(_rules()))
    rate = KEY + 2
    _graph(tmp_path, "9", ["150", *(_amount(rate, due) for due in DUES[1:])])
    same = _pv("9")
    assert got.ratio is not None and got.estimated and not same.estimated
    assert (got.ratio, got.pv) == (same.ratio, same.pv)


def test_a_binding_floor_with_an_index_is_taken(disk: dict, tmp_path: Path) -> None:
    """Пол при индексе дня связывает: 16 % + 2 % < 20 % — купоны по 20 %."""
    disk["2"] = ON_KEY | {"cupon_rus": "Ключевая ставка ЦБ РФ + 2%, но не менее 20%"}
    got = _pv("2", floating.pv_coupons(_rules()))
    _graph(tmp_path, "9", ["150", *(_amount(Decimal(20), due) for due in DUES[1:])])
    assert got.estimated and got.pv == _pv("9").pv


def test_a_floor_without_an_index_is_not_an_estimate(disk: dict) -> None:
    """ИПЦ с полом — граница снизу: в поток не идёт, бумага без потока."""
    disk["3"] = {
        "floating_rate": "1",
        "reference_rate_name_rus": "ИПЦ",
        "cupon_rus": "ИПЦ + 1%, но не менее 10%",
    }
    assert floating.terms_of(disk["3"], _rules(), None).kind == floating.FLOOR
    got = _pv("3", floating.pv_coupons(_rules()))
    assert got.ratio is None and got.why == "будущий купон не оценён"


def test_the_last_known_coupon_is_an_estimate(disk: dict, tmp_path: Path) -> None:
    """Купон устанавливает эмитент — последний известный (15 %), как в рефинансировании."""
    disk["4"] = {"floating_rate": "0", "cupon_rus": "Ставка купона определяется эмитентом"}
    got = _pv("4", floating.pv_coupons(_rules()))
    _graph(tmp_path, "9", ["150", *(_amount(Decimal(15), due) for due in DUES[1:])])
    assert got.estimated and got.pv == _pv("9").pv


def test_the_series_writes_the_estimate_mark_only_when_set() -> None:
    """Точка без оценки пишется как прежде; «да» читается обратно."""
    flowed = _pv_point(estimated=False)
    marked = _pv_point(estimated=True)
    market = _read(json.loads(_written(_one(flowed))))
    assert "ratio_estimated" not in _written(_one(flowed))
    assert not next(iter(market.issuers["1"].values())).ratio_estimated
    again = _read(json.loads(_written(_one(marked))))
    assert next(iter(again.issuers["1"].values())).ratio_estimated


def _pv_point(estimated: bool) -> Point:
    """Точка дня с отношением бумаги и признаком оценки."""
    from finlib.sources.zspread import PriceToPv

    flowed = PriceToPv(Decimal("0.5"), Decimal(45), Decimal(90), "", estimated=estimated)
    return _of_day(TRADE, [(None, Decimal(1), Decimal(45), flowed)])


def _one(point: Point):  # noqa: ANN202
    from finlib.sources.market import Market

    return Market({TRADE: Decimal(100)}, {"1": {TRADE: point}}, {}, {}, 1, 1, ratios=True)


def test_the_parameter_is_declared_off_and_has_no_default() -> None:
    """В методике выключено с основанием; без явного значения методика не грузится."""
    zone = load_market().distress_zone
    assert zone.pv_floating is False and zone.pv_floating_origin
    raw = zone.model_dump()
    raw.pop("pv_floating")
    with pytest.raises(ValidationError):
        Distress(**raw)


def test_an_estimated_pv_is_named_by_the_statement() -> None:
    """Основание по PV с оценённым купоном печатается с суффиксом владельца."""
    from finlib.scoring.market import MarketFinding

    policy = load_market()
    finding = MarketFinding(
        ground=policy.distress_zone.ground, basket="review", subgroup="", escalation=False,
        value=Decimal(50), threshold=Decimal(60), since=TRADE, measure="pv_kbd",
        priced_on=TRADE, price=Decimal(45), pv=Decimal(90), pv_estimated=True,
    )
    assert finding.variant == "pv_estimated"
    text = load_routing().say(finding.ground, finding.variant, **finding.slots(policy))
    assert text.startswith("Цена 45,0 % при PV по КБД 90,0 %, отношение 0,50")
    assert text.endswith("; PV включает оценку неустановленных купонов флоатера")
    plain = load_routing().say(
        finding.ground, "pv", **finding.slots(policy)
    )
    assert text == plain + "; PV включает оценку неустановленных купонов флоатера"


def _curve(monkeypatch: pytest.MonkeyPatch, *pairs: tuple[str, str]) -> list:
    """КБД дня торгов из пар «годы, доходность»; RUONIA на диске нет."""
    points = [(Decimal(years), Decimal(value)) for years, value in pairs]
    monkeypatch.setattr(floating, "curves", lambda: {f"{TRADE}": points})
    monkeypatch.setattr(floating, "ruonia", lambda: ())
    return points


def _hybrid(emission: str) -> dict[date, Decimal]:
    """Оценки гибридом с параметрами методики."""
    zone = load_market().distress_zone
    coupons = floating.pv_coupons(
        _rules(), floating.HYBRID, zone.pv_floating_near_months, zone.pv_floating_basis_days
    )
    return coupons(emission, TRADE)


def test_hybrid_on_a_flat_curve_with_its_basis_is_the_current_index(
    disk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Плоская КБД 15 % при ключевой 16 %: базис −1, форвард ключевой — те же 16 %."""
    disk["1"] = ON_KEY
    _curve(monkeypatch, ("0.25", "15"), ("1", "15"), ("10", "15"))
    current = floating.pv_coupons(_rules())("1", TRADE)
    hybrid = _hybrid("1")
    assert set(hybrid) == set(current) == set(DUES[1:])
    for due in DUES[1:]:
        assert abs(hybrid[due] - current[due]) < Decimal("1e-9")


def test_hybrid_takes_the_forward_beyond_the_near_months(
    disk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Растущая КБД: ближний период — текущий индекс, дальние — форвард минус базис."""
    disk["1"] = ON_KEY
    points = _curve(monkeypatch, ("0.25", "15"), ("1", "16"), ("2", "17"), ("5", "18"))
    hybrid = _hybrid("1")
    near, far = DUES[1], DUES[2]
    # Период 10.01.2090–10.01.2091 начинается через 8 дней — текущий индекс.
    assert hybrid[near] == Decimal(_amount(KEY + 2, near))
    start = date(far.year - 1, 1, 10)
    t1 = Decimal((start - TRADE).days) / 365
    t2 = Decimal((far - TRADE).days) / 365
    index = floating.forward_rate(points, t1, t2) - (Decimal(15) - KEY)
    days = (far - start).days
    assert hybrid[far] == Decimal(1000) * (index + 2) / 100 * days / 365
    assert index > KEY


def test_hybrid_without_a_curve_leaves_the_far_coupons_without_estimate(
    disk: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кривой на день нет — дальних купонов нет, поток не строится, а не текущий индекс."""
    disk["1"] = ON_KEY
    monkeypatch.setattr(floating, "curves", lambda: {})
    monkeypatch.setattr(floating, "ruonia", lambda: ())
    assert set(_hybrid("1")) == {DUES[1]}
    zone = load_market().distress_zone
    coupons = floating.pv_coupons(
        _rules(), floating.HYBRID, zone.pv_floating_near_months, zone.pv_floating_basis_days
    )
    got = _pv("1", coupons)
    assert got.ratio is None and got.why == "будущий купон не оценён"


def test_months_after_keeps_the_day_or_takes_the_last_of_the_month() -> None:
    """31 января плюс месяц — 28 февраля; плюс три месяца — 30 апреля."""
    assert floating.months_after(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert floating.months_after(date(2026, 1, 31), 3) == date(2026, 4, 30)
    assert floating.months_after(date(2026, 11, 15), 3) == date(2027, 2, 15)


def test_forward_of_a_rising_curve_by_annual_compounding() -> None:
    """Форвард 1→2 года: (1,14² / 1,12) − 1 при точках 12 % и 14 %."""
    points = [(Decimal("0.25"), Decimal(10)), (Decimal(1), Decimal(12)), (Decimal(2), Decimal(14))]
    found = floating.forward_rate(points, Decimal(1), Decimal(2))
    expected = (Decimal("1.14") ** 2 / Decimal("1.12") - 1) * 100
    assert found is not None and abs(found - expected) < Decimal("1e-12")
    assert floating.forward_rate(points, Decimal(2), Decimal(1)) is None


def test_the_index_value_is_declared_current_and_has_no_default() -> None:
    """В методике — current; гибрид без параметров окна и значение вне списка — отказ."""
    zone = load_market().distress_zone
    assert zone.pv_floating_index == "current" and zone.pv_floating_index_origin
    raw = zone.model_dump()
    raw.pop("pv_floating_index")
    with pytest.raises(ValidationError):
        Distress(**raw)
    with pytest.raises(ValueError):
        floating.pv_coupons(_rules(), floating.HYBRID)
    with pytest.raises(ValueError):
        floating.pv_coupons(_rules(), "forward")
