"""Запись комплекта МСФО в базу: src_file, факты, журнал качества.

Загрузка идёт одной транзакцией, как и у РСБУ: снятие признака актуальности
с прежних версий, запись комплекта и запись фактов происходят вместе. Иначе
при сбое посередине в базе остаётся либо ноль актуальных версий, либо две.

Ключи те же, что у РСБУ, и стандарт входит в них: `fact_report` различает
величину по МСФО и по РСБУ за одну и ту же дату. Без этого расчёт по одному
стандарту молча затирал бы значения другого.

Роль периода назначается по порядку колонок: первая отчётная дата — `current`,
остальные — сравнительные. Это то же правило приоритета, что в РСБУ:
сравнительное значение не затирает отчётное, а расхождение между ними —
содержательный сигнал о переклассификации, а не техническая деталь.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, execute, fetch_all, fetch_one
from finlib.normalize.loader import PERIOD_RANK
from finlib.quality.codes import CheckCode, CheckStatus, Severity
from finlib.quality.journal import CheckRecord, log_records
from finlib.sources.ifrs_extract import Extraction
from finlib.sources.ifrs_inbox import DocumentProfile
from finlib.sources.ifrs_review import ReviewResult
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Роли периодов по порядку колонок документа. Четвёртой и далее колонке роли
# нет: модель хранит три, как и для РСБУ.
PERIOD_ROLES: tuple[str, ...] = ("current", "previous", "before_previous")

# Организация по МСФО может быть новой: в базе РСБУ её нет, если отчётность
# по ней не загружалась. Наименование не выдумывается — оно остаётся пустым
# до тех пор, пока не будет извлечено из документа: назвать организацию
# по имени файла значило бы взять название из того, что назначил человек,
# выгружавший отчётность.
_ENSURE_ORGANIZATION = """
INSERT INTO organization (inn, name) VALUES (%(inn)s, %(name)s)
ON CONFLICT (inn) DO UPDATE SET
    name = COALESCE(organization.name, EXCLUDED.name),
    updated_at = now()
"""

_DROP_ACTUAL = """
UPDATE src_file SET is_actual = false
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(report_year)s
  AND id <> %(keep)s AND is_actual
"""

_UPSERT_SRC_FILE = """
INSERT INTO src_file (
    inn, standard, report_year, source, raw_path, checksum, form_codes,
    correction_version, is_actual, reporting_type, reporting_kind, unit_code,
    unit_source, digit_grouping, status, meta
) VALUES (
    %(inn)s, %(standard)s, %(report_year)s, 'file', %(raw_path)s, %(checksum)s,
    %(form_codes)s, %(correction_version)s, true, 'full', %(reporting_kind)s,
    %(unit_code)s, 'explicit', %(digit_grouping)s, %(status)s, %(meta)s
)
ON CONFLICT (inn, standard, report_year, source, correction_version) DO UPDATE SET
    raw_path = EXCLUDED.raw_path,
    checksum = EXCLUDED.checksum,
    form_codes = EXCLUDED.form_codes,
    is_actual = true,
    reporting_kind = EXCLUDED.reporting_kind,
    unit_code = EXCLUDED.unit_code,
    digit_grouping = EXCLUDED.digit_grouping,
    status = EXCLUDED.status,
    meta = EXCLUDED.meta,
    loaded_at = now()
RETURNING id
"""

_UPSERT_FACT = """
INSERT INTO fact_report (
    src_file_id, inn, standard, report_date, form_code, line_code,
    source_line_code, value, value_status, period_role
) VALUES (
    %(src_file_id)s, %(inn)s, %(standard)s, %(report_date)s, %(form_code)s,
    %(line_code)s, %(source_line_code)s, %(value)s, 'ok', %(period_role)s
)
ON CONFLICT (inn, standard, report_date, form_code, line_code) DO UPDATE SET
    src_file_id = EXCLUDED.src_file_id,
    source_line_code = EXCLUDED.source_line_code,
    value = EXCLUDED.value,
    period_role = EXCLUDED.period_role,
    updated_at = now()
WHERE period_rank(EXCLUDED.period_role) <= period_rank(fact_report.period_role)
  AND fact_report.value IS DISTINCT FROM EXCLUDED.value
