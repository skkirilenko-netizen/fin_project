"""Загрузка методик скоринга и флагов."""

from decimal import Decimal
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.metrics.definitions import Condition
from finlib.metrics.formula import Node, parse_formula


class StopEffect(StrEnum):
    """Что стоп-фактор делает с классом."""

    NONE = "none"
    LOWEST_CLASS = "lowest_class"
    CAP_AT_CLASS = "cap_at_class"


class Confidence(StrEnum):
    """Уверенность в оценке; значения совпадают с CHECK в схеме."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FlagLevel(StrEnum):
    """Уровень флага."""

    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# --- скоринг ----------------------------------------------------------------


class ScaleType(StrEnum):
    """Тип шкалы уровня."""

    ABSOLUTE = "absolute"
    PERCENTILE = "percentile"


class MetricScale(BaseModel):
    """Кусочно-линейная шкала: значение показателя в балл уровня."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: tuple[tuple[Decimal, Decimal], ...] = Field(min_length=2)
    note: str | None = None

    @model_validator(mode="after")
    def _check_points(self) -> Self:
        """Опорные точки возрастают по значению, баллы в пределах шкалы."""
        values = [value for value, _ in self.points]
        if values != sorted(values) or len(set(values)) != len(values):
            raise ValueError("опорные точки шкалы должны строго возрастать")
        for _, score in self.points:
            if not Decimal(0) <= score <= Decimal(100):
                raise ValueError(f"балл опорной точки {score} вне шкалы 0–100")
        return self

    def score_for(self, value: Decimal) -> Decimal:
        """Балл по значению с линейной интерполяцией между опорными точками.

        За крайними точками балл не меняется: шкала не экстраполируется.
        """
        if value <= self.points[0][0]:
            return self.points[0][1]
        if value >= self.points[-1][0]:
            return self.points[-1][1]
        for (low_value, low_score), (high_value, high_score) in zip(
            self.points, self.points[1:], strict=False
        ):
            if low_value <= value <= high_value:
                span = high_value - low_value
                if span == 0:
                    return high_score
                share = (value - low_value) / span
                return low_score + (high_score - low_score) * share
        return self.points[-1][1]  # pragma: no cover


class CalibrationPoints(BaseModel):
    """Абсолютные опорные точки шкал и объявление их происхождения.

    Опорные точки — не нормативы. Формулировки «ниже норматива» и подобные
    к ним неприменимы; правило для текста заключения — в prompts/rules.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale_type: ScaleType
    origin: str = Field(min_length=1)
    limitation_note: str = Field(min_length=1)
    metrics: dict[str, MetricScale] = Field(default_factory=dict)

    def scale_for(self, metric_code: str) -> MetricScale | None:
        """Шкала показателя, если она задана."""
        return self.metrics.get(metric_code)


class LevelWeights(BaseModel):
    """Веса внутри оценки уровня."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    last_period_weight: Decimal = Field(ge=0)
    persistence_weight: Decimal = Field(ge=0)
    benchmark_band_from_material_change: Decimal = Field(ge=0)


class DynamicsWeights(BaseModel):
    """Веса внутри оценки динамики."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction_weight: Decimal = Field(ge=0)
    stability_weight: Decimal = Field(ge=0)
    gap_weight: Decimal = Field(ge=0)
    gap_saturation: Decimal = Field(gt=1)


class MetricScorePolicy(BaseModel):
    """Как складывается балл одного показателя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level_weight: Decimal = Field(ge=0)
    dynamics_weight: Decimal = Field(ge=0)
    level: LevelWeights
    dynamics: DynamicsWeights


class GroupStatus(StrEnum):
    """Участвует ли группа в балльной оценке."""

    SCORED = "scored"
    EXCLUDED = "excluded"


class GroupPolicy(BaseModel):
    """Группа показателей и её вес."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: Decimal = Field(ge=0)
    scoring_status: GroupStatus = GroupStatus.SCORED
    reason: str | None = None

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        """Исключённая группа весит ноль и объясняет почему; участвующая весит больше нуля."""
        if self.scoring_status is GroupStatus.EXCLUDED:
            if self.weight != 0:
                raise ValueError(
                    f"группа «{self.name}» исключена из балла, но имеет вес {self.weight}"
                )
            if not (self.reason or "").strip():
                raise ValueError(f"группа «{self.name}» исключена из балла без объяснения причины")
        elif self.weight <= 0:
            raise ValueError(
                f"группа «{self.name}» участвует в балле, но имеет нулевой вес: "
                "исключение объявляется полем scoring_status"
            )
        return self


class ClassDef(BaseModel):
    """Класс финансового состояния и нижняя граница балла."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    min_score: Decimal = Field(ge=0)


