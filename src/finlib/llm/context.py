"""Сборка блоков контекста для модели из БД.

Модель получает только посчитанное. Исходный файл отчётности ей не передаётся,
и ни одно число не появляется здесь иначе как из fact_report, metric_value
или assessment — вместе с кодом строки или кодом показателя (инвариант 3).

Числа подаются уже округлёнными: у модели не должно быть повода считать
самой, а постпроверка сверяет её ответ ровно с этими значениями.
"""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.metrics.definitions import MetricsCatalog, Unit, load_metrics
from finlib.metrics.derived import describe as describe_derived
from finlib.metrics.derived import parse as parse_derived
from finlib.metrics.derived import unit_of as derived_unit
from finlib.normalize.lines import LinesCatalog, ReportingType, load_lines
from finlib.quality.periods import limitations as period_limitations
from finlib.scoring.definitions import ScoringCatalog, load_scoring
from finlib.standards import Standard

logger = logging.getLogger(__name__)

_ORGANIZATION = """
SELECT o.inn, o.name, o.short_name, o.ogrn, o.okved, o.region,
       s.reporting_type, s.standard, s.unit_code, s.unit_source, s.knd
FROM organization o
JOIN src_file s ON s.inn = o.inn AND s.report_year = %(year)s AND s.is_actual
WHERE o.inn = %(inn)s
LIMIT 1
"""

_FACTS = """
SELECT report_date, form_code, line_code, source_line_code, value
FROM fact_report
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
  AND value IS NOT NULL
ORDER BY report_date DESC, form_code, line_code
"""

_METRICS = """
SELECT report_date, metric_code, value, status, confidence, reason
FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
ORDER BY metric_code, report_date DESC
"""

_ASSESSMENT = """
SELECT * FROM assessment
WHERE inn = %(inn)s AND standard = %(s)s AND report_date = %(d)s
"""
_GROUPS = "SELECT * FROM assessment_group WHERE assessment_id = %(id)s ORDER BY group_code"
_ASSESSMENT_METRICS = (
    "SELECT * FROM assessment_metric WHERE assessment_id = %(id)s ORDER BY metric_code"
)
_FLAGS = "SELECT * FROM assessment_flag WHERE assessment_id = %(id)s ORDER BY flag_code"

_PERIODS = """
SELECT DISTINCT report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s ORDER BY report_date DESC LIMIT 3
"""


@dataclass
class ConclusionContext:
    """Готовые блоки контекста и их склейка."""

    inn: str
    report_date: date
    organization: str
    data: str
    metrics: str
    flags: str
    assessment: str
    limitations: str

    def blocks(self) -> str:
        """Все блоки одной строкой — с ними же сверяется ответ модели."""
        return "\n\n".join(
            [
                self.organization,
                self.data,
                self.metrics,
                self.flags,
                self.assessment,
                self.limitations,
            ]
        )


def money(value: Decimal) -> str:
    """Денежная величина: целые тысячи рублей с разделителями разрядов."""
    rounded = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f"{rounded:,}".replace(",", " ")


def ratio(value: Decimal) -> str:
    """Коэффициент: два знака после запятой."""
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}".replace(".", ",")


def days(value: Decimal) -> str:
    """Дни: один знак после запятой."""
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}".replace(".", ",")


def percent(value: Decimal) -> str:
    """Процент: один знак после запятой, как и дни."""
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}".replace(".", ",")


def format_metric(value: Decimal, unit: Unit) -> str:
    """Значение показателя в его единице измерения."""
    if unit is Unit.THOUSAND_RUB:
        return f"{money(value)} тыс. руб."
    if unit is Unit.DAYS:
        return f"{days(value)} дн."
    if unit is Unit.PERCENT:
        return f"{percent(value)} %"
    return ratio(value)


def _periods(inn: str, conn: PgConnection | None, standard: Standard) -> list[date]:
    """До трёх последних периодов с рассчитанными показателями."""
    rows = fetch_all(_PERIODS, {"inn": inn, "standard": standard.value}, conn=conn)
    return [row["report_date"] for row in rows]


def _organization_block(
    inn: str, report_date: date, conn: PgConnection | None, catalog: LinesCatalog
) -> str:
    """Реквизиты организации и происхождение отчётности."""
    row = fetch_one(_ORGANIZATION, {"inn": inn, "year": report_date.year}, conn=conn)
    if row is None:
        raise ValueError(f"для ИНН {inn} нет отчётности за {report_date.year} год")
    kind = ReportingType(row["reporting_type"])
    lines = [
        "=== ОРГАНИЗАЦИЯ ===",
        f"ИНН: {row['inn']}",
        f"Наименование: {row['short_name'] or row['name'] or '—'}",
        f"ОГРН: {row['ogrn'] or '—'}",
        f"Основной вид деятельности (ОКВЭД): {row['okved'] or '—'}",
        f"Регион: {row['region'] or '—'}",
        f"Отчётный период: {report_date:%d.%m.%Y}",
        f"Стандарт отчётности: {'РСБУ' if row['standard'] == 'rsbu' else 'МСФО'}",
        f"Набор форм: {catalog.reporting_types[kind].name}",
        "Единица измерения: тысячи рублей"
        + (" (принята по умолчанию, источником не указана)"
           if row["unit_source"] == "assumed" else ""),
    ]
    return "\n".join(lines)


