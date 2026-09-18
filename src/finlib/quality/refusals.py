"""Отказ как объект: причина, место, семейство и что с ним делать.

**Раздел «Ограничения анализа» — перечень того, что нужно запросить
у организации**, а не место для оговорок о методике. До 18.09.2026 он делал
второе и не делал первого: причины, по которым показатель не рассчитан,
туда не попадали вовсе, а в сборке стоял цикл, тело которого состояло
из одного `continue` — написано так, будто решение исполнено, а исполнять
нечего.

**Отказ устроен одинаково в обоих контурах**, поэтому механизм один.
Разводить РСБУ и МСФО здесь нечем: не раскрытая строка баланса и не
извлечённая величина примечания различаются местом, а не природой.

**Три семейства, и третьего не бывает.** Из отказа либо следует запрос
к организации, либо не следует — и тогда это неприменимость показателя
к этим данным или наш собственный пробел. Поле `request` обязательно
у каждого: пустое скрыло бы, к какому семейству отказ относится, а
семейство и есть решение о том, что делать дальше.

**Потеря отказа по дороге тише всего остального**: документ выглядит
полным. Поэтому счёт произведённых отказов сверяется с числом названных
в разделе, и расхождение блокирует документ.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings

logger = logging.getLogger(__name__)


class Kind(StrEnum):
    """Семейство отказа: что с ним делать."""

    DATA_MISSING = "data_missing"
    NOT_APPLICABLE = "not_applicable"
    OUR_GAP = "our_gap"


class ReasonDef(BaseModel):
    """Формулировка отказа: текст и вытекающее из него действие."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    kind: Kind
    template: str = Field(min_length=1)
    # Подлежащее свёрнутой строки: перечень показателей, а не первый из них.
    # Без него фраза говорит об одном, а перечисляет восемь.
    group_text: str | None = None
    request: str = Field(min_length=1)

    @model_validator(mode="after")
    def _names_the_place(self) -> Self:
        """Формулировка обязана называть место либо предмет."""
        if "{where}" not in self.template and "{subject}" not in self.template:
            raise ValueError(
                f"{self.code}: формулировка не называет ни предмета, ни места — "
                "такой отказ не превращается в запрос"
            )
        return self


class Grouping(BaseModel):
    """Как одинаковые отказы сводятся в одну строку."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_to_group: int = Field(ge=2)
    template: str = Field(min_length=1)


class RefusalCatalog(BaseModel):
    """Справочник формулировок отказов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    kinds: dict[str, str]
    reasons: tuple[ReasonDef, ...] = Field(min_length=1)
    grouping: Grouping

    @model_validator(mode="after")
    def _codes_are_unique(self) -> Self:
        """Код причины не повторяется: иначе формулировка берётся произвольно."""
        codes = [item.code for item in self.reasons]
        doubled = {code for code in codes if codes.count(code) > 1}
        if doubled:
            raise ValueError(f"код причины повторяется: {sorted(doubled)}")
        return self

    def reason(self, code: str) -> ReasonDef | None:
        """Формулировка по коду причины."""
        return next((item for item in self.reasons if item.code == code), None)


@dataclass(frozen=True, slots=True)
class Refusal:
    """Один отказ: что не сделано, где это лежит и что с этим делать."""

    code: str
    subject: str
    where: str
    text: str
    request: str
    kind: Kind

    def __post_init__(self) -> None:
        """Отказ без действия не бывает: пустое поле скрыло бы семейство."""
        if not self.request.strip():
            raise ValueError(
                f"{self.code}: отказ без указания, что делать. Из отказа следует "
                "либо запрос к организации, либо прямое «запрашивать нечего» "
                "с причиной — третьего не бывает"
            )

    def describe(self) -> str:
        """Строка для раздела: обстоятельство и вытекающее действие."""
        return f"{self.text}. {self.request}"


class RefusalsLostError(Exception):
    """Отказы, произведённые расчётом, не попали в раздел."""


def load_refusals(path: Path | None = None) -> RefusalCatalog:
    """Читает справочник формулировок отказов."""
    source = path or settings.methodology_dir / "refusals.yaml"
    catalog = RefusalCatalog.model_validate(
        yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    )
    logger.info(
        "справочник отказов %s: формулировок %d", catalog.version, len(catalog.reasons)
    )
    return catalog


def refusal(
    code: str,
    subject: str,
    where: str,
    catalog: RefusalCatalog | None = None,
) -> Refusal:
    """Собирает отказ по коду причины, подставляя предмет и место.

    Текст берётся из методики дословно и набирается один раз: набранный
    заново в документе, он разойдётся с журнальным — то же правило, что
    у величины надзорного сигнала.
    """
    catalog = catalog or load_refusals()
    found = catalog.reason(code)
    if found is None:
        raise KeyError(
            f"формулировки отказа {code} нет в методике: свободных строк "
            "с причинами в коде быть не должно"
        )
    return Refusal(
        code,
        subject,
        where,
        found.template.format(subject=subject, where=where).strip(),
        found.request.format(subject=subject, where=where).strip(),
        found.kind,
    )


def section(
    refusals: tuple[Refusal, ...], catalog: RefusalCatalog | None = None
) -> tuple[str, ...]:
    """Строки раздела: одинаковые отказы сводятся, ни один не теряется.

    Двенадцать строк «процентное изменение не определено» подряд читателя
    не осведомляют, а перечень показателей в одной строке — да. Сведение
    не сокращение: все предметы названы поимённо.
    """
    catalog = catalog or load_refusals()
    by_code: dict[str, list[Refusal]] = {}
    for item in refusals:
        by_code.setdefault(item.code, []).append(item)

    lines: list[str] = []
    for code, items in by_code.items():
        if len(items) < catalog.grouping.min_to_group:
            lines.extend(item.describe() for item in items)
            continue
        first = items[0]
        subjects = ", ".join(sorted({item.subject for item in items}))
        reason = catalog.reason(code)
        group_text = reason.group_text or reason.template.format(
            subject="Показатели", where=first.where
        )
        lines.append(
            catalog.grouping.template.format(
                group_text=" ".join(group_text.split()),
                count=len(items),
                subjects=subjects,
            )
            + f". {first.request}"
        )
    return tuple(lines)


def check_complete(produced: tuple[Refusal, ...], lines: tuple[str, ...]) -> None:
    """Сверяет число произведённых отказов с числом названных в разделе.

    Контроль блокирующий: потеря отказа по дороге тише всего остального —
    документ выглядит полным, и обнаружить пропажу нечем.
    """
    named = 0
    for item in produced:
        if any(item.subject in line or item.text.split(".")[0] in line for line in lines):
            named += 1
    if named != len(produced):
        raise RefusalsLostError(
            f"в раздел «Ограничения анализа» попало {named} отказов "
            f"из {len(produced)} произведённых. Документ не формируется: "
            "потерянный отказ — это несделанный запрос к организации"
        )
    logger.info("отказов произведено %d, все названы в разделе", len(produced))
