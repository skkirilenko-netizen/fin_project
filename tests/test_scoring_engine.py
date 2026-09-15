"""Тесты сборки оценки: группы, класс, стоп-факторы, уверенность."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from finlib.metrics.definitions import load_metrics
from finlib.quality.periods import PeriodConfidence
from finlib.quality.thresholds import load_thresholds
from finlib.scoring.definitions import (
    Confidence,
    GroupStatus,
    ScoringCatalog,
    StopEffect,
    load_flags,
    load_scoring,
)
from finlib.scoring.engine import (
    _apply_stop_factor,
    _confidence,
    _group_scores,
    _stop_factor,
    _total_score,
    breadth,
)
from finlib.scoring.metric_score import MetricScore, score_metric

SCORING = load_scoring()
CATALOG = load_metrics()
JUMP = load_thresholds().jump_detection.factor


def built(code: str, *values: str) -> MetricScore:
    """Балл показателя по заданному ряду, со шкалой из методики."""
    series = [Decimal(item) for item in values]
    return score_metric(
        CATALOG.require(code),
        series,
        SCORING.metric_score,
        JUMP,
        SCORING.calibration_points.scale_for(code),
    )


def assess_from(scores: list[MetricScore]) -> tuple[Decimal | None, str, str, str | None]:
    """Балл, класс до стоп-фактора и после; код сработавшего стоп-фактора."""
    groups = _group_scores(scores, SCORING)
    total = _total_score(groups)
    by_score = SCORING.class_for(total) if total is not None else SCORING.require_class(
        SCORING.lowest_class
    )
    policy, _ = _stop_factor(scores, CATALOG, SCORING)
    final = _apply_stop_factor(by_score.code, policy, SCORING)
    return total, by_score.code, final, policy.code if policy else None


# --- синтетические организации ----------------------------------------------


def test_stable_good_company_gets_high_class() -> None:
    """Устойчиво хорошая без роста: высокий класс, а не средний."""
    scores = [
        built("cur_liq", "1.8", "1.8", "1.8"),
        built("equity", "5000", "5000", "5000"),
        built("equity_ratio", "0.7", "0.7", "0.7"),
        built("net_margin", "0.15", "0.15", "0.15"),
        built("roa", "0.12", "0.12", "0.12"),
        built("nwc", "900", "900", "900"),
        built("interest_cover", "6", "6", "6"),
    ]
    total, by_score, final, stop = assess_from(scores)
    assert total is not None and total > Decimal(80)
    assert by_score == "A"
    assert final == "A"
    assert stop is None


def test_improving_bad_company_does_not_get_high_class() -> None:
    """Плохая с улучшением: класс низкий, несмотря на отличную динамику."""
    scores = [
        built("cur_liq", "0.3", "0.5", "0.7"),
        built("equity_ratio", "0.05", "0.08", "0.12"),
        built("net_margin", "-0.20", "-0.10", "-0.02"),
        built("roa", "-0.15", "-0.08", "-0.01"),
        built("nwc", "-800", "-500", "-200"),
    ]
    total, by_score, final, stop = assess_from(scores)
    assert total is not None and total < Decimal(50)
    assert final in {"D", "E"}
    assert stop == "weak_coverage"


def test_negative_equity_drops_to_lowest_class() -> None:
    """Отрицательный собственный капитал опускает класс до низшего."""
    scores = [
        built("cur_liq", "2.0", "2.1", "2.2"),
        built("equity", "500", "100", "-50"),
        built("net_margin", "0.2", "0.2", "0.2"),
    ]
    total, by_score, final, stop = assess_from(scores)
    assert by_score in {"A", "B", "C"}
    assert final == SCORING.lowest_class
    assert stop == "negative_equity"


def test_negative_working_capital_caps_class() -> None:
    """Отрицательный оборотный капитал ограничивает класс, но не опускает до низшего."""
    scores = [
        built("cur_liq", "1.5", "1.6", "1.7"),
        built("equity", "5000", "5200", "5400"),
        built("equity_ratio", "0.7", "0.7", "0.7"),
        built("net_margin", "0.2", "0.2", "0.2"),
        built("nwc", "-100", "-90", "-80"),
    ]
    total, by_score, final, stop = assess_from(scores)
    assert stop == "weak_coverage"
    assert final == "C"
    assert final != SCORING.lowest_class
    assert SCORING.rank_of(final) >= SCORING.rank_of(by_score)


def test_low_interest_cover_caps_class() -> None:
    """Покрытие процентов ниже единицы ограничивает класс."""
    scores = [
        built("equity", "5000", "5200", "5400"),
        built("equity_ratio", "0.8", "0.8", "0.8"),
        built("net_margin", "0.25", "0.25", "0.25"),
        built("interest_cover", "0.9", "0.9", "0.9"),
    ]
    _, by_score, final, stop = assess_from(scores)
    assert stop == "weak_coverage"
    assert SCORING.rank_of(final) >= SCORING.rank_of("C")


def test_negative_equity_outweighs_coverage() -> None:
    """При двух стоп-факторах применяется более тяжёлый."""
    scores = [
        built("equity", "-100", "-200", "-300"),
        built("nwc", "-100", "-90", "-80"),
    ]
    _, _, final, stop = assess_from(scores)
    assert stop == "negative_equity"
    assert final == SCORING.lowest_class


def test_cap_does_not_raise_class() -> None:
    """Ограничение не поднимает класс, если он и так ниже потолка."""
    assert _apply_stop_factor("E", SCORING.stop_factors[1], SCORING) == "E"
    assert _apply_stop_factor("A", SCORING.stop_factors[1], SCORING) == "C"


def test_empty_group_weight_is_redistributed() -> None:
    """Группа без рассчитанных показателей исключается, веса остальных нормируются."""
    scores = [built("cur_liq", "1.8", "1.8"), built("equity_ratio", "0.5", "0.5")]
    groups = _group_scores(scores, SCORING)
    live = [item for item in groups if item.score is not None]
    empty = [item for item in groups if item.score is None]

    assert {item.code for item in live} == {"liquidity", "capital_structure"}
    assert all(item.effective_weight == 0 for item in empty)
    assert sum(item.effective_weight for item in live) == Decimal(1)


def test_group_weights_sum_to_hundred() -> None:
    """Сумма весов групп равна 100: иначе балл зависит от числа заведённых групп."""
    assert sum(item.weight for item in SCORING.groups.values()) == Decimal(100)


def test_broken_group_weights_are_rejected() -> None:
    """Методика с неверной суммой весов не загружается."""
    raw = SCORING.model_dump(mode="json")
    raw["groups"]["liquidity"]["weight"] = 99
    with pytest.raises(ValidationError, match="сумма весов групп"):
        ScoringCatalog.model_validate(raw)


def test_metrics_excluded_from_scoring_are_named() -> None:
    """Исключённые из балла показатели объявлены с причиной и в балл не идут."""
    excluded = [item for item in CATALOG.metrics if not item.in_scoring]
    assert len(excluded) == 11
    for metric in excluded:
        assert metric.scoring_exclusion_reason
    result = built("fin_leverage", "1.5", "1.5")
    assert not result.included
    assert result.score is None
    assert "автономии" in (result.exclusion_reason or "")


def test_every_scored_metric_has_a_level_scale() -> None:
    """В балле не остаётся показателей без шкалы уровня.

    Показатель с одной лишь динамикой награждал бы за улучшение того, чей
    уровень мы оценить не умеем, и соотношение 0,6 к 0,4 соблюдалось бы
    только на словах.
    """
    for metric in CATALOG.metrics:
        if not metric.in_scoring:
            continue
        has_scale = (
            metric.benchmark is not None
            or SCORING.calibration_points.scale_for(metric.code) is not None
        )
        assert has_scale, f"{metric.code} участвует в балле без шкалы уровня"


def test_level_share_is_as_declared() -> None:
    """Фактическое соотношение уровня и динамики совпадает с объявленным."""
    result = built("cur_liq", "1.8", "1.8", "1.8")
    assert result.level is not None and result.dynamics is not None
    expected = (
        result.level * SCORING.metric_score.level_weight
        + result.dynamics * SCORING.metric_score.dynamics_weight
    )
    assert result.score == expected


def test_excluded_metrics_do_not_change_group_weight() -> None:
    """Нерасчётный показатель исключается из группы, вес группы не меняется."""
    scores = [built("cur_liq", "1.8", "1.8"), built("quick_liq")]
    groups = {item.code: item for item in _group_scores(scores, SCORING)}
    liquidity = groups["liquidity"]
    assert liquidity.metrics_used == 1
    assert liquidity.metrics_excluded == 1
    # Номинальный вес хранится долей, как и фактический: два соседних поля
    # в разных единицах давали в приложении 3000,0 % вместо 30,0 %.
    total = sum(item.weight for item in SCORING.groups.values())
    assert liquidity.nominal_weight == SCORING.groups["liquidity"].weight / total


def test_no_metrics_at_all_gives_lowest_class() -> None:
    """Совсем без показателей класс не выдумывается: низший и балл отсутствует."""
    total, _, final, _ = assess_from([built("cur_liq")])
    assert total is None
    assert final == SCORING.lowest_class


# --- границы классов --------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [("100", "A"), ("80", "A"), ("79.99", "B"), ("65", "B"), ("50", "C"), ("35", "D"), ("0", "E")],
)
def test_class_boundaries(score: str, expected: str) -> None:
    """Границы классов нестрогие сверху и не пересекаются."""
    assert SCORING.class_for(Decimal(score)).code == expected


def test_class_bounds_are_monotonic() -> None:
    """Границы убывают от старшего класса к младшему."""
    bounds = [item.min_score for item in SCORING.classes]
    assert bounds == sorted(bounds, reverse=True)


# --- уверенность ------------------------------------------------------------


def test_confidence_is_high_without_reasons() -> None:
    """Без оснований уверенность высокая."""
    groups = _group_scores([built("cur_liq", "1.8", "1.8")], SCORING)
    level, reasons = _confidence(groups, {"cur_liq": PeriodConfidence.VERIFIED}, [], SCORING)
    assert level is Confidence.HIGH
    assert reasons == []


def test_unverified_periods_lower_confidence() -> None:
    """Опора на непроверенные периоды понижает уверенность."""
    groups = _group_scores([built("cur_liq", "1.8", "1.8")], SCORING)
    level, reasons = _confidence(
        groups, {"cur_liq": PeriodConfidence.COMPARATIVE_ONLY}, [], SCORING
    )
    assert level is Confidence.MEDIUM
    assert any("не проверенные" in reason for reason in reasons)


def test_two_reasons_give_low_confidence() -> None:
    """Два основания и более — низкая уверенность."""
    # Из трёх балльных показателей ликвидности рассчитан один.
    scores = [built("cur_liq", "1.8", "1.8"), built("nwc"), built("own_wc_ratio")]
    groups = _group_scores(scores, SCORING)
    level, reasons = _confidence(
        groups, {"cur_liq": PeriodConfidence.COMPARATIVE_ONLY}, [], SCORING, scores
    )
    assert len(reasons) >= 2
    assert level is Confidence.LOW


def test_methodology_exclusions_are_not_incompleteness() -> None:
    """Показатель, исключённый методикой, неполнотой отчётности не считается.

    Иначе сознательное решение не начислять балл читалось бы как нехватка
    данных и понижало бы уверенность на ровном месте.
    """
    scores = [built("cur_liq", "1.8", "1.8"), built("quick_liq", "1.0", "1.0")]
    assert scores[1].excluded_by_methodology
    groups = _group_scores(scores, SCORING)
    _, reasons = _confidence(
        groups, {"cur_liq": PeriodConfidence.VERIFIED}, [], SCORING, scores
    )
    assert not any("не рассчитано" in reason for reason in reasons)


def test_confidence_does_not_change_class() -> None:
    """Уверенность считается отдельно и на класс не влияет."""
    scores = [built("cur_liq", "1.8", "1.8", "1.8"), built("equity", "100", "100", "100")]
    _, _, final_verified, _ = assess_from(scores)
    groups = _group_scores(scores, SCORING)
    _confidence(groups, {"cur_liq": PeriodConfidence.COMPARATIVE_ONLY}, [], SCORING)
    _, _, final_again, _ = assess_from(scores)
    assert final_verified == final_again


# --- методика ---------------------------------------------------------------


def test_stop_factor_gradations_are_documented() -> None:
    """У каждой градации стоп-фактора есть обоснование одной строкой."""
    assert len(SCORING.stop_factors) == 2
    effects = {item.effect for item in SCORING.stop_factors}
    assert effects == {StopEffect.LOWEST_CLASS, StopEffect.CAP_AT_CLASS}
    for factor in SCORING.stop_factors:
        assert len(factor.rationale) > 40, factor.code


def test_flags_never_change_class() -> None:
    """Ни один флаг не влияет на класс: инвариант 2."""
    assert all(not flag.affects_class for flag in load_flags().flags)


# --- достаточность основания -------------------------------------------------


def test_dominant_group_blocks_class() -> None:
    """Если одна группа забирает больше половины веса, класс не присваивается.

    Число показателей тут ни при чём: класс становится функцией одной группы.
    """
    scores = [
        built("equity_ratio", "0.5", "0.5"),
        built("net_margin", "0.2", "0.2"),
        built("roa", "0.1", "0.1"),
        built("roe", "0.15", "0.15"),
    ]
    groups = _group_scores(scores, SCORING)
    metrics_used, groups_used, max_weight = breadth(groups, scores)

    assert max_weight > Decimal("0.5")
    reason = SCORING.sufficiency.blocking_reason(metrics_used, groups_used, max_weight)
    assert reason is not None
    assert "одной группой" in reason


def test_broad_basis_allows_class() -> None:
    """При широком основании класс присваивается."""
    scores = [
        built("cur_liq", "1.8", "1.8"),
        built("nwc", "900", "900"),
        built("own_wc_ratio", "0.3", "0.3"),
        built("equity_ratio", "0.5", "0.5"),
        built("interest_cover", "5", "5"),
        built("net_margin", "0.2", "0.2"),
    ]
    groups = _group_scores(scores, SCORING)
    metrics_used, groups_used, max_weight = breadth(groups, scores)

    assert max_weight <= Decimal("0.5")
    assert SCORING.sufficiency.blocking_reason(metrics_used, groups_used, max_weight) is None


def test_too_few_metrics_blocks_class() -> None:
    """Меньше четырёх показателей — класс не присваивается."""
    reason = SCORING.sufficiency.blocking_reason(3, 3, Decimal("0.4"))
    assert reason is not None
    assert "недостаточно" in reason


def test_single_group_blocks_class() -> None:
    """Одна группа — интегральной оценки нет."""
    reason = SCORING.sufficiency.blocking_reason(9, 1, Decimal("1"))
    assert reason is not None


def test_dominance_is_checked_before_count() -> None:
    """Доминирование группы проверяется раньше числа показателей."""
    reason = SCORING.sufficiency.blocking_reason(2, 2, Decimal("0.9"))
    assert reason is not None
    assert "одной группой" in reason


def test_breadth_lowers_confidence_stepwise() -> None:
    """Узость основания понижает уверенность ступенчато."""
    assert SCORING.sufficiency.breadth_reason(10, 5) is None
    assert SCORING.sufficiency.breadth_reason(8, 4)[0] == "medium"
    assert SCORING.sufficiency.breadth_reason(9, 3)[0] == "medium"
    assert SCORING.sufficiency.breadth_reason(5, 4)[0] == "low"
    assert SCORING.sufficiency.breadth_reason(9, 2)[0] == "low"


def test_narrow_basis_caps_confidence() -> None:
    """Потолок уверенности по узости основания применяется даже без других оснований."""
    scores = [
        built("equity_ratio", "0.5", "0.5"),
        built("net_margin", "0.2", "0.2"),
        built("roa", "0.1", "0.1"),
        built("roe", "0.15", "0.15"),
    ]
    groups = _group_scores(scores, SCORING)
    confidences = dict.fromkeys(
        ("equity_ratio", "net_margin", "roa", "roe"), PeriodConfidence.VERIFIED
    )
    level, reasons = _confidence(groups, confidences, [], SCORING, scores)
    assert level is Confidence.LOW
    assert any("узкое" in reason for reason in reasons)


def test_excluded_group_has_zero_weight_and_reason() -> None:
    """Исключённая группа весит ноль и объясняет, почему исключена."""
    turnover = SCORING.groups["turnover"]
    assert turnover.scoring_status is GroupStatus.EXCLUDED
    assert turnover.weight == Decimal(0)
    assert "перцентил" in (turnover.reason or "")
    assert "turnover" not in SCORING.scored_groups()


def test_scored_group_weights_are_explicit_and_sum_to_hundred() -> None:
    """Веса участвующих групп заданы явно и в сумме дают 100 без пересчёта."""
    scored = SCORING.scored_groups()
    assert len(scored) == 4
    assert sum(item.weight for item in scored.values()) == Decimal(100)


def test_excluded_group_with_weight_is_rejected() -> None:
    """Исключённая группа с ненулевым весом методику не проходит."""
    raw = SCORING.model_dump(mode="json")
    raw["groups"]["turnover"]["weight"] = 15
    with pytest.raises(ValidationError, match="исключена из балла, но имеет вес"):
        ScoringCatalog.model_validate(raw)


def test_scored_group_with_zero_weight_is_rejected() -> None:
    """Нулевой вес у участвующей группы — ошибка: исключение объявляется явно."""
    raw = SCORING.model_dump(mode="json")
    raw["groups"]["liquidity"]["weight"] = 0
    with pytest.raises(ValidationError, match="нулевой вес"):
        ScoringCatalog.model_validate(raw)


def test_turnover_metrics_are_computed_but_unscored() -> None:
    """Показатели оборачиваемости считаются, но баллов не дают."""
    turnover = [item for item in CATALOG.metrics if item.group == "turnover"]
    assert len(turnover) == 3
    for metric in turnover:
        assert not metric.in_scoring
        assert "шкалы уровня" in (metric.scoring_exclusion_reason or "")
