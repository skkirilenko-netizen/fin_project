"""Загрузка комплекта отчётности в fact_report одной транзакцией."""

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.db import PgConnection, cursor, execute, execute_many, fetch_all
from finlib.normalize.lines import LinesCatalog, load_lines
from finlib.normalize.mapper import Fact, LineConflict, MappingResult, map_codes
from finlib.normalize.report import LoadReport
from finlib.quality.codes import CheckCode, CheckStatus
from finlib.quality.journal import CheckRecord, log_records
from finlib.sources.girbo import (
    ASSUMED_UNIT_CODE,
    ASSUMED_UNIT_MULTIPLIER,
    PERIOD_OFFSETS,
    Organization,
    ReportSet,
)
from finlib.utils import ValueStatus

logger = logging.getLogger(__name__)

SOURCE = "gir_bo"

# Приоритет периода: отчётный старше сравнительных. Дублирует period_rank в схеме;
# согласованность проверяется тестом.
PERIOD_RANK: dict[str, int] = {"current": 0, "previous": 1, "before_previous": 2}

# Роль периода в fact_report по названию префикса в ответе источника.
PERIOD_ROLE_BY_PREFIX: dict[str, str] = {
    "current": "current",
    "previous": "previous",
    "beforePrevious": "before_previous",
}

# Сдвиг в годах назад от отчётного года комплекта.
PERIOD_OFFSET_BY_ROLE: dict[str, int] = {
    PERIOD_ROLE_BY_PREFIX[prefix]: offset for prefix, offset in PERIOD_OFFSETS.items()
}
_ROLE_BY_OFFSET: dict[int, str] = {
    offset: role for role, offset in PERIOD_OFFSET_BY_ROLE.items()
}


_UPSERT_ORGANIZATION = """
INSERT INTO organization (inn, girbo_id, name, short_name, ogrn, okpo, okved, region, meta)
VALUES (%(inn)s, %(girbo_id)s, %(name)s, %(short_name)s, %(ogrn)s, %(okpo)s, %(okved)s,
        %(region)s, %(meta)s)
ON CONFLICT (inn) DO UPDATE SET
    girbo_id = EXCLUDED.girbo_id,
    name = EXCLUDED.name,
    short_name = EXCLUDED.short_name,
    ogrn = EXCLUDED.ogrn,
    okpo = EXCLUDED.okpo,
    okved = EXCLUDED.okved,
    region = EXCLUDED.region,
    meta = EXCLUDED.meta,
    updated_at = now()
"""

_SUPERSEDE_OTHER_VERSIONS = """
UPDATE src_file SET is_actual = false
WHERE inn = %(inn)s AND report_year = %(report_year)s AND source = %(source)s
  AND correction_version <> %(correction_version)s AND is_actual
"""

_UPSERT_SRC_FILE = """
INSERT INTO src_file (
    inn, report_year, source, source_url, raw_path, checksum, form_codes, knd, girbo_bfo_id,
    correction_version, is_actual, reporting_type, unit_code, unit_multiplier, unit_source,
    status, meta
) VALUES (
    %(inn)s, %(report_year)s, %(source)s, %(source_url)s, %(raw_path)s, %(checksum)s,
    %(form_codes)s, %(knd)s, %(girbo_bfo_id)s, %(correction_version)s, %(is_actual)s,
    %(reporting_type)s, %(unit_code)s, %(unit_multiplier)s, %(unit_source)s, 'loaded', %(meta)s
)
ON CONFLICT (inn, report_year, source, correction_version) DO UPDATE SET
    source_url = EXCLUDED.source_url,
    raw_path = EXCLUDED.raw_path,
    checksum = EXCLUDED.checksum,
    form_codes = EXCLUDED.form_codes,
    knd = EXCLUDED.knd,
    girbo_bfo_id = EXCLUDED.girbo_bfo_id,
    is_actual = EXCLUDED.is_actual,
    reporting_type = EXCLUDED.reporting_type,
    unit_code = EXCLUDED.unit_code,
    unit_multiplier = EXCLUDED.unit_multiplier,
    unit_source = EXCLUDED.unit_source,
    meta = EXCLUDED.meta,
    loaded_at = now()
RETURNING id
"""

