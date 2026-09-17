"""Приём файла консолидированной отчётности: определение параметров документа.

До извлечения чисел определяются параметры, каждый детерминированно и каждый
с отказом при неопределённости. Порядок не произволен: каждый следующий
имеет смысл только после предыдущего.

**Тип документа проверяется до всего остального.** Годовой отчёт эмитента
на триста страниц финансовой отчётностью не является, но числа в нём есть,
они осмысленны, и любой параметр в нём «определится»: найдётся и валюта,
и единица, и разделитель разрядов. Документ пройдёт приём и превратится
в комплект, которого не существует. Проверено дорого — однажды вместо
отчётности загрузились пять годовых отчётов.

Каждый отказ называет код контроля и причину человеческими словами. Файл,
не ставший комплектом, фактов не порождает: в базу писать нечего, и причина
уходит в журнал, когда организация известна.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_numbers import (
    Grouping,
    GroupingDetection,
    ParsingPolicy,
    detect_grouping,
    load_parsing_policy,
)

logger = logging.getLogger(__name__)


class ReportingKind(StrEnum):
    """Вид отчётности: от него зависит состав раскрытий и оговорки анализа."""

    FULL = "full"
    INTERIM = "interim"
    SPECIAL_PURPOSE = "special_purpose"
    DISCLOSABLE = "disclosable"


@dataclass(frozen=True, slots=True)
class Rejection:
    """Отказ принять документ: код контроля и причина словами."""

    code: CheckCode
    reason: str
    details: dict[str, object] | None = None

    @property
    def accepted(self) -> bool:
        """Принят ли документ; у отказа — нет."""
        return False


@dataclass(frozen=True, slots=True)
class DocumentProfile:
    """Параметры принятого документа.

    Все шесть определены; ни одного значения по умолчанию здесь нет, кроме
    вида отчётности, где умолчание объявлено методикой и неопасно: полная
    годовая отчётность маркеров не несёт, а прочие виды объявляют себя сами.
    """

    forms: tuple[str, ...]
    currency: str
    unit_code: str
    grouping: Grouping
    report_dates: tuple[date, ...]
    reporting_kind: ReportingKind
    grouping_detection: GroupingDetection

    @property
    def accepted(self) -> bool:
        """Принят ли документ."""
        return True

    def describe(self) -> str:
        """Однострочная сводка для журнала."""
        dates = ", ".join(f"{item:%d.%m.%Y}" for item in self.report_dates)
        return (
            f"формы: {len(self.forms)}, валюта {self.currency}, единица "
            f"{self.unit_code}, {self.grouping_detection.describe()}, "
            f"периоды: {dates}, вид отчётности: {self.reporting_kind.value}"
        )


# Дата в шапке таблицы: «31 декабря 2024 года», «31.12.2024».
_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_LONG_DATE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})", re.IGNORECASE
)
_SHORT_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


def identify(
    text: str,
    catalog: IfrsCatalog | None = None,
    policy: ParsingPolicy | None = None,
) -> DocumentProfile | Rejection:
    """Определяет параметры документа либо отказывается его принимать.

    Порядок проверок — часть правила, а не деталь: текстовый слой, тип
    документа, периметр методики, валюта, единица, разделитель разрядов,
    отчётные даты, вид отчётности.
    """
    catalog = catalog or load_ifrs_lines()
    policy = policy or load_parsing_policy()
    lowered = normalize_name(text)

    if len(text.strip()) < policy.text_layer.min_characters:
        return Rejection(
            CheckCode.FILE_TEXT_LAYER_MISSING,
            policy.text_layer.reason,
            {"characters": len(text.strip())},
        )

    forms = _forms_in(text, catalog)
    missing = set(policy.document_kind.required_forms) - set(forms)
    if len(forms) < policy.document_kind.min_forms or missing:
        return Rejection(
            CheckCode.FILE_NOT_STATEMENTS,
            policy.document_kind.reasons["not_statements"],
            {"forms_found": list(forms)},
        )

    institution = _financial_institution(lowered, policy)
    if institution is not None:
        return Rejection(
            CheckCode.FINANCIAL_INSTITUTION,
            policy.financial_institution.reasons["financial_institution"],
            {"marker": institution},
        )

    foreign = _foreign_currency(lowered, policy)
    if foreign is not None:
        return Rejection(
            CheckCode.FILE_CURRENCY_NOT_ROUBLE,
            policy.currency.reasons["not_rouble"],
            {"currency": foreign},
        )
    if not any(marker in lowered for marker in policy.currency.rouble_markers):
        return Rejection(
            CheckCode.FILE_CURRENCY_NOT_DETERMINED,
            policy.currency.reasons["not_determined"],
        )

    unit = _unit(lowered, policy)
    if unit is None:
        return Rejection(
            CheckCode.UNIT_NOT_DETERMINED, policy.units.reasons["not_determined"]
        )

    detection = detect_grouping(text, policy.digit_grouping)
    if not detection.determined:
        reason = policy.digit_grouping.reasons[detection.reason]
        return Rejection(
            CheckCode.DIGIT_GROUPING_NOT_DETERMINED,
            reason,
            {"detection": detection.describe()},
        )

    dates = _report_dates(text, policy)
    if not dates:
        return Rejection(
            CheckCode.FILE_PERIODS_NOT_DETERMINED,
            policy.periods.reasons["not_determined"],
        )

    profile = DocumentProfile(
        forms=forms,
        currency="RUB",
        unit_code=unit,
        grouping=detection.convention,
        report_dates=dates,
        reporting_kind=_reporting_kind(lowered, policy),
        grouping_detection=detection,
    )
    logger.info("документ принят: %s", profile.describe())
    return profile


def _forms_in(text: str, catalog: IfrsCatalog) -> tuple[str, ...]:
    """Коды форм, заголовки которых найдены в документе.

    Заголовки и синонимы берутся из справочника статей: перечень один
    на всю ветку, и расходиться ему не с чем.
    """
    lowered = normalize_name(text)
    found = [
        code
        for code, form in catalog.forms.items()
        if any(name in lowered for name in form.match_names)
    ]
    return tuple(found)


def _financial_institution(lowered: str, policy: ParsingPolicy) -> str | None:
    """Маркер финансовой организации, если он есть.

    Неклассифицированный баланс сам по себе признаком не считается: у него
    много причин, а вот «чистые инвестиции в лизинг» означают ровно одно.
    Отсутствие деления на оборотные и внеоборотные усиливает маркер,
    но не заменяет его.
    """
    for marker in policy.financial_institution.markers:
        if normalize_name(marker) in lowered:
            return marker
    return None


def _foreign_currency(lowered: str, policy: ParsingPolicy) -> str | None:
    """Валюта, отличная от рубля, если она объявлена в шапке."""
    for marker, code in policy.currency.foreign_markers.items():
        if normalize_name(marker) in lowered:
            return code
    return None


def _unit(lowered: str, policy: ParsingPolicy) -> str | None:
    """Код ОКЕИ единицы измерения по шапке формы."""
    for marker, code in policy.units.markers.items():
        if normalize_name(marker) in lowered:
            return code
    return None


def _report_dates(text: str, policy: ParsingPolicy) -> tuple[date, ...]:
    """Отчётные даты документа, от свежей к ранней.

    Число периодов переменное: Норникель даёт три, остальные разобранные
    эмитенты два. Модель принимает N периодов, и число берётся из документа,
    а не задаётся заранее.
    """
    found: set[date] = set()
    for match in _LONG_DATE.finditer(text):
        day, month, year = match.groups()
        found.add(date(int(year), _MONTHS[month.lower()], int(day)))
    for match in _SHORT_DATE.finditer(text):
        day, month, year = match.groups()
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:  # pragma: no cover — нереальная дата в тексте
            continue
    ordered = sorted(found, reverse=True)[: policy.periods.max_count]
    if len(ordered) < policy.periods.min_count:
        return ()
    return tuple(ordered)


def _reporting_kind(lowered: str, policy: ParsingPolicy) -> ReportingKind:
    """Вид отчётности по маркерам документа.

    Умолчание объявлено методикой и неопасно: полная годовая отчётность
    маркеров не несёт, а промежуточная, раскрываемая и специального
    назначения объявляют себя сами — на титульном листе и в заголовках форм.
    """
    for marker, kind in policy.reporting_kind.markers.items():
        if normalize_name(marker) in lowered:
            return ReportingKind(kind)
    return ReportingKind(policy.reporting_kind.default)


def limitation_for(kind: ReportingKind, policy: ParsingPolicy | None = None) -> str | None:
    """Оговорка о виде отчётности для раздела «Ограничения анализа».

    У полной отчётности оговорки нет: ограничивать нечего. У прочих видов
    она обязательна — состав раскрытий у них уже, и показатели, на которых
    построена долговая нагрузка, могут отсутствовать.
    """
    policy = policy or load_parsing_policy()
    return policy.reporting_kind.limitations.get(kind.value)


def share_of_total(value: Decimal, total: Decimal | None) -> Decimal | None:
    """Доля величины в валюте баланса; None — итог не раскрыт либо нулевой.

    Нужна экрану сверки: статья сверх порога существенности не сворачивается
    в «прочее», а выносится отдельной позицией.
    """
    if total is None or total == 0:
        return None
    return abs(value) / abs(total)
