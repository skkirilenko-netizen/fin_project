"""Загрузка комплекта отчётности в fact_report одной транзакцией."""

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.db import PgConnection, cursor, execute, execute_many, fetch_all
from finlib.normalize.lines import LinesCatalog, ReportingType, UnitSource, load_lines
from finlib.normalize.mapper import (
    AmbiguousCode,
    Fact,
    LineConflict,
    MappedLine,
    MappingResult,
    UnrecognizedLine,
    map_by_name,
    map_codes,
)
from finlib.normalize.report import LoadReport
from finlib.quality.codes import MAPPING_CODES, CheckCode, CheckStatus, Severity
from finlib.quality.journal import CheckRecord, log_records
from finlib.quality.values import sign_only_difference
from finlib.sources.model import (
    PERIOD_OFFSETS,
    FormData,
    Organization,
    ReportSet,
    SourceKind,
)
from finlib.standards import Standard
from finlib.utils import ValueStatus

logger = logging.getLogger(__name__)

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
-- Реквизит, которого источник не сообщил, не стирает ранее известный.
-- Выгрузка XLSX не содержит идентификатора ГИР БО и короткого наименования,
-- а у части организаций — и ОКВЭД; без COALESCE загрузка файла молча обнуляла
-- бы то, что принёс живой прогон, и строки шапки документа исчезали бы.
ON CONFLICT (inn) DO UPDATE SET
    girbo_id = COALESCE(EXCLUDED.girbo_id, organization.girbo_id),
    name = COALESCE(EXCLUDED.name, organization.name),
    short_name = COALESCE(EXCLUDED.short_name, organization.short_name),
    ogrn = COALESCE(EXCLUDED.ogrn, organization.ogrn),
    okpo = COALESCE(EXCLUDED.okpo, organization.okpo),
    okved = COALESCE(EXCLUDED.okved, organization.okved),
    region = COALESCE(EXCLUDED.region, organization.region),
    meta = COALESCE(organization.meta, '{}'::jsonb) || COALESCE(EXCLUDED.meta, '{}'::jsonb),
    updated_at = now()
"""

# Актуальная версия года одна, и источник в это не входит. Одна и та же
# отчётность, полученная ресурсом и поданная файлом, — две доставки одного
# комплекта: ключ факта источника не содержит, и держать актуальными обе
# значило бы иметь за год два комплекта, у которых факты общие. Прежние
# версии остаются в базе историей, но в расчёт идёт последняя загруженная.
_SUPERSEDE_OTHER_VERSIONS = """
UPDATE src_file SET is_actual = false
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(report_year)s
  AND NOT (source = %(source)s AND correction_version = %(correction_version)s)
  AND is_actual
"""

_UPSERT_SRC_FILE = """
INSERT INTO src_file (
    inn, standard, report_year, source, source_url, raw_path, checksum, form_codes, knd,
    girbo_bfo_id, correction_version, is_actual, reporting_type, unit_code, unit_multiplier,
    unit_source, status, meta
) VALUES (
    %(inn)s, %(standard)s, %(report_year)s, %(source)s, %(source_url)s, %(raw_path)s,
    %(checksum)s, %(form_codes)s, %(knd)s, %(girbo_bfo_id)s, %(correction_version)s,
    %(is_actual)s, %(reporting_type)s, %(unit_code)s, %(unit_multiplier)s, %(unit_source)s,
    'loaded', %(meta)s
)
ON CONFLICT (inn, standard, report_year, source, correction_version) DO UPDATE SET
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

_CLEAR_MAPPING_RECORDS = """
DELETE FROM dq_log WHERE src_file_id = %(id)s AND check_code = ANY(%(codes)s)
"""

_SELECT_EXISTING = """
SELECT report_date, form_code, line_code, value, value_status, period_role, src_file_id
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s
  AND report_date = ANY(%(dates)s) AND form_code = ANY(%(forms)s)
"""

