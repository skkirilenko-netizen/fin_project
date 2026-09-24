"""Приложение: таблицы показателей, контролей и версий.

Приложение собирается из базы и моделью не пишется. Оно отвечает на вопрос
«откуда это взялось»: какие показатели рассчитаны и какие нет, какие контроли
качества выполнялись, по какой версии методики и какой моделью сделан текст.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from finlib.metrics.definitions import Unit, load_metrics
from finlib.metrics.display import format_metric
from finlib.report.data import ReportData
from finlib.standards import Standard

logger = logging.getLogger(__name__)

CHECK_STATUS_NAMES: dict[str, str] = {
    "pass": "пройден",
    "fail": "провален",
    "warning": "предупреждение",
    "info": "не выполнялся",
}

UNIT_SOURCE_NAMES: dict[str, str] = {
    "form_standard": "определена формой отчётности",
    "explicit": "указана источником",
}

# Конвенция записи чисел исходного документа. Печатается наравне с единицей
# измерения и по той же причине: неверно прочитанный разделитель разрядов
# не ловится ни одним контролем сходимости — сойдётся всё, кроме самих
# величин, и ошибка будет ровно в тысячу раз. Читатель должен иметь
# возможность проверить, как прочитаны числа.
GROUPING_NAMES: dict[str, str] = {
    "russian": "разряды отделены пробелом, десятичный знак — запятая",
    "english": "разряды отделены запятой, десятичный знак — точка",
    "plain": "разделителей разрядов в документе нет",
}

# Источник отдаёт числа машиночитаемо, и разделителя разрядов у них нет вовсе.
# Молчать об этом нельзя: пустая графа читалась бы как несделанная работа.
GROUPING_NOT_APPLICABLE = (
    "не определялась: источник отдаёт числа машиночитаемо, разделителей "
    "разрядов в них нет"
)

# Наименование источника: в документ идёт название, а не машинный код.
SOURCE_NAMES: dict[str, str] = {
    "gir_bo": "Государственный информационный ресурс бухгалтерской отчётности (ГИР БО)",
    "file": "файл отчётности, загруженный вручную",
}

SEVERITY_NAMES: dict[str, str] = {
    "blocking": "блокирующий",
    "warning": "предупреждающий",
    "info": "справочный",
}

@dataclass(frozen=True, slots=True)
class Table:
    """Таблица приложения: заголовок, шапка и строки."""

    title: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str | None = None


def _catalog_of(data: ReportData):
    """Справочник показателей своего стандарта: разрядность у них разная."""
    if data.standard is Standard.IFRS:
        from finlib.metrics.ifrs_view import IfrsMetricsView

        return IfrsMetricsView()
    return load_metrics()


def metrics_table(data: ReportData) -> Table:
    """Показатели за периоды с ролью каждого в оценке.

    Сноска о причинах ставится, только если таблица причин строится:
    ссылаться на таблицу, которой в документе нет, нельзя. Ссылка идёт
    по наименованию, а не «в следующей таблице»: номер и место таблицы
    ставит сборщик документа, и соседство не гарантировано.
    """
    catalog = _catalog_of(data)
    periods = data.periods
    header = (
        "Код",
        "Показатель",
        "Группа",
        *(f"{item:%d.%m.%Y}" for item in periods),
        "Участвует в балле",
    )
    rows: list[tuple[str, ...]] = []
    shown = getattr(catalog, "shown", None)
    for metric in data.metrics:
        scale = catalog.scale_for(metric.code)
        cells = [
            # Словесная замена отрицательной величины — у справочника своего
            # стандарта: «−2,66» у покрытия процентов при убытке выглядит
            # кратностью, а означает отсутствие операционной прибыли.
            shown(metric.code, metric.values[period], data.unit_name)
            if shown is not None and metric.values.get(period) is not None
            else _value(metric.values.get(period), metric.unit, scale, data.unit_name)
            for period in periods
        ]
        rows.append(
            (metric.code, metric.name, metric.group_name, *cells, _role(metric))
        )
    return Table(
        "Показатели за периоды",
        header,
        tuple(rows),
        (
            "«—» означает, что показатель за период не рассчитан; причина "
            "по каждому периоду — в таблице «Периоды, за которые показатель "
            "не рассчитан»."
            if not_calculated_table(data) is not None
            else "«—» означает, что показатель за период не рассчитан."
        ),
    )


EXCLUSION_KIND_NAMES: dict[str, str] = {
    "stop_factor": "служит стоп-фактором",
    "no_level_scale": "нет шкалы уровня",
    "duplicate": "дублирует другой показатель",
    "no_data": "нет данных",
}


def exclusions_table(data: ReportData) -> Table | None:
    """Постоянные причины исключения показателя из балльной оценки.

    Это решение методики, действующее во всех периодах: стоп-фактор,
    отсутствие шкалы уровня, дублирование другого показателя. Периодные
    причины — в отдельной таблице: прежде они были слиты сюда в одну ячейку
    без указания периода, и по такой таблице нельзя было сказать, к чему
    причина относится.

    Порядок строк — фиксированная иерархия причин.
    """
    ordered = sorted(
        (
            item
            for item in data.metrics
            if not item.included and item.exclusion_reason and not item.missing_data
        ),
        key=lambda item: (item.exclusion_rank, item.code),
    )
    rows = tuple(
        (
            metric.code,
            metric.name,
            EXCLUSION_KIND_NAMES.get(metric.exclusion_kind or "", "—"),
            " ".join((metric.exclusion_reason or "").split()),
        )
        for metric in ordered
    )
    if not rows:
        return None
    return Table(
        "Показатели вне балльной оценки по методике",
        ("Код", "Показатель", "Вид причины", "Пояснение"),
        tuple(rows),
        "Причина постоянна и от периода не зависит: это решение методики, "
        "а не пробел в отчётности.",
    )


def not_calculated_table(data: ReportData) -> Table | None:
    """Периоды, за которые показатель не рассчитан, и причина по каждому.

    Графа «Период» обязательна: причина «нет предыдущего периода» верна
    за самый ранний период и неверна за остальные, а в слитой ячейке они
    были неразличимы.
    """
    rows: list[tuple[str, ...]] = []
    for metric in sorted(data.metrics, key=lambda item: item.code):
        for period in sorted(metric.reasons, reverse=True):
            reason = metric.reasons[period]
            if not reason:
                continue
            rows.append(
                (
                    metric.code,
                    metric.name,
                    f"{period:%d.%m.%Y}",
                    " ".join(reason.split()),
                )
            )
    if not rows:
        return None
    return Table(
        "Периоды, за которые показатель не рассчитан",
        ("Код", "Показатель", "Период", "Причина"),
        tuple(rows),
        "Нерассчитанный показатель — пробел в отчётности за этот период, "
        "а не исключение по методике.",
    )


def groups_table(data: ReportData) -> Table | None:
    """Разложение балла по группам.

    Без присвоенного класса таблица не строится: баллы групп при отсутствии
    интегрального класса складываются в число, которого в документе нет,
    и читатель неизбежно сложит его сам.
    """
    if not data.score_in_appendix or not data.groups:
        return None
    # **Рядом с весом стоит и число выпавших показателей.** Фактический вес
    # объясняется именно им: группа, у которой из четырёх показателей в балл
    # вошёл один, весит меньше номинального — и по одной графе «в балле 1»
    # не видно, было ли их четыре или один. Число считалось и хранилось
    # с самого начала (`assessment_group.metrics_excluded`), а в документ
    # не выводилось: сведение записывалось и никем не читалось.
    rows = tuple(
        (
            item["group_name"],
            _score(item["score"]),
            _percent(item["nominal_weight"]),
            _percent(item["effective_weight"]),
            str(item["metrics_used"]),
            str(item["metrics_excluded"]),
        )
        for item in data.groups
    )
    return Table(
        "Балл по группам показателей",
        (
            "Группа",
            "Балл из 100",
            "Вес номинальный",
            "Вес фактический",
            "Показателей в балле",
            "Выпало из балла",
        ),
        rows,
        "Выпавший показатель — исключённый методикой либо нерассчитанный; "
        "причина каждого названа в таблице причин исключения. Фактический вес "
        "группы отличается от номинального ровно на их долю.",
    )


def metric_scores_table(data: ReportData) -> Table | None:
    """Балл каждого показателя: уровень, динамика и на скольких точках.

    **Балл показателя на 40 % состоит из динамики, и число точек — часть
    его защиты.** Динамика, посчитанная на двух точках, и та же величина
    на пяти — разные сведения, а в документе они выглядели одинаково:
    `assessment_metric.periods_used` считался, хранился и не печатался
    нигде. Без него нельзя ни проверить балл, ни возразить ему.

    Таблица строится только при присвоенном классе — по тому же правилу,
    по которому не печатается балл групп: числа, из которых складывается
    несуществующая оценка, читатель сложит сам.
    """
    if not data.score_in_appendix:
        return None
    rows = tuple(
        (
            metric.code,
            metric.name,
            _score(metric.score),
            _score(metric.level_score),
            _score(metric.dynamics_score),
            str(metric.periods_used),
        )
        for metric in data.metrics
        if metric.included
    )
    if not rows:
        return None
    return Table(
        "Балл показателя: из чего сложился",
        (
            "Код",
            "Показатель",
            "Балл",
            "Уровень",
            "Динамика",
            "Точек в динамике",
        ),
        rows,
        "Балл показателя — 0,6 уровня и 0,4 динамики. Динамика требует "
        "минимум двух рассчитанных точек; чем их меньше, тем меньше "
        "наблюдений за ней стоит.",
    )


def checks_table(data: ReportData) -> Table:
    """Выполненные контроли качества с периодом и объектом.

    Без периода и объекта по сводке нельзя установить, какой период отбракован
    и какая строка не сошлась, — а это от неё и требуется. Контроль, который
    применяется к комплекту целиком, объекта не имеет.
    """
    rows = tuple(
        (
            item["check_code"],
            SEVERITY_NAMES.get(item["severity"], item["severity"]),
            _outcome(item),
            _period(item),
            _objects(item["line_codes"]),
            str(item["runs"]),
        )
        for item in data.checks
    )
    note = (
        "Отчётность, не прошедшая блокирующий контроль, в расчёт не идёт. "
        "Объект контроля — строки отчётности, которых он касался; "
        "«комплект целиком» означает запись о комплекте, а не о строке. "
        "Сведение — не контроль: так помечены записи о сопоставлении строк, "
        "о столкновении периодов и о перезаписи значений."
    )
    # **Записи прежних разборов называются, а не пропадают.** В сводку идут
    # записи той версии кода, которой комплект загружен: запись, порождённая
    # разбором, которого больше нет, о нынешнем извлечении не говорит.
    # Но умолчать о них нельзя — иначе неполнота сводки читается как чистота.
    superseded = data.checks_superseded or {}
    records = int(superseded.get("records") or 0)
    if records:
        # Число ставится после двоеточия намеренно: согласование числительного
        # с существительным здесь пришлось бы делать правилом языка, а от него
        # сводка не зависит.
        note += (
            f" Записей прежних версий разбора в журнале: {records} "
            f"(кодов контроля {int(superseded.get('codes') or 0)}). Они описывают "
            "извлечения, которых больше нет, и в сводку не включены."
        )
    return Table(
        "Выполненные контроли качества",
        ("Контроль", "Уровень", "Исход", "Период", "Объект контроля", "Срабатываний"),
        rows,
        note,
    )


def _period(item: dict) -> str:
    """Период записи; у записи о комплекте целиком — год комплекта.

    Прежде здесь стояло слово «комплект», и записи двух комплектов выглядели
    одной строкой, повторённой дважды: у ФосАгро мнение аудитора
    модифицировано у годового и у промежуточного, а таблица показывала два
    одинаковых ряда без всякого признака различия.
    """
    if item["report_date"]:
        return f"{item['report_date']:%d.%m.%Y}"
    year = item.get("report_year")
    return f"комплект {year} года" if year else "комплект"


def _outcome(item: dict) -> str:
    """Исход контроля; у записи журнала, а не контроля, — «сведение».

    **Графа считает то, как называется.** Сводка сопоставления строк
    и перезапись значения — записи о событии и о состоянии комплекта,
    а не контроли: «не выполнялся» в их строке утверждало, что проверка
    не состоялась, тогда как проверки и не было.
    """
    from finlib.quality.codes import JOURNAL_CODES

    if item["check_code"] in {code.value for code in JOURNAL_CODES}:
        return "сведение"
    return CHECK_STATUS_NAMES.get(item["status"], item["status"])


# Сколько кодов строк выводится в графе объекта; остальные считаются.
OBJECTS_SHOWN = 6


def _objects(codes: list[str] | None) -> str:
    """Строки, которых касался контроль, в одной ячейке."""
    if not codes:
        return "комплект"
    ordered = sorted(codes)
    if len(ordered) <= OBJECTS_SHOWN:
        return ", ".join(ordered)
    listed = ", ".join(ordered[:OBJECTS_SHOWN])
    return f"{listed} и ещё {len(ordered) - OBJECTS_SHOWN}"


def _grouping(organization: dict) -> str:
    """Как прочитаны числа исходного документа; для машинных источников — почему нет."""
    found = organization.get("digit_grouping")
    if not found:
        return GROUPING_NOT_APPLICABLE
    return GROUPING_NAMES.get(found, found)


def _recognition(organization: dict) -> str | None:
    """Чем опознаны позиции комплекта: справочником и ранее подтверждённым.

    **Две силы опознания печатаются порознь, потому что доверие к ним разное.**
    Справочник утверждает: строка с таким наименованием означает это у любого
    эмитента. Ранее подтверждённое утверждает меньше — у этого эмитента эта
    строка означает это, — и читатель обязан видеть, сколько позиций принято
    по слабейшему из двух оснований и на каком комплекте оно получено.

    Графы нет вовсе там, где опознавать было нечем: у машинного источника
    строка приходит с кодом. Ноль вместо этого читался бы как «ничего
    не опознано».
    """
    found = (organization.get("meta") or {}).get("recognition")
    if not found:
        return None
    where = ", ".join(found.get("confirmed_from") or ())
    return (
        f"Опознание позиций: справочником {found['by_catalog']}, "
        f"по ранее подтверждённому {found['by_confirmation']} "
        f"из {found['rows_total']} строк"
        + (f" (подтверждено на комплектах {where})" if where else "")
        + "."
    )


def provenance(data: ReportData, model: str, generated_at: datetime) -> list[str]:
    """Происхождение документа: версии, источник, дата."""
    assessment = data.assessment
    organization = data.organization
    forms = "упрощённый" if organization["reporting_type"] == "simplified" else "полный"
    # Формулировки-предположения здесь нет и быть не может: комплект
    # с неопределённой единицей останавливается контролем unit_not_determined
    # и до документа не доходит.
    unit = UNIT_SOURCE_NAMES.get(organization["unit_source"], organization["unit_source"])
    lines = [
        f"Дата формирования: {generated_at:%d.%m.%Y %H:%M}.",
        f"Отчётный период: {data.report_date:%d.%m.%Y}.",
        f"Стандарт отчётности: {data.standard.value.upper()}.",
        f"Набор форм: {forms}.",
        f"Единица измерения: {data.unit_name} ({unit}).",
        f"Запись чисел в исходном документе: {_grouping(organization)}.",
        *filter(None, (_recognition(organization),)),
        f"Источник данных: "
        f"{SOURCE_NAMES.get(organization['source'], organization['source'])}.",
        f"Языковая модель текстовой части: {model}.",
    ]
    if assessment is not None:
        lines.extend(
            [
                f"Версия справочника показателей: {assessment['metrics_version']}.",
                f"Версия методики оценки: {assessment['scoring_version']}.",
            ]
        )
        # **Пустая графа версии не бывает.** Флагов в ветке МСФО нет вовсе,
        # и «Версия справочника флагов: .» читалась как потерянная величина,
        # а не как отсутствие предмета. Отсутствие объявляется словами.
        version = assessment["flags_version"]
        lines.append(
            f"Версия справочника флагов: {version}."
            if version
            else "Флаги в ветке МСФО не применяются."
            if data.standard is Standard.IFRS
            else "Версия справочника флагов: не объявлена."
        )
    if data.accepted_sources:
        lines.append(f"Комплекты отчётности в расчёте: {_years(data.accepted_sources)}.")
    if data.quarantined_sources:
        lines.append(
            f"Комплекты, не прошедшие контроли качества и в расчёт не включённые: "
            f"{_years(data.quarantined_sources)}."
        )
    if data.score_in_appendix and data.stop_factor_code:
        lines.append(
            f"Балл до применения стоп-фактора: {_score(assessment['total_score'])} из 100."
        )
    return lines


def _years(sources: list[dict]) -> str:
    """Перечень комплектов по годам с номером корректировки."""
    return ", ".join(
        f"{item['report_year']} год (корректировка {item['correction_version']})"
        for item in sources
    )


def _value(
    value: Decimal | None, unit: str, scale: int, money_name: str
) -> str:
    """Значение показателя в единице и разрядности методики.

    Округление здесь не своё: оно одно на весь проект и приходит из
    metrics.yaml. Своё дало бы расхождение текста заключения с приложением.
    """
    if value is None:
        return "—"
    return format_metric(value, Unit(unit), scale, money=money_name)


def _role(metric) -> str:
    """Участвует ли показатель в балле."""
    if metric.included:
        return "да"
    return "нет — нет данных" if metric.missing_data else "нет — по методике"


def _score(value: Decimal | None) -> str:
    """Балл с двумя знаками."""
    return "—" if value is None else f"{value:.2f}".replace(".", ",")


def _percent(value: Decimal | None) -> str:
    """Доля в процентах с одним знаком."""
    if value is None:
        return "—"
    return f"{value * 100:.1f}".replace(".", ",") + " %"
