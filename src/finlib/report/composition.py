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
from finlib.standards import Standard

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

    Вступление берётся по стандарту отчётности: кодов строк, утверждённых
    нормативным актом, консолидированная отчётность не содержит, и обещать
    их читателю нельзя.
    """
    facts = data.line_values
    required = data.fact_base_codes(policy)
    found: list[str] = [policy.fact_base_section.intro_text(data.standard)]
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
    """Строка перечня: наименование и величина; у строки отчётности — с кодом.

    Код строки остаётся: для бухгалтерской отчётности «(1600)» — привычная
    ссылка, по которой читатель находит величину в самой отчётности. Код
    показателя не остаётся: `debt_total` и `nwc` — внутренние идентификаторы
    методики, и в тексте документа им места нет. Показатель опознаётся
    по наименованию, и правило состава ищет его так же.
    """
    if code.isdigit():
        value = facts.get(code)
        if value is None:
            return None
        return (
            f"{_line_name(code, lines, reporting_type)} ({code}) — "
            f"{money(value)} {data.unit_name}"
        )
    if code.startswith("ifrs."):
        return _render_ifrs_line(code, data, facts)
    if data.standard is Standard.IFRS:
        return _render_ifrs_metric(code, data)
    metric = catalog.get(code)
    value = _value_of(data, code)
    if metric is None or value is None:
        return None
    shown = format_metric(
        value, metric.unit, catalog.scale_for(code), money=data.unit_name
    )
    return f"{metric.name} — {shown}"


def _render_ifrs_line(
    code: str, data: ReportData, facts: dict[str, Decimal]
) -> str | None:
    """Статья консолидированной отчётности: наименованием, а не кодом.

    Кодов строк, утверждённых нормативным актом, консолидированная отчётность
    не содержит, и статья опознаётся позицией унифицированной модели — код
    её внутренний, тексту документа чужой.

    **Величина из примечания называет примечание и его строку.** Без этого
    покрытие процентов не совпадает ни с одной строкой отчёта о прибыли или
    убытке, и читатель не понимает почему: у Автодора в форме 414, а начислено
    54 382; у Норникеля строка формы объявлена очищенной от капитализированных
    процентов.
    """
    from finlib.normalize.ifrs_lines import load_ifrs_lines
    from finlib.normalize.ifrs_note_lines import load_note_lines

    value = facts.get(code)
    if value is None:
        return None
    position = load_ifrs_lines().get(code)
    name = position.name if position is not None else None
    if name is None:
        note_line = next(
            (item for item in load_note_lines().lines if item.code == code), None
        )
        name = note_line.name if note_line is not None else code
    shown = f"{name} — {money(value)} {data.unit_name}"
    reference = data.line_notes.get(code)
    if reference is None:
        return shown
    number, rows = reference
    where = f"примечание {number}" + (f", «{rows}»" if rows else "")
    return f"{shown} ({where})"


def _render_ifrs_metric(code: str, data: ReportData) -> str | None:
    """Показатель МСФО: наименование и величина в единице методики МСФО."""
    from finlib.metrics.definitions import Unit
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics

    value = _value_of(data, code)
    if value is None:
        return None
    metric = next(
        (item for item in load_ifrs_metrics().metrics if item.code == code), None
    )
    if metric is None:
        return None
    # Единица методики МСФО названа своими словами: «currency» означает
    # величину отчётности, то есть те же тысячи рублей, «ratio» — отношение.
    unit = Unit.THOUSAND_RUB if metric.unit == "currency" else Unit.RATIO
    scale = 0 if unit is Unit.THOUSAND_RUB else 3
    return f"{metric.name} — {format_metric(value, unit, scale, money=data.unit_name)}"


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
    for row in changes[: policy.fact_base_of(data.standard).top_changes]:
        parsed = parse_derived(row["metric_code"])
        if parsed is None or parsed.base in seen:
            continue
        seen.add(parsed.base)
        # Строка отчётности называется с кодом, показатель — одним
        # наименованием. Код производной величины не печатается вовсе:
        # `2330_chg_pct` читателю не говорит ничего, а величина изменения
        # названа словами рядом.
        title = _title_of(parsed.base, data, lines, catalog, reporting_type)
        if title is None:
            # Наименования нет ни в одном справочнике — печатать код нельзя:
            # технический идентификатор в тексте запрещён, и правило поймало бы
            # именно его («ifrs.other_current_assets — изменение за период»).
            continue
        found.append(
            f"{title} — изменение за период {percent(row['value'])} %"
        )
    return found


def _title_of(
    base: str,
    data: ReportData,
    lines: LinesCatalog,
    catalog: MetricsCatalog,
    reporting_type: ReportingType,
) -> str | None:
    """Как величина называется в тексте; None — наименования нет.

    Строка РСБУ называется с кодом — «(1600)» привычная ссылка; статья МСФО
    и показатель — одним наименованием, потому что их коды внутренние. Кода
    производной в тексте нет вовсе: «2330_chg_pct» читателю не говорит ничего,
    а величина изменения названа словами рядом.
    """
    if len(base) == 4 and base.isdigit():
        return f"{_line_name(base, lines, reporting_type)} ({base})"
    if base.startswith("ifrs."):
        from finlib.normalize.ifrs_lines import load_ifrs_lines
        from finlib.normalize.ifrs_note_lines import load_note_lines

        position = load_ifrs_lines().get(base)
        if position is not None:
            return position.name
        note_line = next(
            (item for item in load_note_lines().lines if item.code == base), None
        )
        return note_line.name if note_line is not None else None
    metric = catalog.get(base)
    if metric is not None:
        return metric.name
    _ = data
    return None


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
