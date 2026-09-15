"""Выборка всего, что нужно заключению, одним обращением к базе.

Документ собирается из того же, из чего собирался контекст модели: оценки,
разложения, показателей и журнала качества. Разница в том, что здесь читается
и то, что модели не передавалось, — причины исключения показателей и перечень
выполненных контролей: они идут в приложение, которое модель не пишет.
"""

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.metrics.definitions import EXCLUSION_ORDER, ExclusionKind
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Показатель исключён из балла из-за нехватки данных, а не решением методики.
# Формулировку ставит scoring/metric_score.py; здесь она опознаётся, чтобы
# отделить неполноту отчётности от исключения по методике (CLAUDE.md).
NOT_CALCULATED_MARK = "не рассчитан"

_ORGANIZATION = """
SELECT o.inn, o.name, o.short_name, o.ogrn, o.okved, o.region,
       s.reporting_type, s.standard, s.unit_code, s.unit_source, s.knd,
       s.correction_version, s.source
FROM organization o
JOIN src_file s ON s.inn = o.inn AND s.report_year = %(year)s
                AND s.standard = %(standard)s AND s.is_actual
WHERE o.inn = %(inn)s
LIMIT 1
"""

_ASSESSMENT = """
SELECT * FROM assessment
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = %(d)s
"""

_GROUPS = """
SELECT * FROM assessment_group WHERE assessment_id = %(id)s
ORDER BY nominal_weight DESC, group_code
"""

_METRICS = """
SELECT * FROM assessment_metric WHERE assessment_id = %(id)s
ORDER BY group_code, metric_code
"""

_FLAGS = "SELECT * FROM assessment_flag WHERE assessment_id = %(id)s ORDER BY flag_code"

_PERIODS = """
SELECT DISTINCT report_date FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s ORDER BY report_date DESC LIMIT 3
"""

_METRIC_VALUES = """
SELECT report_date, metric_code, value, status, confidence, reason, reason_code
FROM metric_value
WHERE inn = %(inn)s AND standard = %(standard)s AND report_date = ANY(%(dates)s)
ORDER BY metric_code, report_date DESC
"""

# Контроли качества по комплектам организации. Считается каждый прогон
# контроля, а не строка отчётности: приложение показывает, что выполнялось.
_CHECKS = """
SELECT d.check_code, d.severity, d.status, count(*) AS runs
FROM dq_log d
JOIN src_file s ON s.id = d.src_file_id
WHERE d.inn = %(inn)s AND s.standard = %(standard)s AND s.is_actual
GROUP BY d.check_code, d.severity, d.status
ORDER BY d.check_code, d.severity, d.status
"""

# Комплекты отчётности вместе со статусом. Карантин отбирается здесь, а не
# в запросе: приложение обязано назвать и принятые комплекты, и отбракованные,
# иначе «Ограничения» и «Происхождение документа» противоречат друг другу.
_SOURCES = """
SELECT report_year, reporting_type, correction_version, status, knd, loaded_at,
       quarantine_reason
FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND is_actual
ORDER BY report_year DESC
"""


@dataclass(frozen=True, slots=True)
class MetricRow:
    """Показатель в приложении: значения по периодам и роль в оценке."""

    code: str
    name: str
    unit: str
    group_name: str
    values: dict[date, Decimal | None]
    reasons: dict[date, str | None]
    included: bool
    score: Decimal | None
    level_score: Decimal | None
    dynamics_score: Decimal | None
    exclusion_reason: str | None
    exclusion_kind: str | None

    @property
    def missing_data(self) -> bool:
        """Исключён из-за нехватки данных, а не решением методики."""
        if self.exclusion_kind is not None:
            return self.exclusion_kind == ExclusionKind.NO_DATA.value
        return bool(
            self.exclusion_reason and NOT_CALCULATED_MARK in self.exclusion_reason
        )

    @property
    def exclusion_rank(self) -> int:
        """Место причины в иерархии: стоп-фактор, шкала, дублирование, данные."""
        if self.exclusion_kind is None:
            return len(EXCLUSION_ORDER)
        return ExclusionKind(self.exclusion_kind).rank


