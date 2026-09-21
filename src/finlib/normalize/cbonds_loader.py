"""Запись комплекта из нормализованных данных Cbonds — одной транзакцией.

**Это доставка отчётности, а не второй стандарт.** Стандарт остаётся `ifrs`,
а `src_file.source = 'cbonds'` говорит о способе получения: тот же период
приходит и документом, и от агрегатора, и это два комплекта с общими
позициями. Сила опознания у величин своя — `recognition = 'cbonds'`: они
нормализованы агрегатором, а не прочитаны из отчётности, и доверие к ним
третье.

**Приоритет при столкновении — правило одно на всех** (`normalize/facts.py`):
первоисточник старше агрегатора, и величина документа величиной агрегатора
не затирается. Расхождение при этом пишется в журнал: у ГК «Автодор» так
виден пересмотр 2024 года.

**Три проверки нуля стоят в боевом пути, а не в замере.** Ноль у агрегатора
не означает нуля: источник пишет ноль и там, где величина не раскрыта.
Комплект, у которого не сходится тождество отчётности либо итог равен нулю
при ненулевой деятельности, уходит в карантин — стоп-фактор по такому
капиталу был бы утверждением об эмитенте, сделанным по нераскрытой величине.

**Сопоставление полей — методика** (`methodology/cbonds_mapping.yaml`):
загрузчик полей наизусть не знает, а роды полей (точное соответствие,
агрегат, сверка) объявлены там же. Устройство рассчитано на РСБУ следующим
шагом: вид отчёта — довод, стандарт берётся у него же.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, execute, fetch_all, fetch_one
from finlib.normalize.cbonds_mapping import (
    CbondsMapping,
    ReportDef,
    load_cbonds_mapping,
)
from finlib.normalize.facts import PRIORITY_WHERE
from finlib.quality.codes import CheckCode, CheckStatus, Severity
from finlib.quality.journal import CheckRecord, log_records
from finlib.standards import Standard
from finlib.version import code_version

logger = logging.getLogger(__name__)

# Отчётная дата комплекта агрегатора одна: строка источника описывает один
# период, и роль у величины отчётная — это его прочтение отчёта того года.
PERIOD_ROLE = "current"
RECOGNITION = "cbonds"

# **Коды, которые загрузчик вправе записать, объявлены здесь поимённо.**
# Справочник сопоставления называет их строками, и реестр контролей такого
# вызова не видит: код, упомянутый только в YAML, числился бы неподключённым —
# ровно тот случай, против которого реестр и заведён. Перечень же служит
# и проверкой: справочник с кодом, которого загрузчик не пишет, не загрузится.
EMITTED: tuple[CheckCode, ...] = (
    CheckCode.CBONDS_IDENTITY_MISMATCH,
    CheckCode.CBONDS_SECTIONS_MISMATCH,
    CheckCode.CBONDS_ZERO_TOTAL,
    CheckCode.CBONDS_DEBT_SPLIT_MISMATCH,
    CheckCode.CBONDS_FIELD_MAPPING,
    CheckCode.CBONDS_SET_REJECTED,
    CheckCode.CBONDS_VALUE_MISMATCH,
)


def emitted_codes() -> frozenset[str]:
    """Коды контролей, которые пишет загрузчик доставки от агрегатора."""
    return frozenset(item.value for item in EMITTED)

_ENSURE_ORGANIZATION = """
INSERT INTO organization (inn, name) VALUES (%(inn)s, %(name)s)
ON CONFLICT (inn) DO UPDATE SET
    name = COALESCE(organization.name, EXCLUDED.name),
    updated_at = now()
