"""Справочник статей консолидированной отчётности по МСФО.

Справочник параллельный справочнику РСБУ, а не его продолжение. Причина
в природе данных: кодов строк, утверждённых нормативным актом, в МСФО нет,
позиция опознаётся наименованием через синонимы, а состав статей меняется
от эмитента к эмитенту. В РСБУ первичен код, наименования повторяются
(«Заёмные средства» — и 1410, и 1510), а состав фиксирован формой.

Общее у двух справочников — арифметика состава итогов, и она вынесена
в `quality/totals.py`: контроли сходимости проверяют равенство суммы,
а не природу кодов.

Подтверждённые специфические статьи живут не здесь, а в таблице
`ifrs_line_confirmation`: методика правится руками и диффом, а позиция,
присвоенная во время работы, методикой не является.
"""

import logging
from collections import Counter
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.normalize.lines import Operator, Sign, normalize_name

logger = logging.getLogger(__name__)

# Код позиции: префикс обязателен. Четырёхзначное число в грамматике формул
# уже означает код строки РСБУ, голое строчное имя — константу методики;
# код с точкой не может быть ни тем, ни другим и в fact_report.line_code
# с первого взгляда отличим от кода РСБУ.
CODE_PATTERN = r"^ifrs\.[a-z][a-z0-9_]*$"


class Alias(BaseModel):
    """Наименование, под которым позиция встречена в отчётности эмитента.

    Эмитент назван не для порядка: через полгода при решении, поднимать ли
    специфическую статью в ядро, нужно видеть, откуда взялось написание,
    а не доверять памяти.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    seen_at: str = Field(min_length=1)


class IfrsComponent(BaseModel):
    """Слагаемое итоговой позиции с оператором."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)
    op: Operator = Operator.PLUS


class IfrsPosition(BaseModel):
    """Позиция унифицированной модели статей."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)
    name: str = Field(min_length=1)
    form: str = Field(pattern=CODE_PATTERN)
    section: str = Field(min_length=1)
    sign: Sign = Sign.POSITIVE
    in_brackets: bool = False
    is_total: bool = False
    components: tuple[IfrsComponent, ...] = ()
    aliases: tuple[Alias, ...] = ()
    # Безусловная оговорка о содержании позиции: верна для любого эмитента
    # и идёт в раздел «Ограничения анализа».
    note: str | None = None

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования, по которым позиция опознаётся.

        Собственное наименование входит в перечень наравне с синонимами:
        оно и есть первое из них.
        """
        names = (self.name, *(item.name for item in self.aliases))
        return tuple(dict.fromkeys(normalize_name(name) for name in names))

    @model_validator(mode="after")
    def _total_has_components(self) -> Self:
        """Состав есть только у итоговых позиций и только непустой."""
        if self.is_total and not self.components:
            raise ValueError(f"итоговая позиция {self.code} объявлена без состава")
        if self.components and not self.is_total:
            raise ValueError(f"позиция {self.code} не итоговая, но имеет состав")
        return self


class FormDef(BaseModel):
    """Раздел консолидированной отчётности.

    Кодов ОКУД у них нет: это не формы, утверждённые приказом, и называются
    они у эмитентов по-разному — отсюда синонимы и здесь.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования раздела."""
        return tuple(
            dict.fromkeys(normalize_name(item) for item in (self.name, *self.aliases))
        )


class Materiality(BaseModel):
    """Порог существенности специфической статьи."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    share_of_total_assets: Decimal = Field(gt=0, lt=1)
    origin: str = Field(min_length=1)


class CoreCandidate(BaseModel):
    """Когда подтверждённая статья становится кандидатом в ядро."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distinct_issuers: int = Field(ge=2)
    origin: str = Field(min_length=1)


