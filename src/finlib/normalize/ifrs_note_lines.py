"""Справочник строк примечаний: загрузка и проверка целостности.

Справочник параллельный справочнику статей и **маленький намеренно**:
в примечание ведёт ссылка из строки формы, и объявить остаётся только то,
как называется нужная строка внутри него. Перечень строк в коде не живёт —
он методика и правится диффом.
"""

import logging
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.normalize.ifrs_lines import CODE_PATTERN, Alias
from finlib.normalize.lines import normalize_name

logger = logging.getLogger(__name__)


class NoteLine(BaseModel):
    """Строка примечания, нужная расчёту."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(pattern=CODE_PATTERN)
    name: str = Field(min_length=1)
    # Строки формы, по ссылке которых ищется примечание. Больше нигде строка
    # не ищется: поиск по документу даёт чужое число, а не отсутствие числа.
    found_in: tuple[str, ...] = Field(min_length=1)
    combine: str = "sum"
    aliases: tuple[Alias, ...] = ()

    @property
    def match_names(self) -> tuple[str, ...]:
        """Нормализованные наименования, по которым строка опознаётся."""
        names = (self.name, *(item.name for item in self.aliases))
        return tuple(dict.fromkeys(normalize_name(name) for name in names))


class InterestCover(BaseModel):
    """Состав показателя покрытия процентов по действительной стоимости долга."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: str = Field(pattern=CODE_PATTERN)
    denominator: tuple[str, ...] = Field(min_length=1)
    requires_capitalised_when_net: tuple[str, ...] = Field(min_length=1)
    origin: str = Field(min_length=1)


class NoteLineCatalog(BaseModel):
    """Справочник строк примечаний целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    lines: tuple[NoteLine, ...] = Field(min_length=1)
    interest_cover: InterestCover

    @model_validator(mode="after")
    def _check_integrity(self) -> Self:
        """Коды уникальны, а состав показателя ссылается на заведённые строки."""
        codes = [item.code for item in self.lines]
        doubled = {code for code in codes if codes.count(code) > 1}
        if doubled:
            raise ValueError(f"код строки примечания повторяется: {sorted(doubled)}")
        unknown = [
            code for code in self.interest_cover.denominator if code not in codes
        ]
        if unknown:
            raise ValueError(
                "состав покрытия процентов ссылается на незаведённые строки: "
                f"{unknown}"
            )
        # Одно наименование не может принадлежать двум строкам: иначе величина
        # ляжет в ту, что встретилась раньше, то есть произвольно.
        seen: dict[str, str] = {}
        for line in self.lines:
            for name in line.match_names:
                if name in seen and seen[name] != line.code:
                    raise ValueError(
                        f"наименование «{name}» принадлежит и {seen[name]}, "
                        f"и {line.code}"
                    )
                seen[name] = line.code
        return self

    def get(self, code: str) -> NoteLine | None:
        """Строка справочника по коду; None — такой не заведено."""
        return next((item for item in self.lines if item.code == code), None)

    def for_form_line(self, code: str) -> tuple[NoteLine, ...]:
        """Строки примечаний, которые ищутся по ссылке этой строки формы."""
        return tuple(item for item in self.lines if code in item.found_in)


def default_path() -> Path:
    """Путь к справочнику строк примечаний."""
    return settings.methodology_dir / "ifrs_note_lines.yaml"


def load_note_lines(path: Path | None = None) -> NoteLineCatalog:
    """Читает справочник строк примечаний."""
    source = Path(path) if path is not None else default_path()
    catalog = NoteLineCatalog.model_validate(
        yaml.safe_load(source.read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник строк примечаний %s: строк %d",
        catalog.version,
        len(catalog.lines),
    )
    return catalog