_SELECT_EXISTING = """
SELECT report_date, form_code, line_code, value, value_status, period_role, src_file_id
FROM fact_report
WHERE inn = %(inn)s AND report_date = ANY(%(dates)s) AND form_code = ANY(%(forms)s)
"""

# Сравнительный период не затирает уже загруженное отчётное значение, а
# совпадающее значение не трогает updated_at и не порождает записей журнала.
_UPSERT_FACT = """
INSERT INTO fact_report (
    src_file_id, inn, report_date, form_code, line_code, source_line_code,
    value, value_status, period_role
) VALUES (
    %(src_file_id)s, %(inn)s, %(report_date)s, %(form_code)s, %(line_code)s,
    %(source_line_code)s, %(value)s, %(value_status)s, %(period_role)s
)
ON CONFLICT (inn, report_date, form_code, line_code) DO UPDATE SET
    src_file_id = EXCLUDED.src_file_id,
    source_line_code = EXCLUDED.source_line_code,
    value = EXCLUDED.value,
    value_status = EXCLUDED.value_status,
    period_role = EXCLUDED.period_role,
    updated_at = now()
WHERE period_rank(EXCLUDED.period_role) <= period_rank(fact_report.period_role)
  AND (fact_report.value IS DISTINCT FROM EXCLUDED.value
       OR fact_report.value_status IS DISTINCT FROM EXCLUDED.value_status
       OR fact_report.source_line_code IS DISTINCT FROM EXCLUDED.source_line_code
       OR fact_report.src_file_id IS DISTINCT FROM EXCLUDED.src_file_id
       OR fact_report.period_role IS DISTINCT FROM EXCLUDED.period_role)
"""


@dataclass(frozen=True, slots=True)
class _Existing:
    """Уже загруженное значение строки."""

    value: Decimal | None
    value_status: str
    period_role: str
    src_file_id: int


def period_role(report_year: int, report_date: date) -> str:
    """Каким периодом значение пришло в комплекте отчётного года."""
    offset = report_year - report_date.year
    role = _ROLE_BY_OFFSET.get(offset)
    if role is None:
        raise ValueError(f"период {report_date} не относится к комплекту {report_year} года")
    return role


@dataclass
class BuiltFacts:
    """Факты комплекта и всё, что в них не попало."""

    facts: list[Fact] = dc_field(default_factory=list)
    unknown: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    ambiguous: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    conflicts: list[LineConflict] = dc_field(default_factory=list)


def build_facts(report: ReportSet, catalog: LinesCatalog) -> BuiltFacts:
    """Собирает факты комплекта и перечни кодов, которые в расчёт не пойдут.

    На одну укрупнённую строку упрощённой формы претендует несколько кодов
    (1220, 1230, 1240 и 1260 — это все «Финансовые и другие оборотные активы»).
    Значение берётся у того кода, который его раскрыл; если раскрыли несколько,
    выбор произволен, поэтому строка не грузится и уходит в журнал.
    """
    built = BuiltFacts()

    for form_code, form in report.forms.items():
        codes = {code for values in form.values.values() for code in values}
        filled = {
            code
            for values in form.values.values()
            for code, value in values.items()
            if value is not None
        }
        mapping = map_codes(codes, filled, catalog, report.reporting_type, form_code)
        if mapping.unknown:
            built.unknown[form_code] = tuple(item.source_code for item in mapping.unknown)
        if mapping.ambiguous:
            built.ambiguous[form_code] = tuple(item.source_code for item in mapping.ambiguous)

        for report_date, values in form.values.items():
            role = period_role(report.report_year, report_date)
            for line_code, claims in _group_by_line(values, mapping).items():
                disclosed = [(code, value) for code, value in claims if value is not None]
                if len(disclosed) > 1:
                    built.conflicts.append(
                        LineConflict(
                            form_code=form_code,
                            line_code=line_code,
                            source_codes=tuple(code for code, _ in disclosed),
                            report_date=report_date,
                        )
                    )
                    continue
                if disclosed:
                    source_code, value = disclosed[0]
                else:
                    source_code, value = _undisclosed_source(line_code, claims)
                built.facts.append(
                    Fact(
                        form_code=form_code,
                        line_code=line_code,
                        source_line_code=source_code,
                        value=value,
                        value_status=ValueStatus.OK
                        if value is not None
                        else ValueStatus.NOT_DISCLOSED,
                        period_role=role,
                    )
                )
    return built


