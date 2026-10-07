"""Синтетические граничные случаи парного замера, без запуска замера и БД."""

import sys
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "eval"))
import paired_67  # noqa: E402
import zspread_run  # noqa: E402


def test_snapshot_name_and_read_only_must_both_match() -> None:
    """Только синтетические метаданные: к базе этот тест не подключается."""
    paired_67.check_snapshot({"db": "findb_test", "ro": "on"}, "findb_test")
    for metadata in ({"db": "other", "ro": "on"}, {"db": "findb_test", "ro": "off"}):
        with pytest.raises(ValueError, match="READ ONLY"):
            paired_67.check_snapshot(metadata, "findb_test")


def test_zero_lift_is_valid_in_every_paired_replica() -> None:
    """Сработавшие и события есть, ноль попаданий не отбрасывается."""
    result = paired_67.paired({"a": (2, 0, 1, 4)}, {"a": (2, 1, 1, 4)})
    assert result.old == Decimal(0) and result.new == Decimal(2)
    assert result.low == result.high == Decimal(2)
    assert result.valid == paired_67.REPLICAS and result.excluded == 0


@pytest.mark.parametrize(
    "rows", [{}, {"a": (0, 0, 1, 4)}, {"a": (2, 0, 0, 4)}, {"a": (0, 0, 0, 0)}]
)
def test_undefined_results_never_index_empty_replicas(rows: dict) -> None:
    """Пустой круг и нулевые знаменатели дают явную неопределённость."""
    result = paired_67.paired(rows, rows)
    assert result.old is result.new is result.low is result.high is None
    assert result.valid == 0 and result.excluded == paired_67.REPLICAS
    assert "не определено" in paired_67.result_row("синтетика", "основание", result)


def test_mixed_replicas_account_for_all_and_use_same_indices() -> None:
    """Пустые и пригодные реплики названы, парная разность совпавших мер — ноль."""
    rows = {"a": (2, 1, 1, 4), "b": (0, 0, 0, 0)}
    result = paired_67.paired(rows, rows)
    assert 0 < result.valid < paired_67.REPLICAS
    assert result.valid + result.excluded == paired_67.REPLICAS
    assert result.low == result.high == Decimal(0)
    assert paired_67.paired(rows, rows) == result


@pytest.mark.parametrize(
    "new, message",
    [
        ({"b": (2, 1, 1, 4)}, "охват ИНН"),
        ({"a": (2, 1, 2, 4)}, "события/наблюдения"),
        ({"a": (2, 1, 1, 5)}, "события/наблюдения"),
        ({"a": (2, 3, 1, 4)}, "несогласованные"),
        ({"a": (-1, 0, 1, 4)}, "некорректные"),
    ],
)
def test_unpaired_or_invalid_denominators_stop(new: dict, message: str) -> None:
    """Разный охват не скрывается нулевым дополнением, плохие счётчики не принимаются."""
    with pytest.raises(ValueError, match=message):
        paired_67.paired({"a": (2, 1, 1, 4)}, new)


def test_horizon_boundary_is_actual_selected_day() -> None:
    """Ровно полный горизонт допустим, следующий торговый день — ещё не проверен."""
    cut = date(2090, 1, 2)
    until = cut + timedelta(days=paired_67.HORIZON)
    paired_67.validate_cuts([cut], until)
    with pytest.raises(ValueError, match="неполный горизонт"):
        paired_67.validate_cuts([cut + timedelta(days=1)], until)
    with pytest.raises(ValueError, match="повторяются"):
        paired_67.validate_cuts([cut, cut], until)


