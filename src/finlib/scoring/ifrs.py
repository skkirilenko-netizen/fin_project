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
    # Сработавший стоп-фактор и класс до его применения. Класс E у набравшего
    # по баллу B и класс E у набравшего E — разные сведения, а выглядят
    # одинаково; правило то же, что в РСБУ.
    stop_factor_code: str | None = None
    class_before_stop: str | None = None
    # Узость основания при присвоенном стоп-фактором классе: класс есть,
    # балльной оценки нет, и молчать об этом нельзя.
    breadth_reason: str | None = None

    def describe(self) -> str:
        """Однострочная сводка."""
        if self.class_code is None:
            return f"класс не присвоен: {self.no_class_reason}"
        return (
            f"класс {self.class_code} ({self.class_name}), балл "
            f"{self.score.quantize(Decimal('0.1'))}"
        )


@dataclass(frozen=True, slots=True)
class StopFactors:
    """Стоп-факторы комплекта: проверенное, сработавшее и неприменимое.

    **Число проверенных стоит рядом с числом сработавших.** Ноль сработавших
    при неизвестном числе проверок не означает ни того, что стоп-факторов нет,
    ни того, что они проверялись: ровно так они и не работали — перечень был
    в методике, применение в замере, а расчёт по фактам звал оценку с пустым
    перечнем исключённых.
    """

    checked: int = 0
    triggered: tuple[str, ...] = ()
    # Показатели, вышедшие из балла неприменимостью стоп-фактора, вместе
    # с оговоркой методики: она печатается там, где читатель ищет причину
    # исключения, а не отдельным перечнем.
    excluded_reasons: tuple[tuple[str, str], ...] = ()
    # Класс, которым ограничена оценка, и стоп-фактор, его назначивший.
    cap: str | None = None
    code: str | None = None
    # Сверка сработавшего стоп-фактора с аудиторским заключением.
    audit_state: str = ""
    audit_note: str = ""

    @property
    def excluded(self) -> tuple[str, ...]:
        """Показатели, не идущие в балл из-за неприменимости стоп-фактора."""
        return tuple(code for code, _ in self.excluded_reasons)

    def limitation_of(self, metric: str) -> str | None:
        """Оговорка неприменимости по показателю; None — он в балле."""
        return next((text for code, text in self.excluded_reasons if code == metric), None)

    def describe(self) -> str:
        """Однострочная сводка для журнала и отчёта прогона."""
        head = (
            "сработали: " + ", ".join(self.triggered)
            if self.triggered
            else "ни один не сработал"
        )
        listed = ", ".join(self.excluded)
        return (
            f"стоп-факторы: проверено {self.checked}, {head}"
            + (f"; класс ограничен {self.cap}" if self.cap else "")
            + (f"; неприменимостью исключены: {listed}" if listed else "")
        )


