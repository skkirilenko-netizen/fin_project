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
from pathlib import Path

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_numbers import (
    Grouping,
    GroupingDetection,
    ParsingPolicy,
    ballot,
    detect_grouping,
    load_parsing_policy,
)
from finlib.sources.pdf_text import PdfDocument, read_document

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


def text_of(path: Path) -> PdfDocument:
    """Текстовый слой документа.

    Извлечение живёт в `sources/pdf_text.py` за отдельным интерфейсом:
    библиотеки для PDF различаются тем, насколько точно держат раскладку
    по колонкам, и замена одной на другую не должна трогать разбор форм.

    Разбирается именно слой, а не изображение: распознавание сканов
    не реализовано. Ошибка чтения наверх не поднимается — о ней говорит
    контроль приёма, а не исключение из недр библиотеки. Причины «слой пуст»
    и «файл не прочитан» различаются: предлагать распознавание там, где дело
    в шифровании, значит назвать ложную причину.
    """
    return read_document(path)


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

    headings = form_headings(text, catalog, policy)
    forms = tuple(headings)
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

    # Валюта и единица берутся из шапок форм, а не из всего документа:
    # в отчётности на двести страниц упоминание чужой валюты есть почти
    # всегда, и признаком валюты отчётности оно не является.
    headers = normalize_name(
        " ".join(header_of(text, start, policy) for start in headings.values())
    )

    foreign = _foreign_currency(headers, policy)
    rouble = _rouble(headers, text, headings, policy)
    # Чужая валюта в шапке формы решает дело даже при упоминании рубля рядом:
    # шапка коротка, случайных упоминаний в ней не бывает, а «в миллионах
    # долларов США» и есть объявление валюты отчётности.
    if foreign is not None:
        return Rejection(
            CheckCode.FILE_CURRENCY_NOT_ROUBLE,
            policy.currency.reasons["not_rouble"],
            {"currency": foreign},
        )
    if not rouble:
        return Rejection(
            CheckCode.FILE_CURRENCY_NOT_DETERMINED,
            policy.currency.reasons["not_determined"],
        )

    unit = _unit(headers, policy)
    if unit is None:
        return Rejection(
            CheckCode.UNIT_NOT_DETERMINED, policy.units.reasons["not_determined"]
        )

    # За конвенцию голосуют только денежные величины таблиц: примечания
    # и текстовая часть полны чисел, которые денежными не являются —
    # номеров пунктов, ссылок на стандарты, процентов, — и каждое такое
    # число подаёт ложную улику.
    voting, removed = ballot(forms_text(text, headings))
    detection = detect_grouping(voting, policy.digit_grouping)
    if removed:
        logger.info(
            "голосование за конвенцию: исключено чисел %s",
            ", ".join(f"{name} — {count}" for name, count in sorted(removed.items())),
        )
    if not detection.determined:
        reason = policy.digit_grouping.reasons[detection.reason]
        return Rejection(
            CheckCode.DIGIT_GROUPING_NOT_DETERMINED,
            reason,
            {"detection": detection.describe(), "excluded": removed},
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


def form_headings(
    text: str, catalog: IfrsCatalog, policy: ParsingPolicy
) -> dict[str, int]:
    """Где в документе начинается каждая форма: код формы → позиция заголовка.

    Заголовок ищется **построчно и по ядру наименования**, а не вхождением
    полной фразы в текст. Две причины, обе с настоящей отчётности:

    у Сегежи формы называются «Консолидированный отчет специального
    назначения о финансовом положении» — полная фраза справочника в неё
    не укладывается, и документ был отклонён как не отчётность;

    в оглавлении и в примечаниях те же слова стоят внутри длинных
    предложений, и поиск по всему тексту опознавал бы форму по упоминанию.
    Поэтому строка длиннее заголовка формой не считается.

    **Из нескольких вхождений выбирается то, за которым идёт таблица.**
    Оглавление состоит ровно из таких же коротких строк: «Консолидированный
    отчет о финансовом положении 8». Отличить его по номеру страницы —
    угадывание, а по тому, что идёт следом, — признак: у формы дальше стоят
    строки с величинами, у оглавления — другие строки оглавления. Это та же
    опора на структуру, что и при опознании неподписанного итога.
    """
    limit = policy.document_kind.heading_max_length
    lines = text.split("\n")
    starts: list[int] = []
    position = 0
    for line in lines:
        starts.append(position)
        position += len(line) + 1

    candidates: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > limit:
            continue
        lowered = normalize_name(stripped)
        for code, cores in policy.document_kind.cores.items():
            if any(normalize_name(core) in lowered for core in cores):
                candidates.setdefault(code, []).append(index)
                break

    window = policy.document_kind.lookahead_lines
    found: dict[str, int] = {}
    for code, indexes in candidates.items():
        best = max(indexes, key=lambda item: _table_rows_after(lines, item, window))
        if _table_rows_after(lines, best, window) >= policy.document_kind.min_table_rows:
            found[code] = starts[best]
    return found


# Строка таблицы: не менее двух чисел длиной от трёх цифр. Номер страницы
# в оглавлении — одно короткое число, и под это определение не подходит.
_TABLE_ROW = re.compile(r"(?:\d[\d    ,.]{2,}\D*){2,}")


def _table_rows_after(lines: list[str], index: int, window: int) -> int:
    """Сколько строк с величинами идёт следом за строкой."""
    return sum(
        1 for line in lines[index + 1 : index + 1 + window] if _TABLE_ROW.search(line)
    )


def forms_text(text: str, headings: dict[str, int]) -> str:
    """Текст блоков форм: от заголовка каждой до начала следующей.

    Всё, что вне блоков, — примечания, аудиторское заключение, оглавление —
    в голосовании за конвенцию не участвует: чисел там больше, чем в формах,
    и денежных величин среди них почти нет.
    """
    if not headings:
        return ""
    ordered = sorted(headings.values())
    parts = []
    for index, start in enumerate(ordered):
        end = ordered[index + 1] if index + 1 < len(ordered) else len(text)
        parts.append(text[start:end])
    return "\n".join(parts)


def header_of(text: str, position: int, policy: ParsingPolicy) -> str:
    """Шапка формы: несколько строк после её заголовка.

    Валюта и единица измерения стоят здесь — «(в миллионах российских
    рублей)», — а не где угодно в документе.
    """
    return text[position : position + policy.header_window.characters]


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


def _rouble(
    headers: str,
    text: str,
    headings: dict[str, int],
    policy: ParsingPolicy,
) -> bool:
    """Объявлен ли рубль в шапках форм.

    Маркеры нормализуются так же, как текст: «руб.» приходит как «В млн руб.»,
    и нормализация снимает точку. Но нормализация снимает и знак валюты
    целиком: `normalize_name("₽")` — пустая строка, а пустая строка входит
    в любой текст. Проверка рубля от этого возвращала истину всегда,
    то есть не работала вовсе — при том что ни один тест не падал
    и ни один документ не был отклонён по этой причине.

    Поэтому словесные маркеры ищутся в нормализованном тексте, а знаки
    валюты — в сыром: нормализовать их нечего.
    """
    for marker in policy.currency.rouble_markers:
        normalized = normalize_name(marker)
        if normalized:
            if normalized in headers:
                return True
            continue
        # Знак валюты нормализации не переживает и ищется как есть.
        raw = "".join(
            header_of(text, start, policy) for start in headings.values()
        )
        if marker in raw:
            return True
    return False


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
