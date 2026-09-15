"""Приложение: таблицы показателей, контролей и версий.

Приложение собирается из базы и моделью не пишется. Оно отвечает на вопрос
«откуда это взялось»: какие показатели рассчитаны и какие нет, какие контроли
качества выполнялись, по какой версии методики и какой моделью сделан текст.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from finlib.llm.context import format_metric
from finlib.metrics.definitions import Unit, load_metrics
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
    ссылаться на таблицу, которой в документе нет, нельзя.
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
            "указана в следующей таблице."
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


def not_calculated_table(data: ReportData) -> Table | None:
    """Почему показатель не рассчитан или не вошёл в балл.

    Порядок строк — фиксированная иерархия причин. Прежде причина была одним
    статическим текстом на показатель и печаталась без проверки применимости:
    у организации с положительным капиталом документ разъяснял, чем плох
    отрицательный.
    """
    ordered = sorted(
        (item for item in data.metrics if not item.included),
        key=lambda item: (item.exclusion_rank, item.code),
    )
    rows: list[tuple[str, ...]] = []
    for metric in ordered:
        kind = EXCLUSION_KIND_NAMES.get(metric.exclusion_kind or "", "—")
        reasons = sorted({item for item in metric.reasons.values() if item})
        text = (
            "; ".join(reasons)
            if reasons
            else " ".join((metric.exclusion_reason or "").split())
        )
        if not text:
            continue
        rows.append((metric.code, metric.name, kind, text))
    if not rows:
        return None
    return Table(
        "Показатели вне балльной оценки и причины",
        ("Код", "Показатель", "Вид причины", "Пояснение"),
        tuple(rows),
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
    """Выполненные контроли качества."""
    rows = tuple(
        (
            item["check_code"],
            SEVERITY_NAMES.get(item["severity"], item["severity"]),
            CHECK_STATUS_NAMES.get(item["status"], item["status"]),
            str(item["runs"]),
        )
        for item in data.checks
    )
    return Table(
        "Выполненные контроли качества",
        ("Контроль", "Уровень", "Исход", "Срабатываний"),
        rows,
        "Отчётность, не прошедшая блокирующий контроль, в расчёт не идёт.",
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
        f"Источник данных: {organization['source']}.",
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
