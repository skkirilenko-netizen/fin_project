"""Загрузка справочника строк форм РСБУ с проверкой целостности."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from finlib.config import settings


class Sign(StrEnum):
    """Допустимый знак значения строки."""

    POSITIVE = "positive"
    ANY = "any"


class Operator(StrEnum):
    """Оператор вхождения строки в состав итоговой."""

    PLUS = "+"
    MINUS = "-"


class Component(BaseModel):
    """Слагаемое итоговой строки с оператором."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^\d{4}$")
    op: Operator = Operator.PLUS


class LineDef(BaseModel):
    """Определение строки формы отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=r"^\d{4}$")
    name: str = Field(min_length=1)
    form: str = Field(pattern=r"^\d{7}$")
    section: str = Field(min_length=1)
    sign: Sign = Sign.POSITIVE
    in_brackets: bool = False
    is_total: bool = False
    components: tuple[Component, ...] = ()

    @model_validator(mode="after")
    def _check_components(self) -> Self:
        """Состав есть только у итоговых строк и только непустой."""
        if self.is_total and not self.components:
            raise ValueError(f"итоговая строка {self.code} без состава")
        if not self.is_total and self.components:
            raise ValueError(f"строка {self.code} не итоговая, но имеет состав")
        codes = [component.code for component in self.components]
        if self.code in codes:
            raise ValueError(f"строка {self.code} входит в состав самой себя")
        if len(set(codes)) != len(codes):
            raise ValueError(f"в составе строки {self.code} повторяются коды")
        return self


class FormDef(BaseModel):
    """Форма отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)


class LinesCatalog(BaseModel):
    """Справочник строк всех форм с индексом по коду."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    forms: dict[str, FormDef]
    lines: tuple[LineDef, ...]

    _index: dict[str, LineDef] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Коды уникальны, формы известны, состав ссылается на существующие строки без циклов."""
        index: dict[str, LineDef] = {}
        for line in self.lines:
            if line.code in index:
                raise ValueError(f"код строки {line.code} встречается дважды")
            if line.form not in self.forms:
                raise ValueError(f"строка {line.code} ссылается на неизвестную форму {line.form}")
            index[line.code] = line

        for line in self.lines:
            for component in line.components:
                target = index.get(component.code)
                if target is None:
                    raise ValueError(
                        f"в составе строки {line.code} указан отсутствующий код {component.code}"
                    )
                if target.form != line.form:
                    raise ValueError(
                        f"строка {component.code} формы {target.form} входит в состав "
                        f"строки {line.code} формы {line.form}"
                    )

        _check_no_cycles(index)
        self._index = index
        return self

    def get(self, code: str) -> LineDef | None:
        """Возвращает определение строки или None, если кода нет в справочнике."""
        return self._index.get(code)

    def require(self, code: str) -> LineDef:
        """Возвращает определение строки, иначе поднимает KeyError."""
        line = self._index.get(code)
        if line is None:
            raise KeyError(f"код строки {code} отсутствует в справочнике")
        return line

    def has(self, code: str) -> bool:
        """Проверяет наличие кода в справочнике."""
        return code in self._index

    def for_form(self, form: str) -> tuple[LineDef, ...]:
        """Строки одной формы в порядке справочника."""
        return tuple(line for line in self.lines if line.form == form)

    def totals(self, form: str | None = None) -> tuple[LineDef, ...]:
        """Итоговые строки: всех форм либо одной."""
        return tuple(
            line for line in self.lines if line.is_total and (form is None or line.form == form)
        )

    @property
    def codes(self) -> frozenset[str]:
        """Множество известных кодов строк."""
        return frozenset(self._index)


def _check_no_cycles(index: dict[str, LineDef]) -> None:
    """Проверяет отсутствие циклов в составе итоговых строк."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(code: str, path: tuple[str, ...]) -> None:
        if code in visited:
            return
        if code in visiting:
            chain = " -> ".join((*path, code))
            raise ValueError(f"цикл в составе итоговых строк: {chain}")
        visiting.add(code)
        for component in index[code].components:
            visit(component.code, (*path, code))
        visiting.discard(code)
        visited.add(code)

    for code in index:
        visit(code, ())


def default_path() -> Path:
    """Путь к справочнику строк по умолчанию."""
    return settings.methodology_dir / "lines.yaml"


@lru_cache(maxsize=8)
def load_lines(path: Path | None = None) -> LinesCatalog:
    """Читает и проверяет справочник строк; результат кэшируется по пути."""
    source = Path(path) if path is not None else default_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    return LinesCatalog.model_validate(raw)