class StopFactorPolicy(BaseModel):
    """Последствие стоп-фактора для класса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    metrics: tuple[str, ...] = Field(min_length=1)
    effect: StopEffect
    cap: str | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_cap(self) -> Self:
        """Ограничение класса требует указания, до какого именно."""
        if self.effect is StopEffect.CAP_AT_CLASS and not self.cap:
            raise ValueError(f"стоп-фактор {self.code}: не указан класс ограничения")
        if self.effect is not StopEffect.CAP_AT_CLASS and self.cap:
            raise ValueError(f"стоп-фактор {self.code}: cap задан при эффекте {self.effect}")
        return self


class DowngradeRule(BaseModel):
    """Основание для понижения уверенности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    description: str = Field(min_length=1)
    threshold: Decimal | None = None


class NoClassPolicy(BaseModel):
    """Условия, при которых класс не присваивается вовсе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_group_weight: Decimal = Field(gt=0, le=1)
    max_group_weight_reason: str = Field(min_length=1)
    min_metrics: int = Field(gt=0)
    min_metrics_reason: str = Field(min_length=1)
    min_groups: int = Field(gt=0)
    min_groups_reason: str = Field(min_length=1)


class BreadthLevel(BaseModel):
    """Порог узости основания для одного уровня уверенности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_metrics: int = Field(gt=0)
    max_groups: int = Field(gt=0)
    reason: str = Field(min_length=1)

    def applies(self, metrics: int, groups: int) -> bool:
        """Подпадает ли основание под этот порог."""
        return metrics <= self.max_metrics or groups <= self.max_groups


class BreadthConfidence(BaseModel):
    """Понижение уверенности по узости основания."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    medium: BreadthLevel
    low: BreadthLevel


class SufficiencyPolicy(BaseModel):
    """Достаточность основания для интегральной оценки."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    no_class: NoClassPolicy
    confidence: BreadthConfidence

    def blocking_reason(
        self, metrics: int, groups: int, max_weight: Decimal
    ) -> str | None:
        """Причина, по которой класс не присваивается, если такая есть.

        Доминирование одной группы проверяется первым: оно важнее числа
        показателей, потому что четыре показателя в четырёх группах
        информативнее шести в одной.
        """
        rules = self.no_class
        if max_weight > rules.max_group_weight:
            return " ".join(rules.max_group_weight_reason.split())
        if groups < rules.min_groups:
            return " ".join(rules.min_groups_reason.split())
        if metrics < rules.min_metrics:
            return " ".join(rules.min_metrics_reason.split())
        return None

    def breadth_reason(self, metrics: int, groups: int) -> tuple[str, str] | None:
        """Уровень уверенности по узости основания и объяснение."""
        if self.confidence.low.applies(metrics, groups):
            return Confidence.LOW.value, self.confidence.low.reason
        if self.confidence.medium.applies(metrics, groups):
            return Confidence.MEDIUM.value, self.confidence.medium.reason
        return None


