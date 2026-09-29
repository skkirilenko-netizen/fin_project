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


# **Строка на величину, и отсутствие стороны — тоже строка.** «Сверять было
# нечем» и «не сверяли» — разные сведения, и пропуск строки их слил бы.
# Повторная сверка того же документа переписывает свою строку: сверка —
# состояние на нынешний разбор, а версия разбора стоит в `code_version`.
_RECORD = """
INSERT INTO source_reconciliation (
    inn, document_file_id, document_path, report_date, period_role, line_code,
    aggregator_kind, document_value, document_unit, aggregator_value,
    aggregator_unit, outcome, difference_rub, tolerance_rub, code_version
) VALUES (
    %(inn)s, %(file_id)s, %(path)s, %(date)s, %(role)s, %(code)s, %(kind)s,
    %(doc_value)s, %(doc_unit)s, %(agg_value)s, %(agg_unit)s, %(outcome)s,
    %(difference)s, %(tolerance)s, %(version)s
)
ON CONFLICT (document_path, report_date, line_code) DO UPDATE SET
    inn = EXCLUDED.inn,
    document_file_id = EXCLUDED.document_file_id,
    period_role = EXCLUDED.period_role,
    aggregator_kind = EXCLUDED.aggregator_kind,
    document_value = EXCLUDED.document_value,
    document_unit = EXCLUDED.document_unit,
    aggregator_value = EXCLUDED.aggregator_value,
    aggregator_unit = EXCLUDED.aggregator_unit,
    outcome = EXCLUDED.outcome,
    difference_rub = EXCLUDED.difference_rub,
    tolerance_rub = EXCLUDED.tolerance_rub,
    code_version = EXCLUDED.code_version,
    checked_at = now()
"""

# Комплект документа, если он загружен: путь — ключ сверки, а ссылка
# на комплект позволяет спросить, в каком он статусе.
_FILE_OF = """
SELECT id FROM src_file WHERE raw_path = %(path)s AND source = 'file'
ORDER BY is_actual DESC, id DESC LIMIT 1
"""

# **Мера надёжности — отчётная колонка.** Сравнительную эмитент бывает
# пересчитал, и расхождение там — пересмотр, а не ошибка источника.
_REPORTING_SHARE = """
SELECT count(*) FILTER (WHERE outcome = 'совпало') AS matched,
       count(*) FILTER (WHERE outcome IN ('совпало', 'расходится',
                                          'расходится знаком')) AS compared,
       count(DISTINCT inn) AS issuers
FROM source_reconciliation WHERE period_role = 'reporting'
"""


@dataclass(frozen=True, slots=True)
class Share:
    """Совпавшие в отчётной колонке по записанной сверке и её знаменатели."""

    matched: int
    compared: int
    issuers: int


def record(
    inn: str,
    path: str,
    role: str,
    found: list[Comparison],
    kinds: dict[str, str],
    conn,  # noqa: ANN001 — соединение psycopg2
) -> int:
    """Записывает сверку одной даты одного документа; возвращает число строк."""
    from finlib.db import execute_many, fetch_one
    from finlib.version import code_version

    row = fetch_one(_FILE_OF, {"path": path}, conn=conn)
    version = code_version()
    return execute_many(
        _RECORD,
        [
            {
                "inn": inn,
                "file_id": row["id"] if row else None,
                "path": path,
                "date": item.report_date,
                "role": role,
                "code": item.code,
                "kind": kinds[item.code],
                "doc_value": item.document.value,
                "doc_unit": item.document.unit_code,
                "agg_value": item.aggregator.value,
                "agg_unit": item.aggregator.unit_code,
                "outcome": item.outcome.value,
                "difference": item.difference,
                "tolerance": item.tolerance,
                "version": version,
            }
            for item in found
        ],
        conn=conn,
    )


def reporting_share(conn=None) -> Share:  # noqa: ANN001
    """Доля совпавших в отчётной колонке по записанной сверке.

    **Ноль сверенных — не «ничего не совпало»**, а отсутствие сверки: читающий
    обязан отличить одно от другого по `compared`, а не по доле.
    """
    from finlib.db import fetch_one

    row = fetch_one(_REPORTING_SHARE, {}, conn=conn) or {}
    return Share(
        int(row.get("matched") or 0),
        int(row.get("compared") or 0),
        int(row.get("issuers") or 0),
    )
