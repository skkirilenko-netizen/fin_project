"""Разделы «Фактическая база» и «Вопросы к организации», собранные расчётом.

Оба раздела состоят из того, что уже посчитано. Состав фактической базы задан
`report.yaml`, величины приходят из расчёта с кодами; основания вопросов
ранжированы методикой по тяжести последствий, формулировки предписаны.
Модели оставалось переписать готовые перечни, и проверка 17.09.2026 показала,
что она этого не делает: в фактической базе появлялись производные вместо
величин, а из пяти вопросов три оказывались о нераскрытии строк — ровно те,
что методика запрещает, — причём в одном был введён порог, которого методика
не содержит, а в другом посчитана разность величин двух периодов.

Свобода не исчезает, она переезжает: раздел рисков забрали у модели, и
интерпретация переехала сюда. Поэтому забираются и эти два.
"""

import logging
from decimal import Decimal

from finlib.metrics.definitions import MetricsCatalog
from finlib.metrics.derived import DerivedKind
from finlib.metrics.derived import parse as parse_derived
from finlib.metrics.display import format_metric, money, percent
from finlib.normalize.lines import LinesCatalog, ReportingType
from finlib.report.data import ReportData
from finlib.report.policy import QuestionSubject, ReportPolicy
from finlib.scoring.definitions import ScoringCatalog

logger = logging.getLogger(__name__)


def _line_name(code: str, lines: LinesCatalog, reporting_type: ReportingType) -> str:
    """Наименование строки отчётности; неизвестная строка остаётся кодом."""
    line = lines.get(code, reporting_type)
    return line.name if line is not None else code


def _value_of(data: ReportData, code: str) -> Decimal | None:
    """Значение показателя за отчётный период, если он рассчитан."""
    for row in data.metric_rows:
        if row["metric_code"] == code and row["status"] == "ok":
            return row["value"]
    return None


def fact_base(
    data: ReportData,
    policy: ReportPolicy,
    lines: LinesCatalog,
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
    reporting_type: ReportingType,
) -> list[str]:
    """Раздел «Фактическая база»: обязательные величины и отобранные машинно.

    Величина, которой у организации нет, из перечня выпадает: требовать назвать
    нераскрытую строку значило бы требовать выдумать число.
    """
    facts = data.line_values
    required = data.fact_base_codes(policy)
    found: list[str] = [policy.fact_base_section.intro_text]
    named: set[str] = set()
    for code in required:
        rendered = _render(code, data, lines, catalog, reporting_type, facts)
        if rendered is None:
            continue
        found.append(rendered)
        named.add(code)

    extra = _worth_naming(
        data, policy, lines, catalog, scoring, reporting_type, named, facts
    )
    if extra:
        found.append(policy.fact_base_section.extra_intro_text)
        found.extend(extra)
    return found


def _render(
    code: str,
    data: ReportData,
    lines: LinesCatalog,
    catalog: MetricsCatalog,
    reporting_type: ReportingType,
    facts: dict[str, Decimal],
) -> str | None:
    """Строка перечня: наименование, код в скобках и величина."""
    if code.isdigit():
        value = facts.get(code)
        if value is None:
            return None
        return f"{_line_name(code, lines, reporting_type)} ({code}) — {money(value)} тыс. руб."
    metric = catalog.get(code)
    value = _value_of(data, code)
    if metric is None or value is None:
        return None
    shown = format_metric(value, metric.unit, catalog.scale_for(code))
    return f"{metric.name} ({code}) — {shown}"


