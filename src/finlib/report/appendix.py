"""Приложение: таблицы показателей, контролей и версий.

Приложение собирается из базы и моделью не пишется. Оно отвечает на вопрос
«откуда это взялось»: какие показатели рассчитаны и какие нет, какие контроли
качества выполнялись, по какой версии методики и какой моделью сделан текст.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from finlib.report.data import ReportData

logger = logging.getLogger(__name__)

CHECK_STATUS_NAMES: dict[str, str] = {
    "pass": "пройден",
    "fail": "провален",
    "warning": "предупреждение",
    "info": "не выполнялся",
}

SEVERITY_NAMES: dict[str, str] = {
    "blocking": "блокирующий",
    "warning": "предупреждающий",
    "info": "справочный",
}

UNIT_SUFFIX: dict[str, str] = {
    "thousand_rub": " тыс. руб.",
    "days": " дн.",
    "percent": " %",
    "ratio": "",
}


@dataclass(frozen=True, slots=True)
class Table:
    """Таблица приложения: заголовок, шапка и строки."""

    title: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str | None = None


def metrics_table(data: ReportData) -> Table:
    """Показатели за периоды с ролью каждого в оценке."""
    periods = data.periods
    header = (
        "Код",
        "Показатель",
        "Группа",
        *(f"{item:%d.%m.%Y}" for item in periods),
        "В балле",
    )
    rows: list[tuple[str, ...]] = []
    for metric in data.metrics:
        cells = [
            _value(metric.values.get(period), metric.unit, metric.reasons.get(period))
            for period in periods
        ]
        rows.append(
            (metric.code, metric.name, metric.group_name, *cells, _role(metric))
        )
    return Table(
        "Таблица 1. Показатели за периоды",
        header,
        tuple(rows),
        "«—» означает, что показатель за период не рассчитан; причина указана "
        "в таблице 2.",
    )


def not_calculated_table(data: ReportData) -> Table | None:
    """Почему показатель не рассчитан или не вошёл в балл."""
    rows: list[tuple[str, ...]] = []
    for metric in data.metrics:
        reasons = sorted({item for item in metric.reasons.values() if item})
        if reasons:
            rows.append((metric.code, metric.name, "; ".join(reasons)))
        elif not metric.included and metric.exclusion_reason:
            rows.append(
                (metric.code, metric.name, " ".join(metric.exclusion_reason.split()))
            )
    if not rows:
        return None
    return Table(
        "Таблица 2. Показатели вне балльной оценки и причины",
        ("Код", "Показатель", "Причина"),
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
        "Таблица 3. Балл по группам показателей",
        ("Группа", "Балл из 100", "Вес номинальный", "Вес фактический", "Показателей"),
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
        "Таблица 4. Выполненные контроли качества",
        ("Контроль", "Уровень", "Исход", "Срабатываний"),
        rows,
        "Отчётность, не прошедшая блокирующий контроль, в расчёт не идёт.",
    )


def provenance(data: ReportData, model: str, generated_at: datetime) -> list[str]:
    """Происхождение документа: версии, источник, дата."""
    assessment = data.assessment
    organization = data.organization
    forms = "упрощённый" if organization["reporting_type"] == "simplified" else "полный"
    unit = (
        "принята как предположение"
        if organization["unit_source"] == "assumed"
        else "указана источником"
    )
    lines = [
        f"Дата формирования: {generated_at:%d.%m.%Y %H:%M}.",
        f"Отчётный период: {data.report_date:%d.%m.%Y}.",
        f"Стандарт отчётности: {data.standard.value.upper()}.",
        f"Набор форм: {forms}.",
        f"Единица измерения: тыс. руб. ({unit}).",
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
    if data.sources:
        listed = ", ".join(
            f"{item['report_year']} год (корректировка {item['correction_version']})"
            for item in data.sources
        )
        lines.append(f"Комплекты отчётности в расчёте: {listed}.")
    if data.score_in_appendix and data.stop_factor_code:
        lines.append(
            f"Балл до применения стоп-фактора: {_score(assessment['total_score'])} из 100."
        )
    return lines


def _value(value: Decimal | None, unit: str, reason: str | None) -> str:
    """Значение показателя в его единице измерения либо прочерк."""
    if value is None:
        return "—"
    _ = reason
    if unit == "thousand_rub":
        rendered = f"{value.quantize(Decimal(1), rounding=ROUND_HALF_UP):,}".replace(
            ",", " "
        )
    elif unit in ("days", "percent"):
        rendered = f"{value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}".replace(
            ".", ","
        )
    else:
        rendered = f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}".replace(
            ".", ","
        )
    return f"{rendered}{UNIT_SUFFIX.get(unit, '')}"


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
