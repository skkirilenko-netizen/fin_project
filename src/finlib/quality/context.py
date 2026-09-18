"""Сбор данных комплекта из БД для контролей качества."""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.normalize.lines import LinesCatalog, ReportingType, load_lines
from finlib.quality.codes import CheckCode
from finlib.quality.thresholds import Thresholds, load_thresholds
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_SELECT_SRC_FILE = """
SELECT id, inn, report_year, reporting_type, standard, unit_code, unit_source, status,
       correction_version
FROM src_file WHERE id = %(id)s
"""

_SELECT_FACTS = """
SELECT report_date, form_code, line_code, source_line_code, value, value_status, period_role
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
ORDER BY report_date DESC, form_code, line_code
"""

_SELECT_PERIODS = """
SELECT DISTINCT report_date FROM fact_report
WHERE src_file_id = %(id)s ORDER BY report_date DESC
"""

_SELECT_ALL_PERIODS = """
SELECT DISTINCT report_date FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s ORDER BY report_date DESC
"""

# Записи загрузчика, из-за которых строка не попала в fact_report.
_SELECT_UNLOADED = """
SELECT report_date, form_code, line_code, check_code, details
FROM dq_log
WHERE src_file_id = %(id)s AND check_code = ANY(%(codes)s)
"""

# Стандарт входит в отбор: расхождение периодов по МСФО к комплекту РСБУ
# отношения не имеет. Берётся он у комплекта, оставившего запись, — сама
# запись журнала стандарта не хранит.
_SELECT_MISMATCHES = """
SELECT d.report_date, d.form_code, d.line_code, d.previous_value, d.new_value
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.inn = %(inn)s AND s.standard = %(standard)s AND d.check_code = %(code)s
  AND d.report_date = ANY(%(dates)s)
"""


@dataclass(frozen=True, slots=True)
class LineValue:
    """Значение одной строки отчётности за один период."""

    value: Decimal | None
    value_status: str
    source_line_code: str
    period_role: str


@dataclass
class PeriodFacts:
    """Факты одного периода и строки, которые проверить нельзя."""

    report_date: date
    values: dict[tuple[str, str], LineValue] = field(default_factory=dict)
    # (форма, код строки) -> причина, по которой строка не загружена.
    unverifiable: dict[tuple[str, str], str] = field(default_factory=dict)

    def get(self, form_code: str, line_code: str) -> Decimal | None:
        """Значение строки; None и когда не раскрыто, и когда строки нет."""
        item = self.values.get((form_code, line_code))
        return item.value if item is not None else None

    def is_present(self, form_code: str, line_code: str) -> bool:
        """Есть ли строка в загруженных фактах."""
        return (form_code, line_code) in self.values

    def is_disclosed(self, form_code: str, line_code: str) -> bool:
        """Раскрыто ли значение строки."""
        return self.get(form_code, line_code) is not None

    def has_form(self, form_code: str) -> bool:
        """Загружена ли форма за этот период."""
        return any(form == form_code for form, _ in self.values)

    def blocked_reason(self, form_code: str, line_code: str) -> str | None:
        """Причина, по которой строка не загружена, если такая есть."""
        return self.unverifiable.get((form_code, line_code))


@dataclass
class ReportContext:
    """Всё, что нужно контролям по одному комплекту отчётности."""

    src_file_id: int
    inn: str
    report_year: int
    reporting_type: ReportingType
    standard: Standard
    unit_code: str
    unit_source: str
    status: str
    correction_version: int
    periods: dict[date, PeriodFacts]
    revisions: dict[tuple[date, str, str], tuple[Decimal | None, Decimal | None]]
    known_periods: tuple[date, ...]
    catalog: LinesCatalog
    thresholds: Thresholds

    @property
    def ordered_periods(self) -> tuple[date, ...]:
        """Периоды комплекта от свежего к старому."""
        return tuple(sorted(self.periods, reverse=True))

    @property
    def reporting_date(self) -> date:
        """Отчётная дата комплекта: за неё комплект отвечает полностью."""
        return date(self.report_year, 12, 31)

    def is_reporting_period(self, report_date: date | None) -> bool:
        """Отчётный ли это период комплекта или сравнительный.

        За сравнительный период отвечает его собственный комплект. Ошибка
        в сравнительной колонке не должна отправлять в карантин комплект,
        чья собственная отчётность сходится.
        """
        return report_date == self.reporting_date

    def previous_period(self, report_date: date) -> date | None:
        """Предыдущий период по данным БД, даже если он из другого комплекта."""
        earlier = [item for item in self.known_periods if item < report_date]
        return max(earlier) if earlier else None

    def facts_of(self, report_date: date) -> PeriodFacts | None:
        """Факты периода, если он относится к этому комплекту."""
        return self.periods.get(report_date)