class ConfidencePolicy(BaseModel):
    """Правила уверенности в оценке."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    levels: tuple[Confidence, ...]
    default: Confidence
    downgrade_on: tuple[DowngradeRule, ...]

    def rule(self, code: str) -> DowngradeRule | None:
        """Правило по коду."""
        return next((item for item in self.downgrade_on if item.code == code), None)

    def level_after(self, reasons: int) -> Confidence:
        """Уверенность после понижений: одно основание — средняя, два и более — низкая."""
        if reasons <= 0:
            return self.default
        return Confidence.MEDIUM if reasons == 1 else Confidence.LOW


class ScoringCatalog(BaseModel):
    """Методика расчёта балла и класса."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    calibration_points: CalibrationPoints
    metric_score: MetricScorePolicy
    groups: dict[str, GroupPolicy]
    classes: tuple[ClassDef, ...] = Field(min_length=2)
    lowest_class: str = Field(min_length=1)
    sufficiency: SufficiencyPolicy
    stop_factors: tuple[StopFactorPolicy, ...]
    confidence: ConfidencePolicy

    @model_validator(mode="after")
    def _check_group_weights(self) -> Self:
        """Сумма весов групп обязана равняться 100.

        Веса заданы явными числами и в рантайме не пересчитываются: иначе
        эта проверка перестала бы что-либо проверять. Исключённые группы
        весят ноль и в сумму не вносят ничего.
        """
        total = sum(item.weight for item in self.groups.values())
        if total != Decimal(100):
            raise ValueError(
                f"сумма весов групп равна {total}, а должна равняться 100: "
                "иначе общий балл зависит от того, сколько групп заведено"
            )
        return self

    def scored_groups(self) -> dict[str, GroupPolicy]:
        """Группы, участвующие в балльной оценке."""
        return {
            code: policy
            for code, policy in self.groups.items()
            if policy.scoring_status is GroupStatus.SCORED
        }

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Границы классов убывают, низший класс существует, коды уникальны."""
        codes = [item.code for item in self.classes]
        if len(set(codes)) != len(codes):
            raise ValueError("коды классов повторяются")
        bounds = [item.min_score for item in self.classes]
        if bounds != sorted(bounds, reverse=True):
            raise ValueError("границы классов должны убывать от старшего к младшему")
        if self.lowest_class not in codes:
            raise ValueError(f"низший класс {self.lowest_class} отсутствует в перечне")
        for factor in self.stop_factors:
            if factor.cap and factor.cap not in codes:
                raise ValueError(f"стоп-фактор {factor.code}: класс {factor.cap} неизвестен")
        return self

    def class_for(self, score: Decimal) -> ClassDef:
        """Класс по общему баллу; границы нестрогие сверху."""
        for item in self.classes:
            if score >= item.min_score:
                return item
        return self.classes[-1]

    def require_class(self, code: str) -> ClassDef:
        """Класс по коду."""
        found = next((item for item in self.classes if item.code == code), None)
        if found is None:
            raise KeyError(f"класс {code} отсутствует в методике")
        return found

    def rank_of(self, code: str) -> int:
        """Порядковый номер класса: меньше — старше."""
        return [item.code for item in self.classes].index(code)


# --- флаги ------------------------------------------------------------------


class FlagCondition(BaseModel):
    """Одно условие срабатывания флага."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expression: str = Field(min_length=1)
    condition: Condition
    value: Decimal

    @cached_property
    def tree(self) -> Node:
        """Разобранное выражение."""
        return parse_formula(self.expression)

    def holds(self, computed: Decimal) -> bool:
        """Выполняется ли условие на вычисленном значении."""
        if self.condition is Condition.LT:
            return computed < self.value
        if self.condition is Condition.LTE:
            return computed <= self.value
        if self.condition is Condition.GT:
            return computed > self.value
        return computed >= self.value


class FlagDef(BaseModel):
    """Определение флага."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    level: FlagLevel
    affects_class: bool
    lowers_confidence: bool = False
    combine: str = "all"
    conditions: tuple[FlagCondition, ...] = Field(min_length=1)
    calibration: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Класс флагами не меняется, выражения разбираются при загрузке."""
        if self.affects_class:
            raise ValueError(
                f"флаг {self.code}: класс определяется фиксированной арифметикой "
                "и флагами не меняется (инвариант 2)"
            )
        if self.combine not in {"all", "any"}:
            raise ValueError(f"флаг {self.code}: combine должен быть all или any")
        for condition in self.conditions:
            _ = condition.tree
        return self


class FlagsCatalog(BaseModel):
    """Справочник флагов."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    flags: tuple[FlagDef, ...]

    @model_validator(mode="after")
    def _check_unique(self) -> Self:
        """Коды флагов уникальны."""
        codes = [item.code for item in self.flags]
        if len(set(codes)) != len(codes):
            raise ValueError("коды флагов повторяются")
        return self

    def get(self, code: str) -> FlagDef | None:
        """Определение флага по коду."""
        return next((item for item in self.flags if item.code == code), None)


# --- загрузка ---------------------------------------------------------------


@lru_cache(maxsize=8)
def load_scoring(path: Path | None = None) -> ScoringCatalog:
    """Читает методику скоринга."""
    source = Path(path) if path is not None else settings.methodology_dir / "scoring.yaml"
    return ScoringCatalog.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))


@lru_cache(maxsize=8)
def load_flags(path: Path | None = None) -> FlagsCatalog:
    """Читает справочник флагов."""
    source = Path(path) if path is not None else settings.methodology_dir / "flags.yaml"
    return FlagsCatalog.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