def evaluate_stop_factors(
    metrics: tuple[MetricValue, ...],
    issuer_type: str,
    audit_sections: tuple[str, ...] = (),
    audit_readable: bool = False,
    policy: IfrsMetricsPolicy | None = None,
    types=None,
) -> StopFactors:
    """Проверяет стоп-факторы ветки по величинам комплекта.

    **Одна реализация на замер и на расчёт по фактам.** Прежде применимость
    считал только замер, а перечень величин, по которым стоп-факторы
    проверяются, лежал в `eval/`: в замере стоп-фактор менял исход, а
    в документе его не было вовсе.

    Порядок такой: сперва применимость по типу эмитента и по обстановке —
    неприменимый стоп-фактор в оценку не идёт, а его показатель выходит
    из балла с оговоркой из методики; затем условие по величине; затем
    градация — при нескольких сработавших берётся младший класс.
    """
    from finlib.normalize.ifrs_issuer_type import load_issuer_types
    from finlib.sources.ifrs_issuer_type import applicability, consistency

    policy = policy or load_ifrs_metrics()
    types = types or load_issuer_types()
    values = {item.code: item.value for item in metrics if item.calculable}
    by_code = {item.code: item for item in metrics}
    ranks = {item.code: index for index, item in enumerate(policy.classes)}

    triggered: list[str] = []
    excluded: list[tuple[str, str]] = []
    cap: str | None = None
    code: str | None = None
    for factor in types.stop_factors:
        outcome = applicability(factor.code, issuer_type, values, types)
        if not outcome.applicable:
            excluded.append((factor.metric, " ".join(outcome.limitation.split())))
            continue
        # **Вывод по знаку.** Неположительный числитель при положительном
        # знаменателе доказывает, что отношение ниже единицы, без деления —
        # и доказывает это даже тогда, когда само отношение не посчитано.
        # Правило объявлено методикой у того стоп-фактора, у которого оно
        # верно арифметически.
        found = by_code.get(factor.metric)
        by_sign = (
            factor.proven_by_sign and found is not None and found.below_one_by_sign
        )
        if not by_sign and not factor.holds(values.get(factor.metric)):
            continue
        triggered.append(factor.code)
        if cap is None or ranks[factor.cap] > ranks[cap]:
            cap, code = factor.cap, factor.code

    state, note = ("", "")
    if code is not None:
        found, text = consistency(code, audit_sections, audit_readable, types)
        state, note = found.value, " ".join(text.split())
    return StopFactors(
        checked=len(types.stop_factors),
        triggered=tuple(triggered),
        excluded_reasons=tuple(dict.fromkeys(excluded)),
        cap=cap,
        code=code,
        audit_state=state,
        audit_note=note,
    )


def assess(
    metrics: tuple[MetricValue, ...],
    policy: IfrsMetricsPolicy | None,
    stops: StopFactors,
) -> Assessment:
    """Считает балл, присваивает класс и применяет стоп-фактор.

    `stops` — **обязательный довод**: стоп-факторы, проверенные по величинам
    комплекта. Довод с умолчанием здесь означал бы, что оценку можно посчитать,
    не проверив ни одного стоп-фактора, и отличить это от «ни один не сработал»
    было бы нечем — ровно так ветка и работала.
    """
    policy = policy or load_ifrs_metrics()
    excluded = stops.excluded
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
    narrow = ""
    if len(scores) < rules.min_metrics:
        narrow = rules.min_metrics_reason
    elif len(groups) < rules.min_groups:
        narrow = rules.min_groups_reason
    elif max(item.effective_weight for item in groups) > rules.max_group_weight:
        narrow = rules.max_group_weight_reason
    # **Стоп-фактор старше правила достаточности** — правило объявлено
    # методикой и перенесено из РСБУ вместе с основанием: состояние,
    # установленное одной величиной, узостью основания не отменяется.
    # У Сегежи без него класс не присваивался вовсе: сработавший стоп-фактор
    # молчал, потому что групп показателей оказалось мало.
    overrides = policy.sufficiency.stop_factor_overrides_breadth
    if narrow and not (overrides and stops.cap is not None):
        return Assessment(total, None, "", narrow, groups, divergence, gap)

    # Порог класса сверяется с напечатанным баллом, и правило одно на два
    # стандарта: у РСБУ балл 79,997 печатался как «80,00» при классе B.
    chosen = class_by_printed_score(total, policy.classes)
    # **Стоп-фактор класс только понижает.** Если балл и без него хуже
    # ограничения, ограничение ничего не меняет — но стоп-фактор остаётся
    # названным: он обстоятельство, а не следствие балла.
    final = chosen
    if stops.cap is not None:
        ranks = {item.code: index for index, item in enumerate(policy.classes)}
        if ranks[stops.cap] > ranks[chosen.code]:
            final = next(item for item in policy.classes if item.code == stops.cap)
    return Assessment(
        total,
        final.code,
        final.name,
        "",
        groups,
        divergence,
        gap,
        stop_factor_code=stops.code,
        class_before_stop=chosen.code,
        # Узость основания при сработавшем стоп-факторе не замалчивается:
        # класс присвоен состоянием, а балльной оценки не существует, и число
        # рядом с классом читалось бы как её итог.
        breadth_reason=narrow or None,
    )


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
