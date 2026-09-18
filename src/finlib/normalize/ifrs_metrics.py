"""Справочник показателей по МСФО: состав, группы, шкалы, правила.

Перечни и пороги живут в методике: в коде остаётся правило расчёта.
Загрузка проверяет то, что нельзя проверить глазом, — сумму весов групп,
разрешимость ссылок и порядок опорных точек.
"""

import logging
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class MetricAdjustment(BaseModel):
    """Поправка состава показателя для типа эмитента."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    adjusted_name: str = Field(min_length=1)
    exclude_from_numerator: tuple[dict, ...] = Field(min_length=1)
    requires: tuple[str, ...] = Field(min_length=1)
    where: str = Field(min_length=1)
    on_missing: str = Field(pattern="^not_calculable$")
    reason_code: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    origin: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exclusions_are_named(self) -> Self:
        """У каждой исключаемой позиции объявлены код и причина."""
        for item in self.exclude_from_numerator:
            if "code" not in item or "reason" not in item:
                raise ValueError(f"поправка {self.metric}: исключение без кода или причины")
        return self


class MetricDef(BaseModel):
    """Определение показателя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    group: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    direction: str = Field(pattern="^(higher_better|lower_better)$")
    in_scoring: bool
    numerator: str = Field(min_length=1)
    denominator: str | None = None
    denominator_must_be_positive: bool = False
    denominator_source: str | None = None
    availability: str | None = None
    note: str | None = None
    exclusion_kind: str | None = None
    exclusion_reason: str | None = None

    @model_validator(mode="after")
    def _exclusion_is_explained(self) -> Self:
        """Показатель вне балла обязан объяснить, почему он вне его."""
        if not self.in_scoring and not (self.exclusion_kind and self.exclusion_reason):
            raise ValueError(
                f"{self.code} не идёт в балл, но причина не названа: "
                "исключение решением методики обязано быть объяснено"
            )
        return self


class Group(BaseModel):
    """Группа показателей и её номинальный вес."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    weight: int = Field(gt=0)


class AppendixGroup(BaseModel):
    """Группа показателей приложения: веса у неё нет и быть не может."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


class Scale(BaseModel):
    """Шкала перевода значения показателя в балл уровня."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    points: tuple[tuple[Decimal, int], ...] = Field(min_length=2)
    note: str | None = None

    @model_validator(mode="after")
    def _points_are_ordered(self) -> Self:
        """Баллы опорных точек возрастают: иначе шкала не монотонна."""
        scores = [score for _, score in self.points]
        if scores != sorted(scores):
            raise ValueError("баллы опорных точек обязаны возрастать")
        if scores[0] != 0 or scores[-1] != 100:
            raise ValueError("шкала обязана начинаться нулём и кончаться сотней")
        return self


class Calibration(BaseModel):
    """Опорные точки шкал вместе с происхождением и оговоркой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scale_type: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    limitation_note: str = Field(min_length=1)
    metrics: dict[str, Scale]


class ClassDef(BaseModel):
    """Класс финансового состояния и нижняя граница балла."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    min_score: Decimal


class NoClass(BaseModel):
    """Условия, при которых класс не присваивается."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_group_weight: Decimal
    max_group_weight_reason: str = Field(min_length=1)
    min_metrics: int = Field(ge=1)
    min_metrics_reason: str = Field(min_length=1)
    min_groups: int = Field(ge=1)
    min_groups_reason: str = Field(min_length=1)


class Sufficiency(BaseModel):
    """Достаточность основания для присвоения класса."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    no_class: NoClass


class Divergence(BaseModel):
    """Расхождение двух мер одной группы."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    within_group: str = Field(min_length=1)
    metrics: tuple[str, ...] = Field(min_length=2)
    threshold: int = Field(gt=0)
    note: str = Field(min_length=1)


class Annualisation(BaseModel):
    """Приведение промежуточных величин к году."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    months_from: str = Field(pattern="^report_date_month$")
    months_origin: str = Field(min_length=1)
    scaled: tuple[str, ...] = Field(min_length=1)
    never_scaled: tuple[str, ...] = Field(min_length=1)
    never_scaled_reason: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    marker: str = Field(min_length=1)


class IfrsMetricsPolicy(BaseModel):
    """Справочник показателей по МСФО целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    adjustments: tuple[MetricAdjustment, ...] = Field(min_length=1)
    metrics: tuple[MetricDef, ...] = Field(min_length=1)
    appendix_groups: dict[str, AppendixGroup] = Field(default_factory=dict)
    groups: dict[str, Group]
    calibration_points: Calibration
    classes: tuple[ClassDef, ...] = Field(min_length=2)
    sufficiency: Sufficiency
    divergence: Divergence
    annualisation: Annualisation

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        """Веса, ссылки и шкалы согласованы между собой."""
        total = sum(item.weight for item in self.groups.values())
        if total != 100:
            raise ValueError(f"сумма весов групп {total}, а обязана равняться 100")
        for metric in self.metrics:
            if metric.in_scoring and metric.group not in self.groups:
                raise ValueError(
                    f"{metric.code} идёт в балл, но его группа {metric.group} "
                    "веса не имеет"
                )
            if metric.group not in self.groups and metric.group not in self.appendix_groups:
                raise ValueError(f"{metric.code}: группы {metric.group} нет")
            if metric.in_scoring and metric.code not in self.calibration_points.metrics:
                raise ValueError(
                    f"{metric.code} идёт в балл, но шкалы у него нет: показатель "
                    "без шкалы оценить нечем"
                )
        known = {item.code for item in self.metrics}
        for code in self.divergence.metrics:
            if code not in known:
                raise ValueError(f"расхождение объявлено по чужому показателю: {code}")
        for code in self.adjustments:
            if code.metric not in known:
                raise ValueError(f"поправка объявлена по чужому показателю: {code.metric}")
        return self

    def metric(self, code: str) -> MetricDef | None:
        """Показатель по коду."""
        return next((item for item in self.metrics if item.code == code), None)

    def scored(self) -> tuple[MetricDef, ...]:
        """Показатели, идущие в балл."""
        return tuple(item for item in self.metrics if item.in_scoring)

    def for_type(self, code: str) -> tuple[MetricAdjustment, ...]:
        """Поправки, действующие для этого типа эмитента."""
        return tuple(item for item in self.adjustments if item.type == code)


def load_ifrs_metrics(path: Path | None = None) -> IfrsMetricsPolicy:
    """Читает справочник показателей по МСФО."""
    source = path or settings.methodology_dir / "ifrs_metrics.yaml"
    policy = IfrsMetricsPolicy.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник показателей %s: показателей %d, из них в балле %d",
        policy.version,
        len(policy.metrics),
        len(policy.scored()),
    )
    return policy
