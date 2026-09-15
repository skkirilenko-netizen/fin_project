"""Балл одного показателя: уровень плюс динамика.

Уровень отвечает на вопрос «по какую сторону бесспорного ориентира находится
показатель и насколько устойчиво», динамика — «куда и как он движется».
Одной динамики недостаточно: устойчиво хорошая, но не растущая организация
получала бы средний балл, а слабая, но улучшающаяся — высокий.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from finlib.metrics.definitions import Direction, ExclusionKind, MetricDef
from finlib.scoring.definitions import MetricScale, MetricScorePolicy

logger = logging.getLogger(__name__)

BEST = Decimal(100)
NEUTRAL = Decimal(50)
WORST = Decimal(0)


@dataclass(frozen=True, slots=True)
class MetricScore:
    """Балл показателя с разложением на составляющие."""

    metric_code: str
    group_code: str
    value: Decimal | None
    score: Decimal | None
    level: Decimal | None
    dynamics: Decimal | None
    periods_used: int
    included: bool
    exclusion_reason: str | None = None
    exclusion_kind: ExclusionKind | None = None
    # Исключён решением методики (in_scoring: false), а не отсутствием данных.
    # Для уверенности это разные вещи: сознательный выбор не означает неполноты
    # отчётности.
    excluded_by_methodology: bool = False


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """Ограничивает значение отрезком."""
    return max(low, min(high, value))


def _oriented(delta: Decimal, direction: Direction) -> Decimal:
    """Приводит изменение к направлению «больше — лучше»."""
    return -delta if direction is Direction.LOWER_BETTER else delta


def is_good_side(value: Decimal, benchmark: Decimal, direction: Direction) -> bool:
    """По правильную ли сторону ориентира находится значение."""
    return value < benchmark if direction is Direction.LOWER_BETTER else value > benchmark


def position_score(
    value: Decimal, metric: MetricDef, policy: MetricScorePolicy, scale: MetricScale | None
) -> Decimal | None:
    """Балл за положение одного значения. Шкала кусочно-линейная, не ступенчатая.

    Приоритет у явной шкалы из calibration_points. Если её нет, но есть
    бесспорный ориентир, вокруг него строится линейный переход шириной
    в существенное изменение показателя: скачок класса от сотой доли
    коэффициента объяснить пользователю нельзя. При нулевом ориентире
    переход вырождается — смена знака и есть качественный переход.
    """
    if scale is not None:
        return scale.score_for(value)
    if metric.benchmark is None:
        return None

    band = abs(metric.benchmark) * metric.material_change
    band *= policy.level.benchmark_band_from_material_change
    distance = value - metric.benchmark
    if metric.direction is Direction.LOWER_BETTER:
        distance = -distance
    if band == 0:
        return BEST if distance > 0 else WORST
    return NEUTRAL + NEUTRAL * _clamp(distance / band, Decimal(-1), Decimal(1))


def level_score(
    values: list[Decimal],
    metric: MetricDef,
    policy: MetricScorePolicy,
    scale: MetricScale | None = None,
) -> Decimal | None:
    """Уровень: половина за последний период, половина за устойчивость положения.

    Только последний период — слишком дёргано; только устойчивость — прошлое
    перевешивает нынешний провал. Возвращает None, если у показателя нет
    ни шкалы, ни бесспорного ориентира: выдумывать порог мы не будем.
    """
    if not values:
        return None
    last = position_score(values[-1], metric, policy, scale)
    if last is None:
        return None
    positions = [position_score(item, metric, policy, scale) for item in values]
    known = [item for item in positions if item is not None]
    persistence = sum(known) / Decimal(len(known))
    weights = policy.level
    total = weights.last_period_weight + weights.persistence_weight
    return (last * weights.last_period_weight + persistence * weights.persistence_weight) / total


def dynamics_score(
    values: list[Decimal],
    metric: MetricDef,
    policy: MetricScorePolicy,
    jump_factor: Decimal,
) -> Decimal | None:
    """Динамика: направление, устойчивость тренда, отсутствие разрывов."""
    if len(values) < 2:
        return None

    parts: list[tuple[Decimal, Decimal]] = []
    weights = policy.dynamics

    base = abs(values[-2])
    if base > 0:
        relative = _oriented((values[-1] - values[-2]) / base, metric.direction)
        cut = metric.material_change
        normalized = _clamp(relative / cut, Decimal(-1), Decimal(1))
        direction = NEUTRAL + NEUTRAL * normalized
        parts.append((direction, weights.direction_weight))

    steps = [values[i] - values[i - 1] for i in range(1, len(values))]
    overall = _oriented(values[-1] - values[0], metric.direction)
    if len(steps) >= 2 and overall != 0:
        oriented_steps = [_oriented(step, metric.direction) for step in steps]
        same = sum(1 for step in oriented_steps if (step > 0) == (overall > 0))
        stability = BEST * Decimal(same) / Decimal(len(oriented_steps))
        parts.append((stability, weights.stability_weight))

    # Разрыв гасит слагаемое не мгновенно: от порога скачка до порога,
    # умноженного на gap_saturation, балл убывает линейно.
    worst_ratio = Decimal(0)
    for index in range(1, len(values)):
        previous = abs(values[index - 1])
        if previous > 0:
            worst_ratio = max(worst_ratio, abs(values[index]) / previous)
    if worst_ratio <= jump_factor:
        gap = BEST
    else:
        span = jump_factor * (weights.gap_saturation - 1)
        excess = _clamp((worst_ratio - jump_factor) / span, Decimal(0), Decimal(1))
        gap = BEST * (Decimal(1) - excess)
    parts.append((gap, weights.gap_weight))

    if not parts:
        return None
    total_weight = sum(weight for _, weight in parts)
    return sum(value * weight for value, weight in parts) / total_weight


def score_metric(
    metric: MetricDef,
    values: list[Decimal],
    policy: MetricScorePolicy,
    jump_factor: Decimal,
    scale: MetricScale | None = None,
) -> MetricScore:
    """Считает балл показателя по ряду значений от старого к новому."""
    if not metric.in_scoring:
        return MetricScore(
            metric.code, metric.group, values[-1] if values else None, None, None, None,
            len(values), False,
            (metric.scoring_exclusion_reason or "").strip() or "исключён из балльной оценки",
            metric.scoring_exclusion_kind,
            excluded_by_methodology=True,
        )
    if not values:
        return MetricScore(
            metric.code, metric.group, None, None, None, None, 0, False,
            "показатель не рассчитан ни за один период",
            ExclusionKind.NO_DATA,
        )

    level = level_score(values, metric, policy, scale)
    dynamics = dynamics_score(values, metric, policy, jump_factor)

    if level is None and dynamics is None:
        return MetricScore(
            metric.code, metric.group, values[-1], None, None, None, len(values), False,
            "бесспорного ориентира нет, а периодов недостаточно для оценки динамики",
        )

    parts = [
        (level, policy.level_weight),
        (dynamics, policy.dynamics_weight),
    ]
    usable = [(value, weight) for value, weight in parts if value is not None]
    total_weight = sum(weight for _, weight in usable)
    score = sum(value * weight for value, weight in usable) / total_weight

    return MetricScore(
        metric_code=metric.code,
        group_code=metric.group,
        value=values[-1],
        score=score,
        level=level,
        dynamics=dynamics,
        periods_used=len(values),
        included=True,
    )
