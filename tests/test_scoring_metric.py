"""Тесты балла показателя: уровень и динамика по отдельности."""

from decimal import Decimal

import pytest

from finlib.metrics.definitions import Direction, load_metrics
from finlib.quality.thresholds import load_thresholds
from finlib.scoring.definitions import ScaleType, load_scoring
from finlib.scoring.metric_score import (
    dynamics_score,
    is_good_side,
    level_score,
    score_metric,
)

SCORING = load_scoring()
POLICY = SCORING.metric_score
JUMP = load_thresholds().jump_detection.factor
CATALOG = load_metrics()


def metric(code: str):
    """Определение показателя."""
    return CATALOG.require(code)


def dec(*values: str) -> list[Decimal]:
    """Ряд значений от старого к новому."""
    return [Decimal(value) for value in values]


def score(code: str, values: list[Decimal]):
    """Балл показателя по ряду."""
    return score_metric(metric(code), values, POLICY, JUMP)


# --- то, ради чего вводился уровень -----------------------------------------


def test_stable_good_beats_improving_bad() -> None:
    """Устойчиво хорошая без роста должна получать больше, чем плохая с ростом.

    Это исходная претензия к баллу на одной динамике: там сравнение было
    обратным. Текущая ликвидность 1,5 три периода подряд против роста
    с 0,3 до 0,8 при ориентире 1,0.
    """
    stable = score("cur_liq", dec("1.5", "1.5", "1.5"))
    improving = score("cur_liq", dec("0.3", "0.5", "0.8"))

    assert stable.score > improving.score
    assert stable.level == Decimal(100)
    assert improving.level == Decimal(0)
    assert improving.dynamics == Decimal(100), "рост при этом оценён высоко"


def test_stable_good_score_is_high() -> None:
    """Устойчиво хорошая получает высокий балл, а не средний."""
    result = score("cur_liq", dec("1.5", "1.5", "1.5"))
    assert result.score > Decimal(80)


def test_improving_bad_is_not_high() -> None:
    """Плохая с улучшением не получает высокий балл при максимальной динамике."""
    result = score("cur_liq", dec("0.3", "0.5", "0.8"))
    assert result.dynamics == Decimal(100)
    assert result.score == Decimal(40)


# --- уровень ----------------------------------------------------------------


def test_level_is_none_without_benchmark() -> None:
    """Без бесспорного ориентира уровень не оценивается: порог не выдумывается."""
    assert metric("asset_turnover").benchmark is None
    assert level_score(dec("1", "2"), metric("asset_turnover"), POLICY) is None


def test_level_combines_last_and_persistence() -> None:
    """Половина за последний период, половина за устойчивость положения.

    Переход вокруг ориентира линейный шириной в существенное изменение
    показателя (для текущей ликвидности 0,15), поэтому 1,1 и 0,9 дают не 100
    и 0, а промежуточные значения.
    """
    values = dec("1.2", "1.3", "1.1", "0.9")
    result = level_score(values, metric("cur_liq"), POLICY)
    assert result is not None
    assert result.quantize(Decimal("0.01")) == Decimal("45.83")


def test_level_scale_is_piecewise_linear() -> None:
    """Шкала не ступенчатая: сотая доля коэффициента не даёт скачка балла."""
    near_below = level_score(dec("0.999"), metric("cur_liq"), POLICY)
    near_above = level_score(dec("1.001"), metric("cur_liq"), POLICY)
    assert near_below is not None and near_above is not None
    assert abs(near_above - near_below) < Decimal(2), "у границы ориентира скачка нет"


def test_calibrated_scale_is_used_when_given() -> None:
    """Явная шкала из методики перекрывает ориентир."""
    scale = SCORING.calibration_points.scale_for("equity_ratio")
    assert scale is not None
    assert scale.score_for(Decimal("0.30")) == Decimal(50)
    assert scale.score_for(Decimal("0.35")) == Decimal("62.5"), "линейная интерполяция"
    assert scale.score_for(Decimal("0.05")) == Decimal(0), "ниже шкалы не экстраполируется"
    assert scale.score_for(Decimal("0.90")) == Decimal(100), "выше шкалы не экстраполируется"


