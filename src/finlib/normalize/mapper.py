"""Сопоставление кодов источника со строками справочника."""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.lines import LineDef, LinesCatalog, ReportingType
from finlib.utils import ValueStatus

logger = logging.getLogger(__name__)


class MappingOutcome(StrEnum):
    """Итог сопоставления кода отчётности со справочником."""

    MAPPED = "mapped"
    UNKNOWN = "unknown"  # кода нет ни в одном наборе — повод проверить справочник
    IGNORED = "ignored"  # код объявлен неиспользуемым осознанно
    NOT_APPLICABLE = "not_applicable"  # код есть в полном наборе, но не в этом
    AMBIGUOUS = "ambiguous"  # претендентов несколько, выбрать нельзя
    # Наименования нет в справочнике: строка упрощённой формы опознаётся
    # по нему, и без опознания она в fact_report не попадёт.
    NOT_RECOGNIZED = "not_recognized"


@dataclass(frozen=True, slots=True)
class MappedLine:
    """Результат сопоставления одного кода отчётности."""

    source_code: str
    outcome: MappingOutcome
    line: LineDef | None = None
    candidates: tuple[str, ...] = ()
    # Наименование строки, как оно напечатано в отчётности; есть только
    # у источников, которые наименования отдают.
    source_name: str | None = None

    @property
    def line_code(self) -> str:
        """Канонический код строки; обращаться можно только к сопоставленному коду."""
        if self.line is None:
            raise ValueError(f"код {self.source_code} не сопоставлен со справочником")
        return self.line.code


@dataclass(frozen=True, slots=True)
class Fact:
    """Одно значение, готовое к записи в fact_report."""

    form_code: str
    line_code: str
    source_line_code: str
    value: Decimal | None
    value_status: ValueStatus
    period_role: str


@dataclass(frozen=True, slots=True)
class AmbiguousCode:
    """Код, который не удалось привязать к единственной строке справочника."""

    form_code: str
    source_code: str
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnrecognizedLine:
    """Строка, которую не опознал справочник: в fact_report она не попадёт.

    Раскрытые значения хранятся вместе со строкой: нераскрытая строка — пробел
    справочника и повод его пополнить, а вот строка с ненулевым значением —
    тихая потеря данных, и уровень записи у неё другой.
    """

    form_code: str
    source_code: str
    name: str
    disclosed: tuple[tuple[date, Decimal], ...] = ()

    @property
    def lost(self) -> tuple[date, Decimal] | None:
        """Самый свежий период с ненулевым значением, если такой есть."""
        found = [item for item in self.disclosed if item[1] != 0]
        return max(found, key=lambda item: item[0]) if found else None


@dataclass(frozen=True, slots=True)
class LineConflict:
    """Несколько исходных кодов раскрыли значение для одной укрупнённой строки."""

    form_code: str
    line_code: str
    source_codes: tuple[str, ...]
    report_date: date


@dataclass
class MappingResult:
    """Сопоставление всех кодов одной формы."""

    mapped: dict[str, MappedLine] = field(default_factory=dict)
    unknown: list[MappedLine] = field(default_factory=list)
    ignored: list[MappedLine] = field(default_factory=list)
    not_applicable: list[MappedLine] = field(default_factory=list)
    ambiguous: list[MappedLine] = field(default_factory=list)
    not_recognized: list[MappedLine] = field(default_factory=list)


def map_codes(
    codes: set[str],
    filled: set[str],
    catalog: LinesCatalog,
    reporting_type: ReportingType,
    form: str,
) -> MappingResult:
    """Сопоставляет коды формы со справочником, разрешая неоднозначность по занятости.

    `filled` — коды, у которых в комплекте есть раскрытое значение. Если код
    допускают несколько укрупнённых строк, но канонические коды остальных
    претендентов в отчётности заполнены, место остаётся одно и выбор
    однозначен. Иначе строка не сопоставляется.
    """
    result = MappingResult()
    for code in sorted(codes):
        if catalog.is_ignored(code, form):
            result.ignored.append(MappedLine(code, MappingOutcome.IGNORED))
            continue
        candidates = catalog.candidates_for_code(code, reporting_type, form)
        if not candidates:
            # Упрощённая форма приходит в той же схеме, что полная, поэтому
            # в ответе есть коды, которых у упрощённого набора нет вовсе.
            # Это неприменимость, а не пробел в справочнике.
            if reporting_type is not ReportingType.FULL and catalog.candidates_for_code(
                code, ReportingType.FULL, form
            ):
                result.not_applicable.append(MappedLine(code, MappingOutcome.NOT_APPLICABLE))
            else:
                result.unknown.append(MappedLine(code, MappingOutcome.UNKNOWN))
            continue
        if len(candidates) == 1:
            result.mapped[code] = MappedLine(code, MappingOutcome.MAPPED, candidates[0])
            continue

        free = _resolve_by_occupancy(code, candidates, filled)
        if len(free) == 1:
            result.mapped[code] = MappedLine(code, MappingOutcome.MAPPED, free[0])
            continue
        result.ambiguous.append(
            MappedLine(
                code,
                MappingOutcome.AMBIGUOUS,
                candidates=tuple(line.code for line in candidates),
            )
        )
    return result


def map_by_name(
    names: dict[str, str],
    catalog: LinesCatalog,
    reporting_type: ReportingType,
    form: str,
) -> MappingResult:
    """Сопоставляет строки упрощённой формы со справочником по наименованию.

    Для упрощённых форм код укрупнённой строки — подсказка: он указывается
    по показателю с наибольшим удельным весом и между периодами меняется.
    Поэтому ключом служит наименование, а код передаётся только затем, чтобы
    развести строки-тёзки.

    Игнорируемые коды отсекаются и здесь: решение методики не использовать
    код не зависит от того, каким источником пришла отчётность.
    """
    if reporting_type is ReportingType.FULL:
        raise ValueError(
            "опознание по наименованию определено только для упрощённых форм: "
            "в полных формах наименования повторяются, ключом служит код строки"
        )
    result = MappingResult()
    for code in sorted(names):
        name = names[code]
        if catalog.is_ignored(code, form):
            result.ignored.append(MappedLine(code, MappingOutcome.IGNORED, source_name=name))
            continue
        line = catalog.match_by_name(name, reporting_type, form, source_code=code)
        if line is None:
            result.not_recognized.append(
                MappedLine(code, MappingOutcome.NOT_RECOGNIZED, source_name=name)
            )
            continue
        result.mapped[code] = MappedLine(
            code, MappingOutcome.MAPPED, line, source_name=name
        )
    return result


def _resolve_by_occupancy(
    code: str, candidates: tuple[LineDef, ...], filled: set[str]
) -> tuple[LineDef, ...]:
    """Отсекает претендентов, чьё место уже занято собственным каноническим кодом.

    Если пришли и 1150, и 1190, то 1150 забирает «Материальные внеоборотные
    активы», и для 1190 остаётся ровно одно место. Это исключение занятых
    вариантов, а не угадывание приоритета.
    """
    return tuple(
        line for line in candidates if line.code == code or line.code not in filled
    )
