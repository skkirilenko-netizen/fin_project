"""Загрузка определений показателей из methodology/metrics.yaml."""

from decimal import Decimal
from enum import StrEnum
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.metrics.formula import (
    Node,
    average_codes,
    constant_names,
    denominator_of,
    line_codes,
    parse_formula,
)
from finlib.normalize.lines import LinesCatalog, ReportingType, load_lines


class Direction(StrEnum):
    """Куда лучше двигаться показателю; основа интерпретации динамики."""

    HIGHER_BETTER = "higher_better"
    LOWER_BETTER = "lower_better"
    NEUTRAL = "neutral"


class Unit(StrEnum):
    """Единица измерения показателя."""

    RATIO = "ratio"
    THOUSAND_RUB = "thousand_rub"
    DAYS = "days"
    PERCENT = "percent"


class Condition(StrEnum):
    """Условие срабатывания стоп-фактора."""

    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"


class StopFactor(BaseModel):
    """Порог, имеющий содержательный смысл вне отрасли."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    condition: Condition
    value: Decimal
    note: str = Field(min_length=1)

    def triggered(self, value: Decimal) -> bool:
        """Сработал ли стоп-фактор на этом значении."""
        if self.condition is Condition.LT:
            return value < self.value
        if self.condition is Condition.LTE:
            return value <= self.value
        if self.condition is Condition.GT:
            return value > self.value
        return value >= self.value


class GroupDef(BaseModel):
    """Группа показателей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


class MetricDef(BaseModel):
    """Определение одного показателя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    group: str = Field(min_length=1)
    unit: Unit
    direction: Direction
    # Относительное изменение, считающееся существенным. Задаётся у каждого
    # показателя: для автономии 10 % — крупное движение, для рентабельности
    # капитала — шум. Происхождение значений — в блоке calibration.
    material_change: Decimal = Field(gt=0)
    # Бесспорный ориентир: только знак или единица, ни одного отраслевого
    # порога. Отсутствие означает, что балл строится на одной динамике.
    benchmark: Decimal | None = None
    # Участвует ли показатель в балльной оценке. Рассчитывается и попадает
    # в заключение он в любом случае; false означает, что в балл он не идёт —
    # например, потому что дублирует другой показатель или служит
    # стоп-фактором, а не измерением.
    in_scoring: bool = True
    scoring_exclusion_reason: str | None = None
    # Знаменатель по экономическому смыслу неотрицателен: капитал, активы,
    # выручка, обязательства. Если фактически он отрицателен, коэффициент
    # не интерпретируется — минус в знаменателе делает «меньше — лучше»
    # похвалой за катастрофу. Признак задаётся здесь, а не списком в коде:
    # есть показатели, где минус в знаменателе осмыслен.
    denominator_must_be_positive: bool = False
    formula: str = Field(min_length=1)
    formulas: dict[ReportingType, str] = Field(default_factory=dict)
    applicable_to: tuple[ReportingType, ...] = Field(min_length=1)
    # Безусловная оговорка о содержании показателя: верна для любой
    # организации и любого набора отчётности. Идёт в раздел «Ограничения
    # анализа» заключения.
    note: str | None = None
    # Описание методики: почему показатель устроен так, а не иначе, и при
    # каком наборе отчётности он не считается. **В промпт не передаётся.**
    # Условная формулировка, поданная модели рядом с посчитанным значением,
    # читается как факт об организации: оговорку «в упрощённой отчётности
    # не рассчитывается» модель выдала за утверждение о Газпроме, который
    # сдаёт полную отчётность. Проверяется тестом, а не соглашением.
    methodology_note: str | None = None
    zero_denominator_note: str | None = None
    stop_factor: StopFactor | None = None

    @cached_property
    def trees(self) -> dict[ReportingType, Node]:
        """Разобранные формулы по наборам отчётности."""
        result: dict[ReportingType, Node] = {}
        for reporting_type in self.applicable_to:
            text = self.formulas.get(reporting_type, self.formula)
            result[reporting_type] = parse_formula(text)
        return result

    def tree_for(self, reporting_type: ReportingType) -> Node | None:
        """Дерево формулы для набора; None — показатель к набору неприменим."""
        return self.trees.get(reporting_type)

    def formula_text(self, reporting_type: ReportingType) -> str:
        """Текст формулы, применяемой для набора."""
        return self.formulas.get(reporting_type, self.formula)

    def requires_previous(self, reporting_type: ReportingType) -> bool:
        """Нужен ли предыдущий период: есть ли в формуле средние величины."""
        tree = self.tree_for(reporting_type)
        return bool(tree is not None and average_codes(tree))

    def is_applicable(self, reporting_type: ReportingType) -> bool:
        """Рассчитывается ли показатель для этого набора отчётности."""
        return reporting_type in self.applicable_to

    def denominator_for(self, reporting_type: ReportingType) -> Node | None:
        """Поддерево знаменателя формулы для набора отчётности."""
        tree = self.tree_for(reporting_type)
        return denominator_of(tree) if tree is not None else None

    @model_validator(mode="after")
    def _check_denominator_flag(self) -> Self:
        """Признак знаменателя требует, чтобы знаменатель в формуле был."""
        if not self.denominator_must_be_positive:
            return self
        for reporting_type in self.applicable_to:
            if denominator_of(self.trees[reporting_type]) is None:
                raise ValueError(
                    f"показатель {self.code}: denominator_must_be_positive задан, "
                    f"но формула для набора {reporting_type} делением не заканчивается"
                )
        return self

    @model_validator(mode="after")
    def _check_scoring_exclusion(self) -> Self:
        """Исключение из балла требует названной причины."""
        if not self.in_scoring and not (self.scoring_exclusion_reason or "").strip():
            raise ValueError(
                f"показатель {self.code} исключён из балльной оценки без объяснения причины"
            )
        return self

    @model_validator(mode="after")
    def _check_formulas(self) -> Self:
        """Формулы разбираются, переопределения относятся к применимым наборам."""
        for reporting_type in self.formulas:
            if reporting_type not in self.applicable_to:
                raise ValueError(
                    f"показатель {self.code}: переопределение формулы для набора "
                    f"{reporting_type}, к которому показатель неприменим"
                )
        _ = self.trees  # разбор формул на этапе загрузки, а не расчёта
        return self


class MaterialChangeCalibration(BaseModel):
    """Происхождение отсечек существенного изменения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    organizations: int = Field(gt=0)
    observations: int = Field(gt=0)
    source: str = Field(min_length=1)
    note: str = Field(min_length=1)