class IfrsCatalog(BaseModel):
    """Унифицированная модель статей консолидированной отчётности."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    forms: dict[str, FormDef]
    positions: tuple[IfrsPosition, ...] = Field(min_length=1)
    materiality: Materiality
    core_candidate: CoreCandidate

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Справочник связен: коды уникальны, состав существует, синонимы врозь."""
        self._check_codes_are_unique()
        self._check_forms_exist()
        self._check_components_exist()
        self._check_aliases_do_not_overlap()
        return self

    def _check_codes_are_unique(self) -> None:
        """Один код — одна позиция."""
        repeated = [
            code
            for code, count in Counter(item.code for item in self.positions).items()
            if count > 1
        ]
        if repeated:
            raise ValueError(f"коды позиций повторяются: {', '.join(sorted(repeated))}")

    def _check_forms_exist(self) -> None:
        """Позиция объявлена в разделе, который есть в справочнике."""
        unknown = {item.form for item in self.positions} - set(self.forms)
        if unknown:
            raise ValueError(
                f"позиции ссылаются на неизвестные разделы: {', '.join(sorted(unknown))}"
            )

    def _check_components_exist(self) -> None:
        """Все слагаемые итогов существуют и лежат в том же разделе.

        Итог, ссылающийся на несуществующую позицию, не сойдётся никогда,
        а контроль сходимости сообщит об этом как о дефекте отчётности —
        то есть свалит нашу недоработку на эмитента.
        """
        known = {item.code for item in self.positions}
        for position in self.positions:
            missing = [item.code for item in position.components if item.code not in known]
            if missing:
                raise ValueError(
                    f"в составе {position.code} названы неизвестные позиции: "
                    f"{', '.join(sorted(missing))}"
                )
            by_code = {item.code: item for item in self.positions}
            other_form = [
                item.code
                for item in position.components
                if by_code[item.code].form != position.form
            ]
            if other_form:
                raise ValueError(
                    f"в составе {position.code} названы позиции другого раздела: "
                    f"{', '.join(sorted(other_form))}"
                )

    def _check_aliases_do_not_overlap(self) -> None:
        """Одно наименование не может принадлежать двум позициям.

        Опознание идёт по наименованию, и пересечение синонимов означает,
        что статья ляжет в ту позицию, которая встретилась раньше, — то есть
        произвольно. Это не дефект отчётности, а дефект справочника,
        и находиться он должен при загрузке, а не при разборе файла эмитента.
        """
        owners: dict[str, list[str]] = {}
        for position in self.positions:
            for name in position.match_names:
                owners.setdefault(name, []).append(position.code)
        overlapping = {
            name: codes for name, codes in owners.items() if len(codes) > 1
        }
        if overlapping:
            listed = "; ".join(
                f"«{name}» — {', '.join(sorted(codes))}"
                for name, codes in sorted(overlapping.items())
            )
            raise ValueError(f"наименования принадлежат нескольким позициям: {listed}")

    def get(self, code: str) -> IfrsPosition | None:
        """Позиция по коду; None — кода нет в справочнике."""
        return next((item for item in self.positions if item.code == code), None)

    def require(self, code: str) -> IfrsPosition:
        """Позиция по коду; отсутствие — ошибка справочника."""
        found = self.get(code)
        if found is None:
            raise KeyError(f"позиции {code} нет в справочнике МСФО")
        return found

    def match_by_name(self, name: str) -> IfrsPosition | None:
        """Позиция по наименованию из отчётности; None — не опознана.

        Неопознанная статья не теряется: её обязан записать разбор файла,
        и она же требует ручного подтверждения на экране сверки (задача 23).
        """
        normalized = normalize_name(name)
        return next(
            (item for item in self.positions if normalized in item.match_names), None
        )

    def match_form(self, name: str) -> str | None:
        """Код раздела по его заголовку в отчётности; None — не опознан."""
        normalized = normalize_name(name)
        return next(
            (code for code, form in self.forms.items() if normalized in form.match_names),
            None,
        )

    def totals(self, form: str | None = None) -> tuple[IfrsPosition, ...]:
        """Итоговые позиции — те, что проверяются контролями сходимости."""
        return tuple(
            item
            for item in self.positions
            if item.is_total and (form is None or item.form == form)
        )

    def for_form(self, form: str) -> tuple[IfrsPosition, ...]:
        """Позиции одного раздела отчётности."""
        return tuple(item for item in self.positions if item.form == form)


def default_path() -> Path:
    """Путь к справочнику статей МСФО."""
    return settings.methodology_dir / "ifrs_lines.yaml"


@lru_cache(maxsize=8)
def load_ifrs_lines(path: Path | None = None) -> IfrsCatalog:
    """Читает унифицированную модель статей консолидированной отчётности."""
    source = Path(path) if path is not None else default_path()
    catalog = IfrsCatalog.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник МСФО %s: позиций %d, из них итоговых %d",
        catalog.version,
        len(catalog.positions),
        len(catalog.totals()),
    )
    return catalog
