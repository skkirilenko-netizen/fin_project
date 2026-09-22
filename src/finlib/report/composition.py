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
from finlib.quality.refusals import Kind as RefusalKind
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
    """Показатель МСФО: наименование и величина так, как она печатается.

    Округление и словесная замена отрицательной величины берутся из одного
    места (`IfrsMetricsView.shown`): набирать число здесь значило бы завести
    второй способ его напечатать.
    """
    from finlib.metrics.ifrs_view import IfrsMetricsView

    value = _value_of(data, code)
    if value is None:
        return None
    view = IfrsMetricsView()
    metric = view.get(code)
    if metric is None:
        return None
    return f"{metric.name} — {view.shown(code, value, money=data.unit_name)}"


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
        # Стоп-фактор ищется в справочнике своего стандарта: коды общие,
        # а перечень величин у ветки МСФО свой.
        from finlib.report.data import stop_factor_of

        factor = stop_factor_of(data.stop_factor_code, data.standard)
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
    #
    # **Наибольшее — по существенности, а не по проценту.** Процент измеряет
    # изменение относительно собственной прошлой величины, и наверх выносит
    # мелочь: у Левенгука первым стоял итог раздела I с 9 778,1 %, у ФосАгро —
    # налог на прибыль к возмещению с 11 901 %. Существенность изменения —
    # его доля в размере организации: валюта баланса для статей на дату,
    # выручка для статей за период. База объявлена методикой по формам,
    # и у строки потока её нет вовсе — такая статья в перечень не идёт.
    changes = [
        row
        for row in data.derived
        if row["status"] == "ok"
        and row["report_date"] == data.report_date
        and (parsed := parse_derived(row["metric_code"])) is not None
        and parsed.kind is DerivedKind.CHANGE_PCT
    ]
    # **База существенности и равная ей строка в перечень не идут.** Доля
    # изменения базы в себе самой равна единице, и такая строка стоит наверху
    # у каждого эмитента, не говоря о нём ничего: у Сегежи «Итого капитал
    # и обязательства» заняло вторую строку — итог пассива, то есть та же
    # валюта баланса, которую обязательный состав уже назвал. Перечень
    # объявлен методикой своего стандарта, а не выведен здесь.
    bases = _base_codes(data, lines)
    weights = {
        row["metric_code"]: None
        if (parsed := parse_derived(row["metric_code"])) is not None
        and parsed.base in bases
        else _materiality_of(row, data, lines, reporting_type)
        for row in changes
    }
    measured = [row for row in changes if weights[row["metric_code"]] is not None]
    measured.sort(key=lambda row: weights[row["metric_code"]], reverse=True)
    top = policy.fact_base_of(data.standard).top_changes
    listed = 0
    for row in measured:
        if listed >= top:
            break
        parsed = parse_derived(row["metric_code"])
        # **Отсечка считается после отбора, а не до него.** Самыми
        # существенными оказываются сами базы — валюта баланса и выручка, —
        # а они и так названы обязательным составом: выбирая первые пять
        # до отбрасывания названных, перечень терял по три статьи из пяти.
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
        # **Величина изменения печатается рядом с процентом.** Перечень
        # упорядочен по существенности, то есть по абсолютному изменению
        # к размеру организации, и без самой величины читателю не видно,
        # почему статья стоит выше другой с большим процентом.
        absolute = _absolute_change(parsed.base, data)
        shown = f"{title} — изменение за период {percent(row['value'])} %"
        if absolute is not None:
            shown += f", на {money(absolute)} {data.unit_name}"
        found.append(shown)
        listed += 1
    return found


def _absolute_change(base: str, data: ReportData) -> Decimal | None:
    """Абсолютное изменение статьи за отчётный период; None — не посчитано."""
    return next(
        (
            row["value"]
            for row in data.derived
            if row["status"] == "ok"
            and row["report_date"] == data.report_date
            and (parsed := parse_derived(row["metric_code"])) is not None
            and parsed.kind is DerivedKind.CHANGE_ABS
            and parsed.base == base
        ),
        None,
    )