"""

_EXISTING = """
SELECT report_date, form_code, line_code, value, period_role
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
"""

_INSERT_CONFIRMATION = """
INSERT INTO ifrs_line_confirmation (
    code, inn, src_file_id, report_date, source_name, form_code, value,
    share_of_assets, confirmed_by, note
) VALUES (
    %(code)s, %(inn)s, %(src_file_id)s, %(report_date)s, %(source_name)s,
    %(form_code)s, %(value)s, %(share)s, %(confirmed_by)s, %(note)s
)
ON CONFLICT (code, inn, report_date, source_name) DO UPDATE SET
    src_file_id = EXCLUDED.src_file_id,
    value = EXCLUDED.value,
    share_of_assets = EXCLUDED.share_of_assets,
    confirmed_by = EXCLUDED.confirmed_by,
    note = EXCLUDED.note,
    confirmed_at = now()
"""


@dataclass
class LoadResult:
    """Итог записи комплекта МСФО."""

    src_file_id: int
    inn: str
    facts_written: int = 0
    facts_total: int = 0
    revisions: tuple[str, ...] = ()
    quarantined: bool = False
    confirmations: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        """Однострочная сводка со счётчиками проверенного."""
        return (
            f"ИНН {self.inn}, комплект {self.src_file_id}: фактов записано "
            f"{self.facts_written} из {self.facts_total}, расхождений "
            f"со сравнительными данными {len(self.revisions)}, "
            f"подтверждённых статей {self.confirmations}, "
            + ("КАРАНТИН" if self.quarantined else "расчёт разрешён")
        )


def load_extraction(
    inn: str,
    extraction: Extraction,
    profile: DocumentProfile,
    review: ReviewResult,
    conn: PgConnection,
    *,
    raw_path: str | None = None,
    checksum: str | None = None,
    correction_version: int = 0,
    confirmed_by: str | None = None,
    confirmations: dict[str, str] | None = None,
    organization_name: str | None = None,
) -> LoadResult:
    """Пишет принятый комплект МСФО одной транзакцией.

    confirmations — коды, присвоенные человеком неопознанным статьям:
    наименование в отчётности → код позиции. Пустой словарь означает, что
    подтверждения не было, и статьи остались неопознанными.

    Комплект, не прошедший экран сверки без подтверждения, уходит в карантин:
    извлечение, о котором машина не знает, что перед ней, в расчёт не идёт.
    """
    execute(
        _ENSURE_ORGANIZATION, {"inn": inn, "name": organization_name}, conn=conn
    )
    report_date = profile.report_dates[0]
    unconfirmed = _unconfirmed(extraction, confirmations or {})
    quarantined = not review.automatic and bool(unconfirmed or not confirmed_by)

    src_file_id = _write_src_file(
        inn,
        profile,
        review,
        conn,
        raw_path=raw_path,
        checksum=checksum,
        correction_version=correction_version,
        quarantined=quarantined,
    )

    revisions = _revisions(inn, extraction, profile, conn)
    written = _write_facts(inn, src_file_id, extraction, profile, conn)

    records = _journal_records(inn, src_file_id, extraction, review, revisions, quarantined)
    log_records(records, conn=conn)

    saved = 0
    if confirmations and confirmed_by:
        saved = _save_confirmations(
            inn, src_file_id, report_date, extraction, confirmations, confirmed_by, conn
        )

    result = LoadResult(
        src_file_id=src_file_id,
        inn=inn,
        facts_written=written,
        facts_total=len(extraction.values),
        revisions=tuple(revisions),
        quarantined=quarantined,
        confirmations=saved,
        notes=extraction.notes,
    )
    logger.info("загрузка МСФО: %s", result.summary())
    return result


def _write_src_file(
    inn: str,
    profile: DocumentProfile,
    review: ReviewResult,
    conn: PgConnection,
    *,
    raw_path: str | None,
    checksum: str | None,
    correction_version: int,
    quarantined: bool,
) -> int:
    """Записывает комплект и снимает актуальность с прежних версий года."""
    report_year = profile.report_dates[0].year
    meta = {
        "reporting_kind": profile.reporting_kind.value,
        "review_outcome": review.outcome.value,
        "review_reasons": [item.value for item in review.reasons],
        "notes_under_forms": list(profile.forms),
        "grouping_evidence": profile.grouping_detection.describe(),
    }
    row = fetch_one(
        _UPSERT_SRC_FILE,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": report_year,
            "raw_path": raw_path,
            "checksum": checksum,
            "form_codes": list(profile.forms),
            "correction_version": correction_version,
            "reporting_kind": profile.reporting_kind.value,
            "unit_code": profile.unit_code,
            "digit_grouping": profile.grouping.value,
            "status": "quarantine" if quarantined else "loaded",
            "meta": json.dumps(meta, ensure_ascii=False),
        },
        conn=conn,
    )
    assert row is not None
    src_file_id = int(row["id"])
    execute(
        _DROP_ACTUAL,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": report_year,
            "keep": src_file_id,
        },
        conn=conn,
    )
    return src_file_id


def _write_facts(
    inn: str,
    src_file_id: int,
    extraction: Extraction,
    profile: DocumentProfile,
    conn: PgConnection,
) -> int:
    """Пишет факты комплекта; возвращает число записанных."""
    roles = _roles(profile.report_dates)
    written = 0
    for item in extraction.values:
        role = roles.get(item.report_date)
        if role is None:
            # Колонок в документе больше, чем ролей периода: четвёртая
            # и далее в модель не пишутся. Молчать об этом нельзя,
            # и запись об этом делает журнал качества.
            continue
        execute(
            _UPSERT_FACT,
            {
                "src_file_id": src_file_id,
                "inn": inn,
                "standard": Standard.IFRS.value,
                "report_date": item.report_date,
                "form_code": _form_of(extraction, item.code),
                "line_code": item.code,
                "source_line_code": item.code,
                "value": item.value,
                "period_role": role,
            },
            conn=conn,
        )
        written += 1
    return written


def _roles(report_dates: tuple[date, ...]) -> dict[date, str]:
    """Роль периода по порядку колонок документа."""
    return {
        item: PERIOD_ROLES[index]
        for index, item in enumerate(report_dates)
        if index < len(PERIOD_ROLES)
    }


def _form_of(extraction: Extraction, code: str) -> str:
    """Форма, из которой извлечена позиция."""
    for form_code, form in extraction.forms.items():
        if any(item.code == code for item in form.values):
            return form_code
    return "ifrs.unknown"  # pragma: no cover — позиция всегда из своей формы


def _revisions(
    inn: str,
    extraction: Extraction,
    profile: DocumentProfile,
    conn: PgConnection,
) -> list[str]:
    """Расхождения сравнительных данных с ранее загруженной отчётностью.

    Один и тот же период приходит дважды: отчётным в своём комплекте
    и сравнительным в более позднем. Значения расходятся при
    переклассификации, и расхождение — содержательный сигнал, а не
    техническая деталь. Проверка та же по смыслу, что у РСБУ, но здесь
    сравнивается с тем, что уже лежит в базе: другого источника истины
    для прошлой отчётности эмитента у нас нет.
    """
    dates = list(profile.report_dates)
    existing = {
        (row["report_date"], row["line_code"]): row
        for row in fetch_all(
            _EXISTING,
            {"inn": inn, "standard": Standard.IFRS.value, "dates": dates},
            conn=conn,
        )
    }
    roles = _roles(profile.report_dates)
    found: list[str] = []
    for item in extraction.values:
        row = existing.get((item.report_date, item.code))
        if row is None or row["value"] is None:
            continue
        if row["value"] == item.value:
            continue
        incoming_role = roles.get(item.report_date)
        if incoming_role is None:
            continue
        # Сообщаем о расхождении независимо от того, какое значение победит:
        # приоритет решает, что попадёт в расчёт, а журнал — что разошлось.
        found.append(
            f"{item.code} за {item.report_date:%d.%m.%Y}: было {row['value']} "
            f"({row['period_role']}), пришло {item.value} ({incoming_role})"
        )
    return found


def _unconfirmed(extraction: Extraction, confirmations: dict[str, str]) -> list[str]:
    """Неопознанные статьи, которым человек кода не присвоил."""
    return [
        row.source_name
        for row in extraction.unrecognised
        if row.source_name not in confirmations
    ]


def _journal_records(
    inn: str,
    src_file_id: int,
    extraction: Extraction,
    review: ReviewResult,
    revisions: list[str],
    quarantined: bool,
) -> list[CheckRecord]:
    """Записи журнала качества по итогам приёма и сверки.

    Счётчик проверенного идёт рядом со счётчиком сработавшего: одной записью
    сообщается, сколько итогов сверено и сколько строк опознано, — иначе
    отсутствие провалов неотличимо от невыполненной проверки.
    """
    records = [
        CheckRecord(
            inn=inn,
            check_code=CheckCode.LINE_MAPPING,
            status=CheckStatus.INFO,
            severity=Severity.INFO,
            message=(
                f"Опознано позиций {review.rows_recognised} из {review.rows_total}; "
                f"итогов сверено {review.totals_checked}, не сошлось "
                f"{len(review.totals_failed)}; строк сложено с другими "
                f"{len(extraction.merged)}, спорных позиций "
                f"{len(extraction.contested)}; правдоподобие конвенции: "
                f"{review.plausibility.describe() if review.plausibility else '—'}"
            ),
            src_file_id=src_file_id,
            details={
                "rows_recognised": review.rows_recognised,
                "rows_total": review.rows_total,
                "totals_checked": review.totals_checked,
                "totals_failed": len(review.totals_failed),
                "totals_by_structure": [
                    code
                    for form in extraction.forms.values()
                    for code in form.totals_by_structure
                ],
                # Сложение и спор — события разбора, и молчать о них нельзя:
                # затирание величины было неотличимо от честного опознания
                # ровно потому, что нигде не считалось.
                "merged_rows": [
                    {"code": code, "name": name, "kind": kind}
                    for code, name, kind in extraction.merged
                ],
                "contested_codes": [code for code, _ in extraction.contested],
            },
        )
    ]

    for code, reason in zip(review.check_codes, review.reasons, strict=False):
        records.append(
            CheckRecord(
                inn=inn,
                check_code=code,
                status=CheckStatus.FAIL if quarantined else CheckStatus.WARNING,
                severity=Severity.BLOCKING if quarantined else Severity.WARNING,
                message=(
                    f"Экран сверки: {reason.value}. "
                    + "; ".join(review.problems[:3])
                ),
                src_file_id=src_file_id,
            )
        )

    for text in revisions:
        records.append(
            CheckRecord(
                inn=inn,
                check_code=CheckCode.PERIOD_VALUE_MISMATCH,
                status=CheckStatus.WARNING,
                severity=Severity.WARNING,
                message=f"Сравнительные данные расходятся с загруженными: {text}",
                src_file_id=src_file_id,
            )
        )
    return records


def _save_confirmations(
    inn: str,
    src_file_id: int,
    report_date: date,
    extraction: Extraction,
    confirmations: dict[str, str],
    confirmed_by: str,
    conn: PgConnection,
) -> int:
    """Сохраняет коды, присвоенные человеком неопознанным статьям.

    Наименование хранится дословно: по коду не увидеть, одну ли вещь
    подтверждали у разных эмитентов под разными названиями, — код присваивали
    мы. Доля от валюты баланса хранится рядом: по ней видно, ради чего статья
    вынесена отдельной позицией.
    """
    assets = extraction.value_of("ifrs.total_assets", report_date)
    saved = 0
    for row in extraction.unrecognised:
        code = confirmations.get(row.source_name)
        if code is None:
            continue
        share = (
            row.largest / abs(assets) if assets not in (None, 0) else Decimal(0)
        )
        execute(
            _INSERT_CONFIRMATION,
            {
                "code": code,
                "inn": inn,
                "src_file_id": src_file_id,
                "report_date": report_date,
                "source_name": row.source_name,
                "form_code": row.form,
                "value": row.values[0] if row.values else None,
                "share": share,
                "confirmed_by": confirmed_by,
                "note": None,
            },
            conn=conn,
        )
        saved += 1
    return saved


__all__ = ["LoadResult", "PERIOD_RANK", "load_extraction"]
