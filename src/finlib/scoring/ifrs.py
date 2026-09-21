"""Балл, группы и класс по МСФО (задача 27).

**Правило доминирования группы перенесено из РСБУ намеренно.** Оно здесь
не умозрительно: в долговую нагрузку входит покрытие погашений ближайшего
года, которое не считается ни у кого, и группа весом 40 держится на двух
показателях из трёх. Если после перераспределения одна группа забирает
больше половины веса, класс не присваивается — довод тот же, что в РСБУ:
четыре показателя в четырёх группах информативнее шести в одной.

**Расхождение двух мер одной группы объявляется, а не сглаживается.**
Чистый долг / EBITDA и FFO / Долг измеряют одно и то же разными способами;
систематическое расхождение означает либо содержательный сигнал, либо
неверную калибровку одной из шкал, и различить их может только человек.

**Неприменимость стоп-фактора работает в расчёте.** Показатель считается
и печатается, но в балл и в стоп-факторы не идёт — с оговоркой из методики,
дословно.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

from finlib.metrics.ifrs import MetricValue
from finlib.normalize.ifrs_metrics import IfrsMetricsPolicy, Scale, load_ifrs_metrics
from finlib.scoring.scale import class_by_printed_score, interpolate

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetricScore:
    """Балл одного показателя."""

    code: str
    name: str
    group: str
    value: Decimal
    score: Decimal

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        return (
            f"{self.name}: {self.value.quantize(Decimal('0.001'))} → "
            f"{self.score.quantize(Decimal('0.1'))}"
        )


@dataclass(frozen=True, slots=True)
class GroupScore:
    """Балл группы вместе с номинальным и фактическим весом."""

    code: str
    name: str
    score: Decimal
    nominal_weight: Decimal
    effective_weight: Decimal
    metrics: tuple[MetricScore, ...]

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        return (
            f"{self.name}: {self.score.quantize(Decimal('0.1'))} "
            f"(вес номинальный {self.nominal_weight:.1%}, "
            f"фактический {self.effective_weight:.1%}, "
            f"показателей в балле {len(self.metrics)})"
        )


@dataclass(frozen=True, slots=True)
class Assessment:
    """Итог оценки: балл, класс либо отказ с причиной."""

    score: Decimal | None = None
    class_code: str | None = None
    class_name: str = ""
    no_class_reason: str = ""
    groups: tuple[GroupScore, ...] = ()
    divergence: tuple[str, ...] = field(default_factory=tuple)
    # Разрыв двух мер одной группы печатается всегда, а не только при
    # превышении порога: ноль превышений при неизвестном разрыве неотличим
    # от несделанного сравнения.
    divergence_gap: Decimal | None = None

    def describe(self) -> str:
        """Однострочная сводка."""
        if self.class_code is None:
            return f"класс не присвоен: {self.no_class_reason}"
        return (
            f"класс {self.class_code} ({self.class_name}), балл "
            f"{self.score.quantize(Decimal('0.1'))}"
        )


def assess(
    metrics: tuple[MetricValue, ...],
    policy: IfrsMetricsPolicy | None = None,
    excluded: tuple[str, ...] = (),
) -> Assessment:
    """Считает балл и присваивает класс — либо отказывает с причиной.

    `excluded` — показатели, исключённые из балла неприменимостью
    стоп-фактора или решением по типу эмитента. Они уже рассчитаны
    и в приложение идут; в балл — нет.
    """
    policy = policy or load_ifrs_metrics()
    scores: list[MetricScore] = []
    for item in metrics:
        if not item.in_scoring or not item.calculable or item.code in excluded:
            continue
        scale = policy.calibration_points.metrics.get(item.code)
        if scale is None:
            continue
        scores.append(
            MetricScore(
                item.code, item.name, item.group, item.value, _level(item.value, scale)
            )
        )

    groups = _groups(scores, policy)
    if not groups:
        return Assessment(no_class_reason=policy.sufficiency.no_class.min_metrics_reason)

    total = sum(
        (item.score * item.effective_weight for item in groups), start=Decimal(0)
    )
    divergence, gap = _divergence(scores, policy)
    rules = policy.sufficiency.no_class
    if len(scores) < rules.min_metrics:
        return Assessment(
            total, None, "", rules.min_metrics_reason, groups, divergence, gap
        )
    if len(groups) < rules.min_groups:
        return Assessment(
            total, None, "", rules.min_groups_reason, groups, divergence, gap
        )
    if max(item.effective_weight for item in groups) > rules.max_group_weight:
        return Assessment(
            total, None, "", rules.max_group_weight_reason, groups, divergence, gap
        )

    # Порог класса сверяется с напечатанным баллом, и правило одно на два
    # стандарта: у РСБУ балл 79,997 печатался как «80,00» при классе B.
    chosen = class_by_printed_score(total, policy.classes)
    return Assessment(total, chosen.code, chosen.name, "", groups, divergence, gap)


def _groups(
    scores: list[MetricScore], policy: IfrsMetricsPolicy
) -> tuple[GroupScore, ...]:
    """Баллы групп с перераспределением веса на рассчитанные.

    Вес группы, у которой не рассчитано ни одного показателя, переходит
    к остальным: иначе ненайденная величина понижала бы балл, а это
    не оценка, а наказание за неполноту данных.
    """
    by_group: dict[str, list[MetricScore]] = {}
    for item in scores:
        by_group.setdefault(item.group, []).append(item)
    if not by_group:
        return ()
    nominal = {
        code: Decimal(policy.groups[code].weight) / Decimal(100) for code in by_group
    }
    total = sum(nominal.values(), start=Decimal(0))
    return tuple(
        GroupScore(
            code,
            policy.groups[code].name,
            sum((item.score for item in items), start=Decimal(0)) / Decimal(len(items)),
            nominal[code],
            nominal[code] / total,
            tuple(items),
        )
        for code, items in sorted(by_group.items())
    )


def _divergence(
    scores: list[MetricScore], policy: IfrsMetricsPolicy
) -> tuple[tuple[str, ...], Decimal | None]:
    """Расхождение двух мер одной группы; разрыв возвращается всегда.

    Ноль превышений порога при неизвестном разрыве неотличим от несделанного
    сравнения, поэтому величина разрыва отдаётся и тогда, когда порог
    не превышен.
    """
    found = {
        item.code: item for item in scores if item.code in policy.divergence.metrics
    }
    if len(found) < len(policy.divergence.metrics):
        return (), None
    values = [item.score for item in found.values()]
    gap = max(values) - min(values)
    if gap <= policy.divergence.threshold:
        return (), gap
    listed = "; ".join(item.describe() for item in found.values())
    logger.info("расхождение мер долговой нагрузки: %s", listed)
    return (f"{policy.divergence.note} Расхождение {gap:.0f} балла: {listed}.",), gap


def level(value: Decimal, scale: Scale) -> Decimal:
    """Балл уровня по калибровочной шкале — та же функция, что в оценке.

    Названа открыто, потому что спрашивают её двое: оценка и тезис. Второе
    выражение того же балла разошлось бы с первым, и тезис говорил бы
    о другой части шкалы, чем класс.
    """
    return _level(value, scale)


def _level(value: Decimal, scale: Scale) -> Decimal:
    """Балл уровня по кусочно-линейной шкале.

    Арифметика та же, что у шкал РСБУ, и живёт она в одном месте
    (`scoring/scale.py`). Прежде здесь была своя реализация того же правила,
    и на нулевой ширине отрезка две давали разный балл: одна по верхней
    точке, другая по нижней.
    """
    return interpolate(scale.points, value)