# Сравнительный период не затирает уже загруженное отчётное значение, а
# совпадающее значение не трогает updated_at и не порождает записей журнала.
_UPSERT_FACT = """
INSERT INTO fact_report (
    src_file_id, inn, standard, report_date, form_code, line_code, source_line_code,
    value, value_status, period_role
) VALUES (
    %(src_file_id)s, %(inn)s, %(standard)s, %(report_date)s, %(form_code)s, %(line_code)s,
    %(source_line_code)s, %(value)s, %(value_status)s, %(period_role)s
)
ON CONFLICT (inn, standard, report_date, form_code, line_code) DO UPDATE SET
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
    # Сколько кодов формы сопоставлено со справочником. Счётчик проверенного:
    # без него нули по unknown и ambiguous ничем не подтверждены — журнал
    # молчит и когда разобраны все коды, и когда разбора не было.
    mapped: dict[str, int] = dc_field(default_factory=dict)
    unknown: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    ignored: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    not_applicable: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    ambiguous: dict[str, tuple[str, ...]] = dc_field(default_factory=dict)
    ambiguous_details: list[AmbiguousCode] = dc_field(default_factory=list)
    conflicts: list[LineConflict] = dc_field(default_factory=list)
    not_recognized: list[UnrecognizedLine] = dc_field(default_factory=list)


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
        if form.names and report.reporting_type is not ReportingType.FULL:
            # Источник отдал наименования, а набор упрощённый: код в нём —
            # подсказка, ключом служит наименование.
            mapping = map_by_name(form.names, catalog, report.reporting_type, form_code)
            built.not_recognized.extend(
                _unrecognized(form, item) for item in mapping.not_recognized
            )
        else:
            mapping = map_codes(codes, filled, catalog, report.reporting_type, form_code)
        built.mapped[form_code] = len(mapping.mapped)
        if mapping.unknown:
            built.unknown[form_code] = tuple(item.source_code for item in mapping.unknown)
        if mapping.ignored:
            built.ignored[form_code] = tuple(item.source_code for item in mapping.ignored)
        if mapping.not_applicable:
            built.not_applicable[form_code] = tuple(
                item.source_code for item in mapping.not_applicable
            )
        if mapping.ambiguous:
            built.ambiguous[form_code] = tuple(item.source_code for item in mapping.ambiguous)
            built.ambiguous_details.extend(
                AmbiguousCode(form_code, item.source_code, item.candidates)
                for item in mapping.ambiguous
            )

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


def _unrecognized(form: FormData, item: MappedLine) -> UnrecognizedLine:
    """Собирает неопознанную строку вместе со значениями, которые она несла."""
    disclosed: list[tuple[date, Decimal]] = []
    for report_date, values in sorted(form.values.items()):
        value = values.get(item.source_code)
        if value is not None:
            disclosed.append((report_date, value))
    return UnrecognizedLine(
        form_code=form.form_code,
        source_code=item.source_code,
        name=item.source_name or "",
        disclosed=tuple(disclosed),
    )


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
    standard: Standard = Standard.RSBU,
    source: SourceKind = SourceKind.GIR_BO,
    unit_source: UnitSource | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> LoadReport:
    """Загружает один комплект отчётности целиком в переданной транзакции.

    Снятие признака актуальности с прежних версий, запись src_file и фактов
    происходят вместе: при падении посередине в базе не останется ни двух
    актуальных версий, ни комплекта без фактов.

    `source` входит в ключ уникальности комплекта: одна и та же отчётность,
    полученная ресурсом и поданная файлом, — два разных комплекта, и признак
    актуальности снимается только внутри своего источника.

    `unit_source` передаёт источник, который единицу измерения объявляет сам
    (выгрузка XLSX её печатает). Умолчание — правило «форма задаёт единицу».
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
                "standard": standard.value,
                "report_year": report.report_year,
                "source": source.value,
                "correction_version": report.correction_version,
            },
            conn=conn,
        )

    src_file_id = _upsert_src_file(
        report,
        conn,
        raw_path,
        checksum,
        source_url,
        standard,
        catalog,
        source,
        unit_source,
        meta_extra,
    )
    result.src_file_id = src_file_id

    built = build_facts(report, catalog)
    result.facts_total = len(built.facts)
    result.unknown_codes = built.unknown
    result.ignored_codes = built.ignored
    result.not_applicable_codes = built.not_applicable
    result.ambiguous_codes = built.ambiguous
    result.line_conflicts = len(built.conflicts)
    result.not_recognized = len(built.not_recognized)
    result.not_recognized_with_value = sum(
        1 for line in built.not_recognized if line.lost is not None
    )
    result.periods = tuple(
        sorted({d for form in report.forms.values() for d in form.values}, reverse=True)
    )

    existing = _read_existing(report, conn, standard)
    to_write, records = _decide(report, built.facts, existing, src_file_id, result)

    if to_write:
        execute_many(
            _UPSERT_FACT,
            [
                {
                    "src_file_id": src_file_id,
                    "inn": report.inn,
                    "standard": standard.value,
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
    records.extend(_code_records(report, src_file_id, built.unknown, built.ambiguous_details))
    records.extend(_conflict_records(report, src_file_id, built.conflicts))
    records.extend(_not_recognized_records(report, src_file_id, built.not_recognized))
    records.extend(_mapping_summary(report, src_file_id, built))
    # Записи о сопоставлении строк описывают состояние комплекта, а не событие:
    # повторная загрузка снимает прежние и кладёт нынешние.
    execute(
        _CLEAR_MAPPING_RECORDS,
        {"id": src_file_id, "codes": [code.value for code in MAPPING_CODES]},
        conn=conn,
    )
    log_records(records, conn=conn)
    logger.info("загрузка: %s", result.summary())
    return result


def max_correction(
    inn: str,
    report_year: int,
    source: SourceKind,
    standard: Standard,
    conn: PgConnection,
) -> int | None:
    """Наибольшая загруженная корректировка комплекта; None — комплектов нет."""
    rows = fetch_all(
        "SELECT max(correction_version) AS version FROM src_file "
        "WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(year)s "
        "AND source = %(source)s",
        {
            "inn": inn,
            "standard": standard.value,
            "year": report_year,
            "source": source.value,
        },
        conn=conn,
    )
    version = rows[0]["version"] if rows else None
    return int(version) if version is not None else None


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
            # Пустые ключи в meta не пишутся: слияние jsonb затёрло бы ими
            # ранее известные реквизиты.
            "meta": json.dumps(
                {
                    key: value
                    for key, value in (
                        ("kpp", organization.kpp),
                        ("okopf", organization.okopf),
                    )
                    if value is not None
                },
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
    standard: Standard,
    catalog: LinesCatalog,
    source: SourceKind,
    declared_unit: UnitSource | None,
    meta_extra: dict[str, Any] | None = None,
) -> int:
    """Пишет комплект как единицу обработки и возвращает его идентификатор."""
    units = catalog.units
    unit_source = declared_unit if declared_unit is not None else units.source_for(
        report.form_codes
    )
    params: dict[str, Any] = {
        "inn": report.inn,
        "standard": standard.value,
        "report_year": report.report_year,
        "source": source.value,
        "source_url": source_url,
        "raw_path": raw_path,
        "checksum": checksum,
        "form_codes": list(report.form_codes),
        "knd": report.knd,
        "girbo_bfo_id": report.girbo_bfo_id,
        "correction_version": report.correction_version,
        "is_actual": report.is_actual,
        "reporting_type": report.reporting_type.value,
        # Единица определяется формой, а не полезной нагрузкой: ГИР БО её
        # не сообщает, а ошибка в тысячу раз не ловится ни одним контролем.
        # Комплект из неизвестных форм единицы не получает и уходит
        # в карантин контролем unit_not_determined. Источник, печатающий
        # единицу в самой выгрузке, объявляет её сам (unit_source = explicit),
        # и в комплект попадает только та, что опознана справочником:
        # неопознанная останавливает разбор до создания комплекта.
        "unit_code": units.okei_code,
        "unit_multiplier": units.multiplier,
        "unit_source": unit_source.value,
        "meta": json.dumps(
            {
                "period_depth": {f: len(d.values) for f, d in report.forms.items()},
                **(meta_extra or {}),
            },
            ensure_ascii=False,
        ),
    }
    with cursor(conn, dict_rows=False) as cur:
        cur.execute(_UPSERT_SRC_FILE, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("не удалось записать src_file")
    return int(row[0])


def _read_existing(
    report: ReportSet, conn: PgConnection, standard: Standard
) -> dict[tuple[date, str, str], _Existing]:
    """Читает уже загруженные значения затрагиваемых периодов и форм."""
    dates = sorted({d for form in report.forms.values() for d in form.values})
    if not dates:
        return {}
    rows = fetch_all(
        _SELECT_EXISTING,
        {
            "inn": report.inn,
            "standard": standard.value,
            "dates": dates,
            "forms": list(report.form_codes),
        },
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

        sign_only = sign_only_difference(previous.value, fact.value)

        if incoming_rank > existing_rank:
            # Сравнительное значение не трогает загруженное отчётное.
            result.facts_kept_by_priority += 1
            if changed and sign_only:
                result.sign_conventions += 1
                records.append(
                    _sign_record(report, fact, previous, report_date, src_file_id)
                )
            elif changed:
                result.period_mismatches += 1
                records.append(
                    _mismatch_record(
                        report, fact, previous, report_date, src_file_id, winner="stored"
                    )
                )
            continue

        if not changed and previous.src_file_id == src_file_id:
            result.facts_unchanged += 1
            continue

        to_write.append(fact)
        result.facts_written += 1
        if not changed:
            continue

        if sign_only:
            # Величина та же, знак обратный: соглашение о печати, а не
            # пересмотр. Значение при этом пишется по общему правилу —
            # спорен знак, а не то, какая доставка свежее.
            result.sign_conventions += 1
            records.append(
                _sign_record(report, fact, previous, report_date, src_file_id)
            )
        elif incoming_rank < existing_rank:
            # Отчётное значение вытесняет ранее загруженное сравнительное.
            # Это то же расхождение периодов, только обнаруженное с другой
            # стороны: журнал не должен зависеть от порядка загрузки.
            result.period_mismatches += 1
            records.append(
                _mismatch_record(
                    report, fact, previous, report_date, src_file_id, winner="incoming"
                )
            )
        else:
            result.overwritten += 1
            records.append(_overwrite_record(report, fact, previous, report_date, src_file_id))
    return to_write, records


def _mismatch_record(
    report: ReportSet,
    fact: Fact,
    previous: _Existing,
    report_date: date,
    src_file_id: int,
    *,
    winner: str,
) -> CheckRecord:
    """Расхождение отчётного и сравнительного значений одного периода.

    Фиксируется независимо от того, в каком порядке загружены комплекты:
    победило ли уже лежащее отчётное значение или его принесла эта загрузка.
    """
    kept_role = previous.period_role if winner == "stored" else fact.period_role
    rejected_role = fact.period_role if winner == "stored" else previous.period_role
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
            "Отчётное и сравнительное значения периода расходятся: признак "
            "переклассификации или исправления. Сохранено отчётное значение"
        ),
        details={
            "winner": winner,
            "kept_period_role": kept_role,
            "rejected_period_role": rejected_role,
            "source_report_year": report.report_year,
            "source_line_code": fact.source_line_code,
        },
    )


def _sign_record(
    report: ReportSet,
    fact: Fact,
    previous: _Existing,
    report_date: date,
    src_file_id: int,
) -> CheckRecord:
    """Величина совпала, знак обратный: соглашение о печати, а не пересмотр.

    Проверено на данных: у ПАО «Газпром» строка 2411 за 2023 год приходит
    как 14 235 635 и как −14 235 635 из двух доставок одного периода.
    Величина не пересмотрена — расходится способ печати расхода: одна и та же
    организация печатает налог на прибыль в одном году в скобках, в другом
    без них. Прежде это шло кодом `period_value_mismatch`, по которому
    считается интенсивность пересмотра, и сигнал мерил соглашение о знаке,
    а не эмитента.
    """
    return CheckRecord(
        inn=report.inn,
        check_code=CheckCode.SIGN_CONVENTION_MISMATCH,
        status=CheckStatus.WARNING,
        src_file_id=src_file_id,
        report_date=report_date,
        form_code=fact.form_code,
        line_code=fact.line_code,
        previous_value=previous.value,
        new_value=fact.value,
        message=(
            "Величина совпадает, знак обратный: расхождение соглашения "
            "о печати знака, а не пересмотр отчётности"
        ),
        details={
            "stored_period_role": previous.period_role,
            "incoming_period_role": fact.period_role,
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
            check_code=CheckCode.MULTIPLE_SOURCE_CODES,
            status=CheckStatus.WARNING,
            src_file_id=src_file_id,
            report_date=conflict.report_date,
            form_code=conflict.form_code,
            line_code=conflict.line_code,
            message=(
                "Значение укрупнённой строки раскрыто сразу несколькими кодами: "
                "аномалия отчётности, выбрать источник нельзя, строка не загружена"
            ),
            details={
                "source_codes": list(conflict.source_codes),
                "reporting_type": report.reporting_type.value,
            },
        )
        for conflict in conflicts
    ]


def _not_recognized_records(
    report: ReportSet, src_file_id: int, lines: list[UnrecognizedLine]
) -> list[CheckRecord]:
    """Записи о строках, которые справочник не опознал по наименованию.

    Уровень зависит от того, что строка несла. Пустая строка — пробел
    справочника: её стоит завести, но отчётность из-за неё не останавливается.
    Строка с ненулевым значением — тихая потеря данных: величина есть
    в отчётности, в расчёт она не попадёт, и никакой контроль сходимости её
    не хватится, если строка не входит ни в один проверяемый итог. Такой
    комплект в расчёт не идёт.
    """
    records: list[CheckRecord] = []
    for line in lines:
        lost = line.lost
        records.append(
            CheckRecord(
                inn=report.inn,
                check_code=CheckCode.LINE_NOT_RECOGNIZED,
                status=CheckStatus.FAIL if lost else CheckStatus.WARNING,
                severity=Severity.BLOCKING if lost else Severity.WARNING,
                src_file_id=src_file_id,
                report_date=lost[0] if lost else report.report_date,
                form_code=line.form_code,
                line_code=line.source_code,
                new_value=lost[1] if lost else None,
                message=(
                    "Строка отчётности не опознана по наименованию и в расчёт не попала, "
                    "а значение у неё раскрыто: данные были бы потеряны молча"
                )
                if lost
                else (
                    "Строка отчётности не опознана по наименованию и в расчёт не попала; "
                    "значения она не несёт"
                ),
                details={
                    "source_name": line.name,
                    "reporting_type": report.reporting_type.value,
                    "disclosed": {
                        f"{period:%Y-%m-%d}": str(value) for period, value in line.disclosed
                    },
                },
            )
        )
    return records


def _mapping_summary(
    report: ReportSet, src_file_id: int, built: BuiltFacts
) -> list[CheckRecord]:
    """Сводка судеб кодов по каждой форме комплекта.

    Счётчик проверенного рядом со счётчиком нарушений: записи о неизвестных
    и неоднозначных кодах пишутся только при срабатывании, и ноль таких
    записей сам по себе не означает ничего. Здесь названо, сколько кодов
    формы разобрано и как именно, — и тогда ноль неизвестных кодов становится
    утверждением, а не молчанием.
    """
    records: list[CheckRecord] = []
    for form_code in sorted(report.forms):
        mapped = built.mapped.get(form_code, 0)
        ignored = len(built.ignored.get(form_code, ()))
        not_applicable = len(built.not_applicable.get(form_code, ()))
        unknown = len(built.unknown.get(form_code, ()))
        ambiguous = len(built.ambiguous.get(form_code, ()))
        not_recognized = sum(
            1 for item in built.not_recognized if item.form_code == form_code
        )
        records.append(
            CheckRecord(
                inn=report.inn,
                check_code=CheckCode.LINE_MAPPING,
                status=CheckStatus.INFO,
                severity=Severity.INFO,
                message=(
                    f"Форма {form_code}: сопоставлено кодов {mapped}, "
                    f"игнорируется методикой {ignored}, неприменимо к набору "
                    f"{not_applicable}, неизвестно {unknown}, "
                    f"неоднозначно {ambiguous}, не опознано по наименованию "
                    f"{not_recognized}"
                ),
                src_file_id=src_file_id,
                form_code=form_code,
                details={
                    "mapped": mapped,
                    "ignored": ignored,
                    "not_applicable": not_applicable,
                    "unknown": unknown,
                    "ambiguous": ambiguous,
                    "not_recognized": not_recognized,
                },
            )
        )
    return records


def _code_records(
    report: ReportSet,
    src_file_id: int,
    unknown: dict[str, tuple[str, ...]],
    ambiguous: list[AmbiguousCode],
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
    for item in ambiguous:
        records.append(
            CheckRecord(
                inn=report.inn,
                check_code=CheckCode.AMBIGUOUS_LINE_CODE,
                status=CheckStatus.WARNING,
                src_file_id=src_file_id,
                report_date=report.report_date,
                form_code=item.form_code,
                line_code=item.source_code,
                message=(
                    "Код допускают несколько укрупнённых строк, выбрать однозначно нельзя: "
                    "строка не загружена"
                ),
                # Перечень претендентов нужен контролям: итог, в состав которого
                # входит любая из этих строк, проверить нельзя.
                details={
                    "candidates": list(item.candidates),
                    "reporting_type": report.reporting_type.value,
                },
            )
        )
    return records
