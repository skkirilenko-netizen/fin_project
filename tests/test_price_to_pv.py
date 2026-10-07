"""Ценовой признак по PV потока на КБД: параметр методики, оба значения, синтетика.

Признак от номинала ловит бумагу, низкую по устройству: почти бескупонная
бумага на пять лет при ставке 15 % стоит около половины номинала, и это
не мнение о возврате тела. Отношение цены к PV по кривой того же дня
это различает; купонная бумага за 40 % при PV около 95 % — нет.
"""

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.scoring.market import findings
from finlib.scoring.routing import load_routing
from finlib.sources import cbonds
from finlib.sources.market import (
    Market,
    MarketPolicy,
    Point,
    _of_day,
    _read,
    _written,
    load_market,
)
from finlib.sources.zspread import price_to_pv

INN = "7700000000"
TRADE = date(2090, 1, 2)
RATE = Decimal(15)


def _policy(measure: str = "nominal", substitution: bool | None = None) -> MarketPolicy:
    """Боевая методика с подменённым измерением зоны."""
    policy = load_market()
    zone = policy.distress_zone.model_copy(
        update={"measure": measure, "substitution": substitution}
    )
    return policy.model_copy(update={"distress_zone": zone})


def _flow(root: Path, emission: str, coupon: Decimal, years: int) -> None:
    """Синтетический график на одну бумагу номиналом 1000: купон раз в год и погашение."""
    items = [
        {
            "date": f"{TRADE.year + number}-01-10",
            "cupon_sum": str(coupon),
            "redemtion": "1000" if number == years else "0",
            "emission_nominal_price": "1000",
        }
        for number in range(1, years + 1)
    ]
    (root / f"flow_{emission}.json").write_text(
        json.dumps({"items": items}), encoding="utf-8"
    )


def _row(secid: str) -> dict:
    """Строка среза биржи: номинал, НКД и без оферты."""
    return {"SECID": secid, "FACEVALUE": "1000", "ACCINT": "0"}


def _point(day: date, price: Decimal, secid: str, by_code: dict[str, str]) -> Point:
    """Точка ряда так, как её сводит ряд: цена и отношение у бумаги дня."""
    flowed = price_to_pv(_row(secid), day, lambda years: RATE, price, by_code)
    return _of_day(day, [(None, Decimal(1), price, flowed)])


def _market(points: list[Point], ratios: bool = True) -> Market:
    """Ряд одного эмитента с постоянным ориентиром."""
    return Market(
        benchmark={item.day: Decimal(100) for item in points},
        issuers={INN: {item.day: item for item in points}},
        counted={},
        census={},
        universe=1,
        with_isin=1,
        ratios=ratios,
    )


