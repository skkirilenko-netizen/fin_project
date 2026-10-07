"""Замер 8 (МСФО (IFRS) 16): перенос шкалы квантилем, знаменатели и сверка величин.

Синтетика, без базы: проверяется устройство замера — что поздние точки
в порог не входят, что каждая опорная точка переносится по своей доле, что
оценка снизу в распределение не идёт и что вариант доходит до боевого маршрута.
"""

import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from finlib.config import settings
from finlib.metrics.ifrs import Inputs, compute_all
from finlib.scoring.routing import route
from finlib.standards import Standard

sys.path.insert(0, str(settings.base_dir / "eval"))

import threshold_calibration_run as run  # noqa: E402

EARLY = (date(2026, 3, 2), date(2026, 3, 9))
LATE = date(2026, 8, 3)
SCALE = [
    (Decimal("5.0"), Decimal(0)),
    (Decimal("3.5"), Decimal(35)),
    (Decimal("2.0"), Decimal(70)),
    (Decimal("1.0"), Decimal(100)),
]


def _pairs(days: tuple[date, ...], factor: Decimal = Decimal(2)) -> dict:
    """Десять эмитентов на дату: прежняя 1…10, с арендой — в `factor` раз больше."""
    return {
        (f"inn{number}", day): (Decimal(number), Decimal(number) * factor)
        for day in days
        for number in range(1, 11)
    }


def test_every_scale_point_moves_by_its_own_share() -> None:
    """5,0 — 60 % по худшую сторону → p40 нового; 3,5 → p30; 2,0 → p10; 1,0 → p0."""
    moved = run.transfer(_pairs(EARLY), SCALE, high_bad=True)
    assert [item.worse for item in moved] == [Decimal(60), Decimal(70), Decimal(90), Decimal(100)]
    assert [item.mark for item in moved] == [Decimal("40.0"), Decimal("30.0"),
                                             Decimal("10.0"), Decimal("0.0")]
    assert [item.moved for item in moved] == [Decimal(10), Decimal(8), Decimal(4), Decimal(2)]


def test_late_points_do_not_move_the_threshold() -> None:
    """Точки после раздела — с выбросами — порога не трогают."""
    early = run.transfer(_pairs(EARLY), SCALE, high_bad=True)
    late = _pairs((LATE,), factor=Decimal(100))
    assert run.transfer(_pairs(EARLY) | late, SCALE, high_bad=True) == early


def test_no_exact_pairs_is_a_refusal() -> None:
    """Пар нет — отказ с причиной, а не шкала из ничего."""
    with pytest.raises(ValueError, match="замер 8"):
        run.transfer(_pairs((LATE,)), SCALE, high_bad=True)


def test_quantile_variant_carries_both_edges_and_the_new_definition() -> None:
    """Конец шкалы — перенесённая точка 5,0; нижняя часть — балл 34 на новой шкале."""
    over = run.quantile_variant(run.transfer(_pairs(EARLY), SCALE, high_bad=True))
    assert over.review == {"net_debt_ebitda": Decimal(10)}
    lower = Decimal(10) + (Decimal(8) - Decimal(10)) * run._lower() / Decimal(35)
    assert over.attention == {"net_debt_ebitda": lower}
    assert over.metrics == dict(run.LEASES_VARIANT.metrics)
    assert over.floors == dict(run.LEASES_VARIANT.floors)
    assert over.standard is Standard.IFRS


def _metric(code: str, value: str | None) -> SimpleNamespace:
    """Показатель строки маршрута: точный либо не посчитанный."""
    return SimpleNamespace(
        code=code, value=Decimal(value) if value else None, calculable=value is not None
    )


def _row(inn: str, *metrics: SimpleNamespace, standard: Standard = Standard.IFRS,
         basket: str = "clear") -> SimpleNamespace:
    """Строка маршрута с вердиктом и посчитанными величинами."""
    return SimpleNamespace(
        inn=inn, standard=standard, verdict=SimpleNamespace(basket=basket), computed=metrics
    )


