"""Справочник аудиторского заключения: виды мнения, разделы, оговорки.

Перечни живут в методике, а не в коде: вид мнения, наименования разделов
и тексты оговорок правятся диффом. В коде остаётся только правило чтения.
"""

import logging
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class OpinionKind(BaseModel):
    """Вид мнения и заголовки, которыми он объявляется."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    modified: bool
    headings: tuple[str, ...] = Field(min_length=1)


class ReportSection(BaseModel):
    """Раздел заключения, само наличие которого — признак."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    headings: tuple[str, ...] = Field(min_length=1)
    note: str | None = None


class AuditSignal(BaseModel):
    """Сигнал, выводимый из заключения.

    **Условие объявлено у каждого сигнала, а способ задать его не один.**
    Сигнал по разделу называет раздел и слова, которыми раздел объявляет
    предмет; сигнал по виду мнения не называет ни того ни другого — вид
    объявлен заголовком самого раздела мнения. Требовать раздел у обоих
    значило бы придумать его тому, у кого его нет.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    level: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    section: str | None = None
    markers: tuple[str, ...] = ()
    formulation: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    calibration_status: str = Field(min_length=1)

    @model_validator(mode="after")
    def _condition_names_what_it_needs(self) -> Self:
        """Условие по разделу обязано назвать раздел и слова, прочие — нет."""
        if self.condition == "section_found":
            if not self.section or not self.markers:
                raise ValueError(
                    f"сигнал {self.code}: условие по разделу требует раздел и слова"
                )
            return self
        if self.condition == "opinion_modified":
            if self.section or self.markers:
                raise ValueError(
                    f"сигнал {self.code}: условие по виду мнения раздела не имеет"
                )
            return self
        raise ValueError(f"сигнал {self.code}: условие {self.condition} неизвестно")


class ConfidenceRule(BaseModel):
    """Понижение уверенности в оценке по сведениям заключения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lowered_by_modified_opinion: bool
    reason: str = Field(min_length=1)
    origin: str = Field(min_length=1)


class Attribution(BaseModel):
    """Чем подписано заключение и как цитата печатается в документе."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auditor_markers: tuple[str, ...] = Field(min_length=1)
    auditor_forms: tuple[str, ...] = Field(min_length=1)
    date_pattern: str = Field(min_length=1)
    date_line_max_length: int = Field(ge=10)
    quote_template: str = Field(min_length=1)

    @model_validator(mode="after")
    def _template_names_its_parts(self) -> Self:
        """Шаблон цитаты обязан называть раздел, текст и подпись."""
        for part in ("{section}", "{text}", "{signed}"):
            if part not in self.quote_template:
                raise ValueError(f"шаблон цитаты не содержит {part}")
        return self


class AuditPolicy(BaseModel):
    """Правила чтения аудиторского заключения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    report_headings: dict[str, tuple[str, ...]]
    before_forms: bool
    ends_before: tuple[str, ...] = Field(min_length=1)
    attribution: Attribution
    opinions: tuple[OpinionKind, ...] = Field(min_length=1)
    sections: tuple[ReportSection, ...] = Field(min_length=1)
    signals: tuple[AuditSignal, ...] = Field(min_length=1)
    # Разделы, печатаемые в документе дословно.
    quoted_sections: tuple[str, ...] = Field(min_length=1)
    confidence: ConfidenceRule
    limitations: dict[str, str]

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Типы задания объявлены, сигналы ссылаются на заведённые разделы."""
        for kind in ("audit", "review"):
            if kind not in self.report_headings:
                raise ValueError(f"не объявлены заголовки для типа задания {kind}")
        sections = {item.code for item in self.sections}
        for signal in self.signals:
            if signal.section is not None and signal.section not in sections:
                raise ValueError(
                    f"сигнал {signal.code} ссылается на незаведённый раздел "
                    f"{signal.section}"
                )
        # Цитируемый раздел обязан быть заведённым: иначе документ обещает
        # дословный текст раздела, которого чтение не ищет вовсе.
        for code in self.quoted_sections:
            if code not in sections:
                raise ValueError(f"цитируемый раздел {code} не заведён в справочнике")
        # Немодифицированный вид обязан стоять последним: его заголовок
        # «Мнение» — начало всех прочих, и опознайся он первым, мнение
        # с оговоркой стало бы немодифицированным.
        if self.opinions[-1].modified:
            raise ValueError(
                "немодифицированное мнение обязано стоять последним в перечне: "
                "его заголовок является началом остальных"
            )
        for kind in ("review", "not_readable", "absent", "modified"):
            if kind not in self.limitations:
                raise ValueError(f"не объявлена оговорка {kind}")
        return self

    def opinion(self, code: str) -> OpinionKind | None:
        """Вид мнения по коду."""
        return next((item for item in self.opinions if item.code == code), None)

    def section(self, code: str) -> ReportSection | None:
        """Раздел по коду."""
        return next((item for item in self.sections if item.code == code), None)


def default_path() -> Path:
    """Путь к справочнику аудиторского заключения."""
    return settings.methodology_dir / "ifrs_audit.yaml"


def load_audit_policy(path: Path | None = None) -> AuditPolicy:
    """Читает правила чтения аудиторского заключения."""
    source = Path(path) if path is not None else default_path()
    policy = AuditPolicy.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник заключения %s: видов мнения %d, разделов %d, сигналов %d",
        policy.version,
        len(policy.opinions),
        len(policy.sections),
        len(policy.signals),
    )
    return policy