@pytest.fixture
def flows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Каталог графиков Cbonds: почти бескупонная и купонная бумага; у третьей графика нет."""
    monkeypatch.setattr(cbonds, "CACHE", tmp_path)
    _flow(tmp_path, "1", Decimal("0.1"), 5)
    _flow(tmp_path, "2", Decimal(150), 3)
    return {"RU000ZERO01": "1", "RU000COUP01": "2", "RU000NOFLOW": "3"}


def _days(count: int = 5) -> list[date]:
    """Торговые дни подряд, последний — день отчёта."""
    return [TRADE + timedelta(days=number) for number in range(count)]


def _price_ground(policy: MarketPolicy, market: Market, today: date):  # noqa: ANN202
    """Ценовое основание эмитента на дату либо None."""
    return next(
        (item for item in findings(policy, market, INN, today) if item.ground.endswith("distress")),
        None,
    )


def test_the_default_measure_is_nominal_and_pv_needs_a_declared_substitution() -> None:
    """Маршрут по умолчанию прежний; признак по PV без объявленной подстановки не грузится."""
    zone = load_market().distress_zone
    assert zone.measure == "nominal" and zone.threshold == zone.price_below_percent
    assert zone.ratio_below == Decimal("0.6")
    with pytest.raises(ValueError, match="substitution"):
        type(zone).model_validate({**zone.model_dump(), "measure": "pv_kbd"})


def test_almost_zero_coupon_at_half_par_is_not_distress_by_pv(flows: dict[str, str]) -> None:
    """Купон 0,01 % за 50 % на пять лет: от номинала — признак, по PV — нет."""
    days = _days()
    points = [_point(day, Decimal(50), "RU000ZERO01", flows) for day in days]
    assert points[-1].ratio is not None and Decimal("0.95") < points[-1].ratio < Decimal("1.05")
    market = _market(points)
    assert _price_ground(_policy(), market, days[-1]) is not None
    for substitution in (True, False):
        assert _price_ground(_policy("pv_kbd", substitution), market, days[-1]) is None


def test_coupon_bond_at_forty_with_pv_near_par_is_distress(flows: dict[str, str]) -> None:
    """Купонная бумага за 40 % при PV около 95 % и выше — признак есть, формулировка владельца."""
    days = _days()
    points = [_point(day, Decimal(40), "RU000COUP01", flows) for day in days]
    assert points[-1].ratio_pv is not None and points[-1].ratio_pv > Decimal(95)
    policy = _policy("pv_kbd", False)
    found = _price_ground(policy, _market(points), days[-1])
    assert found is not None and found.variant == "pv" and not found.by_nominal
    assert found.value < 60 and found.threshold == 60
    text = load_routing().say(found.ground, found.variant, **found.slots(policy))
    assert text.startswith("Цена 40,0 % при PV по КБД ")
    assert "(порог 0,6)" in text and f"на {days[-1]:%d.%m.%Y}" in text
    assert "дн. по медиане" not in text
    # Отношение — грязная цена к PV, и напечатанные величины ему соответствуют.
    assert found.price is not None and found.pv is not None
    assert abs(found.price / found.pv * 100 - found.value) < Decimal("0.0001")


def test_without_a_flow_substitution_prints_its_mark(flows: dict[str, str]) -> None:
    """Поток не построен: с подстановкой — цена от номинала с пометкой, без неё — признака нет."""
    days = _days()
    points = [_point(day, Decimal(40), "RU000NOFLOW", flows) for day in days]
    assert points[-1].ratio is None and points[-1].unflowed == Decimal(40)
    policy = _policy("pv_kbd", True)
    found = _price_ground(policy, _market(points), days[-1])
    assert found is not None and found.by_nominal and found.variant == "pv_nominal"
    text = load_routing().say(found.ground, found.variant, **found.slots(policy))
    assert "Цена 40,0 % номинала" in text and text.endswith("— по номиналу: поток не построен")
    assert _price_ground(_policy("pv_kbd", False), _market(points), days[-1]) is None


def test_the_lower_of_ratio_and_substituted_price_counts(flows: dict[str, str]) -> None:
    """Две бумаги эмитента: в сравнение идёт наименьшее, как в замере 6."""
    day = _days(1)[0]
    flowed = price_to_pv(_row("RU000COUP01"), day, lambda years: RATE, Decimal(90), flows)
    bare = price_to_pv(_row("RU000NOFLOW"), day, lambda years: RATE, Decimal(55), flows)
    point = _of_day(
        day, [(None, Decimal(1), Decimal(90), flowed), (None, Decimal(1), Decimal(55), bare)]
    )
    market = _market([point])
    found = _price_ground(_policy("pv_kbd", True), market, day)
    assert found is not None and found.by_nominal and found.value == Decimal(55)
    assert _price_ground(_policy("pv_kbd", False), market, day) is None


def test_a_recovered_ratio_names_its_low(flows: dict[str, str]) -> None:
    """Отношение вернулось выше порога в пределах срока: основание стоит и называет минимум."""
    days = _days(6)
    prices = [Decimal(40) if number == 2 else Decimal(90) for number in range(6)]
    points = [
        _point(day, price, "RU000COUP01", flows) for day, price in zip(days, prices, strict=True)
    ]
    policy = _policy("pv_kbd", False)
    found = _price_ground(policy, _market(points), days[-1])
    assert found is not None and found.variant == "pv_recovered" and found.low_day == days[2]
    text = load_routing().say(found.ground, found.variant, **found.slots(policy))
    assert text.startswith("Отношение цены к PV по КБД опускалось до 0,4")
    assert f"{days[2]:%d.%m.%Y}" in text and "по номиналу" not in text


def test_a_series_without_ratios_refuses_instead_of_staying_silent(
    flows: dict[str, str],
) -> None:
    """Ряд, собранный без отношения, признаку по PV не годится: «не считали» ≠ «потока нет»."""
    days = _days()
    points = [_point(day, Decimal(40), "RU000COUP01", flows) for day in days]
    with pytest.raises(ValueError, match="без отношения цены к PV"):
        _price_ground(_policy("pv_kbd", True), _market(points, ratios=False), days[-1])
    # Признак от номинала на том же ряду работает как прежде.
    assert _price_ground(_policy(), _market(points, ratios=False), days[-1]) is not None


def test_the_written_series_keeps_ratios_and_old_files_read_as_not_computed(
    flows: dict[str, str],
) -> None:
    """Запись ряда хранит отношение и отметку расчёта; прежний файл — «не считали»."""
    points = [_point(day, Decimal(40), "RU000COUP01", flows) for day in _days(2)]
    again = _read(json.loads(_written(_market(points))))
    assert again.ratios and again.issuers[INN] == {item.day: item for item in points}
    old = json.loads(_written(_market(points)))
    old.pop("ratios")
    for own in old["issuers"].values():
        for item in own.values():
            for name in ("ratio", "ratio_price", "ratio_pv", "unflowed"):
                item.pop(name)
    legacy = _read(old)
    assert not legacy.ratios and legacy.issuers[INN][points[0].day].ratio is None


def test_the_series_computes_ratios_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flows: dict[str, str],
) -> None:
    """Сборка ряда из среза: при `nominal` отношения нет и ряд об этом говорит."""
    from finlib.sources import market

    moex = tmp_path / "moex"
    moex.mkdir()
    day = TRADE
    curve = [{"period": 1, "value": 15}, {"period": 10, "value": 15}]
    (moex / "zcyc_by_day.json").write_text(
        json.dumps({f"{day}": {"yearyields": curve}}), encoding="utf-8"
    )
    core = [
        {"SECID": f"RU000CORE0{number}", "FACEUNIT": "RUB", "YIELDATWAP": 17,
         "DURATION": 700, "NUMTRADES": 50, "VALUE": 5000000, "LEGALCLOSEPRICE": 99}
        for number in range(5)
    ]
    mine = [
        {**_row(secid), "FACEUNIT": "RUB", "NUMTRADES": 3, "VALUE": 1000,
         "LEGALCLOSEPRICE": price, "COUPONPERCENT": coupon}
        for secid, price, coupon in (
            ("RU000ZERO01", 50, 0.01), ("RU000COUP01", 40, 15), ("RU000NOFLOW", 45, 10),
        )
    ]
    (moex / f"xsec_{day}.json").write_text(
        json.dumps({"history": core + mine}), encoding="utf-8"
    )
    monkeypatch.setattr(market, "CACHE", moex)
    monkeypatch.setattr(market, "holders", lambda: {item["SECID"]: INN for item in mine})
    monkeypatch.setattr(market, "universe", lambda: [INN])
    monkeypatch.setattr(market, "emission_map", lambda: (flows, {}))

    plain = market.build(_policy())
    assert not plain.ratios and plain.issuers[INN][day].ratio is None
    assert plain.issuers[INN][day].price == Decimal(40)

    found = market.build(_policy("pv_kbd", True))
    point = found.issuers[INN][day]
    assert found.ratios and point.price == Decimal(40)
    assert point.ratio_price == Decimal(40) and point.ratio is not None and point.ratio < 1
    assert point.unflowed == Decimal(45)
    assert found.counted["поток не построен: графика нет"] == 1
