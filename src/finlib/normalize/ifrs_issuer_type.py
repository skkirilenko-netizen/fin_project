"""Справочник типов эмитента и применимости стоп-факторов.

Нормы неприменимости живут в методике, а не в коде: прочитав справочник,
надо видеть, какой стоп-фактор к какому типу не применяется и при каком
условии. Поправка состава показателя сюда не входит — она в
`methodology/ifrs_metrics.yaml`, потому что относится к показателю,
а не к оценке.
"""

import logging
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class IssuerType(BaseModel):
    """Тип эмитента и признаки, по которым он опознаётся."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    default: bool = False
    determined_at: str | None = None
    structural_any_of: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    min_markers: int = 0
    confirmation: str = Field(min_length=1)

    @model_validator(mode="after")
    def _features_declared(self) -> Self:
        """У типа, определяемого по отчётности, есть структурный признак."""
        if self.default or self.determined_at:
            return self
        if not self.structural_any_of:
            raise ValueError(
                f"тип {self.code} определяется по отчётности, но структурного "
                "признака не имеет: по одним словам тип не определяется"
            )
        for code in self.structural_any_of:
            if not code.startswith("ifrs."):
                raise ValueError(f"признак {code} не код позиции МСФО")
        return self


class StopFactor(BaseModel):
    """Стоп-фактор ветки МСФО: величина, условие и последствие для класса.

    **Всё объявлено здесь, а не в замере.** Прежде перечень стоял в методике,
    а величина, по которой стоп-фактор проверяется, — в `eval/`: замер их
    применял, расчёт по фактам не знал о них вовсе.

    `statement` печатается читателю, `rationale` — нет: первое говорит,
    что стоп-фактор означает для этой организации, второе — почему методика
    устроена так, и рядом с оценкой читалось бы как оправдание.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Показатель, по которому проверяется условие. Именно показатель, а не
    # статья: у эмитента, не раскрывшего капитал отдельной строкой, статьи
    # нет, а показатель считается.
    metric: str = Field(min_length=1)
    condition: str = Field(pattern="^(lt|lte|gt|gte)$")
    value: str = Field(min_length=1)
    # Класс, которым ограничивается оценка при срабатывании.
    cap: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @property
    def threshold(self) -> Decimal:
        """Отсечка условия величиной."""
        return Decimal(self.value)

    def holds(self, value: Decimal | None) -> bool:
        """Сработал ли стоп-фактор на этой величине; None — проверять нечем."""
        if value is None:
            return False
        threshold = self.threshold
        if self.condition == "lt":
            return value < threshold
        if self.condition == "lte":
            return value <= threshold
        if self.condition == "gt":
            return value > threshold
        return value >= threshold


class Condition(BaseModel):
    """Условие неприменимости по обстановке."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric: str = Field(min_length=1)
    condition: str = Field(pattern="^(lt|lte|gt|gte)$")
    value: str = Field(min_length=1)


class NotApplicable(BaseModel):
    """Норма неприменимости стоп-фактора."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_factor: str = Field(min_length=1)
    kind: str = Field(pattern="^(by_type|by_context)$")
    type: str | None = None
    when: Condition | None = None
    rationale: str = Field(min_length=1)
    limitation: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(min_length=1)

    @model_validator(mode="after")
    def _norm_is_complete(self) -> Self:
        """У неприменимости по типу назван тип, по обстановке — условие."""
        if self.kind == "by_type" and not self.type:
            raise ValueError(f"неприменимость по типу без типа: {self.stop_factor}")
        if self.kind == "by_context" and self.when is None:
            raise ValueError(
                f"неприменимость по обстановке без условия: {self.stop_factor}"
            )
        return self


class AuditConsistency(BaseModel):
    """Сверка стоп-фактора с аудиторским заключением."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmed_by: dict[str, tuple[str, ...]]
    confirmed_note: str = Field(min_length=1)
    unconfirmed_note: str = Field(min_length=1)
    not_readable_note: str = Field(min_length=1)


class IssuerTypePolicy(BaseModel):
    """Справочник типов эмитента целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    types: tuple[IssuerType, ...] = Field(min_length=2)
    stop_factors: tuple[StopFactor, ...] = Field(min_length=1)
    not_applicable: tuple[NotApplicable, ...] = Field(min_length=1)
    audit_consistency: AuditConsistency

    @model_validator(mode="after")
    def _references_resolve(self) -> Self:
        """Нормы ссылаются на заведённые типы и стоп-факторы."""
        types = {item.code for item in self.types}
        factors = {item.code for item in self.stop_factors}
        if sum(1 for item in self.types if item.default) != 1:
            raise ValueError("тип по умолчанию обязан быть ровно один")
        for norm in self.not_applicable:
            if norm.stop_factor not in factors:
                raise ValueError(f"норма ссылается на чужой стоп-фактор: {norm.stop_factor}")
            if norm.type is not None and norm.type not in types:
                raise ValueError(f"норма ссылается на чужой тип: {norm.type}")
        for code in self.audit_consistency.confirmed_by:
            if code not in factors:
                raise ValueError(f"сверка ссылается на чужой стоп-фактор: {code}")
        return self

    @property
    def fallback(self) -> IssuerType:
        """Тип по умолчанию."""
        return next(item for item in self.types if item.default)

    def type_of(self, code: str) -> IssuerType | None:
        """Тип по коду."""
        return next((item for item in self.types if item.code == code), None)

    def norms_for(self, stop_factor: str) -> tuple[NotApplicable, ...]:
        """Нормы неприменимости этого стоп-фактора."""
        return tuple(
            item for item in self.not_applicable if item.stop_factor == stop_factor
        )


def load_issuer_types(path: Path | None = None) -> IssuerTypePolicy:
    """Читает справочник типов эмитента."""
    source = path or settings.methodology_dir / "ifrs_issuer_type.yaml"
    policy = IssuerTypePolicy.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник типов %s: типов %d, норм неприменимости %d",
        policy.version,
        len(policy.types),
        len(policy.not_applicable),
    )
    return policy


# Состав показателей живёт в своём справочнике: поправки по типу — лишь
# часть его, и держать их порознь значило бы иметь два описания одного.
def load_ifrs_metrics(path: Path | None = None):
    """Читает состав показателей по МСФО — из `normalize/ifrs_metrics.py`."""
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics as loader

    return loader(path)