def _group_by_line(
    values: dict[str, Decimal | None], mapping: MappingResult
) -> dict[str, list[tuple[str, Decimal | None]]]:
    """Собирает исходные коды по канонической строке справочника."""
    grouped: dict[str, list[tuple[str, Decimal | None]]] = {}
    for source_code in sorted(values):
        mapped = mapping.mapped.get(source_code)
        if mapped is None:
            continue  # неизвестные и неоднозначные коды в fact_report не идут
        grouped.setdefault(mapped.line_code, []).append((source_code, values[source_code]))
    return grouped


def _undisclosed_source(
    line_code: str, claims: list[tuple[str, Decimal | None]]
) -> tuple[str, None]:
    """Какой исходный код записать, когда значение не раскрыл никто."""
    codes = [code for code, _ in claims]
    return (line_code if line_code in codes else codes[0], None)


def load_report_set(
    report: ReportSet,
    organization: Organization,
    conn: PgConnection,
    *,
    catalog: LinesCatalog | None = None,
    raw_path: str | None = None,
    checksum: str | None = None,
    source_url: str | None = None,
) -> LoadReport:
    """Загружает один комплект отчётности целиком в переданной транзакции.

    Снятие признака актуальности с прежних версий, запись src_file и фактов
    происходят вместе: при падении посередине в базе не останется ни двух
    актуальных версий, ни комплекта без фактов.
    """
    catalog = catalog if catalog is not None else load_lines()
    result = LoadReport(
        inn=report.inn,
        report_year=report.report_year,
        correction_version=report.correction_version,
    )

    _upsert_organization(organization, conn)
    if report.is_actual:
        result.superseded_versions = execute(
            _SUPERSEDE_OTHER_VERSIONS,
            {
                "inn": report.inn,
                "report_year": report.report_year,
                "source": SOURCE,
                "correction_version": report.correction_version,
            },
            conn=conn,
        )

    src_file_id = _upsert_src_file(report, conn, raw_path, checksum, source_url)
    result.src_file_id = src_file_id

    built = build_facts(report, catalog)
    result.facts_total = len(built.facts)
    result.unknown_codes = built.unknown
    result.ambiguous_codes = built.ambiguous
    result.line_conflicts = len(built.conflicts)
    result.periods = tuple(
        sorted({d for form in report.forms.values() for d in form.values}, reverse=True)
    )

    existing = _read_existing(report, conn)
    to_write, records = _decide(report, built.facts, existing, src_file_id, result)

    if to_write:
        execute_many(
            _UPSERT_FACT,
            [
                {
                    "src_file_id": src_file_id,
                    "inn": report.inn,
                    "report_date": _fact_date(report, fact),
                    "form_code": fact.form_code,
                    "line_code": fact.line_code,
                    "source_line_code": fact.source_line_code,
                    "value": fact.value,
                    "value_status": fact.value_status.value,
                    "period_role": fact.period_role,
                }
                for fact in to_write
            ],
            conn=conn,
        )
    records.extend(_code_records(report, src_file_id, built.unknown, built.ambiguous))
    records.extend(_conflict_records(report, src_file_id, built.conflicts))
    log_records(records, conn=conn)
    logger.info("загрузка: %s", result.summary())
    return result


def _fact_date(report: ReportSet, fact: Fact) -> date:
    """Дата периода факта, восстановленная из его роли."""
    return date(report.report_year - PERIOD_OFFSET_BY_ROLE[fact.period_role], 12, 31)