@pytest.mark.parametrize("change", ["benchmark", "point", "extra", "missing"])
def test_g_reproduction_checks_both_directions(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Несовпадение ориентира, значения, лишней или пропавшей точки останавливает замер."""
    day = date(2090, 1, 2)
    base = SimpleNamespace(benchmark={day: Decimal(100)}, issuers={"a": {day: "point"}}, census={})
    again = SimpleNamespace(benchmark=dict(base.benchmark), issuers={"a": {day: "point"}})
    monkeypatch.setattr(zspread_run, "build", lambda *args: again)
    zspread_run.verify_g({day.isoformat(): []}, base)
    if change == "benchmark":
        again.benchmark[day] = Decimal(101)
    elif change == "point":
        again.issuers["a"][day] = "other"
    elif change == "extra":
        again.issuers["b"] = {day: "extra"}
    else:
        again.issuers = {}
    with pytest.raises(ValueError, match="G из pickle не воспроизводит"):
        zspread_run.verify_g({day.isoformat(): []}, base)


def _series(price: Decimal, ratios: bool) -> paired_67.Market:
    """Ряд одного эмитента на один день."""
    from finlib.sources.market import Point

    day = date(2090, 1, 2)
    return paired_67.Market(
        benchmark={day: Decimal(100)},
        issuers={"a": {day: Point(day, None, price, Decimal(1), ratio=Decimal("0.5"))}},
        counted={}, census={}, universe=1, with_isin=1, ratios=ratios,
    )


def test_rebuilt_series_must_match_saved_except_the_ratio() -> None:
    """Ряд с отношением сверяется с сохранённым: иначе разность мерила бы и смену ряда."""
    paired_67.verify_rebuild(_series(Decimal(40), False), _series(Decimal(40), True))
    with pytest.raises(ValueError, match="у 1 эмитентов"):
        paired_67.verify_rebuild(_series(Decimal(40), False), _series(Decimal(41), True))
    with pytest.raises(ValueError, match="без отношения"):
        paired_67.verify_rebuild(_series(Decimal(40), False), _series(Decimal(40), False))


def _gz_rows(day: date) -> dict[str, list[tuple]]:
    """Строки G/Z одного дня в формате `zspread_rows.pkl`: без отношения к PV.

    Поля: код, ИНН, G, Z, причина, ядро, оборот, цена.
    """
    core = [
        (f"RU{number}", None, Decimal(100 + 50 * number), Decimal(90 + 50 * number), "",
         True, Decimal(1), Decimal(99))
        for number in range(5)
    ]
    own = ("RUA", "a", Decimal(400), Decimal(380), "", False, Decimal(2), Decimal(55))
    return {day.isoformat(): [*core, own]}


def test_gz_rows_without_ratio_build_and_verify_against_a_pv_series() -> None:
    """Строка G/Z без отношения читается как «отношения нет»; сверка G его не требует."""
    from dataclasses import replace

    day = date(2090, 1, 2)
    rows = _gz_rows(day)
    again = zspread_run.build(rows, 2, lambda item: True, {})
    point = again.issuers["a"][day]
    assert point.spread == Decimal(400) and point.price == Decimal(55)
    assert point.ratio is None and point.unflowed is None
    # Ряд на диске при `pv_kbd` несёт отношение: сверка G его не замечает…
    with_pv = replace(point, ratio=Decimal("0.6"), ratio_price=Decimal(56),
                      ratio_pv=Decimal(93), unflowed=Decimal(54))
    base = replace(again, issuers={"a": {day: with_pv}})
    zspread_run.verify_g(rows, base)
    # …а расхождение цены, спреда и оборота по-прежнему останавливает замер.
    moved = replace(again, issuers={"a": {day: replace(with_pv, price=Decimal(56))}})
    with pytest.raises(ValueError, match="G из pickle не воспроизводит"):
        zspread_run.verify_g(rows, moved)


def test_early_and_late_parts_are_both_required() -> None:
    """Решение — по поздней части; пустая часть — отказ, а не интервал из ничего."""
    cuts = [date(2090, month, 1) for month in range(1, 7)]
    early, late = paired_67.split_cuts(cuts, date(2090, 4, 1))
    assert early == cuts[:3] and late == cuts[3:]
    with pytest.raises(ValueError, match="пуста"):
        paired_67.split_cuts(cuts, date(2091, 1, 1))


def test_pv_variants_declare_substitution_and_keep_the_rest() -> None:
    """Варианты замера — боевая методика с одной подменой измерения."""
    policy = paired_67.load_market()
    for substitution in (True, False):
        rule = paired_67.pv_policy(policy, substitution)
        assert rule.distress_zone.measure == "pv_kbd"
        assert rule.distress_zone.substitution is substitution
        assert rule.lifetime == policy.lifetime and rule.ladder == policy.ladder


def _nominal() -> paired_67.MarketPolicy:
    """Боевая методика с ценой от номинала: замер 7 сравнивает спреды, не цену к PV."""
    loaded = paired_67.load_market()
    zone = loaded.distress_zone.model_copy(update={"measure": "nominal"})
    return loaded.model_copy(update={"distress_zone": zone})


def _market7(issuers: dict[str, list[date]], level: Decimal) -> paired_67.Market:
    """Ряд из точек со спредом 300 б. п. и ориентиром дня `level`."""
    from finlib.sources.market import Point

    days = sorted({day for own in issuers.values() for day in own})
    return paired_67.Market(
        benchmark={day: level for day in days},
        issuers={
            inn: {day: Point(day, Decimal(300), Decimal(99), Decimal(1)) for day in own}
            for inn, own in issuers.items()
        },
        counted={}, census={}, universe=len(issuers), with_isin=len(issuers),
    )


_DAYS7 = [date(2090, 1, 2) + timedelta(days=7 * number) for number in range(10)]


def _parts7() -> list[paired_67.Part]:
    """Ранняя и поздняя части синтетического календаря."""
    split = _DAYS7[5]
    return [
        ("Поздняя часть", _DAYS7[5:], lambda day: day >= split),
        ("Ранняя часть", _DAYS7[:5], lambda day: day < split),
    ]


def test_variant_tallies_share_the_frame_circle_and_observation() -> None:
    """Ряд варианта без эмитента и ранних дней не меняет знаменателя: разность считается."""
    policy = _nominal()
    base = _market7({"a": _DAYS7, "b": _DAYS7}, Decimal(150))
    variant = _market7({"a": _DAYS7[3:]}, Decimal(80))
    grounds = {"Разбор": {"market_spread_level"}}
    when = {"a": _DAYS7[-1] + timedelta(days=10)}
    old = paired_67.tallies(policy, base, when, set(), grounds, _DAYS7)
    # Без рамки — ровно тот отказ, что оборвал замер 7 после заголовка таблицы.
    alone = paired_67.tallies(policy, variant, when, set(), grounds, _DAYS7)
    with pytest.raises(ValueError, match="охват ИНН различается"):
        paired_67.paired(old["Разбор"][0], alone["Разбор"][0])
    new = paired_67.tallies(policy, variant, when, set(), grounds, _DAYS7, frame=base)
    assert paired_67.paired(old["Разбор"][0], new["Разбор"][0]).members == 2
    assert paired_67.uncovered(base, variant, _DAYS7) == (10 + 3, 20)


def test_measure7_prints_every_step_with_floor_and_parts() -> None:
    """Обе ступени в каждой части, по каждой — дни ниже пола и квантиль пола."""
    policy = _nominal()
    base = _market7({"a": _DAYS7, "b": _DAYS7}, Decimal(150))
    variants = {
        "а) Z, полное ядро": (policy, _market7({"a": _DAYS7, "b": _DAYS7}, Decimal(90))),
        "б) Z, ядро без госбумаг": (policy, _market7({"a": _DAYS7}, Decimal(120))),
    }
    grounds = {"Разбор": {"market_spread_level"}}
    lines = paired_67.measure7(policy, base, variants, {}, set(), grounds, _parts7())
    text = "\n".join(lines)
    for part in ("Поздняя часть", "Ранняя часть"):
        assert f"## {part}: срезов 5" in text
    assert sum(line.startswith("| а) Z, полное ядро | Разбор |") for line in lines) == 2
    assert sum(line.startswith("| б) Z, ядро без госбумаг | Разбор |") for line in lines) == 2
    assert "| а) Z, полное ядро | 5 из 5 | 100.0% | 0 из 10 |" in lines
    assert "| б) Z, ядро без госбумаг | 0 из 5 | 0.0% | 5 из 10 |" in lines
    assert "| G, полное ядро (база) | 0 из 5 | 0.0% | — |" in lines


@pytest.mark.parametrize("case", ["grounds", "variant_days", "circle", "variants"])
def test_measure7_refuses_an_empty_result(case: str) -> None:
    """Пустой результат — исключение с причиной, а не таблица без строк."""
    policy = _nominal()
    base = _market7({"a": _DAYS7}, Decimal(150))
    variants = {"а) Z, полное ядро": (policy, _market7({"a": _DAYS7}, Decimal(90)))}
    grounds: dict[str, set[str]] = {"Разбор": {"market_spread_level"}}
    if case == "grounds":
        grounds = {}
    elif case == "variant_days":
        variants = {"а) Z, полное ядро": (policy, _market7({"a": _DAYS7[:5]}, Decimal(90)))}
    elif case == "circle":
        base = _market7({}, Decimal(150))
        base.benchmark.update({day: Decimal(150) for day in _DAYS7})
    else:
        variants = {}
    with pytest.raises(ValueError, match="замер 7"):
        paired_67.measure7(policy, base, variants, {}, set(), grounds, _parts7())