"""

_UPSERT_SRC_FILE = """
INSERT INTO src_file (
    inn, standard, report_year, source, form_codes, correction_version,
    is_actual, reporting_type, reporting_kind, unit_code, unit_source,
    status, meta, code_version
) VALUES (
    %(inn)s, %(standard)s, %(report_year)s, 'cbonds', %(form_codes)s, 0,
    true, 'full', 'full', %(unit_code)s, 'explicit',
    %(status)s, %(meta)s, %(code_version)s
)
ON CONFLICT (inn, standard, report_year, source, correction_version) DO UPDATE SET
    form_codes = EXCLUDED.form_codes,
    unit_code = EXCLUDED.unit_code,
    status = EXCLUDED.status,
    meta = EXCLUDED.meta,
    code_version = EXCLUDED.code_version,
    loaded_at = now()
RETURNING id
"""

# **Признак актуальности комплекта агрегатора снимается только с его же
# доставок.** Комплект документа за тот же год остаётся актуальным: это
# другой способ получения той же отчётности, и в расчёт идут оба — величины
# документа старше по правилу приоритета.
_DROP_ACTUAL = """
UPDATE src_file SET is_actual = false
WHERE inn = %(inn)s AND standard = %(standard)s AND report_year = %(report_year)s
  AND source = 'cbonds' AND id <> %(keep)s AND is_actual
"""

_UPSERT_FACT = (
    """
INSERT INTO fact_report (
    src_file_id, inn, standard, report_date, form_code, line_code,
    source_line_code, value, value_status, period_role, recognition
) VALUES (
    %(src_file_id)s, %(inn)s, %(standard)s, %(report_date)s, %(form_code)s,
    %(line_code)s, %(source_line_code)s, %(value)s, 'ok', %(period_role)s,
    %(recognition)s
)
ON CONFLICT (inn, standard, report_date, form_code, line_code) DO UPDATE SET
    src_file_id = EXCLUDED.src_file_id,
    source_line_code = EXCLUDED.source_line_code,
    value = EXCLUDED.value,
    period_role = EXCLUDED.period_role,
    recognition = EXCLUDED.recognition,
    updated_at = now()
