"""Состав регрессионного набора МСФО и правила его измерения (задача 29).

Состав — `eval/ifrs_sample.csv`, правила измерения — `eval/ifrs_regression_set.
yaml`. Лежат они порознь по той же причине, что и в РСБУ: состав правится
руками и часто, правила — редко и с обоснованием.

**Чем набор МСФО отличается от набора РСБУ.** Там отчётность берётся из ГИР БО
по ИНН, и участие организации в прогоне ничего не стоит. Здесь документ
выгружается руками, и цена участия — работа человека. Поэтому у каждого
эмитента объявлено, нужна ли выгрузка (`data_source`), а у каждого признака —
откуда он берётся (`source`). Объём ручной работы должен быть виден из файла,
а не выясняться по ходу прогона.

Модуль ничего не считает и никуда не ходит: он читает состав, проверяет его
и отвечает на вопрос «сколько документов выгружать и что они покрывают».
"""

import csv
import importlib.util
from collections import Counter
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

SAMPLE_PATH = settings.base_dir / "eval" / "ifrs_sample.csv"
RULES_PATH = settings.base_dir / "eval" / "ifrs_regression_set.yaml"


@lru_cache(maxsize=1)
def _inn_is_valid_ref():
    """Проверка ИНН из прогонщика РСБУ: определение одно на оба набора.

    Два способа проверять одно и то же неминуемо разойдутся, а опечатка
    в ИНН даёт не ошибку прогона, а тихий пропуск: источник просто
    не находит эмитента, и набор оказывается меньше, чем считает.
    """
    path = settings.base_dir / "eval" / "regression_run.py"
    spec = importlib.util.spec_from_file_location("regression_run", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._inn_is_valid


class DataSource(StrEnum):
    """Откуда берутся данные об эмитенте и нужна ли ручная выгрузка."""

    # Признаки берутся из нормализованных данных: документ не выгружается.
    CBONDS = "cbonds"
    # Признак есть только в документе: выгрузка обязательна.
    PDF = "pdf"
    # И то и другое: документ разбирается, данные агрегатора служат сверкой.
    BOTH = "both"

    @property
    def needs_document(self) -> bool:
        """Требует ли эмитент ручной выгрузки документа."""
        return self is not DataSource.CBONDS


class RunStatus(StrEnum):
    """Идёт ли эмитент в прогон."""

    RUN = "run"
    # Резерв остаётся в составе и в прогон не идёт: завтра понадобится.
    RESERVE = "reserve"


class Outcome(StrEnum):
    """Ожидаемый исход прогона по эмитенту."""

    ANALYSIS = "analysis"
    # Отказ — тоже успех: эмитент вне периметра подтверждает правило.
    REFUSAL = "refusal"
    MANUAL_REVIEW = "manual_review"


# Значения графы category_status: заявленное состояние гипотезы. Фактическое
# состояние считает прогон, а не файл.
CATEGORY_STATUS = frozenset({"гипотеза", "подтверждено косвенно", "подтверждено"})


class Entry(BaseModel):
    """Эмитент набора: ИНН, заявленная категория и основание включения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cat_id: int = Field(gt=0)
    category: str = Field(min_length=1)
    name: str = Field(min_length=1)
    inn: str = Field(pattern=r"^\d{10}$|^\d{12}$")
    reason: str = Field(min_length=1)
    category_status: str = Field(min_length=1)
    run_status: RunStatus = RunStatus.RUN
    expected_outcome: Outcome = Outcome.ANALYSIS
    data_source: DataSource = DataSource.BOTH

    @model_validator(mode="after")
    def _check(self) -> Self:
        """ИНН сходится по контрольным разрядам, статус гипотезы известен."""
        if not _inn_is_valid_ref()(self.inn):
            raise ValueError(f"ИНН {self.inn} не сходится по контрольным разрядам")
        if self.category_status not in CATEGORY_STATUS:
            listed = ", ".join(sorted(CATEGORY_STATUS))
            raise ValueError(
                f"статус категории «{self.category_status}» неизвестен; "
                f"допустимы: {listed}"
            )
        return self

    @property
    def in_run(self) -> bool:
        """Идёт ли эмитент в прогон."""
        return self.run_status is RunStatus.RUN

    @property
    def needs_document(self) -> bool:
        """Нужна ли по этому эмитенту ручная выгрузка."""
        return self.in_run and self.data_source.needs_document


class Feature(BaseModel):
    """Признак покрытия: наименование и то, откуда он берётся."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    source: DataSource

    @model_validator(mode="after")
    def _check(self) -> Self:
        """У признака объявлен источник, и это не «оба»: признак берётся откуда-то."""
        if self.source is DataSource.BOTH:
            raise ValueError(
                f"признак «{self.name}»: источник both объявлен у эмитента, "
                "а не у признака — признак берётся либо из данных, либо "
                "из документа"
            )
        return self


class Category(BaseModel):
    """Категория состава и признак, которым она подтверждается."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(gt=0)
    name: str = Field(min_length=1)
    feature: str | None = None
    # Причина, по которой машинного признака нет. Категория без признака
    # и без причины — недосмотр, поэтому одно из двух обязательно.
    manual: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Объявлено ровно одно: признак либо причина его отсутствия."""
        if bool(self.feature) == bool(self.manual):
            raise ValueError(
                f"категория «{self.name}»: объявите либо feature, либо manual "
                "с причиной, по которой машинного признака нет"
            )
        return self


class Uncovered(BaseModel):
    """Признак, которого добор документов не даст, и почему."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    feature: str = Field(min_length=1)
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Metric(BaseModel):
    """Метрика прогона со знаменателем.

    Знаменатель обязателен: доля без него неотличима от отсутствия измерения.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    denominator: str = Field(min_length=1)


class Rules(BaseModel):
    """Правила измерения набора."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    sources: dict[str, str]
    features: dict[str, Feature]
    categories: tuple[Category, ...]
    outcomes: dict[str, str]
    metrics: dict[str, Metric]
    uncovered: tuple[Uncovered, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Признаки категорий существуют, исходы объявлены, источники названы."""
        for category in self.categories:
            if category.feature and category.feature not in self.features:
                raise ValueError(
                    f"категория «{category.name}» опирается на признак "
                    f"{category.feature}, которого в справочнике признаков нет"
                )
        declared = {item.value for item in Outcome}
        if set(self.outcomes) != declared:
            raise ValueError(
                "перечень исходов разошёлся с кодом: объявлены "
                f"{sorted(self.outcomes)}, в коде {sorted(declared)}"
            )
        for item in (DataSource.CBONDS, DataSource.PDF):
            if item.value not in self.sources:
                raise ValueError(f"источник {item.value} не объявлен в sources")
        return self


class IfrsSet(BaseModel):
    """Состав набора вместе с правилами его измерения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[Entry, ...]
    rules: Rules

    @model_validator(mode="after")
    def _check(self) -> Self:
        """Категории состава объявлены в правилах, ИНН не повторяются."""
        known = {item.id: item for item in self.rules.categories}
        for entry in self.entries:
            category = known.get(entry.cat_id)
            if category is None:
                raise ValueError(
                    f"эмитент {entry.inn}: категории {entry.cat_id} "
                    "в правилах измерения нет"
                )
            if category.name != entry.category:
                raise ValueError(
                    f"эмитент {entry.inn}: категория {entry.cat_id} называется "
                    f"«{category.name}», а в составе «{entry.category}»"
                )
        repeated = [
            inn for inn, count in Counter(item.inn for item in self.entries).items()
            if count > 1
        ]
        if repeated:
            raise ValueError(f"ИНН повторяются в составе: {', '.join(repeated)}")
        return self

    @property
    def in_run(self) -> tuple[Entry, ...]:
        """Эмитенты, идущие в прогон."""
        return tuple(item for item in self.entries if item.in_run)

    @property
    def documents_needed(self) -> tuple[Entry, ...]:
        """Эмитенты, по которым нужен документ."""
        return tuple(item for item in self.entries if item.needs_document)

    def documents_on_hand(self, root: Path | None = None) -> tuple[Entry, ...]:
        """Эмитенты, документ которых уже лежит в `data/raw/ifrs/{ИНН}/`.

        Считается по каталогу, а не объявляется в составе: объявленное
        наличие документа устаревает в тот день, когда файл переложили,
        и набор врал бы о себе сам.
        """
        root = root or settings.base_dir / "data" / "raw" / "ifrs"
        return tuple(
            item
            for item in self.documents_needed
            if any((root / item.inn).glob("*.pdf"))
        )

    def documents_to_fetch(self, root: Path | None = None) -> tuple[Entry, ...]:
        """Эмитенты, документ которых ещё предстоит выгрузить руками."""
        on_hand = {item.inn for item in self.documents_on_hand(root)}
        return tuple(
            item for item in self.documents_needed if item.inn not in on_hand
        )

    def features_by_source(self, source: DataSource) -> tuple[str, ...]:
        """Признаки, берущиеся из этого источника."""
        return tuple(
            code
            for code, item in sorted(self.rules.features.items())
            if item.source is source
        )

    def describe(self, root: Path | None = None) -> str:
        """Однострочная сводка: сколько эмитентов и сколько ручной работы."""
        return (
            f"эмитентов в составе {len(self.entries)}, из них в прогоне "
            f"{len(self.in_run)}; документов нужно "
            f"{len(self.documents_needed)}, на руках "
            f"{len(self.documents_on_hand(root))}, выгрузить "
            f"{len(self.documents_to_fetch(root))}; признаков без выгрузки "
            f"{len(self.features_by_source(DataSource.CBONDS))} из "
            f"{len(self.rules.features)}"
        )


def load_entries(path: Path | None = None) -> tuple[Entry, ...]:
    """Читает состав набора из CSV."""
    path = path or SAMPLE_PATH
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return tuple(
            Entry(
                cat_id=int(row["cat_id"]),
                category=row["category"].strip(),
                name=row["name"].strip(),
                inn=row["inn"].strip(),
                reason=row["reason"].strip(),
                category_status=row["category_status"].strip(),
                run_status=RunStatus(row["run_status"].strip()),
                expected_outcome=Outcome(row["expected_outcome"].strip()),
                data_source=DataSource(row["data_source"].strip()),
            )
            for row in csv.DictReader(handle, delimiter=";")
        )


def load_rules(path: Path | None = None) -> Rules:
    """Читает правила измерения набора из YAML."""
    path = path or RULES_PATH
    return Rules.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_set(
    sample: Path | None = None, rules: Path | None = None
) -> IfrsSet:
    """Читает набор целиком: состав и правила его измерения."""
    return IfrsSet(entries=load_entries(sample), rules=load_rules(rules))


def main() -> None:
    """Печатает состав набора и объём ручной работы."""
    found = load_set()
    print(found.describe())
    print("\nВЫГРУЗИТЬ ДОКУМЕНТЫ")
    for entry in found.documents_to_fetch():
        print(f"  {entry.inn} {entry.name} — {entry.reason}")
    print("\nДОКУМЕНТЫ НА РУКАХ")
    for entry in found.documents_on_hand():
        print(f"  {entry.inn} {entry.name}")
    print("\nПРИЗНАКИ БЕЗ ВЫГРУЗКИ")
    for code in found.features_by_source(DataSource.CBONDS):
        print(f"  {code}: {found.rules.features[code].name}")
    print("\nЧТО ОСТАНЕТСЯ НЕПРОВЕРЕННЫМ")
    for item in found.rules.uncovered:
        print(f"  {item.name}: {item.reason.strip()}")


if __name__ == "__main__":
    main()