@dataclass
class ReportData:
    """Всё, что нужно документу, прочитанное один раз."""

    inn: str
    report_date: date
    standard: Standard
    organization: dict
    unit_name: str
    assessment: dict | None
    groups: list[dict] = field(default_factory=list)
    metrics: list[MetricRow] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    periods: list[date] = field(default_factory=list)
    derived: list[dict] = field(default_factory=list)
    metric_rows: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    @property
    def class_code(self) -> str | None:
        """Присвоенный класс; None — если основание оказалось недостаточным."""
        return self.assessment["class_code"] if self.assessment else None

    def text_context(self, lines_catalog, reporting_type, catalog):
        """Собирает контекст для контроля утверждений текста.

        Проверка опирается на расчёт, а не на то, как текст выглядит: строки
        набора форм, показатели с отменённым знаменателем, показатели в днях
        и неприменимые шаблонные блоки берутся отсюда.
        """
        from finlib.llm.textcheck import TextContext
        from finlib.metrics.definitions import Unit

        known = frozenset(
            code
            for code in {item.code for item in lines_catalog.lines}
            if lines_catalog.has(code, reporting_type)
        )
        refused = {
            row["metric_code"]: catalog.require(row["metric_code"]).name
            for row in self.metric_rows
            if row["reason_code"] in ("negative_denominator", "sign_change")
            and catalog.get(row["metric_code"]) is not None
        }
        days = frozenset(
            item.name for item in catalog.metrics if item.unit is Unit.DAYS
        )
        return TextContext(
            known_lines=known,
            refused_metrics=refused,
            days_metrics=days,
            forbidden_templates=self.forbidden_templates(catalog),
        )

    def forbidden_templates(self, catalog) -> dict[str, str]:
        """Шаблонные блоки, условие применения которых не выполнено.

        Оговорка показателя идёт в заключение, только если показатель
        участвовал в расчёте. Прежде шаблонный блок печатался без проверки
        применимости: у организации с положительным капиталом документ
        разъяснял, чем плох отрицательный.
        """
        used = {item.code for item in self.metrics}
        found: dict[str, str] = {}
        for metric in catalog.metrics:
            if metric.code in used or not metric.note:
                continue
            found[" ".join(metric.note.split())] = (
                f"показатель «{metric.name}» в расчёте не участвовал"
            )
        return found

    def scale_of(self, code: str) -> int:
        """Разрядность отображения показателя: одна на весь документ."""
        from finlib.metrics.definitions import load_metrics

        return load_metrics().scale_for(code)

    @property
    def breadth_reason(self) -> str | None:
        """Почему балльная оценка не формируется; None — основание достаточно."""
        return self.assessment.get("breadth_reason") if self.assessment else None

    @property
    def stop_factor_code(self) -> str | None:
        """Код сработавшего стоп-фактора."""
        return self.assessment["stop_factor_code"] if self.assessment else None

    @property
    def score_in_appendix(self) -> bool:
        """Приводится ли балл в приложении.

        Без присвоенного класса балл не приводится **нигде** — ни в разделе 1,
        ни в приложении. При отрицательном собственном капитале он бывает
        высоким: у организации с крошечным балансом коэффициенты вырождаются,
        и «балл 67, класс не присвоен» читается как противоречие, хотя
        арифметика верна. Балл без класса ничего не сообщает и вводит
        в заблуждение.
        """
        if self.assessment is None or self.assessment["total_score"] is None:
            return False
        # Класс, присвоенный стоп-фактором при узком основании, балла
        # не раскрывает: балльной оценки просто нет, и число рядом с классом
        # читалось бы как её итог.
        if self.assessment.get("breadth_reason"):
            return False
        return bool(self.class_code)

    @property
    def score_in_summary(self) -> bool:
        """Приводится ли балл в разделе «Ключевой вывод».

        При сработавшем стоп-факторе — нет: класс определён стоп-фактором,
        а не баллом, и соседство «балл 85, класс E» подрывает доверие
        к оценке. В приложении балл при этом остаётся, с пометкой
        «до применения стоп-фактора».
        """
        return self.score_in_appendix and not self.stop_factor_code

    @property
    def accepted_sources(self) -> list[dict]:
        """Комплекты, принятые в расчёт."""
        return [item for item in self.sources if item["status"] != "quarantine"]

    @property
    def quarantined_sources(self) -> list[dict]:
        """Комплекты, отбракованные контролями качества.

        Прежде приложение перечисляло их среди принятых, и «Происхождение
        документа» противоречило разделу «Ограничения анализа», где тот же
        комплект назван невключённым.
        """
        return [item for item in self.sources if item["status"] == "quarantine"]

    @property
    def missing_metrics(self) -> list[MetricRow]:
        """Показатели, не вошедшие в балл из-за нехватки данных."""
        return [item for item in self.metrics if item.missing_data]

    @property
    def excluded_by_methodology(self) -> list[MetricRow]:
        """Показатели, исключённые решением методики, а не нехваткой данных.

        Порядок — фиксированная иерархия причин: стоп-фактор, отсутствие шкалы
        уровня, дублирование с другим показателем, отсутствие данных.
        """
        found = [
            item
            for item in self.metrics
            if not item.included and not item.missing_data and item.exclusion_reason
        ]
        return sorted(found, key=lambda item: (item.exclusion_rank, item.code))


