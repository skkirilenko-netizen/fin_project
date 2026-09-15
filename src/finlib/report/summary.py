"""Раздел 1 «Ключевой вывод»: собирается из базы, моделью не пишется.

Класс, балл и стоп-факторы — арифметика методики (инвариант 2), и доверять
их изложение модели нельзя. Раздел строится здесь, а постпроверка следит,
чтобы модель не переписала его в своих разделах.

Класс присваивается не всегда. Отсутствие класса — штатный исход, а не сбой:
основание может оказаться слишком узким. Тогда вместо класса приводится
причина отказа и перечень показателей, которых не хватило.
"""

import logging
from dataclasses import dataclass

from finlib.report.data import ReportData
from finlib.scoring.definitions import ScoringCatalog

logger = logging.getLogger(__name__)

CONFIDENCE_NAMES: dict[str, str] = {
    "high": "высокая",
    "medium": "средняя",
    "low": "низкая",
}


@dataclass(frozen=True, slots=True)
class Paragraph:
    """Абзац раздела с пометкой, выделять ли его."""

    text: str
    bold: bool = False


def build_summary(data: ReportData, scoring: ScoringCatalog) -> list[Paragraph]:
    """Текст раздела «Ключевой вывод»."""
    if data.assessment is None:
        return [
            Paragraph(
                "Оценка финансового состояния не рассчитана: показатели "
                "за отчётный период отсутствуют.",
                bold=True,
            )
        ]

    paragraphs = [*_verdict(data, scoring), *_stop_factors(data, scoring)]
    paragraphs.extend(_confidence(data))
    paragraphs.extend(_flags(data))
    return paragraphs


def _verdict(data: ReportData, scoring: ScoringCatalog) -> list[Paragraph]:
    """Класс и балл либо причина, по которой класс не присвоен."""
    assessment = data.assessment
    if assessment is None:
        return []

    if not data.class_code:
        found = [
            Paragraph(
                f"Класс финансового состояния не присвоен. "
                f"{assessment['no_class_reason']}.",
                bold=True,
            )
        ]
        found.extend(_missing(data, scoring))
        return found

    verdict = f"Класс финансового состояния: {data.class_code} — {assessment['class_name']}."
    if data.score_in_summary:
        verdict = f"{verdict} Балл: {_score(assessment['total_score'])} из 100."
    found = [Paragraph(verdict, bold=True)]
    if data.breadth_reason:
        # Класс присвоен стоп-фактором, а не баллом: узость основания
        # не отменяет стоп-фактор, но и балльной оценки не даёт. Два этих
        # утверждения стоят рядом, а не вместо друг друга — иначе читатель
        # видит класс E и не знает, что расчёт возможен по двум группам
        # из пяти.
        #
        # Текст breadth_reason сюда не переносится дословно: он написан
        # для случая, когда класс не присвоен, и содержит слова
        # «интегральный класс не формируется», которые рядом с присвоенным
        # классом читаются как противоречие.
        found.append(
            Paragraph(
                "Класс определён стоп-фактором и от величины балла не зависит. "
                f"Балльная оценка не формируется: {_breadth(data, scoring)}."
            )
        )
        # Счёт групп уже назван строкой выше — во втором абзаце он не нужен.
        found.extend(_missing(data, scoring, counted=True))
    return found


def _breadth(data: ReportData, scoring: ScoringCatalog) -> str:
    """Насколько узко основание: групп в расчёте против групп методики."""
    used = len([item for item in data.groups if item["score"] is not None])
    total = len(scoring.groups)
    return f"расчёт возможен по {used} группам показателей из {total}"


def _missing(
    data: ReportData, scoring: ScoringCatalog, *, counted: bool = False
) -> list[Paragraph]:
    """Чего не хватило для класса.

    Нехватка данных и исключение решением методики разведены намеренно:
    первое — пробел в отчётности, второе — наше решение, и смешивать их
    значит выдавать одно за другое.
    """
    paragraphs: list[Paragraph] = []
    missing = data.missing_metrics
    if missing:
        listed = ", ".join(f"«{item.name}»" for item in missing)
        paragraphs.append(
            Paragraph(
                f"Не рассчитаны из-за отсутствия данных в отчётности "
                f"({len(missing)}): {listed}."
            )
        )
    excluded = data.excluded_by_methodology
    if excluded:
        listed = ", ".join(f"«{item.name}»" for item in excluded)
        paragraphs.append(
            Paragraph(
                f"Не участвуют в балльной оценке по методике ({len(excluded)}): "
                f"{listed}. Это решение методики, а не пробел в отчётности; "
                f"значения показателей приведены в приложении."
            )
        )
    groups = [item for item in data.groups if item["score"] is not None]
    if groups:
        listed = ", ".join(
            f"«{item['group_name']}» ({item['metrics_used']})" for item in groups
        )
        # Баллы групп здесь не приводятся намеренно. Класс не присвоен именно
        # потому, что основание узкое, — и высокий балл единственной уцелевшей
        # группы («Рентабельность» — 94 из 100 у организации с отрицательным
        # собственным капиталом) прочитался бы как оценка состояния, которой
        # мы как раз и не даём. По той же причине их нет и в приложении.
        opening = (
            "Группы, по которым расчёт возможен"
            if counted
            else f"Расчёт возможен по {len(groups)} группам показателей "
            f"из {len(scoring.groups)}"
        )
        paragraphs.append(
            Paragraph(
                f"{opening} (в скобках — число показателей): {listed}. "
                f"Балл по ним не приводится: он описывает часть картины "
                f"и без интегральной оценки вводил бы в заблуждение."
            )
        )
    return paragraphs


def _stop_factors(data: ReportData, scoring: ScoringCatalog) -> list[Paragraph]:
    """Сработавший стоп-фактор и что он означает."""
    code = data.stop_factor_code
    if not code:
        return []
    policy = next((item for item in scoring.stop_factors if item.code == code), None)
    name = policy.name if policy else code
    statement = " ".join(policy.statement.split()) if policy else ""
    return [Paragraph(f"Сработал стоп-фактор «{name}». {statement}", bold=True)]


def _confidence(data: ReportData) -> list[Paragraph]:
    """Уверенность в оценке и чем она ограничена."""
    assessment = data.assessment
    if assessment is None:
        return []
    level = CONFIDENCE_NAMES.get(assessment["confidence"], assessment["confidence"])
    paragraphs = [Paragraph(f"Уверенность в оценке: {level}.")]
    reasons = assessment["confidence_reasons"] or []
    if reasons:
        paragraphs.append(Paragraph("Что ограничивает уверенность:"))
        paragraphs.extend(Paragraph(f"— {reason}") for reason in reasons)
    return paragraphs


def _flags(data: ReportData) -> list[Paragraph]:
    """Сработавшие флаги: готовые формулировки, сокращать нельзя."""
    if not data.flags:
        return []
    paragraphs = [Paragraph("Обстоятельства, меняющие прочтение оценки:")]
    for flag in data.flags:
        paragraphs.append(Paragraph(f"— {flag['flag_name']}. {flag['message']}"))
    return paragraphs


def _score(value) -> str:
    """Балл с двумя знаками, запятая как десятичный знак."""
    return f"{value:.2f}".replace(".", ",")