def _materiality_of(
    row: dict, data: ReportData, lines: LinesCatalog, reporting_type: ReportingType
) -> Decimal | None:
    """Доля абсолютного изменения статьи в базе её формы; None — не измеряется.

    Базы объявлены методикой по формам, и у каждого стандарта своим
    справочником: у РСБУ — `lines.yaml`, у МСФО — `ifrs_lines.yaml`. Считается
    одно и то же, а вопрос базы у них разный по устройству: у РСБУ форма
    известна из справочника строк, у МСФО — из позиции.

    Не измеряется в трёх случаях, и все три означают одно — ранжировать нечем:
    у формы нет базы (строка потока), база не раскрыта, изменения в абсолютной
    величине нет. Показатель-отношение базы не имеет вовсе: его динамика
    приведена в разделе интерпретации вместе с уровнями.
    """
    parsed = parse_derived(row["metric_code"])
    if parsed is None:
        return None
    absolute = _absolute_change(parsed.base, data)
    if absolute is None:
        return None
    base_code = _materiality_base(parsed.base, data, lines, reporting_type)
    if base_code is None:
        return None
    base_value = data.line_values.get(base_code)
    if not base_value:
        return None
    return abs(absolute) / abs(base_value)


def _base_codes(data: ReportData, lines: LinesCatalog) -> frozenset[str]:
    """Базы существенности и равные им строки — по справочнику стандарта."""
    if data.standard is Standard.IFRS:
        from finlib.normalize.ifrs_lines import load_ifrs_lines

        return load_ifrs_lines().materiality.base_codes
    return lines.materiality.base_codes


def _materiality_base(
    code: str, data: ReportData, lines: LinesCatalog, reporting_type: ReportingType
) -> str | None:
    """Код строки-базы существенности для статьи; None — базы нет."""
    if data.standard is Standard.IFRS:
        from finlib.normalize.ifrs_lines import load_ifrs_lines

        catalog = load_ifrs_lines()
        position = catalog.get(code)
        if position is None:
            return None
        declared = catalog.materiality.bases.get(position.form)
        return declared.base if declared is not None else None
    line = lines.get(code, reporting_type)
    if line is None:
        return None
    return lines.materiality.base_of(line.form)


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


def _plans_note(data: ReportData) -> tuple[bool, int | None]:
    """Объявлена ли неопределённость непрерывности и где раскрыты планы.

    Двое, а не одно: раздела в заключении может не быть вовсе — тогда вопроса
    не возникает, — а быть он может без ссылки на примечание, и тогда вопрос
    задаётся, но номера не называет. Свести их в одно значило бы либо
    промолчать о планах, либо выдумать номер.
    """
    from finlib.normalize.ifrs_audit import load_audit_policy
    from finlib.sources.ifrs_audit import audit_from_meta

    audit = audit_from_meta(data.organization.get("meta"))
    if audit is None:
        return False, None
    policy = load_audit_policy()
    if policy.plans_reference.section not in audit.sections:
        return False, None
    return True, audit.plans_note(policy)


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

    # Сигнал заключения — такое же основание вопроса, как сигнал
    # по показателю: уровень объявлен методикой, и место в очереди у него
    # то же. Прежде о нём вопроса не возникало, и «Запрос пояснений» отсылал
    # к разделу вопросов, в котором стояло «расчётом не выявлено».
    levelled = [(item["level"], item["signal_name"]) for item in data.signals]
    levelled += [(item.level, item.name) for item in data.audit_signals]
    for level, name in levelled:
        subject = (
            QuestionSubject.SUPERVISORY_SIGNAL
            if level == "supervisory"
            else QuestionSubject.ATTENTION_SIGNAL
        )
        by_subject.setdefault(subject, []).append(
            policy.questions.question(subject, name=name)
        )

    # **Планы руководства — единственный вопрос о будущем.** Аудитор объявил
    # существенную неопределённость и указал, что планы раскрыты, а их
    # исполнимости не оценивал: это вопрос к организации, и без него документ
    # фиксирует сомнение аудитора, ничего у организации не спрашивая.
    declared, note = _plans_note(data)
    if declared:
        by_subject.setdefault(QuestionSubject.GOING_CONCERN_PLANS, []).append(
            policy.questions.question(
                QuestionSubject.GOING_CONCERN_PLANS,
                where=policy.questions.plans_where_text(note),
            )
        )

    if data.stop_factor_code:
        from finlib.report.data import stop_factor_of

        factor = stop_factor_of(data.stop_factor_code, data.standard)
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
        # **Наш пробел вопросом не становится.** Семейство отказа объявлено
        # методикой (`refusals.yaml`): из `data_missing` следует запрос,
        # из `our_gap` и `not_applicable` — прямое «запрашивать нечего».
        # Прежде документ говорил об одном показателе двумя голосами:
        # «Ограничения» — «величина есть в отчётности, извлечение за нами»,
        # а «Вопросы» просили у организации расшифровки того же показателя.
        if row.refusal_kind(data.report_date) is not RefusalKind.DATA_MISSING:
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
