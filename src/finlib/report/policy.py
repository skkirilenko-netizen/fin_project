"""Правила состава документа: обязательные величины, вопросы, предложения.

Состав разделов не может зависеть от того, что модель сочтёт заслуживающим
упоминания. Экспертная оценка показала, чем это кончается: раздел «Фактическая
база» без совокупного долга и выручки, вопросы о строках, которых применённый
набор форм не предусматривает, и документ без вывода о дальнейших действиях.

Здесь читается `methodology/report.yaml`: перечни машинные, формулировки
предписанные. Ни один текст отсюда не переписывается — ни моделью, ни кодом.
"""

import logging
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class Trigger(StrEnum):
    """Машинные признаки, по которым выводится предложение о действиях.

    Признак — факт расчёта, а не суждение: сработал сигнал, сработал
    стоп-фактор, класс не присвоен, комплект отбракован, данные устарели.
    """

    ALWAYS = "always"
    SUPERVISORY_SIGNAL = "supervisory_signal"
    ATTENTION_SIGNAL = "attention_signal"
    STOP_FACTOR = "stop_factor"
    NO_CLASS = "no_class"
    FLAG_CONFLICT = "flag_conflict"
    STALE_DATA = "stale_data"
    QUARANTINED_SET = "quarantined_set"
    BLOCKING_CHECK_FAILED = "blocking_check_failed"


class QuestionSubject(StrEnum):
    """Основания вопросов к организации, в порядке тяжести последствий."""

    SUPERVISORY_SIGNAL = "supervisory_signal"
    STOP_FACTOR = "stop_factor"
    FLAG_CONFLICT = "flag_conflict"
    ATTENTION_SIGNAL = "attention_signal"
    QUARANTINED_SET = "quarantined_set"
    MISSING_METRIC = "missing_metric"


class Freshness(BaseModel):
    """Допустимый разрыв между отчётной датой и днём формирования документа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_months: int = Field(gt=0)
    origin: str = Field(min_length=1)
    statement: str = Field(min_length=1)

    def stale(self, months: int) -> bool:
        """Превышен ли разрыв."""
        return months > self.max_months

    def message(self, months: int) -> str:
        """Предписанная оговорка с подставленным разрывом."""
        return " ".join(self.statement.split()).replace("{months}", str(months))


class FactBase(BaseModel):
    """Обязательный состав раздела «Фактическая база»."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: tuple[str, ...] = Field(min_length=1)
    metrics: tuple[str, ...] = Field(min_length=1)
    top_changes: int = Field(ge=0)
    origin: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_lines(self) -> Self:
        """Строки задаются кодами РСБУ: перечень машинный, а не описательный."""
        for code in self.lines:
            if not (code.isdigit() and len(code) == 4):
                raise ValueError(f"«{code}» не похож на код строки отчётности")
        return self

    def required(
        self, lines: frozenset[str] | set[str], metrics: frozenset[str] | set[str]
    ) -> tuple[str, ...]:
        """Обязательные величины, существующие у этой организации.

        Отсутствующие отбрасываются: требовать назвать нераскрытую строку
        или нерассчитанный показатель значило бы требовать выдумать величину.
        """
        return (
            *(code for code in self.lines if code in lines),
            *(code for code in self.metrics if code in metrics),
        )


class Questions(BaseModel):
    """Сколько вопросов задавать и в каком порядке их основания."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_count: int = Field(gt=0)
    max_count: int = Field(gt=0)
    subject_order: tuple[QuestionSubject, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_counts(self) -> Self:
        """Нижняя граница не выше верхней, основания не повторяются."""
        if self.min_count > self.max_count:
            raise ValueError(
                f"вопросов не может быть от {self.min_count} до {self.max_count}"
            )
        if len(set(self.subject_order)) != len(self.subject_order):
            raise ValueError("основания вопросов в порядке повторяются")
        return self

    def rank_of(self, subject: QuestionSubject) -> int:
        """Место основания в порядке; неизвестное уходит в конец."""
        order = list(self.subject_order)
        return order.index(subject) if subject in order else len(order)


class Action(BaseModel):
    """Предложение по дальнейшим действиям с предписанной формулировкой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    when: tuple[Trigger, ...] = Field(min_length=1)
    text: str = Field(min_length=1)

    def fires(self, triggers: set[Trigger]) -> bool:
        """Выполнен ли хотя бы один признак."""
        return Trigger.ALWAYS in self.when or bool(triggers & set(self.when))

    @property
    def message(self) -> str:
        """Формулировка одной строкой, как она уйдёт в документ."""
        return " ".join(self.text.split())


class ReportPolicy(BaseModel):
    """Справочник правил состава документа."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    freshness: Freshness
    fact_base: FactBase
    questions: Questions
    actions: tuple[Action, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_codes(self) -> Self:
        """Коды предложений уникальны."""
        codes = [item.code for item in self.actions]
        if len(set(codes)) != len(codes):
            raise ValueError("коды предложений по действиям повторяются")
        return self

    def actions_for(self, triggers: set[Trigger]) -> list[Action]:
        """Предложения, условия которых выполнены, в порядке справочника."""
        return [item for item in self.actions if item.fires(triggers)]


def default_path() -> Path:
    """Путь к справочнику правил состава документа."""
    return settings.methodology_dir / "report.yaml"


@lru_cache(maxsize=8)
def load_policy(path: Path | None = None) -> ReportPolicy:
    """Читает правила состава документа."""
    source = Path(path) if path is not None else default_path()
    return ReportPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )


def months_between(earlier, later) -> int:
    """Полных месяцев между отчётной датой и днём формирования документа."""
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return max(months, 0)