def build_context(
    src_file_id: int,
    conn: PgConnection,
    *,
    catalog: LinesCatalog | None = None,
    thresholds: Thresholds | None = None,
) -> ReportContext:
    """Собирает контекст комплекта: факты, непроверяемые строки, пересмотры."""
    src = fetch_one(_SELECT_SRC_FILE, {"id": src_file_id}, conn=conn)
    if src is None:
        raise ValueError(f"комплект {src_file_id} не найден")

    period_rows = fetch_all(_SELECT_PERIODS, {"id": src_file_id}, conn=conn)
    dates = [row["report_date"] for row in period_rows]
    periods = {report_date: PeriodFacts(report_date) for report_date in dates}

    standard = Standard(src["standard"])
    facts_params = {"inn": src["inn"], "standard": standard.value, "dates": dates}
    for row in fetch_all(_SELECT_FACTS, facts_params, conn=conn):
        facts = periods.get(row["report_date"])
        if facts is None:
            continue
        facts.values[(row["form_code"], row["line_code"])] = LineValue(
            value=row["value"],
            value_status=row["value_status"],
            source_line_code=row["source_line_code"],
            period_role=row["period_role"],
        )

    _apply_unloaded(src_file_id, periods, conn)

    revisions = {
        (row["report_date"], row["form_code"], row["line_code"]): (
            row["previous_value"],
            row["new_value"],
        )
        for row in fetch_all(
            _SELECT_MISMATCHES,
            {
                "inn": src["inn"],
                "standard": standard.value,
                "code": CheckCode.PERIOD_VALUE_MISMATCH.value,
                "dates": dates,
            },
            conn=conn,
        )
    }

    known = tuple(
        row["report_date"]
        for row in fetch_all(
            _SELECT_ALL_PERIODS, {"inn": src["inn"], "standard": standard.value}, conn=conn
        )
    )

    return ReportContext(
        src_file_id=src_file_id,
        inn=src["inn"],
        report_year=src["report_year"],
        reporting_type=ReportingType(src["reporting_type"]),
        standard=standard,
        unit_code=src["unit_code"],
        unit_source=src["unit_source"],
        status=src["status"],
        correction_version=src["correction_version"],
        periods=periods,
        revisions=revisions,
        known_periods=known,
        catalog=catalog if catalog is not None else load_lines(),
        thresholds=thresholds if thresholds is not None else load_thresholds(),
    )


def _apply_unloaded(
    src_file_id: int, periods: dict[date, PeriodFacts], conn: PgConnection
) -> None:
    """Отмечает строки, не попавшие в fact_report по решению загрузчика.

    Такая строка отсутствует не потому, что организация её не раскрыла, а
    потому, что мы отказались угадывать. Контроль, в состав которого она
    входит, обязан дать «не проверяемо», а не провал с карантином.
    """
    codes = [CheckCode.AMBIGUOUS_LINE_CODE.value, CheckCode.MULTIPLE_SOURCE_CODES.value]
    for row in fetch_all(_SELECT_UNLOADED, {"id": src_file_id, "codes": codes}, conn=conn):
        details: dict[str, Any] = row["details"] or {}
        form_code = row["form_code"]
        if row["check_code"] == CheckCode.AMBIGUOUS_LINE_CODE.value:
            # Неоднозначный код мог принадлежать любому из претендентов,
            # поэтому непроверяемыми становятся все они и во всех периодах.
            blocked = [str(code) for code in details.get("candidates", [])]
            reason = (
                f"код {row['line_code']} допускают строки "
                f"{', '.join(blocked)}; выбор не сделан, строка не загружена"
            )
            targets = list(periods.values())
        else:
            blocked = [row["line_code"]]
            reason = (
                f"значение строки раскрыто несколькими кодами "
                f"({', '.join(str(c) for c in details.get('source_codes', []))}); "
                "строка не загружена"
            )
            facts = periods.get(row["report_date"])
            targets = [facts] if facts is not None else []
        for facts in targets:
            for line_code in blocked:
                facts.unverifiable[(form_code, line_code)] = reason