class Calibration(BaseModel):
    """Блок происхождения подобранных величин методики."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    material_change: MaterialChangeCalibration


# Окончания кодов производных величин. Живут здесь, а не в derived.py:
# справочник обязан отвергнуть показатель, чей код с ними совпадает, ещё
# при загрузке, иначе производная и показатель делили бы один код.
DERIVED_SUFFIXES: tuple[str, ...] = ("_chg_abs", "_chg_pct", "_share")


class ChangeDef(BaseModel):
    """Какие величины получают изменение за период."""

    model_config = ConfigDict(extra="forbid")

    lines: tuple[str, ...] = Field(min_length=1)
    metrics: bool

    @model_validator(mode="after")
    def _check_lines(self) -> Self:
        """Коды строк четырёхзначны и не повторяются."""
        _require_line_codes(self.lines, "derived.change.lines")
        return self


class ShareDef(BaseModel):
    """Вертикальная структура: доля строки в итоге."""

    model_config = ConfigDict(extra="forbid")

    denominator: str = Field(pattern=r"^\d{4}$")
    lines: tuple[str, ...] = Field(min_length=1)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_lines(self) -> Self:
        """Коды строк четырёхзначны, не повторяются и не равны знаменателю."""
        _require_line_codes(self.lines, "derived.share.lines")
        if self.denominator in self.lines:
            raise ValueError(
                f"derived.share.lines: строка {self.denominator} — сам знаменатель, "
                "её доля равна 100 по определению"
            )
        return self


class DerivedDef(BaseModel):
    """Производные величины: изменения за период и доли в итоге.

    В балльную оценку не входят: динамика уже учтена слагаемым в балле
    показателя, и повторный учёт того же изменения был бы двойным счётом.
    """

    model_config = ConfigDict(extra="forbid")

    change: ChangeDef
    share: ShareDef


def _require_line_codes(codes: tuple[str, ...], where: str) -> None:
    """Проверяет, что перечень — четырёхзначные коды строк без повторов."""
    seen: set[str] = set()
    for code in codes:
        if not (len(code) == 4 and code.isdigit()):
            raise ValueError(f"{where}: {code!r} не похож на код строки отчётности")
        if code in seen:
            raise ValueError(f"{where}: код {code} встречается дважды")
        seen.add(code)


class MetricsCatalog(BaseModel):
    """Справочник показателей."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    # Блок обязателен: подобранные величины без указания происхождения
    # неотличимы от выдуманных.
    calibration: Calibration
    derived: DerivedDef
    groups: dict[str, GroupDef]
    metrics: tuple[MetricDef, ...]

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Коды уникальны, группы известны."""
        seen: set[str] = set()
        for metric in self.metrics:
            if metric.code in seen:
                raise ValueError(f"код показателя {metric.code} встречается дважды")
            if metric.group not in self.groups:
                raise ValueError(
                    f"показатель {metric.code} ссылается на неизвестную группу {metric.group}"
                )
            if metric.code.endswith(DERIVED_SUFFIXES):
                raise ValueError(
                    f"код показателя {metric.code} оканчивается как производная величина: "
                    "коды производных строятся из кодов показателей, и это дало бы совпадение"
                )
            seen.add(metric.code)
        return self

    def get(self, code: str) -> MetricDef | None:
        """Определение показателя по коду."""
        return next((item for item in self.metrics if item.code == code), None)

    def require(self, code: str) -> MetricDef:
        """Определение показателя; отсутствие — ошибка."""
        metric = self.get(code)
        if metric is None:
            raise KeyError(f"показатель {code} отсутствует в методике")
        return metric

    def for_type(self, reporting_type: ReportingType) -> tuple[MetricDef, ...]:
        """Показатели, применимые к набору отчётности."""
        return tuple(item for item in self.metrics if item.is_applicable(reporting_type))

    def scored(self, reporting_type: ReportingType) -> tuple[MetricDef, ...]:
        """Показатели, участвующие в балльной оценке."""
        return tuple(item for item in self.for_type(reporting_type) if item.in_scoring)

    def by_group(self, group: str) -> tuple[MetricDef, ...]:
        """Показатели одной группы."""
        return tuple(item for item in self.metrics if item.group == group)

    def stop_factors(self) -> tuple[MetricDef, ...]:
        """Показатели, у которых объявлен стоп-фактор."""
        return tuple(item for item in self.metrics if item.stop_factor is not None)

    def stop_factor_values(self) -> dict[str, frozenset[Decimal]]:
        """Пороги стоп-факторов по коду показателя.

        Это единственные числа-ориентиры, объявленные методикой прямо, и
        называть их в заключении разрешено — но только при своём показателе.
        «Покрытие процентов ниже 1» правомерно, «текущая ликвидность ниже 1»
        — выдуманный норматив, хотя число то же самое.
        """
        return {
            item.code: frozenset({item.stop_factor.value})
            for item in self.stop_factors()
            if item.stop_factor
        }

    def validate_against(self, catalog: LinesCatalog, constants: set[str]) -> None:
        """Проверяет, что формулы и производные опираются на существующие строки."""
        known = (
            set(self.derived.change.lines)
            | set(self.derived.share.lines)
            | {self.derived.share.denominator}
        )
        for code in sorted(known):
            if not any(catalog.has(code, item) for item in ReportingType):
                raise ValueError(
                    f"производные величины: строка {code} отсутствует в справочнике строк"
                )
        for metric in self.metrics:
            for reporting_type in metric.applicable_to:
                tree = metric.trees[reporting_type]
                for code in line_codes(tree):
                    if not catalog.has(code, reporting_type):
                        raise ValueError(
                            f"показатель {metric.code}: строка {code} отсутствует "
                            f"в наборе {reporting_type}"
                        )
                unknown = constant_names(tree) - constants
                if unknown:
                    raise ValueError(
                        f"показатель {metric.code}: неизвестные константы {sorted(unknown)}"
                    )


def default_path() -> Path:
    """Путь к определениям показателей по умолчанию."""
    return settings.methodology_dir / "metrics.yaml"


@lru_cache(maxsize=8)
def load_metrics(path: Path | None = None) -> MetricsCatalog:
    """Читает и проверяет определения показателей; результат кэшируется."""
    from finlib.quality.thresholds import load_thresholds

    source = Path(path) if path is not None else default_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    catalog = MetricsCatalog.model_validate(raw)
    catalog.validate_against(load_lines(), set(load_thresholds().constants))
    return catalog
