"""Стандарт отчётности: сквозное измерение всей модели данных.

Измерение называется `standard`, а не `source`: `src_file.source` занят
и означает другое — способ получения комплекта (ресурс или поданный файл).
Одна и та же отчётность приходит обоими способами, оставаясь отчётностью
одного стандарта.

Правила обращения со стандартом — в `methodology/standards.yaml`: какой
из них служит базой оценки, что происходит при смешении, как объявлено
расхождение между стандартами. В коде их нет.
"""

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class Standard(StrEnum):
    """Стандарт отчётности; значения совпадают с CHECK в схеме БД.

    Ряды по РСБУ и по МСФО несопоставимы и смешению не подлежат, поэтому
    стандарт входит в ключи src_file, fact_report, metric_value и assessment.
    Ветка ifrs пока не реализована — значение заведено заранее, чтобы при
    её появлении не мигрировать данные.
    """

    RSBU = "rsbu"
    IFRS = "ifrs"


class BaseStandardRule(BaseModel):
    """Какой стандарт служит базой оценки и что при этом оговаривается."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    preference: tuple[Standard, ...] = Field(min_length=1)
    rsbu_base_note: str = Field(min_length=1)
    secondary_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _all_standards_are_ordered(self) -> "BaseStandardRule":
        """Порядок предпочтения объявлен для всех стандартов и без повторов."""
        if len(set(self.preference)) != len(self.preference):
            raise ValueError("порядок предпочтения стандартов содержит повторы")
        missing = set(Standard) - set(self.preference)
        if missing:
            listed = ", ".join(sorted(item.value for item in missing))
            raise ValueError(f"порядок предпочтения не объявлен для стандартов: {listed}")
        return self

    def choose(self, available: set[Standard]) -> Standard | None:
        """Стандарт, служащий базой оценки; None — рассчитанного нет вовсе."""
        for item in self.preference:
            if item in available:
                return item
        return None


class MixingRule(BaseModel):
    """Что сообщается, когда величины показателя относятся к разным стандартам."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1)


class DivergenceRule(BaseModel):
    """Заготовка сигнала о расхождении показателя между стандартами.

    Объявлена, но не действует: порога нет, потому что наблюдений нет.
    Причина названа в справочнике, а не подразумевается, — иначе через месяц
    неработающий сигнал нельзя будет отличить от забытого.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    active: bool
    inactive_reason: str = Field(min_length=1)
    comparable_metrics: tuple[str, ...] = Field(min_length=1)
    period_match: str = Field(min_length=1)

    @model_validator(mode="after")
    def _inactive_has_no_threshold(self) -> "DivergenceRule":
        """Действующий сигнал обязан принести порог, а его в справочнике нет."""
        if self.active:
            raise ValueError(
                "сигнал расхождения объявлен действующим, но порога у него нет: "
                "величина отсечки задаётся после набора данных ветки МСФО"
            )
        return self


class PeriodPreference(BaseModel):
    """Выбор между годовым комплектом и промежуточным за тот же год.

    **Объявляется явно, потому что это тот случай, где мы уже получали
    произвольный выбор.** Выборка «комплект года» без порядка возвращает
    любой из четырёх, и расхождение видно только тогда, когда его ищут.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: str = Field(pattern="^(annual|interim)$")
    basis_origin: str = Field(min_length=1)
    interim_use: str = Field(min_length=1)
    rolling_formula: str = Field(min_length=1)
    rolling_origin: str = Field(min_length=1)
    interim_balance: str = Field(min_length=1)
    interim_confidence: str = Field(pattern="^(same|lower)$")
    interim_confidence_origin: str = Field(min_length=1)
    both_actual: bool


class StandardsPolicy(BaseModel):
    """Правила обращения со стандартом отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    base_standard: BaseStandardRule
    mixing: MixingRule
    divergence: DivergenceRule
    # **Годовой комплект и промежуточный за один год.** Правило объявлено
    # целиком: основание оценки — годовой, промежуточный служит наблюдением
    # между годовыми и приводится к скользящим двенадцати месяцам.
    period_preference: PeriodPreference


def default_path() -> Path:
    """Путь к справочнику правил обращения со стандартом."""
    return settings.methodology_dir / "standards.yaml"


@lru_cache(maxsize=8)
def load_standards(path: Path | None = None) -> StandardsPolicy:
    """Читает правила обращения со стандартом отчётности."""
    source = Path(path) if path is not None else default_path()
    return StandardsPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
