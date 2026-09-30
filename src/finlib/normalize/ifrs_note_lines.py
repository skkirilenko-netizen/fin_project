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


class MaturityBucket(BaseModel):
    """Корзина печати сроков: границы в месяцах от отчётной даты."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    from_months: int = Field(ge=0)
    # Пусто — корзина открыта сверху («свыше 5 лет»).
    to_months: int | None = None


class MaturityRows(BaseModel):
    """Наименования строк таблицы сроков по родам: долг, аренда, прочее.

    Строка, не названная ни в одном роде, не опознана и печатается поимённо:
    угадать по слову «кредит» нельзя — «кредиторская задолженность» его
    содержит.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    debt: tuple[Alias, ...] = Field(min_length=1)
    # Долгоподобные обязательства: не займы, но долг по существу (решение
    # владельца 30.09.2026 — концессионные и инвестиционные соглашения
    # Автодора). Печатаются второй величиной «займы + долгоподобные».
    debt_like: tuple[Alias, ...] = ()
    lease: tuple[Alias, ...] = Field(min_length=1)
    other: tuple[Alias, ...] = ()

    def kind_of(self, name: str) -> str | None:
        """Род строки по наименованию; None — наименование не заведено."""
        wanted = normalize_name(name)
        for kind in ("debt", "debt_like", "lease", "other"):
            if any(normalize_name(item.name) == wanted for item in getattr(self, kind)):
                return kind
        return None


class MaturityStorage(BaseModel):
    """Коды хранения сроков: основа и род строк + границы графы в месяцах.

    Графа хранится как напечатана (решение владельца 30.09.2026):
    `ifrs.debt_cf_due_m012_m024` — недисконтированные потоки по займам
    от 12 до 24 месяцев; открытая сверху — `…_m060_plus`. В корзины печати
    графы сводятся только при печати.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Префикс кода по основе и роду строк: {основа: {род: префикс}}.
    prefixes: dict[str, dict[str, str]] = Field(min_length=1)
    open_end: str = Field(min_length=1)
    digits: int = Field(ge=1)

    def code(self, basis: str, kind: str, start: int, end: int | None) -> str:
        """Код факта графы: префикс основы и рода, границы в месяцах."""
        head = self.prefixes[basis][kind]
        tail = self.open_end if end is None else f"m{end:0{self.digits}d}"
        return f"{head}_m{start:0{self.digits}d}_{tail}"


class BalanceFallback(BaseModel):
    """Запасная опора сверки, когда баланс не прочитан (решение 30.09.2026).

    Итог примечания, расшифровывающего балансовую строку займов. Примечание
    опознаётся наименованием по указателю и оглавлению, итог — наименованием
    строки: ссылки из формы нет, потому что нет самой формы.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    note_titles: tuple[Alias, ...] = Field(min_length=1)
    total_names: tuple[Alias, ...] = Field(min_length=1)


class DebtMaturity(BaseModel):
    """Сроки погашения долга: основы, корзины печати, роды строк, сверка."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Строки формы, по ссылке которых ищется примечание о долге.
    found_in: tuple[str, ...] = Field(min_length=1)
    # Строки аренды: сверка «займы + аренда», решение владельца 29.09.2026.
    lease_lines: tuple[str, ...] = Field(min_length=1)
    buckets: tuple[MaturityBucket, ...] = Field(min_length=1)
    rows: MaturityRows
    storage: MaturityStorage
    balance_fallback: BalanceFallback | None = None
    origin: str = Field(min_length=1)

    @model_validator(mode="after")
    def _buckets_are_contiguous(self) -> Self:
        """Корзины идут подряд от нуля, последняя открыта сверху."""
        edge = 0
        for bucket in self.buckets:
            if bucket.from_months != edge:
                raise ValueError(f"корзина {bucket.code} начинается не с {edge} месяцев")
            if bucket.to_months is None:
                if bucket is not self.buckets[-1]:
                    raise ValueError("открытой сверху может быть только последняя корзина")
                break
            edge = bucket.to_months
        if self.buckets[-1].to_months is not None:
            raise ValueError("последняя корзина должна быть открыта сверху")
        return self


class NoteLineCatalog(BaseModel):
    """Справочник строк примечаний целиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    lines: tuple[NoteLine, ...] = Field(min_length=1)
    interest_cover: InterestCover
    # Сроки погашения долга (уровень 2). Пусто — состав не утверждён,
    # и разбор сроков отказывается, а не берёт умолчание.
    debt_maturity: DebtMaturity | None = None

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