def load_report_data(
    inn: str,
    conn: PgConnection | None = None,
    *,
    report_date: date | None = None,
    standard: Standard = Standard.RSBU,
    catalog=None,
    scoring=None,
) -> ReportData:
    """Читает из базы всё, что понадобится документу."""
    from finlib.metrics.definitions import load_metrics
    from finlib.normalize.lines import load_lines
    from finlib.scoring.definitions import load_scoring

    catalog = catalog if catalog is not None else load_metrics()
    scoring = scoring if scoring is not None else load_scoring()

    params = {"inn": inn, "standard": standard.value}
    periods = [row["report_date"] for row in fetch_all(_PERIODS, params, conn=conn)]
    if not periods:
        raise ValueError(f"для ИНН {inn} нет рассчитанных показателей")
    target = report_date or periods[0]

    organization = fetch_one(
        _ORGANIZATION, {"inn": inn, "year": target.year, "standard": standard.value}, conn=conn
    )
    if organization is None:
        raise ValueError(f"для ИНН {inn} нет комплекта отчётности за {target.year} год")

    header = fetch_one(
        _ASSESSMENT, {"inn": inn, "standard": standard.value, "d": target}, conn=conn
    )
    groups: list[dict] = []
    flags: list[dict] = []
    scored: dict[str, dict] = {}
    if header is not None:
        by_id = {"id": header["id"]}
        groups = fetch_all(_GROUPS, by_id, conn=conn)
        flags = fetch_all(_FLAGS, by_id, conn=conn)
        scored = {row["metric_code"]: row for row in fetch_all(_METRICS, by_id, conn=conn)}

    values = fetch_all(
        _METRIC_VALUES, {**params, "dates": periods}, conn=conn
    )
    by_metric: dict[str, list[dict]] = {}
    for row in values:
        by_metric.setdefault(row["metric_code"], []).append(row)

    group_names = {code: item.name for code, item in scoring.groups.items()}
    metrics = [
        _metric_row(code, by_metric[code], scored.get(code), catalog, group_names)
        for code in sorted(by_metric)
        if catalog.get(code) is not None
    ]

    return ReportData(
        inn=inn,
        report_date=target,
        standard=standard,
        organization=dict(organization),
        unit_name=load_lines().units.name,
        assessment=dict(header) if header is not None else None,
        groups=groups,
        metrics=metrics,
        flags=flags,
        periods=periods,
        metric_rows=values,
        derived=[
            row
            for row in values
            if row["status"] == "ok" and catalog.get(row["metric_code"]) is None
        ],
        checks=fetch_all(_CHECKS, params, conn=conn),
        sources=fetch_all(_SOURCES, params, conn=conn),
    )


def _metric_row(
    code: str, points: list[dict], scored: dict | None, catalog, group_names: dict[str, str]
) -> MetricRow:
    """Собирает строку приложения по одному показателю."""
    metric = catalog.require(code)
    return MetricRow(
        code=code,
        name=metric.name,
        unit=metric.unit.value,
        group_name=group_names.get(metric.group, metric.group),
        values={
            item["report_date"]: item["value"] if item["status"] == "ok" else None
            for item in points
        },
        reasons={
            item["report_date"]: item["reason"] if item["status"] != "ok" else None
            for item in points
        },
        included=bool(scored and scored["included"]),
        score=scored["score"] if scored else None,
        level_score=scored["level_score"] if scored else None,
        dynamics_score=scored["dynamics_score"] if scored else None,
        exclusion_reason=scored["exclusion_reason"] if scored else None,
        exclusion_kind=scored["exclusion_kind"] if scored else None,
    )
