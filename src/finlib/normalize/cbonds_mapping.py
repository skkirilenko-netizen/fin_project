"""Справочник сопоставления полей Cbonds с позициями модели.

**Сопоставление — методика, а не деталь загрузчика.** Решение о том, чем
считать «Краткосрочный долг» агрегатора, участвует в суждении об эмитенте:
от него зависит долг, а от долга — долговая нагрузка. Поэтому оно объявлено
диффом в `methodology/cbonds_mapping.yaml`, а здесь только чтение и проверки
состава.

**Род поля объявляется обязательно** (`kind`): точное соответствие, агрегат
или сверка. Агрегат обязан сказать, что он покрывает и где это видно;
агрегат, который всё-таки грузится, обязан назвать причину — без неё
величина неизвестного состава попадала бы в факты молча.
"""

import logging
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)

# Формы нашей модели под короткими именами справочника: писать полный код
# позиции у каждого поля значило бы повторить его тридцать раз.
FORMS: dict[str, str] = {
    "position": "ifrs.statement_of_financial_position",
    "profit_or_loss": "ifrs.statement_of_profit_or_loss",
    "cash_flow": "ifrs.statement_of_cash_flows",
}


class FieldDef(BaseModel):
    """Поле источника и позиция модели, которой оно отвечает."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^ifrs\.[a-z][a-z0-9_]*$")
    form: str = Field(min_length=1)
    kind: str = Field(pattern="^(exact|aggregate|control)$")
    loaded: bool = True
    covers: tuple[str, ...] = ()
    seen_at: str | None = None

    @model_validator(mode="after")
    def _form_is_known(self) -> Self:
        """Форма названа коротким именем из перечня, а не произвольно."""
        if self.form not in FORMS:
            raise ValueError(f"{self.code}: форма {self.form} не объявлена")
        return self

    @model_validator(mode="after")
    def _aggregate_declares_itself(self) -> Self:
        """Агрегат объявляет состав и место наблюдения.

        Поле шире нашей позиции — это решение о данных, и оно обязано быть
        видно в справочнике: у Черкизово нематериальные активы агрегатора
        включают гудвил, и без объявления это выглядело бы ошибкой разбора.
        """
        if self.kind != "aggregate":
            return self
        if not self.covers or not (self.seen_at or "").strip():
            raise ValueError(
                f"{self.code}: агрегат объявлен без состава либо без наблюдения"
            )
        return self

    @property
    def form_code(self) -> str:
        """Полный код формы нашей модели."""
        return FORMS[self.form]


class SumControl(BaseModel):
    """Сверка итога с суммой частей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: str = Field(min_length=1)
    parts: tuple[str, ...] = Field(min_length=2)
    check: str = Field(min_length=1)
    origin: str | None = None


class EqualityControl(BaseModel):
    """Сверка двух полей между собой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    check: str = Field(min_length=1)


class ZeroTotal(BaseModel):
    """Ноль итога при ненулевой деятельности: признак нераскрытия."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    totals: tuple[str, ...] = Field(min_length=1)
    activity: tuple[str, ...] = Field(min_length=1)
    check: str = Field(min_length=1)


class Controls(BaseModel):
    """Сверки вида отчёта."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    debt_split: SumControl
    identity: EqualityControl
    sections: SumControl


class ReportDef(BaseModel):
    """Вид отчёта источника целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    standard: str = Field(min_length=1)
    name: str = Field(min_length=1)
    filter_field: str = Field(min_length=1)
    annual_only: bool
    currency_field: str = Field(min_length=1)
    unit_field: str = Field(min_length=1)
    standard_field: str = Field(min_length=1)
    units: dict[str, str] = Field(min_length=1)
    consolidated_marks: tuple[str, ...] = Field(min_length=1)
    standalone_marks: tuple[str, ...] = Field(min_length=1)
    fields: dict[str, FieldDef] = Field(min_length=1)
    controls: Controls
    zero_total: ZeroTotal
    reported: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _marks_are_not_empty(self) -> Self:
        """Признак консолидации не бывает пустой строкой.

        Пустая строка входит в любой текст — тот же дефект, что маркер рубля
        и звёздочка сноски. Здесь он дал бы «консолидированная» у каждой
        записи, и различить два стандарта стало бы нечем.
        """
        for name in (*self.consolidated_marks, *self.standalone_marks):
            if not name.strip():
                raise ValueError("признак стандарта объявлен пустой строкой")
        return self

    @model_validator(mode="after")
    def _controls_name_known_fields(self) -> Self:
        """Сверка ссылается на поля, которые справочник знает."""
        known = set(self.fields) | set(self.reported.values())
        named = {
            self.controls.identity.left,
            self.controls.identity.right,
            self.controls.debt_split.total,
            *self.controls.debt_split.parts,
            self.controls.sections.total,
            *self.controls.sections.parts,
            *self.zero_total.totals,
            *self.zero_total.activity,
        }
        stray = named - known
        if stray:
            raise ValueError(f"сверка ссылается на неизвестные поля: {sorted(stray)}")
        return self

    def check_codes(self) -> frozenset[str]:
        """Коды контролей, которые объявляет вид отчёта."""
        return frozenset(
            {
                self.controls.identity.check,
                self.controls.sections.check,
                self.controls.debt_split.check,
                self.zero_total.check,
            }
        )

    def loaded_fields(self) -> dict[str, FieldDef]:
        """Поля, которые становятся фактами."""
        return {
            name: item
            for name, item in self.fields.items()
            if item.kind != "control" and item.loaded
        }


class Substitution(BaseModel):
    """Замена величины, объявленная методикой."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: str | tuple[str, ...]
    fallback: str | tuple[str, ...]
    shown_as: str | None = None
    origin: str = Field(min_length=1)


class CbondsMapping(BaseModel):
    """Справочник сопоставления целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    reports: dict[str, ReportDef] = Field(min_length=1)
    substitutions: dict[str, Substitution] = Field(min_length=1)

    def report(self, name: str) -> ReportDef:
        """Вид отчёта по имени; неизвестный — ошибка, а не умолчание."""
        found = self.reports.get(name)
        if found is None:
            raise KeyError(
                f"вида отчёта {name} в справочнике сопоставления нет: "
                "поля источника наизусть загрузчик не знает"
            )
        return found


def load_cbonds_mapping(path: Path | None = None) -> CbondsMapping:
    """Читает справочник сопоставления полей Cbonds."""
    source = path or settings.methodology_dir / "cbonds_mapping.yaml"
    mapping = CbondsMapping.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    for name, report in mapping.reports.items():
        logger.info(
            "сопоставление %s (%s): полей %d, из них грузится %d, агрегатов %d",
            name,
            mapping.version,
            len(report.fields),
            len(report.loaded_fields()),
            sum(1 for item in report.fields.values() if item.kind == "aggregate"),
        )
    return mapping