def test_collect_keeps_only_exact_pairs_and_counts_the_rest() -> None:
    """Оценка снизу, пропуск прежней и отказ — числами; чужой стандарт и корзина — мимо."""
    values = run.Values()
    rows = [
        _row("a", _metric(run.DEBT, "2.5"), _metric(run.DEBT_LEASES, "6.5")),
        _row("b", _metric(run.DEBT, "2.5"), _metric(run.DEBT_FLOOR, "2.5")),
        _row("c", _metric(run.DEBT_LEASES, "4")),
        _row("d", _metric(run.DEBT, "1.5")),
        _row("e", _metric(run.DEBT, "1"), _metric(run.DEBT_LEASES, "2"),
             standard=Standard.RSBU),
        _row("f", _metric(run.DEBT, "1"), _metric(run.DEBT_LEASES, "2"),
             basket="structural_pool"),
    ]
    run.collect(values, rows, EARLY[0])
    assert values.pairs == {("a", EARLY[0]): (Decimal("2.5"), Decimal("6.5"))}
    assert values.lower_bound == 1 and values.old_missing == 1 and values.refused == 1
    assert set(values.old) == {("a", EARLY[0]), ("b", EARLY[0]), ("d", EARLY[0])}
    with pytest.raises(ValueError, match="дважды"):
        run.collect(run.Values(), [rows[0], rows[0]], EARLY[0])


def _history(inn: str, value: str | None) -> dict:
    """Точка записанной истории корпоративного периметра МСФО."""
    return {
        "inn": inn, "as_of": EARLY[0], "standard": Standard.IFRS.value, "basket": "clear",
        "inputs": {"metrics": {run.DEBT: value} if value else {}},
        "created_at": None, "grounds_all": [],
    }


def test_value_control_stops_on_a_changed_value_and_names_refetched(monkeypatch) -> None:  # noqa: ANN001
    """Совпало — молча; разошлось — расхождение; перезабрано — отдельно, с файлами."""
    values = run.Values()
    values.old = {("a", EARLY[0]): Decimal("2.5"), ("b", EARLY[0]): Decimal("3"),
                  ("c", EARLY[0]): Decimal("4")}
    monkeypatch.setattr(
        run, "refetched", lambda inn, _: ("flow_c.json",) if inn == "c" else ()
    )
    said = run.value_control(
        [_history("a", "2.5"), _history("b", "2.9"), _history("c", "3.9"),
         _history("d", None)],
        values,
    )
    assert said.compared == 3
    assert len(said.differ) == 1 and said.differ[0].startswith("b ")
    assert set(said.refetched) == {"c"}


def _computed(borrowings: str) -> tuple:
    """Показатели МСФО: займы, аренда 4 000 000, EBITDA 1 000 000, здоровое прочее."""
    return compute_all(
        Inputs(
            {
                "ifrs.long_term_borrowings": Decimal(borrowings),
                "ifrs.short_term_borrowings": Decimal("1000000"),
                "ifrs.cash": Decimal("500000"),
                "ifrs.operating_profit": Decimal("700000"),
                "ifrs.depreciation": Decimal("-300000"),
                "ifrs.long_term_lease_liabilities": Decimal("3500000"),
                "ifrs.short_term_lease_liabilities": Decimal("500000"),
                "ifrs.total_equity": Decimal("6"),
                "ifrs.total_assets": Decimal("10"),
                "ifrs.total_current_assets": Decimal("25"),
                "ifrs.total_current_liabilities": Decimal("10"),
            },
            {},
        )
    )


def _grounds(computed: tuple, over: object) -> set:
    """Основания маршрута МСФО о долговой нагрузке."""
    verdict = route(
        computed, unit="тыс. руб.", quarantined=False, today=date(2026, 5, 1),
        latest_annual=date(2025, 12, 31), thresholds=over,
    )
    return {item.ground for item in verdict.findings if item.subject == "net_debt_ebitda"}


def test_quantile_variant_reaches_the_live_route() -> None:
    """С арендой 6,5: при прежней шкале — за концом, при перенесённой (10; 8,06) — нет.

    9,5 с арендой — между 8,06 и 10: нижняя часть перенесённой шкалы.
    """
    over = run.quantile_variant(run.transfer(_pairs(EARLY), SCALE, high_bad=True))
    assert "level_off_scale" in _grounds(_computed("2000000"), run.LEASES_VARIANT)
    assert not _grounds(_computed("2000000"), over)
    assert _grounds(_computed("5000000"), over) == {"metric_in_lower_band"}