"""
    + PRIORITY_WHERE
)

_EXISTING = """
SELECT form_code, line_code, value, recognition FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = %(date)s
"""


@dataclass(frozen=True, slots=True)
class Rejection:
    """Строка источника, не ставшая комплектом: код контроля и причина."""

    check_code: CheckCode
    reason: str


@dataclass
class LoadOutcome:
    """Итог загрузки одной строки источника."""

    inn: str
    report_date: date | None = None
    src_file_id: int | None = None
    facts: int = 0
    quarantined: bool = False
    rejection: Rejection | None = None
    # Сработавшие проверки нуля и сверки: перечень кодов с сообщением.
    failures: tuple[tuple[str, str], ...] = ()
    # Расхождения с величинами первоисточника: содержательный сигнал.
    mismatches: tuple[str, ...] = ()
    checked: int = 0

    @property
    def accepted(self) -> bool:
        """Стала ли строка комплектом."""
        return self.rejection is None

    def describe(self) -> str:
        """Однострочное описание для прогона."""
        if self.rejection is not None:
            return f"{self.inn}: не принят — {self.rejection.reason}"
        mark = " (карантин)" if self.quarantined else ""
        return (
            f"{self.inn} за {self.report_date:%d.%m.%Y}: фактов {self.facts}, "
            f"сверок {self.checked}, не сошлось {len(self.failures)}"
            f"{mark}"
        )


@dataclass
class UniverseOutcome:
    """Итог загрузки набора строк."""

    loaded: list[LoadOutcome] = field(default_factory=list)
    rejected: list[LoadOutcome] = field(default_factory=list)

    @property
    def quarantined(self) -> list[LoadOutcome]:
        """Комплекты, ушедшие в карантин по проверкам нуля."""
        return [item for item in self.loaded if item.quarantined]


def number(value: object) -> Decimal | None:
    """Величина источника в Decimal; пустое значение остаётся None.

    `parse_float=Decimal` при чтении ответа уже сделал своё, но строка
    у источника приходит и текстом: `float` для сумм не используется нигде.
    """
    if value in (None, ""):
        return None
    return Decimal(str(value))


def _unit_of(row: dict, report: ReportDef) -> str | None:
    """Код ОКЕИ по единице источника; пустая единица — отказ, а не умолчание."""
    declared = row.get(report.unit_field)
    if declared in (None, "", "0"):
        return None
    return report.units.get(str(declared))


def _standard_of(row: dict, report: ReportDef) -> str | None:
    """Консолидация по признаку источника: сравнение строки целиком.

    «МСФО» входит в «МСФО(к)» подстрокой, поэтому сравниваются приведённые
    строки целиком: вхождением неконсолидированная отчётность сошла бы
    за консолидированную, а это другой предмет.
    """
    declared = " ".join(str(row.get(report.standard_field) or "").split())
    if declared in report.consolidated_marks:
        return "consolidated"
    if declared in report.standalone_marks:
        return "standalone"
    return None


def _checked_reason(row: dict, report: ReportDef) -> Rejection | None:
    """Проверяет пригодность строки: валюта, единица, стандарт, период."""
    currency = str(row.get(report.currency_field) or "").strip()
    if currency != "RUB":
        return Rejection(
            CheckCode.CBONDS_SET_REJECTED,
            f"валюта {currency or 'не указана'}: методика рублёвая",
        )
    if _unit_of(row, report) is None:
        return Rejection(
            CheckCode.CBONDS_SET_REJECTED,
            "единица измерения не объявлена либо неизвестна: умолчания у неё нет, "
            "ошибка в тысячу раз не ловится ни одним контролем сходимости",
        )
    kind = _standard_of(row, report)
    if kind is None:
        return Rejection(
            CheckCode.CBONDS_SET_REJECTED,
            f"стандарт отчётности не опознан: {row.get(report.standard_field)!r}",
        )
    if kind != "consolidated":
        return Rejection(
            CheckCode.CBONDS_SET_REJECTED,
            "отчётность по МСФО неконсолидированная: она относится к отдельному "
            "юридическому лицу, и смешивать её с консолидированной нельзя",
        )
    if report.annual_only and not str(row.get("date") or "").endswith("12-31"):
        return Rejection(
            CheckCode.CBONDS_SET_REJECTED,
            f"период {row.get('date')} не годовой: ключ комплекта содержит год, "
            "и квартальная строка заняла бы место годовой",
        )
    return None


def _zero_checks(row: dict, report: ReportDef) -> list[tuple[str, str]]:
    """Три проверки нуля и сверка долга: что не сошлось и с какими числами.

    **Ноль у агрегатора не означает нуля.** Источник пишет ноль и там, где
    величина не раскрыта: у ГК «Автодор» за 31.03.2024 активы и капитал
    нулевые при выручке 715. Признаков три, и каждый со своим кодом.
    """
    found: list[tuple[str, str]] = []
    control = report.controls.identity
    left, right = number(row.get(control.left)), number(row.get(control.right))
    if None not in (left, right) and left != right:
        found.append((control.check, f"{control.left} {left} против {control.right} {right}"))

    sections = report.controls.sections
    total = number(row.get(sections.total))
    parts = [number(row.get(name)) for name in sections.parts]
    if total is not None and None not in parts:
        got = sum(parts, start=Decimal(0))
        if got != total:
            found.append(
                (sections.check, f"{sections.total} {total} против суммы частей {got}")
            )

    zero = report.zero_total
    activity = [number(row.get(name)) or Decimal(0) for name in zero.activity]
    if any(value != 0 for value in activity):
        for name in zero.totals:
            if number(row.get(name)) == 0:
                found.append(
                    (zero.check, f"{name} равен нулю при ненулевой деятельности")
                )

    debt = report.controls.debt_split
    declared = number(row.get(debt.total))
    split = [number(row.get(name)) for name in debt.parts]
    if declared is not None and None not in split:
        got = sum(split, start=Decimal(0))
        if got != declared:
            found.append(
                (debt.check, f"{debt.total} {declared} против суммы срочностей {got}")
            )
    return found


def _reported(row: dict, report: ReportDef) -> dict[str, str]:
    """Величины, посчитанные самим агрегатором: хранятся, но не факты.

    Состав у них его, а не наш: у ЛСР чистый долг занижен ровно на величину
    счетов эскроу, то есть источник применяет собственную поправку. В фактах
    им места нет, а для сверки и для объявленной замены EBITDA они нужны.
    """
    found: dict[str, str] = {}
    for name, code in report.reported.items():
        value = number(row.get(code))
        if value is not None:
            found[name] = str(value)
    return found


def load_row(
    row: dict,
    conn: PgConnection,
    mapping: CbondsMapping | None = None,
    report_name: str = "report_msfo_real",
) -> LoadOutcome:
    """Пишет одну строку источника комплектом — всё одной транзакцией.

    Возвращает итог с числом фактов, сверок и расхождений. Строка, не ставшая
    комплектом, фактов не порождает, а причина называется кодом контроля:
    файл, не ставший комплектом, и строка, им не ставшая, ведут себя одинаково.
    """
    mapping = mapping or load_cbonds_mapping()
    report = mapping.report(report_name)
    # Код контроля из справочника обязан быть тем, который загрузчик пишет:
    # иначе запись ушла бы под кодом, которого нет ни в реестре, ни в сводке.
    stray = report.check_codes() - emitted_codes()
    if stray:
        raise ValueError(
            f"справочник сопоставления называет коды контроля, которых загрузчик "
            f"не пишет: {sorted(stray)}"
        )
    inn = str(row.get("emitent_inn") or "").strip()
    if not inn:
        return LoadOutcome(
            inn="",
            rejection=Rejection(
                CheckCode.CBONDS_SET_REJECTED,
                "в строке источника нет ИНН: привязать комплект не к чему",
            ),
        )

    rejection = _checked_reason(row, report)
    if rejection is not None:
        _log_rejection(inn, rejection, conn)
        return LoadOutcome(inn=inn, rejection=rejection)

    moment = date.fromisoformat(str(row["date"]))
    failures = _zero_checks(row, report)
    quarantined = any(
        code
        in (
            CheckCode.CBONDS_IDENTITY_MISMATCH.value,
            CheckCode.CBONDS_SECTIONS_MISMATCH.value,
            CheckCode.CBONDS_ZERO_TOTAL.value,
        )
        for code, _ in failures
    )

    execute(
        _ENSURE_ORGANIZATION,
        {"inn": inn, "name": (row.get("emitent_name_rus") or "").strip() or None},
        conn=conn,
    )
    fields = report.loaded_fields()
    forms = sorted({item.form_code for item in fields.values()})
    meta = {
        "cbonds": {
            "report": report_name,
            "row_id": row.get("id"),
            "updated_at": row.get("update_time"),
            "src_updated_at": row.get("src_updated_at"),
            "reported": _reported(row, report),
            # Агрегаты объявлены у комплекта: величина шире нашей позиции,
            # и читатель документа обязан это видеть.
            "aggregates": {
                item.code: item.seen_at
                for item in fields.values()
                if item.kind == "aggregate"
            },
        }
    }
    src_file = fetch_one(
        _UPSERT_SRC_FILE,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": moment.year,
            "form_codes": forms,
            "unit_code": _unit_of(row, report),
            "status": "quarantine" if quarantined else "loaded",
            "meta": json.dumps(meta, ensure_ascii=False),
            "code_version": code_version(),
        },
        conn=conn,
    )
    assert src_file is not None
    src_file_id = int(src_file["id"])
    execute(
        _DROP_ACTUAL,
        {
            "inn": inn,
            "standard": Standard.IFRS.value,
            "report_year": moment.year,
            "keep": src_file_id,
        },
        conn=conn,
    )

    existing = {
        (item["form_code"], item["line_code"]): item
        for item in fetch_all(
            _EXISTING,
            {"inn": inn, "standard": Standard.IFRS.value, "date": moment},
            conn=conn,
        )
    }
    written = 0
    mismatches: list[str] = []
    for name, item in fields.items():
        value = number(row.get(name))
        if value is None:
            continue
        previous = existing.get((item.form_code, item.code))
        if (
            previous is not None
            and previous["recognition"] != RECOGNITION
            and previous["value"] != value
        ):
            # Величина первоисточника не затирается, но расхождение содержательно:
            # у ГК «Автодор» так виден пересмотр сравнительных данных.
            mismatches.append(
                f"{item.code}: первоисточник {previous['value']}, агрегатор {value}"
            )
        execute(
            _UPSERT_FACT,
            {
                "src_file_id": src_file_id,
                "inn": inn,
                "standard": Standard.IFRS.value,
                "report_date": moment,
                "form_code": item.form_code,
                "line_code": item.code,
                "source_line_code": name,
                "value": value,
                "period_role": PERIOD_ROLE,
                "recognition": RECOGNITION,
            },
            conn=conn,
        )
        written += 1

    outcome = LoadOutcome(
        inn=inn,
        report_date=moment,
        src_file_id=src_file_id,
        facts=written,
        quarantined=quarantined,
        failures=tuple(failures),
        mismatches=tuple(mismatches),
        checked=4,
    )
    log_records(_records(outcome, report, len(fields)), conn=conn)
    logger.info("Cbonds: %s", outcome.describe())
    return outcome


def _log_rejection(inn: str, rejection: Rejection, conn: PgConnection) -> None:
    """Отказ приёма — запись журнала: строка без комплекта не молчит."""
    log_records(
        [
            CheckRecord(
                inn=inn,
                check_code=rejection.check_code,
                status=CheckStatus.FAIL,
                severity=Severity.BLOCKING,
                message=rejection.reason,
            )
        ],
        conn=conn,
    )


def _records(
    outcome: LoadOutcome, report: ReportDef, fields: int
) -> list[CheckRecord]:
    """Записи журнала: сработавшее и пройденное вместе со знаменателем.

    **Пройденная проверка идёт в журнал наравне со сработавшей.** Иначе сводка
    комплекта, прошедшего чисто, не содержит ни одной записи, и по документу
    нельзя сказать, что именно проверено.
    """
    records: list[CheckRecord] = []
    failed = {code for code, _ in outcome.failures}
    for code, message in outcome.failures:
        records.append(
            CheckRecord(
                inn=outcome.inn,
                check_code=CheckCode(code),
                status=CheckStatus.FAIL,
                report_date=outcome.report_date,
                src_file_id=outcome.src_file_id,
                message=message,
            )
        )
    for code in (
        report.controls.identity.check,
        report.controls.sections.check,
        report.zero_total.check,
        report.controls.debt_split.check,
    ):
        if code in failed:
            continue
        records.append(
            CheckRecord(
                inn=outcome.inn,
                check_code=CheckCode(code),
                status=CheckStatus.PASS,
                report_date=outcome.report_date,
                src_file_id=outcome.src_file_id,
                message="сошлось",
            )
        )
    aggregates = sum(
        1 for item in report.fields.values() if item.kind == "aggregate" and item.loaded
    )
    records.append(
        CheckRecord(
            inn=outcome.inn,
            check_code=CheckCode.CBONDS_FIELD_MAPPING,
            status=CheckStatus.INFO,
            report_date=outcome.report_date,
            src_file_id=outcome.src_file_id,
            message=(
                f"полей справочника {fields}, величин записано {outcome.facts}, "
                f"агрегатов среди них {aggregates}"
            ),
        )
    )
    for message in outcome.mismatches:
        records.append(
            CheckRecord(
                inn=outcome.inn,
                check_code=CheckCode.CBONDS_VALUE_MISMATCH,
                status=CheckStatus.INFO,
                report_date=outcome.report_date,
                src_file_id=outcome.src_file_id,
                message=(
                    "величина агрегатора расходится с первоисточником "
                    f"и не записана: {message}"
                ),
            )
        )
    return records
