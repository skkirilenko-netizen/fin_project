"""Сверка агрегатора с документом: мера надёжности данных Cbonds.

**Разобранный документ — эталон, агрегатор — проверяемый** (уровень 3
заключений по МСФО, решение владельца 28.09.2026). Там, где у эмитента есть
и PDF, и комплект агрегатора на ту же отчётную дату, ключевые величины
сравниваются между собой; доля совпавших и есть мера того, насколько
заключению уровня 1 можно верить без документа.

**Сравниваются коды, которые агрегатор отдаёт фактами.** Перечня здесь нет:
агрегатор объявляет свои коды справочником (`cbonds_mapping.yaml`), и второй
перечень разошёлся бы с ним. Код, не извлечённый из документа, — исход
«нет в документе», а не пропуск.

**Допуск — одна единица более грубой стороны, и это не порог, а строение
данных.** Документ в миллионах и агрегатор в тысячах совпадают с точностью
до округления составителя: «663 888 млн» против «663 887 912 тыс.» — одно
и то же число. Процентного допуска нет: доля от величины назначалась бы
числом, а расхождение в одну позицию бывает и в первой значащей цифре.

**Исходов пять, и слитые они лгали бы:** совпало, расходится, расходится
только знаком (соглашение о знаке расхода, а не другая величина), нет
у агрегатора, нет в документе. Отсутствие с одной стороны — не расхождение
и не совпадение.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.cbonds_loader import RowReading, read_row
from finlib.sources.cbonds_events import OKEI_MULTIPLIER

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
    """Исход сверки одной величины."""

    MATCH = "совпало"
    DIFFER = "расходится"
    SIGN = "расходится знаком"
    NO_AGGREGATOR = "нет у агрегатора"
    NO_DOCUMENT = "нет в документе"


@dataclass(frozen=True, slots=True)
class Side:
    """Величина одной стороны в своей единице."""

    value: Decimal | None
    unit_code: str | None

    @property
    def roubles(self) -> Decimal | None:
        """Величина в рублях; None — величины либо единицы нет."""
        if self.value is None or self.unit_code is None:
            return None
        multiplier = OKEI_MULTIPLIER.get(str(self.unit_code))
        return None if multiplier is None else self.value * multiplier


@dataclass(frozen=True, slots=True)
class Comparison:
    """Сверка одной величины на одну отчётную дату."""

    code: str
    report_date: date
    document: Side
    aggregator: Side
    outcome: Outcome
    # Расхождение в рублях и допуск — одна единица более грубой стороны.
    difference: Decimal | None
    tolerance: Decimal | None


def _coarser(first: Side, second: Side) -> Decimal | None:
    """Одна единица более грубой стороны в рублях; None — единица неизвестна."""
    found = [
        OKEI_MULTIPLIER.get(str(side.unit_code))
        for side in (first, second)
        if side.unit_code is not None
    ]
    if len(found) < 2 or None in found:
        return None
    return max(found)  # type: ignore[type-var]


def compare(
    code: str, report_date: date, document: Side, aggregator: Side
) -> Comparison:
    """Сверяет одну величину двух сторон."""
    if document.value is None:
        return Comparison(code, report_date, document, aggregator, Outcome.NO_DOCUMENT, None, None)
    if aggregator.value is None:
        return Comparison(
            code, report_date, document, aggregator, Outcome.NO_AGGREGATOR, None, None
        )
    ours, theirs, edge = document.roubles, aggregator.roubles, _coarser(document, aggregator)
    if ours is None or theirs is None or edge is None:
        # Единица неизвестна хотя бы у одной стороны: сверять нечем, и сказать
        # «совпало» по сырым числам значило бы сравнить тысячи с миллионами.
        raise ValueError(
            f"{code} на {report_date}: единица не известна "
            f"(документ {document.unit_code}, агрегатор {aggregator.unit_code})"
        )
    gap = abs(ours - theirs)
    if gap <= edge:
        outcome = Outcome.MATCH
    elif abs(ours + theirs) <= edge and ours != 0:
        outcome = Outcome.SIGN
    else:
        outcome = Outcome.DIFFER
    return Comparison(code, report_date, document, aggregator, outcome, gap, edge)


def aggregator_reading(rows: list[dict], inn: str, report_date: date) -> RowReading | None:
    """Строка агрегатора на дату, прочитанная путём загрузки; None — строки нет.

    **Хранимые факты для сверки не годятся**: ключ факта источника не знает,
    и на дату, где лежит комплект документа, величины агрегатора в базу
    не попадают — у ЛСР за 2025 год записан один факт агрегатора из тридцати.
    Строк на дату бывает несколько (консолидированная и нет); берутся
    принятые загрузкой, и две принятые — ошибка, а не выбор.
    """
    found = [
        read_row(row)
        for row in rows
        if str(row.get("emitent_inn") or "").strip() == inn
        and str(row.get("date")) == report_date.isoformat()
    ]
    if not found:
        return None
    accepted = [item for item in found if item.rejection is None]
    if len(accepted) > 1:
        raise ValueError(f"{inn} на {report_date}: у агрегатора принятых строк {len(accepted)}")
    return accepted[0] if accepted else found[0]


def aggregator_codes() -> dict[str, str]:
    """Коды, которые агрегатор отдаёт фактами, и род поля: `exact` либо `aggregate`.

    Род печатается рядом с исходом: поле-агрегат у источника собрано из
    нескольких статей, и расхождение с одной статьёй документа у него
    бывает по устройству, а не по ошибке.
    """
    from finlib.normalize.cbonds_mapping import load_cbonds_mapping

    fields = load_cbonds_mapping().report("report_msfo_real").loaded_fields()
    return {item.code: item.kind for item in fields.values()}


def reconcile(
    document: dict[str, Decimal],
    document_unit: str,
    aggregator: dict[str, Decimal | None],
    aggregator_unit: str | None,
    report_date: date,
    codes: tuple[str, ...],
) -> list[Comparison]:
    """Сверка названных кодов по одной отчётной дате."""
    return [
        compare(
            code,
            report_date,
            Side(document.get(code), document_unit),
            Side(aggregator.get(code), aggregator_unit),
        )
        for code in codes
    ]