def _upsert_organization(organization: Organization, conn: PgConnection) -> None:
    """Пишет реквизиты организации, обновляя уже сохранённые."""
    execute(
        _UPSERT_ORGANIZATION,
        {
            "inn": organization.inn,
            "girbo_id": organization.girbo_id,
            "name": organization.full_name,
            "short_name": organization.short_name,
            "ogrn": organization.ogrn,
            "okpo": organization.okpo,
            "okved": organization.okved,
            "region": organization.region,
            "meta": json.dumps(
                {"kpp": organization.kpp, "okopf": organization.okopf},
                ensure_ascii=False,
            ),
        },
        conn=conn,
    )


def _upsert_src_file(
    report: ReportSet,
    conn: PgConnection,
    raw_path: str | None,
    checksum: str | None,
    source_url: str | None,
) -> int:
    """Пишет комплект как единицу обработки и возвращает его идентификатор."""
    params: dict[str, Any] = {
        "inn": report.inn,
        "report_year": report.report_year,
        "source": SOURCE,
        "source_url": source_url,
        "raw_path": raw_path,
        "checksum": checksum,
        "form_codes": list(report.form_codes),
        "knd": report.knd,
        "girbo_bfo_id": report.girbo_bfo_id,
        "correction_version": report.correction_version,
        "is_actual": report.is_actual,
        "reporting_type": report.reporting_type.value,
        "unit_code": ASSUMED_UNIT_CODE,
        "unit_multiplier": ASSUMED_UNIT_MULTIPLIER,
        "unit_source": "assumed",
        "meta": json.dumps({"period_depth": {f: len(d.values) for f, d in report.forms.items()}}),
    }
    with cursor(conn, dict_rows=False) as cur:
        cur.execute(_UPSERT_SRC_FILE, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("не удалось записать src_file")
    return int(row[0])


def _read_existing(
    report: ReportSet, conn: PgConnection
) -> dict[tuple[date, str, str], _Existing]:
    """Читает уже загруженные значения затрагиваемых периодов и форм."""
    dates = sorted({d for form in report.forms.values() for d in form.values})
    if not dates:
        return {}
    rows = fetch_all(
        _SELECT_EXISTING,
        {"inn": report.inn, "dates": dates, "forms": list(report.form_codes)},
        conn=conn,
    )
    return {
        (row["report_date"], row["form_code"], row["line_code"]): _Existing(
            value=row["value"],
            value_status=row["value_status"],
            period_role=row["period_role"],
            src_file_id=row["src_file_id"],
        )
        for row in rows
    }


def _decide(
    report: ReportSet,
    facts: list[Fact],
    existing: dict[tuple[date, str, str], _Existing],
    src_file_id: int,
    result: LoadReport,
) -> tuple[list[Fact], list[CheckRecord]]:
    """Решает по каждому факту: писать, пропустить или зафиксировать расхождение."""
    to_write: list[Fact] = []
    records: list[CheckRecord] = []

    for fact in facts:
        report_date = _fact_date(report, fact)
        previous = existing.get((report_date, fact.form_code, fact.line_code))
        if previous is None:
            to_write.append(fact)
            result.facts_written += 1
            continue

        incoming_rank = PERIOD_RANK[fact.period_role]
        existing_rank = PERIOD_RANK[previous.period_role]
        changed = previous.value != fact.value or previous.value_status != fact.value_status.value

        if incoming_rank > existing_rank:
            # Сравнительное значение не трогает загруженное отчётное.
            result.facts_kept_by_priority += 1
            if changed:
                result.period_mismatches += 1
                records.append(
                    _mismatch_record(report, fact, previous, report_date, src_file_id)
                )
            continue

        if not changed and previous.src_file_id == src_file_id:
            result.facts_unchanged += 1
            continue

        to_write.append(fact)
        result.facts_written += 1
        if changed:
            result.overwritten += 1
            records.append(_overwrite_record(report, fact, previous, report_date, src_file_id))
    return to_write, records


def _mismatch_record(
    report: ReportSet,
    fact: Fact,
    previous: _Existing,
    report_date: date,
    src_file_id: int,
) -> CheckRecord:
    """Расхождение сравнительного значения с ранее загруженным отчётным."""
    return CheckRecord(
        inn=report.inn,
        check_code=CheckCode.PERIOD_VALUE_MISMATCH,
        status=CheckStatus.WARNING,
        src_file_id=src_file_id,
        report_date=report_date,
        form_code=fact.form_code,
        line_code=fact.line_code,
        previous_value=previous.value,
        new_value=fact.value,
        message=(
            "Сравнительное значение расходится с отчётным, загруженным ранее: "
            "признак переклассификации или исправления. Сохранено отчётное значение"
        ),
        details={
            "kept_period_role": previous.period_role,
            "rejected_period_role": fact.period_role,
            "source_report_year": report.report_year,
            "source_line_code": fact.source_line_code,
        },
    )


def _overwrite_record(
    report: ReportSet,
    fact: Fact,
    previous: _Existing,
    report_date: date,
    src_file_id: int,
) -> CheckRecord:
    """Перезапись ранее загруженного значения."""
    return CheckRecord(
        inn=report.inn,
        check_code=CheckCode.FACT_OVERWRITE,
        status=CheckStatus.INFO,
        src_file_id=src_file_id,
        report_date=report_date,
        form_code=fact.form_code,
        line_code=fact.line_code,
        previous_value=previous.value,
        new_value=fact.value,
        message="Значение перезаписано при повторной загрузке",
        details={
            "previous_status": previous.value_status,
            "new_status": fact.value_status.value,
            "previous_period_role": previous.period_role,
            "new_period_role": fact.period_role,
            "correction_version": report.correction_version,
        },
    )


def _conflict_records(
    report: ReportSet, src_file_id: int, conflicts: list[LineConflict]
) -> list[CheckRecord]:
    """Записи о строках, за которые спорят несколько раскрытых кодов."""
    return [
        CheckRecord(
            inn=report.inn,
            check_code=CheckCode.AMBIGUOUS_LINE_CODE,
            status=CheckStatus.WARNING,
            src_file_id=src_file_id,
            report_date=conflict.report_date,
            form_code=conflict.form_code,
            line_code=conflict.line_code,
            message=(
                "Значение укрупнённой строки раскрыто сразу несколькими кодами: "
                "выбрать источник нельзя, строка не загружена"
            ),
            details={
                "source_codes": list(conflict.source_codes),
                "reporting_type": report.reporting_type.value,
            },
        )
        for conflict in conflicts
    ]


def _code_records(
    report: ReportSet,
    src_file_id: int,
    unknown: dict[str, tuple[str, ...]],
    ambiguous: dict[str, tuple[str, ...]],
) -> list[CheckRecord]:
    """Записи о кодах, которые не попали в fact_report."""
    records: list[CheckRecord] = []
    for form_code, codes in unknown.items():
        for code in codes:
            records.append(
                CheckRecord(
                    inn=report.inn,
                    check_code=CheckCode.UNKNOWN_LINE_CODE,
                    status=CheckStatus.WARNING,
                    src_file_id=src_file_id,
                    report_date=report.report_date,
                    form_code=form_code,
                    line_code=code,
                    message="Код строки отсутствует в справочнике и в расчёт не попадёт",
                    details={"reporting_type": report.reporting_type.value},
                )
            )
    for form_code, codes in ambiguous.items():
        for code in codes:
            records.append(
                CheckRecord(
                    inn=report.inn,
                    check_code=CheckCode.AMBIGUOUS_LINE_CODE,
                    status=CheckStatus.WARNING,
                    src_file_id=src_file_id,
                    report_date=report.report_date,
                    form_code=form_code,
                    line_code=code,
                    message=(
                        "Код допускают несколько укрупнённых строк, выбрать однозначно нельзя: "
                        "строка не загружена"
                    ),
                    details={"reporting_type": report.reporting_type.value},
                )
            )
    return records