def test_autonomy_scale_step_is_twenty_five_per_tenth() -> None:
    """Шаг шкалы автономии — 25 баллов на каждые 0,10, верхняя точка 0,50."""
    scale = SCORING.calibration_points.scale_for("equity_ratio")
    assert scale is not None
    previous = None
    for value, expected in (("0.10", 0), ("0.20", 25), ("0.30", 50), ("0.40", 75), ("0.50", 100)):
        score = scale.score_for(Decimal(value))
        assert score == Decimal(expected)
        if previous is not None:
            assert score - previous == Decimal(25)
        previous = score


def test_calibration_origin_is_declared_not_normative() -> None:
    """Опорные точки объявлены калибровкой, а не нормативом."""
    points = SCORING.calibration_points
    assert points.scale_type is ScaleType.ABSOLUTE
    assert "не являются нормативами" in points.origin
    assert "перцентил" in points.origin
    assert "отраслевой привязки" in points.limitation_note


def test_level_does_not_let_history_hide_current_failure() -> None:
    """Прошлое благополучие не перевешивает нынешний провал."""
    long_good = dec("1.5", "1.5", "1.5", "1.5", "1.5", "0.5")
    assert level_score(long_good, metric("cur_liq"), POLICY) < Decimal(50)


def test_good_side_depends_on_direction() -> None:
    """Правильная сторона ориентира зеркальна для «меньше — лучше»."""
    assert is_good_side(Decimal("1.5"), Decimal(1), Direction.HIGHER_BETTER)
    assert not is_good_side(Decimal("0.5"), Decimal(1), Direction.HIGHER_BETTER)
    assert is_good_side(Decimal("0.5"), Decimal(1), Direction.LOWER_BETTER)
    assert not is_good_side(Decimal("1.5"), Decimal(1), Direction.LOWER_BETTER)


def test_level_works_with_single_period() -> None:
    """Одного периода хватает для уровня, хотя динамики нет."""
    result = score("nwc", dec("100"))
    assert result.level == Decimal(100)
    assert result.dynamics is None
    assert result.score == Decimal(100)


# --- динамика ---------------------------------------------------------------


def test_direction_uses_metric_own_cutoff() -> None:
    """Отсечка существенного изменения берётся у самого показателя."""
    # У автономии отсечка 10 %, у рентабельности капитала — 100 %.
    assert metric("equity_ratio").material_change == Decimal("0.10")
    assert metric("roe").material_change == Decimal("1.00")

    change = dec("0.50", "0.55")  # рост на 10 %
    strict = dynamics_score(change, metric("equity_ratio"), POLICY, JUMP)
    loose = dynamics_score(change, metric("roe"), POLICY, JUMP)
    assert strict is not None and loose is not None
    # Для автономии это исчерпывающее движение, для рентабельности капитала — шум.
    assert strict > loose
    assert strict == Decimal(100)


def test_no_change_is_neutral() -> None:
    """Отсутствие изменения — середина шкалы, а не ноль."""
    result = dynamics_score(dec("1.5", "1.5"), metric("cur_liq"), POLICY, JUMP)
    assert result is not None
    assert Decimal(50) < result < Decimal(70), "плюс слагаемое за отсутствие разрывов"


def test_decline_lowers_direction() -> None:
    """Падение показателя снижает динамику."""
    fall = dynamics_score(dec("1.5", "1.0"), metric("cur_liq"), POLICY, JUMP)
    rise = dynamics_score(dec("1.0", "1.5"), metric("cur_liq"), POLICY, JUMP)
    assert fall is not None and rise is not None
    assert fall < rise