def _data_block(
    inn: str,
    periods: list[date],
    conn: PgConnection | None,
    catalog: LinesCatalog,
    standard: Standard,
    reporting_type: ReportingType,
) -> str:
    """Раскрытые строки отчётности за два последних периода."""
    dates = periods[:2]
    rows = fetch_all(
        _FACTS, {"inn": inn, "standard": standard.value, "dates": dates}, conn=conn
    )
    by_line: dict[str, dict[date, Decimal]] = {}
    for row in rows:
        by_line.setdefault(row["line_code"], {})[row["report_date"]] = row["value"]

    lines = ["=== ДАННЫЕ ОТЧЁТНОСТИ (тыс. руб.) ==="]
    header = "Код   Наименование строки" + "".join(f"  |  {d:%d.%m.%Y}" for d in dates)
    lines.append(header)
    for code in sorted(by_line):
        line = catalog.get(code, reporting_type)
        name = line.name if line is not None else "—"
        values = "  |  ".join(
            money(by_line[code][d]) if d in by_line[code] else "не раскрыто" for d in dates
        )
        lines.append(f"{code}  {name}  |  {values}")
    return "\n".join(lines)


def _metrics_block(
    inn: str,
    periods: list[date],
    conn: PgConnection | None,
    catalog: MetricsCatalog,
    standard: Standard,
    lines_catalog: LinesCatalog,
    reporting_type: ReportingType,
) -> str:
    """Показатели, производные величины и причины, по которым остальные не рассчитаны.

    Производные — изменения за период и доли в валюте баланса — идут отдельным
    перечнем после показателей: иначе полторы сотни строк заслонили бы два
    десятка собственно показателей.
    """
    rows = fetch_all(
        _METRICS, {"inn": inn, "standard": standard.value, "dates": periods}, conn=conn
    )
    by_metric: dict[str, list[dict]] = {}
    for row in rows:
        by_metric.setdefault(row["metric_code"], []).append(row)

    lines = ["=== ПОКАЗАТЕЛИ ==="]
    derived_lines: list[str] = []
    not_calculable: list[str] = []
    for code in sorted(by_metric):
        parsed = parse_derived(code)
        if parsed is not None:
            name = describe_derived(parsed, lines_catalog, catalog, reporting_type)
            if name is None:
                continue
            unit = derived_unit(parsed, catalog.get(parsed.base))
            rendered = _series(by_metric[code], unit)
            if rendered is None:
                reason = by_metric[code][0]["reason"] or "причина не указана"
                not_calculable.append(f"{code} «{name}»: {reason}")
                continue
            derived_lines.append(f"{code}  «{name}»  {rendered}")
            continue

        metric = catalog.get(code)
        if metric is None:
            continue
        rendered = _series(by_metric[code], metric.unit)
        if rendered is None:
            reason = by_metric[code][0]["reason"] or "причина не указана"
            not_calculable.append(f"{code} «{metric.name}»: {reason}")
            continue
        note = f"\n      оговорка: {' '.join(metric.note.split())}" if metric.note else ""
        lines.append(f"{code}  «{metric.name}»  {rendered}{note}")

    if derived_lines:
        lines.append("")
        lines.append("Изменения за период и структура баланса — величины готовы,")
        lines.append("считать их заново не нужно:")
        lines.extend(derived_lines)
        lines.append(f"  {' '.join(catalog.derived.share.note.split())}")

    if not_calculable:
        lines.append("")
        lines.append("Не рассчитаны:")
        lines.extend(f"  {item}" for item in not_calculable)
    return "\n".join(lines)


def _series(points: list[dict], unit: Unit) -> str | None:
    """Ряд значений по периодам; None, если ни одно не рассчитано."""
    calculated = [item for item in points if item["status"] == "ok"]
    if not calculated:
        return None
    return "  |  ".join(
        f"{item['report_date']:%d.%m.%Y}: {format_metric(item['value'], unit)}"
        for item in calculated
    )