def _worth_naming(
    data: ReportData,
    policy: ReportPolicy,
    lines: LinesCatalog,
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
    reporting_type: ReportingType,
    named: set[str],
    facts: dict[str, Decimal],
) -> list[str]:
    """Величины сверх обязательных, отобранные машинно, а не на глаз.

    Правило одно на всех: участие в сработавшем стоп-факторе и вхождение
    в число наибольших изменений за период.
    """
    found: list[str] = []
    seen = set(named)
    if data.stop_factor_code:
        factor = next(
            (
                item
                for item in scoring.stop_factors
                if item.code == data.stop_factor_code
            ),
            None,
        )
        for code in factor.metrics if factor is not None else ():
            if code in seen:
                continue
            rendered = _render(code, data, lines, catalog, reporting_type, facts)
            if rendered is None:
                continue
            seen.add(code)
            found.append(f"{rendered} — величина стоп-фактора")

    # Только отчётный период: в metric_value лежат производные всех периодов,
    # и без отбора в перечень наибольших изменений попадало движение
    # трёхлетней давности, противоречащее тезису о том же показателе.
    changes = [
        row
        for row in data.derived
        if row["status"] == "ok"
        and row["report_date"] == data.report_date
        and (parsed := parse_derived(row["metric_code"])) is not None
        and parsed.kind is DerivedKind.CHANGE_PCT
    ]
    changes.sort(key=lambda row: abs(row["value"]), reverse=True)
    for row in changes[: policy.fact_base.top_changes]:
        parsed = parse_derived(row["metric_code"])
        if parsed is None or parsed.base in seen:
            continue
        seen.add(parsed.base)
        name = (
            _line_name(parsed.base, lines, reporting_type)
            if parsed.base_is_line
            else (catalog.get(parsed.base).name if catalog.get(parsed.base) else parsed.base)
        )
        found.append(
            f"{name} ({parsed.base}) — изменение за период "
            f"{percent(row['value'])} % ({row['metric_code']})"
        )
    return found


def questions(
    data: ReportData,
    policy: ReportPolicy,
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
    quarantined_years: list[int],
) -> list[str]:
    """Вопросы к организации: предписанные формулировки в порядке методики.

    Порядок задан тяжестью основания, а не порядком, в каком основания пришли
    из расчёта: вопрос о нераскрытой строке стоял первым при отрицательном
    оборотном капитале в 521 млрд руб., и перечень выглядел случайным.
    """
    by_subject: dict[QuestionSubject, list[str]] = {}

    for signal in data.signals:
        subject = (
            QuestionSubject.SUPERVISORY_SIGNAL
            if signal["level"] == "supervisory"
            else QuestionSubject.ATTENTION_SIGNAL
        )
        by_subject.setdefault(subject, []).append(
            policy.questions.question(subject, name=signal["signal_name"])
        )

    if data.stop_factor_code:
        factor = next(
            (
                item
                for item in scoring.stop_factors
                if item.code == data.stop_factor_code
            ),
            None,
        )
        if factor is not None:
            by_subject.setdefault(QuestionSubject.STOP_FACTOR, []).append(
                policy.questions.question(
                    QuestionSubject.STOP_FACTOR, name=factor.name
                )
            )

    conflict = data.flag_conflict()
    if conflict is not None:
        listed = ", ".join(
            f"«{catalog.require(code).name}»"
            for code in conflict.metrics
            if catalog.get(code) is not None
        )
        if listed:
            by_subject.setdefault(QuestionSubject.FLAG_CONFLICT, []).append(
                policy.questions.question(
                    QuestionSubject.FLAG_CONFLICT, metrics=listed
                )
            )

    for year in quarantined_years:
        by_subject.setdefault(QuestionSubject.QUARANTINED_SET, []).append(
            policy.questions.question(QuestionSubject.QUARANTINED_SET, year=str(year))
        )

    for row in data.metrics:
        # Нехватка данных и исключение решением методики — разные вещи:
        # о втором спрашивать нечего, это наш выбор, а не пробел отчётности.
        if row.included or not row.missing_data:
            continue
        by_subject.setdefault(QuestionSubject.MISSING_METRIC, []).append(
            policy.questions.question(QuestionSubject.MISSING_METRIC, name=row.name)
        )

    ordered: list[str] = []
    for subject in policy.questions.subject_order:
        ordered.extend(by_subject.get(subject, []))
    if not ordered:
        return [policy.questions.none_found_text]
    # Дубли снимаются по тексту: два основания одного рода дают один вопрос.
    unique = list(dict.fromkeys(ordered))
    return unique[: policy.questions.max_count]