def test_direction_respects_lower_better() -> None:
    """Для «меньше — лучше» снижение показателя улучшает динамику."""
    fall = dynamics_score(dec("2.0", "1.0"), metric("debt_to_equity"), POLICY, JUMP)
    rise = dynamics_score(dec("1.0", "2.0"), metric("debt_to_equity"), POLICY, JUMP)
    assert fall is not None and rise is not None
    assert fall > rise


def test_gap_zeroes_component() -> None:
    """Разрыв более чем в пять раз обнуляет слагаемое за отсутствие разрывов."""
    smooth = dynamics_score(dec("1.0", "1.2", "1.4"), metric("cur_liq"), POLICY, JUMP)
    jumpy = dynamics_score(dec("1.0", "1.2", "12.0"), metric("cur_liq"), POLICY, JUMP)
    assert smooth is not None and jumpy is not None
    assert smooth > jumpy


def test_stability_counts_steps_in_trend_direction() -> None:
    """Устойчивость — доля шагов в сторону общего тренда.

    Последний шаг у обоих рядов одинаков (1,2 → 1,3), поэтому слагаемое
    за направление совпадает и сравниваются именно устойчивости: у ровного
    ряда все шаги в сторону тренда, у рваного — только один из трёх.
    """
    steady = dynamics_score(dec("1.0", "1.1", "1.2", "1.3"), metric("cur_liq"), POLICY, JUMP)
    zigzag = dynamics_score(dec("1.4", "1.0", "1.2", "1.3"), metric("cur_liq"), POLICY, JUMP)
    assert steady is not None and zigzag is not None
    assert steady > zigzag


def test_dynamics_needs_two_periods() -> None:
    """Одного периода для динамики недостаточно."""
    assert dynamics_score(dec("1.5"), metric("cur_liq"), POLICY, JUMP) is None


# --- исключение показателя --------------------------------------------------


def test_metric_without_values_is_excluded() -> None:
    """Нерассчитанный показатель в балл не идёт, причина сохраняется."""
    result = score("cur_liq", [])
    assert not result.included
    assert result.score is None
    assert "не рассчитан" in (result.exclusion_reason or "")


def test_single_period_without_benchmark_is_excluded() -> None:
    """Без ориентира и без второго периода оценивать нечем."""
    result = score("asset_turnover", dec("0.5"))
    assert not result.included
    assert "недостаточно" in (result.exclusion_reason or "")


def test_score_is_decimal_in_range() -> None:
    """Балл — Decimal в пределах шкалы."""
    for values in (dec("1.5", "1.6"), dec("0.1", "0.2"), dec("2.0", "0.5")):
        result = score("cur_liq", values)
        assert isinstance(result.score, Decimal)
        assert Decimal(0) <= result.score <= Decimal(100)


@pytest.mark.parametrize("code", [m.code for m in load_metrics().metrics])
def test_every_metric_has_material_change(code: str) -> None:
    """У каждого показателя задана собственная отсечка существенного изменения."""
    assert CATALOG.require(code).material_change > 0


def test_calibration_origin_is_declared() -> None:
    """Происхождение отсечек — поле методики, а не комментарий."""
    calibration = CATALOG.calibration.material_change
    assert calibration.organizations == 3
    assert calibration.observations == 159
    assert "задачи 11" in calibration.note


def test_benchmarks_are_only_sign_or_unity() -> None:
    """Ориентиры бесспорны: только ноль и единица, ни одного отраслевого порога."""
    values = {m.benchmark for m in CATALOG.metrics if m.benchmark is not None}
    assert values == {Decimal(0), Decimal(1)}
    assert sum(1 for m in CATALOG.metrics if m.benchmark is not None) == 10


def test_autonomy_has_no_duplicate_benchmark() -> None:
    """У автономии ориентира нет: «больше нуля» дублировал бы собственный капитал."""
    assert CATALOG.require("equity_ratio").benchmark is None
    assert CATALOG.require("equity").benchmark == Decimal(0)
