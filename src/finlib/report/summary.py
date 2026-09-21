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
from datetime import datetime

from finlib.quality.codes import check_name
from finlib.report.data import ReportData
from finlib.report.policy import load_policy
from finlib.scoring.definitions import ScoringCatalog
from finlib.standards import Standard

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


def build_summary(
    data: ReportData,
    scoring: ScoringCatalog,
    generated_at: datetime | None = None,
) -> list[Paragraph]:
    """Текст раздела «Ключевой вывод».

    generated_at нужен для оговорки об актуальности данных: разрыв между
    отчётной датой и днём формирования документа — обстоятельство документа,
    а не расчёта, и вычислить его можно только здесь.
    """
    if data.assessment is None:
        return [
            Paragraph(
                "Оценка финансового состояния не рассчитана: показатели "
                "за отчётный период отсутствуют.",
                bold=True,
            )
        ]

    paragraphs = [*_verdict(data, scoring), *_stop_factors(data, scoring)]
    paragraphs.extend(_flag_conflict(data))
    paragraphs.extend(_blocking_checks(data))
    paragraphs.extend(_freshness(data, generated_at))
    paragraphs.extend(_confidence(data, scoring))
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
    if data.score_in_summary:
        # Класс ступенчат по своей природе, и рядом с ним приводится шкала:
        # иначе читателю не видно, насколько балл далёк от соседней ступени.
        found.append(Paragraph(f"Соответствие балла классу: {_scale(scoring)}."))
    found.extend(_class_before_stop(data, scoring))
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
    # Формулировка берётся из справочника **своего** стандарта: коды у РСБУ
    # и МСФО одни, а тексты разные, и чужой текст приметы не имеет — правило
    # чистоты стандарта его не поймает.
    from finlib.report.data import stop_factor_of

    policy = stop_factor_of(code, data.standard)
    name = policy.name if policy else code
    statement = policy.statement if policy else ""
    # **Ограничение слабее присвоенного класса действующим не называется.**
    # У Сегежи класс E, а формулировка обещает ограничение классом D:
    # обстоятельство в силе, ограничение — нет, и «класс ограничен
    # неустойчивым состоянием» рядом с «класс E» противоречит само себе.
    from finlib.report.data import cap_is_weaker
    from finlib.report.policy import load_policy

    if policy is not None and cap_is_weaker(policy.cap, data):
        statement = f"{statement} {load_policy().risks.cap_not_binding_text}"
    return [Paragraph(f"Сработал стоп-фактор «{name}». {statement}", bold=True)]


def _scale(scoring: ScoringCatalog) -> str:
    """Шкала соответствия балла классу, как она задана методикой."""
    parts = [f"{item.code} — от {_bound(item.min_score)}" for item in scoring.classes]
    return ", ".join(parts) + " (граница относится к старшему классу)"


def _bound(value) -> str:
    """Граница класса без хвостовых нулей: «80», а не «80,00»."""
    return format(value.normalize(), "f").replace(".", ",")


def _class_before_stop(
    data: ReportData, scoring: ScoringCatalog
) -> list[Paragraph]:
    """Класс до и после применения стоп-фактора.

    Без этого не видно, что именно сделал стоп-фактор: класс E у организации,
    набравшей по баллу класс B, и класс E у организации, набравшей E, — разные
    сведения, а в документе выглядели одинаково.
    """
    assessment = data.assessment
    if assessment is None or not data.stop_factor_code:
        return []
    before = assessment.get("class_before_stop")
    if not before or before == data.class_code:
        return []
    name = scoring.require_class(before).name
    return [
        Paragraph(
            f"До применения стоп-фактора расчёт давал класс {before} — "
            f"{name.lower()}; стоп-фактор изменил его на {data.class_code}."
        )
    ]


def _flag_conflict(data: ReportData) -> list[Paragraph]:
    """Столкновение флага и стоп-фактора, построенного на его показателях.

    Стоп-фактор не смягчается: флаг, отменяющий стоп-фактор, был бы путём
    обхода оценки. Но и молчать о столкновении нельзя — оно означает, что
    оценка построена на показателях, прочтение которых сам же расчёт
    поставил под вопрос.
    """
    conflict = data.flag_conflict()
    if conflict is None:
        return []
    return [Paragraph(conflict.message, bold=True)]


def _blocking_checks(data: ReportData) -> list[Paragraph]:
    """Провал блокирующего контроля качества.

    Отбракованный комплект в расчёт не идёт, и читатель обязан узнать об этом
    из «Ключевого вывода», а не из приложения.
    """
    failures = data.blocking_failures
    if not failures:
        return []
    # В «Ключевом выводе» стоит наименование контроля, а не его код: код —
    # механизм проверки, его место в приложении и в журнале качества.
    listed = ", ".join(sorted({f"«{check_name(item['check_code'])}»" for item in failures}))
    periods = sorted(
        {
            f"{item['report_date']:%d.%m.%Y}"
            for item in failures
            if item["report_date"] is not None
        }
    )
    where = f" Затронутые отчётные даты: {', '.join(periods)}." if periods else ""
    return [
        Paragraph(
            f"Блокирующие контроли качества дали отказ: {listed}. Отчётность, "
            f"не прошедшая такой контроль, в расчёт не включена, и выводы "
            f"опираются на оставшиеся периоды.{where}",
            bold=True,
        )
    ]


def _freshness(data: ReportData, generated_at: datetime | None) -> list[Paragraph]:
    """Оговорка о разрыве между отчётной датой и днём формирования документа."""
    if generated_at is None:
        return []
    policy = load_policy().freshness
    months = data.months_since_report(generated_at)
    if not policy.stale(months):
        return []
    return [Paragraph(policy.message(months), bold=True)]


def _confidence(data: ReportData, scoring: ScoringCatalog) -> list[Paragraph]:
    """Уверенность в оценке, порядок её определения и чем она ограничена."""
    assessment = data.assessment
    if assessment is None:
        return []
    level = CONFIDENCE_NAMES.get(assessment["confidence"], assessment["confidence"])
    paragraphs = [Paragraph(f"Уверенность в оценке: {level}.")]
    paragraphs.append(Paragraph(_confidence_rule(scoring, data.standard)))
    reasons = assessment["confidence_reasons"] or []
    if reasons:
        paragraphs.append(Paragraph("Что ограничивает уверенность:"))
        paragraphs.extend(Paragraph(f"— {reason}") for reason in reasons)
    return paragraphs


def _confidence_rule(scoring: ScoringCatalog, standard: Standard) -> str:
    """Как получается уверенность: порядок, а не результат.

    Прежде в документе стояло одно слово — «средняя», — и откуда оно взялось,
    читателю было неоткуда узнать.

    **Порядок свой у каждого стандарта.** В основаниях РСБУ стоят флаги
    и длина ряда, которых ветка МСФО не считает вовсе: перечень РСБУ
    в заключении по МСФО назвал бы читателю основания, ни одно из которых
    не проверялось.
    """
    if standard is Standard.IFRS:
        from finlib.normalize.ifrs_metrics import load_ifrs_metrics

        return " ".join(load_ifrs_metrics().confidence.rule_text.split())
    grounds = "; ".join(
        " ".join(rule.description.split()).rstrip(".")
        for rule in scoring.confidence.downgrade_on
    )
    return (
        "Уверенность определяется числом оснований для понижения: без "
        "оснований — высокая, при одном — средняя, при двух и более — низкая. "
        f"Основания заданы методикой: {grounds}. Узость основания оценки "
        "понижает уверенность отдельно, по числу показателей и групп."
    )


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
