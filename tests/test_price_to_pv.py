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
from typing import get_args

import pytest
import yaml

from finlib.config import settings
from finlib.scoring.market import findings
from finlib.scoring.routing import RoutingPolicy, load_routing, measure_context
from finlib.sources import cbonds
from finlib.sources.market import (
    DistressMeasure,
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


def test_the_measure_is_pv_with_substitution_and_pv_needs_a_declared_substitution() -> None:
    """Действует признак по PV с подстановкой (07.10.2026); без подстановки не грузится."""
    zone = load_market().distress_zone
    assert zone.measure == "pv_kbd" and zone.substitution is True
    assert zone.measure_status == "accepted"
    assert zone.ratio_below == Decimal("0.6") and zone.threshold == zone.ratio_below * 100
    with pytest.raises(ValueError, match="substitution"):
        type(zone).model_validate({**zone.model_dump(), "substitution": None})

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


def test_the_nominal_statement_is_chosen_only_by_substitution(flows: dict[str, str]) -> None:
    """Формулировка `pv_nominal` — тогда и только тогда, когда величина взята подстановкой.

    Решение владельца 09.10.2026: при подстановке основание считается
    к номиналу, и текст «% номинала» верен; без подстановки он был бы ложью.
    """
    days = _days()
    day = days[-1]
    flowed = price_to_pv(_row("RU000COUP01"), day, lambda years: RATE, Decimal(40), flows)
    bare = price_to_pv(_row("RU000NOFLOW"), day, lambda years: RATE, Decimal(55), flows)
    cases = {
        "поток есть": [_point(item, Decimal(40), "RU000COUP01", flows) for item in days],
        "потока нет": [_point(item, Decimal(40), "RU000NOFLOW", flows) for item in days],
        # Отношение ниже цены подставленной бумаги: в сравнение идёт оно.
        "обе, ниже отношение": [_of_day(
            day, [(None, Decimal(1), Decimal(40), flowed), (None, Decimal(1), Decimal(55), bare)]
        )],
    }
    seen = set()
    for name, points in cases.items():
        for substitution in (True, False):
            found = _price_ground(_policy("pv_kbd", substitution), _market(points), day)
            if found is None:
                continue
            assert (found.variant == "pv_nominal") == found.by_nominal, name
            assert substitution or not found.by_nominal, name
            seen.add(found.variant)
    assert seen == {"pv", "pv_nominal"}


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


def _strings(value: object, path: str = "") -> list[tuple[str, str]]:
    """Все строки выгрузки справочника с путём к каждой."""
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [item for key, inner in value.items() for item in _strings(inner, f"{path}.{key}")]
    if isinstance(value, list | tuple):
        return [
            item
            for number, inner in enumerate(value)
            for item in _strings(inner, f"{path}[{number}]")
        ]
    return []


def test_the_ground_is_said_by_the_measure_and_substitution_keeps_its_mark(
    flows: dict[str, str],
) -> None:
    """Наименование и обоснования — по действующей мере; подстановка — пометкой в тексте.

    Отчёт 09.10.2026: восемь смен «Внимание → Разбор» по ценовому основанию
    печатались «цена бумаги ниже 60 % номинала» при `measure: pv_kbd`.
    """
    routing = load_routing()
    zone = load_market().distress_zone
    ground = next(item for item in routing.basket("review").grounds
                  if item.code == "market_price_distress")
    layer = routing.basket("review").subgroup("market_risk")
    assert layer is not None
    assert ground.name == "цена к PV по КБД ниже порога"
    # Порог — из методики рынка, а не числом в тексте.
    said = f"ниже {zone.ratio_below_said} от PV по КБД"
    assert said in ground.why and said in layer.why
    assert "{" not in ground.why + layer.why
    # **Ни один текст при pv_kbd не говорит «номинала»**, кроме формулировок
    # для прежней меры и для подстановки (`pv_nominal`, пометка `nominal_mark`).
    elsewhere = {"default", "recovered", "pv_nominal"}
    spoken = [
        (path, text)
        for path, text in _strings(routing.model_dump())
        if "_by_measure" not in path
        and not (".market_price_distress." in path and path.rsplit(".", 1)[-1] in elsewhere)
    ]
    assert [path for path, text in spoken if "номинала" in text] == []
    # При прежней мере тексты прежние.
    raw = yaml.safe_load((settings.methodology_dir / "routing.yaml").read_text(encoding="utf-8"))
    by_nominal = RoutingPolicy.model_validate(raw, context=measure_context("nominal"))
    ground = next(item for item in by_nominal.basket("review").grounds
                  if item.code == "market_price_distress")
    layer = by_nominal.basket("review").subgroup("market_risk")
    assert layer is not None
    assert ground.name == "цена бумаги ниже 60 % номинала"
    assert ground.why.startswith("Ниже этой цены доходность перестаёт быть ценой риска")
    assert layer.why.startswith("Наблюдение сегодняшнего дня: цена ниже 60 % номинала")
    # Справочник без меры не читается: текст выбрать было бы не по чему.
    with pytest.raises(ValueError, match="мера не передана"):
        RoutingPolicy.model_validate(raw)
    # Мера, у которой нет своего текста, не грузится вовсе.
    context = measure_context("nominal")
    context["distress_measures"] = (*get_args(DistressMeasure), "z")
    with pytest.raises(ValueError, match="печатался бы чужим"):
        RoutingPolicy.model_validate(raw, context=context)
    # Подстановка номинала в тексты не идёт: у неё своя пометка.
    day = _days(1)[0]
    bare = price_to_pv(_row("RU000NOFLOW"), day, lambda years: RATE, Decimal(55), flows)
    policy = _policy("pv_kbd", True)
    market = _market([_of_day(day, [(None, Decimal(1), Decimal(55), bare)])])
    found = _price_ground(policy, market, day)
    assert found is not None and found.by_nominal
    text = routing.say(found.ground, found.variant, **found.slots(policy))
    assert text.endswith(policy.distress_zone.nominal_mark)


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
