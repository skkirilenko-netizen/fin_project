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
from decimal import Decimal

from finlib.db import PgConnection, fetch_all, fetch_one
from finlib.metrics.definitions import MetricsCatalog, Unit, load_metrics
from finlib.metrics.derived import describe as describe_derived
from finlib.metrics.derived import parse as parse_derived
from finlib.metrics.derived import unit_of as derived_unit
from finlib.metrics.display import round_to
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

# Сигналы передаются наименованием и уровнем, без величин: их формулировки
# детерминированы и попадают в документ сами, а числа из них в блоках модели
# не нужны — она обязана опираться на показатели, а не пересказывать сигнал.
_SIGNALS = """
SELECT signal_code, signal_name, level FROM assessment_signal
WHERE assessment_id = %(id)s
ORDER BY CASE level WHEN 'supervisory' THEN 0 ELSE 1 END, signal_code
"""

_QUARANTINED = """
SELECT report_year FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND is_actual
  AND status = 'quarantine'
ORDER BY report_year
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
    # Состав разделов: какие величины обязаны быть названы в фактической базе
    # и по каким основаниям задаются вопросы. Перечни машинные, порядок —
    # из methodology/report.yaml, а не на усмотрение модели.
    composition: str = ""

    def blocks(self) -> str:
        """Все блоки одной строкой — с ними же сверяется ответ модели."""
        return "\n\n".join(
            item
            for item in [
                self.organization,
                self.data,
                self.metrics,
                self.flags,
                self.assessment,
                self.composition,
                self.limitations,
            ]
            if item
        )


# Оформление единиц измерения. Разрядность приходит из методики
# (metrics.yaml, блок display): округление в проекте одно на всех, иначе
# текст заключения и приложение расходятся между собой.
UNIT_SUFFIX: dict[Unit, str] = {
    Unit.THOUSAND_RUB: " тыс. руб.",
    Unit.DAYS: " дн.",
    Unit.PERCENT: " %",
    Unit.RATIO: "",
}


def _digits(value: Decimal, scale: int) -> str:
    """Число с разделителями разрядов и запятой как десятичным знаком."""
    return f"{round_to(value, scale):,}".replace(",", " ").replace(".", ",")


def money(value: Decimal) -> str:
    """Денежная величина: целые тысячи рублей с разделителями разрядов."""
    return _digits(value, 0)


def ratio(value: Decimal) -> str:
    """Коэффициент: два знака после запятой."""
    return _digits(value, 2)


def days(value: Decimal) -> str:
    """Дни: один знак после запятой."""
    return _digits(value, 1)


def percent(value: Decimal) -> str:
    """Процент: один знак после запятой, как и дни."""
    return _digits(value, 1)


def format_metric(value: Decimal, unit: Unit, scale: int | None = None) -> str:
    """Значение показателя в его единице измерения и разрядности методики."""
    if scale is None:
        scale = load_metrics().display.scale_for(unit)
    return f"{_digits(value, scale)}{UNIT_SUFFIX.get(unit, '')}"


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
        # Оговорки о предположении здесь нет: единица определена формой
        # отчётности, а комплект с неопределённой единицей до расчёта
        # не доходит — его останавливает контроль unit_not_determined.
        f"Единица измерения: {catalog.units.name}",
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
        # Оговорок здесь нет намеренно: в блок ПОКАЗАТЕЛИ идёт только
        # фактическое состояние показателя у этой организации — значение
        # либо причина, по которой он не рассчитан. Оговорки о содержании
        # показателя собраны в блоке ОГРАНИЧЕНИЯ АНАЛИЗА; рядом со значением
        # модель читает их как утверждение о самой организации.
        lines.append(f"{code}  «{metric.name}»  {rendered}")

    if derived_lines:
        lines.append("")
        lines.append("Изменения за период и структура баланса — величины готовы,")
        lines.append("считать их заново не нужно:")
        lines.extend(derived_lines)

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
    if assessment.get("breadth_reason"):
        lines.append(
            f"Балльная оценка не формируется: {assessment['breadth_reason']}"
        )

    stop = assessment["stop_factor_code"]
    if stop:
        policy = next((item for item in scoring.stop_factors if item.code == stop), None)
        # В промпт идёт statement — что стоп-фактор означает для этой
        # организации. Поле rationale объясняет устройство методики
        # («бывает штатным режимом при быстром обороте») и рядом с оценкой
        # читалось бы как оправдание положения именно этой организации.
        lines.append(
            f"Сработал стоп-фактор «{policy.name if policy else stop}»: "
            f"{' '.join(policy.statement.split()) if policy else ''}"
        )
        lines.append(
            "ВНИМАНИЕ: при сработавшем стоп-факторе балл в текст заключения "
            "не выносится — он приводится только в приложении."
        )
    else:
        score = assessment["total_score"]
        # Балл подаётся только вместе с классом. Без класса он ничего
        # не сообщает: «балл 74, класс не присвоен» читается как противоречие,
        # а у организации с отрицательным капиталом — как оправдание.
        if score is not None and assessment["class_code"] and not assessment.get("breadth_reason"):
            lines.append(f"Общий балл: {ratio(score)} из 100")

    lines.append(f"Уверенность в оценке: {assessment['confidence']}")
    lines.append("")
    scored = [item for item in assessment["groups"] if item["score"] is not None]
    if not assessment["class_code"] or assessment.get("breadth_reason"):
        # Без класса баллы групп не подаются. Класс не присвоен именно потому,
        # что основание узкое, и высокий балл единственной уцелевшей группы
        # прочитался бы как оценка состояния, которой мы не даём.
        listed = ", ".join(
            f"{item['group_name']} (показателей {item['metrics_used']})"
            for item in scored
        )
        lines.append(
            f"Расчёт оказался возможен только по группам: {listed or 'нет'}. "
            "Баллы групп не приводятся: балльная оценка не сформирована."
        )
    else:
        lines.append("Баллы по группам:")
        for group in scored:
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


def _line_notes(
    inn: str,
    periods: list[date],
    conn: PgConnection | None,
    lines_catalog: LinesCatalog,
    standard: Standard,
    reporting_type: ReportingType,
) -> list[str]:
    """Оговорки справочника по строкам, раскрытым у этой организации.

    Оговорка, оставшаяся только в справочнике, считается потерянной: строка
    участвует в расчёте и в тексте заключения, а ограничение её содержания
    читателю не видно.
    """
    rows = fetch_all(
        _FACTS, {"inn": inn, "standard": standard.value, "dates": periods[:2]}, conn=conn
    )
    notes: list[str] = []
    for code in sorted({row["line_code"] for row in rows}):
        line = lines_catalog.get(code, reporting_type)
        if line is not None and line.note:
            notes.append(f"{line.name} (строка {code}): {' '.join(line.note.split())}")
    return notes


def _composition_block(
    inn: str,
    periods: list[date],
    conn: PgConnection | None,
    lines_catalog: LinesCatalog,
    catalog: MetricsCatalog,
    scoring: ScoringCatalog,
    reporting_type: ReportingType,
    assessment: dict | None,
    standard: Standard,
) -> str:
    """Обязательный состав фактической базы и основания вопросов.

    Состав раздела не может зависеть от того, что модель сочтёт заслуживающим
    упоминания: по ПАО «Газпром» раздел не содержал ни совокупного долга,
    ни выручки, зато содержал сведения о нераскрытии одной строки. Перечень
    задан методикой, порядок оснований вопросов — тяжестью последствий.
    """
    from finlib.report.policy import QuestionSubject, load_policy

    policy = load_policy()
    target = periods[0]
    facts = fetch_all(
        _FACTS, {"inn": inn, "standard": standard.value, "dates": [target]}, conn=conn
    )
    disclosed = {row["line_code"]: row["value"] for row in facts}
    values = fetch_all(
        _METRICS, {"inn": inn, "standard": standard.value, "dates": [target]}, conn=conn
    )
    calculated = {
        row["metric_code"]: row["value"] for row in values if row["status"] == "ok"
    }

    lines = ["=== СОСТАВ РАЗДЕЛОВ ==="]
    lines.append("Раздел 2 «Фактическая база» обязан назвать эти величины,")
    lines.append("каждую с её кодом в скобках:")
    for code in policy.fact_base.required(
        frozenset(disclosed), frozenset(calculated)
    ):
        if code in disclosed:
            line = lines_catalog.get(code, reporting_type)
            name = line.name if line is not None else "—"
            lines.append(f"  {code}  «{name}»  {money(disclosed[code])} тыс. руб.")
            continue
        metric = catalog.require(code)
        lines.append(
            f"  {code}  «{metric.name}»  "
            f"{format_metric(calculated[code], metric.unit, catalog.scale_for(code))}"
        )

    subjects = _question_subjects(
        inn, conn, scoring, catalog, assessment, standard, policy
    )
    if subjects:
        lines.append("")
        lines.append(
            f"Раздел 6 «Вопросы к организации»: основания перечислены в порядке "
            f"убывания связанного риска, по одному вопросу на основание, "
            f"не больше {policy.questions.max_count}:"
        )
        lines.extend(f"  {number}. {text}" for number, text in enumerate(subjects, 1))
    _ = QuestionSubject
    return "\n".join(lines)


def _question_subjects(
    inn: str,
    conn: PgConnection | None,
    scoring: ScoringCatalog,
    catalog: MetricsCatalog,
    assessment: dict | None,
    standard: Standard,
    policy,
) -> list[str]:
    """Основания вопросов в порядке, заданном методикой.

    Ранжирование наше, а не модели: вопрос о нераскрытии строки стоял первым
    при отрицательном оборотном капитале в 521 млрд руб., и перечень выглядел
    случайным. Числа в основания не подставляются — они есть в блоках выше.
    """
    from finlib.report.policy import QuestionSubject
    from finlib.scoring.definitions import load_flags

    signals: list[dict] = []
    if assessment is not None:
        signals = fetch_all(_SIGNALS, {"id": assessment["id"]}, conn=conn)
    quarantined = fetch_all(
        _QUARANTINED, {"inn": inn, "standard": standard.value}, conn=conn
    )
    factor = None
    if assessment is not None and assessment["stop_factor_code"]:
        factor = next(
            (
                item
                for item in scoring.stop_factors
                if item.code == assessment["stop_factor_code"]
            ),
            None,
        )

    by_subject: dict[QuestionSubject, list[str]] = {}
    for signal in signals:
        kind = (
            QuestionSubject.SUPERVISORY_SIGNAL
            if signal["level"] == "supervisory"
            else QuestionSubject.ATTENTION_SIGNAL
        )
        by_subject.setdefault(kind, []).append(
            f"надзорный сигнал «{signal['signal_name']}»"
            if kind is QuestionSubject.SUPERVISORY_SIGNAL
            else f"обстоятельство, требующее внимания: «{signal['signal_name']}»"
        )
    if factor is not None:
        by_subject.setdefault(QuestionSubject.STOP_FACTOR, []).append(
            f"сработавший стоп-фактор «{factor.name}»"
        )
        flags_catalog = load_flags()
        for row in assessment["flags"] if assessment else []:
            flag = flags_catalog.get(row["flag_code"])
            if flag is None or not flag.conflicts_with(factor):
                continue
            shared = ", ".join(
                f"«{catalog.require(code).name}»"
                for code in flag.conflicts_with(factor)
                if catalog.get(code) is not None
            )
            by_subject.setdefault(QuestionSubject.FLAG_CONFLICT, []).append(
                f"показатели, попавшие и под флаг «{flag.name}», "
                f"и под стоп-фактор: {shared}"
            )
    for row in quarantined:
        by_subject.setdefault(QuestionSubject.QUARANTINED_SET, []).append(
            f"комплект отчётности за {row['report_year']} год, "
            f"не прошедший контроли качества"
        )
    for metric in assessment["metrics"] if assessment else []:
        if metric["included"] or metric["exclusion_kind"] != "no_data":
            continue
        definition = catalog.get(metric["metric_code"])
        if definition is None:
            continue
        by_subject.setdefault(QuestionSubject.MISSING_METRIC, []).append(
            f"показатель «{definition.name}», не рассчитанный из-за нехватки данных"
        )

    ordered: list[str] = []
    for subject in policy.questions.subject_order:
        ordered.extend(by_subject.get(subject, []))
    return ordered[: policy.questions.max_count]


def _limitations_block(
    inn: str,
    periods: list[date],
    conn: PgConnection | None,
    scoring: ScoringCatalog,
    catalog: MetricsCatalog,
    lines_catalog: LinesCatalog,
    reporting_type: ReportingType,
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

    # Оговорка о содержании показателя — безусловная, то есть верная для любой
    # организации. Условные формулировки живут в methodology_note и в промпт
    # не идут: рядом с посчитанным значением модель выдаёт их за факт.
    used = {item["metric_code"] for item in (assessment["metrics"] if assessment else [])}
    for code in sorted(used):
        metric = catalog.get(code)
        if metric is not None and metric.note:
            notes.append(f"{metric.name}: {' '.join(metric.note.split())}")
    notes.extend(
        _line_notes(inn, periods, conn, lines_catalog, standard, reporting_type)
    )
    notes.append(" ".join(catalog.derived.share.note.split()))

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
        composition=_composition_block(
            inn,
            periods,
            conn,
            lines_catalog,
            metrics_catalog,
            scoring,
            reporting_type,
            assessment,
            standard,
        ),
        limitations=_limitations_block(
            inn,
            periods,
            conn,
            scoring,
            metrics_catalog,
            lines_catalog,
            reporting_type,
            assessment,
            standard,
        ),
    )
