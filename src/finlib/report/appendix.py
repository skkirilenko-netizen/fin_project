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


def metrics_table(data: ReportData) -> Table:
    """Показатели за периоды с ролью каждого в оценке.

    Сноска о причинах ставится, только если таблица причин строится:
    ссылаться на таблицу, которой в документе нет, нельзя. Ссылка идёт
    по наименованию, а не «в следующей таблице»: номер и место таблицы
    ставит сборщик документа, и соседство не гарантировано.
    """
    catalog = load_metrics()
    periods = data.periods
    header = (
        "Код",
        "Показатель",
        "Группа",
        *(f"{item:%d.%m.%Y}" for item in periods),
        "Участвует в балле",
    )
    rows: list[tuple[str, ...]] = []
    for metric in data.metrics:
        scale = catalog.scale_for(metric.code)
        cells = [
            _value(metric.values.get(period), metric.unit, scale) for period in periods
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
    rows = tuple(
        (
            item["group_name"],
            _score(item["score"]),
            _percent(item["nominal_weight"]),
            _percent(item["effective_weight"]),
            str(item["metrics_used"]),
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
        ),
        rows,
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
            CHECK_STATUS_NAMES.get(item["status"], item["status"]),
            f"{item['report_date']:%d.%m.%Y}" if item["report_date"] else "комплект",
            _objects(item["line_codes"]),
            str(item["runs"]),
        )
        for item in data.checks
    )
    return Table(
        "Выполненные контроли качества",
        ("Контроль", "Уровень", "Исход", "Период", "Объект контроля", "Срабатываний"),
        rows,
        "Отчётность, не прошедшая блокирующий контроль, в расчёт не идёт. "
        "Объект контроля — строки отчётности, которых он касался; "
        "«комплект» означает контроль комплекта целиком.",
    )


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
        f"Источник данных: "
        f"{SOURCE_NAMES.get(organization['source'], organization['source'])}.",
        f"Языковая модель текстовой части: {model}.",
    ]
    if assessment is not None:
        lines.extend(
            [
                f"Версия справочника показателей: {assessment['metrics_version']}.",
                f"Версия методики оценки: {assessment['scoring_version']}.",
                f"Версия справочника флагов: {assessment['flags_version']}.",
            ]
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


def _value(value: Decimal | None, unit: str, scale: int) -> str:
    """Значение показателя в единице и разрядности методики.

    Округление здесь не своё: оно одно на весь проект и приходит из
    metrics.yaml. Своё дало бы расхождение текста заключения с приложением.
    """
    if value is None:
        return "—"
    return format_metric(value, Unit(unit), scale)


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