def _assessment_block(
    assessment: dict | None, scoring: ScoringCatalog, catalog: MetricsCatalog
) -> str:
    """Класс, балл и разложение по группам."""
    lines = ["=== ОЦЕНКА ==="]
    if assessment is None:
        lines.append("Оценка не рассчитана.")
        return "\n".join(lines)

    if assessment["class_code"]:
        lines.append(f"Класс: {assessment['class_code']} — {assessment['class_name']}")
    else:
        lines.append(f"Класс не присвоен. Причина: {assessment['no_class_reason']}")

    stop = assessment["stop_factor_code"]
    if stop:
        policy = next((item for item in scoring.stop_factors if item.code == stop), None)
        lines.append(
            f"Сработал стоп-фактор «{policy.name if policy else stop}»: "
            f"{' '.join(policy.rationale.split()) if policy else ''}"
        )
        lines.append(
            "ВНИМАНИЕ: при сработавшем стоп-факторе балл в текст заключения "
            "не выносится — он приводится только в приложении."
        )
    else:
        score = assessment["total_score"]
        if score is not None:
            lines.append(f"Общий балл: {ratio(score)} из 100")

    lines.append(f"Уверенность в оценке: {assessment['confidence']}")
    lines.append("")
    lines.append("Баллы по группам:")
    for group in assessment["groups"]:
        if group["score"] is None:
            continue
        lines.append(
            f"  {group['group_name']}: {ratio(group['score'])} из 100, "
            f"вес в оценке {ratio(group['effective_weight'] * 100)} %, "
            f"показателей в расчёте {group['metrics_used']}"
        )
    _ = catalog
    return "\n".join(lines)


def _flags_block(assessment: dict | None) -> str:
    """Сработавшие флаги с готовыми формулировками."""
    lines = ["=== ФЛАГИ ==="]
    flags = assessment["flags"] if assessment else []
    if not flags:
        lines.append("Флагов не сработало.")
        return "\n".join(lines)
    for flag in flags:
        lines.append(f"[{flag['level']}] {flag['flag_name']}")
        lines.append(f"  {flag['message']}")
    return "\n".join(lines)


def _limitations_block(
    inn: str,
    conn: PgConnection | None,
    scoring: ScoringCatalog,
    catalog: MetricsCatalog,
    assessment: dict | None,
    standard: Standard,
) -> str:
    """Ограничения анализа: готовые формулировки, которые нельзя сокращать."""
    lines = ["=== ОГРАНИЧЕНИЯ АНАЛИЗА ==="]
    notes: list[str] = [" ".join(scoring.calibration_points.limitation_note.split())]
    notes.extend(period_limitations(inn, conn, standard))

    if assessment is not None:
        for metric in assessment["metrics"]:
            if not metric["included"] and metric["exclusion_reason"]:
                continue  # причины исключения приведены в блоке ПОКАЗАТЕЛИ
        if assessment["confidence_reasons"]:
            notes.extend(assessment["confidence_reasons"])

    used = {item["metric_code"] for item in (assessment["metrics"] if assessment else [])}
    for code in sorted(used):
        metric = catalog.get(code)
        if metric is not None and metric.note:
            notes.append(f"{metric.name}: {' '.join(metric.note.split())}")

    lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


def build_context(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    lines_catalog: LinesCatalog | None = None,
    metrics_catalog: MetricsCatalog | None = None,
    scoring: ScoringCatalog | None = None,
) -> ConclusionContext:
    """Собирает контекст заключения по организации."""
    lines_catalog = lines_catalog if lines_catalog is not None else load_lines()
    metrics_catalog = metrics_catalog if metrics_catalog is not None else load_metrics()
    scoring = scoring if scoring is not None else load_scoring()

    periods = _periods(inn, conn, standard)
    if not periods:
        raise ValueError(f"для ИНН {inn} нет рассчитанных показателей")
    target = report_date or periods[0]

    header = fetch_one(
        _ASSESSMENT, {"inn": inn, "s": standard.value, "d": target}, conn=conn
    )
    assessment: dict | None = None
    if header is not None:
        params = {"id": header["id"]}
        assessment = dict(header)
        assessment["groups"] = fetch_all(_GROUPS, params, conn=conn)
        assessment["metrics"] = fetch_all(_ASSESSMENT_METRICS, params, conn=conn)
        assessment["flags"] = fetch_all(_FLAGS, params, conn=conn)

    row = fetch_one(_ORGANIZATION, {"inn": inn, "year": target.year}, conn=conn)
    reporting_type = ReportingType(row["reporting_type"]) if row else ReportingType.FULL

    return ConclusionContext(
        inn=inn,
        report_date=target,
        organization=_organization_block(inn, target, conn, lines_catalog),
        data=_data_block(inn, periods, conn, lines_catalog, standard, reporting_type),
        metrics=_metrics_block(
            inn, periods, conn, metrics_catalog, standard, lines_catalog, reporting_type
        ),
        flags=_flags_block(assessment),
        assessment=_assessment_block(assessment, scoring, metrics_catalog),
        limitations=_limitations_block(
            inn, conn, scoring, metrics_catalog, assessment, standard
        ),
    )
