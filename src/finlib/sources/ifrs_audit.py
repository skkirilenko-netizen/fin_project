"""Чтение аудиторского заключения: тип задания, вид мнения, разделы.

**Вид мнения объявлен заголовком раздела**, и читается он оттуда. Это третий
случай подряд, когда строение документа надёжнее поиска по словам: раздел
строки задаёт ближайший итог ниже неё, примечание находится по ссылке
из формы, вид мнения — по заголовку. Поиск по словам дал бы здесь чужой
ответ: слово «оговорка» стоит и в шаблонном абзаце об ответственности
аудитора у всех без исключения.

**Три состояния определённости, и путать их нельзя.**

| Состояние | Что значит |
|---|---|
| `determined` | заключение прочитано, вид мнения назван |
| `not_readable` | заключение в документе есть, но страницы без текстового слоя |
| `absent` | заключения в документе нет вовсе |

У Автодора заключение занимает страницы 3–7, и все пять — изображение без
текста: сказать по нему «мнение немодифицированное» значило бы выдать
незнание за результат. У промежуточной отчётности заключения может не быть
вовсе, и это другое сведение, а не то же самое.

**Обзорная проверка — отдельный тип задания.** Объём процедур там меньше
аудита, и аудитор прямо пишет, что мнения не выражает; свести её к виду
мнения значило бы выдать меньшую уверенность за большую.
"""

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

from finlib.normalize.ifrs_audit import AuditPolicy, load_audit_policy
from finlib.normalize.lines import normalize_name
from finlib.sources.pdf_text import PdfDocument

logger = logging.getLogger(__name__)

# Запись оглавления кончается номером страницы: заголовком заключения
# она не является, как и заголовком примечания.
_CONTENTS_TAIL = re.compile(r"\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*$")


class Determination(StrEnum):
    """Определён ли вид мнения и почему нет."""

    DETERMINED = "determined"
    NOT_READABLE = "not_readable"
    ABSENT = "absent"


class Engagement(StrEnum):
    """Тип задания: аудит или обзорная проверка."""

    AUDIT = "audit"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Итог чтения заключения."""

    determination: Determination
    engagement: Engagement | None = None
    opinion: str | None = None
    opinion_name: str = ""
    modified: bool | None = None
    sections: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    pages: tuple[int, int] | None = None
    unreadable_pages: tuple[int, ...] = ()

    def describe(self) -> str:
        """Однострочная сводка для отчёта и журнала."""
        if self.determination is Determination.ABSENT:
            return "заключения в документе нет"
        if self.determination is Determination.NOT_READABLE:
            pages = ", ".join(str(item) for item in self.unreadable_pages)
            return f"заключение не прочитано: страницы без текстового слоя {pages}"
        kind = "обзорная проверка" if self.engagement is Engagement.REVIEW else "аудит"
        sections = ", ".join(self.sections) if self.sections else "нет"
        return f"{kind}, {self.opinion_name}; разделы-признаки: {sections}"

    def limitations(self, policy: AuditPolicy) -> tuple[str, ...]:
        """Оговорки для раздела «Ограничения анализа» — дословно из методики."""
        found: list[str] = []
        if self.determination is Determination.ABSENT:
            found.append(policy.limitations["absent"])
        if self.determination is Determination.NOT_READABLE:
            found.append(policy.limitations["not_readable"])
        if self.engagement is Engagement.REVIEW:
            found.append(policy.limitations["review"])
        if self.modified:
            found.append(policy.limitations["modified"])
        return tuple(found)


def read_audit_report(
    text: str,
    document: PdfDocument | None = None,
    before: int = 0,
    policy: AuditPolicy | None = None,
) -> AuditReport:
    """Читает заключение: тип задания, вид мнения и разделы-признаки.

    `before` — смещение первой формы: заключение стоит до неё, и дальше
    искать незачем. Это то же опирание на строение, что и везде здесь.
    """
    policy = policy or load_audit_policy()
    lines, offsets = _lines_with_offsets(text)
    limit = before or len(text)

    heading = _report_heading(lines, offsets, limit, policy)
    if heading is None:
        # Заголовка в тексте нет. Но заключение могло быть объявлено
        # оглавлением либо лежать на страницах без текстового слоя — тогда
        # это «не прочитано», а не «нет».
        lost = _pages_before_forms(document, limit)
        if lost or _declared_in_contents(lines, policy):
            return AuditReport(Determination.NOT_READABLE, unreadable_pages=lost)
        return AuditReport(Determination.ABSENT)

    index, engagement = heading
    start = offsets[index]
    end = limit
    block = [
        lines[position].strip()
        for position in range(index, len(lines))
        if offsets[position] < end
    ]

    pages = None
    if document is not None:
        pages = (document.page_at(start), document.page_at(max(start, end - 1)))
    lost = _unreadable_within(document, pages)

    opinion = _opinion_of(block, policy)
    if opinion is None:
        # Заголовок заключения есть, а раздела мнения нет: так выглядит
        # заключение, у которого текстом взят только титул.
        return AuditReport(
            Determination.NOT_READABLE,
            engagement=engagement,
            pages=pages,
            unreadable_pages=lost,
        )

    sections = _sections_of(block, policy)
    return AuditReport(
        Determination.DETERMINED,
        engagement=engagement,
        opinion=opinion.code,
        opinion_name=opinion.name,
        modified=opinion.modified,
        sections=sections,
        signals=_signals_of(block, sections, policy),
        pages=pages,
        unreadable_pages=lost,
    )


def _lines_with_offsets(text: str) -> tuple[list[str], list[int]]:
    """Строки документа вместе со смещением начала каждой."""
    lines = text.split("\n")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    return lines, offsets


def _lines_until(offsets: list[int], end: int) -> int:
    """Сколько строк умещается до смещения."""
    return sum(1 for item in offsets if item < end)


def _report_heading(
    lines: list[str], offsets: list[int], limit: int, policy: AuditPolicy
) -> tuple[int, Engagement] | None:
    """Строка заголовка заключения и тип задания; None — заголовка нет.

    Запись оглавления заголовком не считается: она кончается номером
    страницы. Иначе заключением становилось бы содержание — у Автодора
    оно называет заключение, которого в тексте нет.
    """
    for index, line in enumerate(lines):
        if offsets[index] >= limit:
            break
        stripped = line.strip()
        if not stripped or _CONTENTS_TAIL.search(stripped):
            continue
        # Запись оглавления переносится, и номер страницы остаётся на второй
        # строке: «Аудиторское заключение независимых аудиторов о раскрываемой»
        # / «консолидированной финансовой отчетности 3-4». Это по-прежнему
        # оглавление, а не заголовок — склейка та же, что у примечаний.
        following = lines[index + 1].strip() if index + 1 < len(lines) else ""
        if following[:1].islower() and _CONTENTS_TAIL.search(following):
            continue
        normalized = normalize_name(stripped)
        for kind in (Engagement.REVIEW, Engagement.AUDIT):
            for heading in policy.report_headings[kind.value]:
                if normalized.startswith(normalize_name(heading)):
                    return index, kind
    return None


def _declared_in_contents(lines: list[str], policy: AuditPolicy) -> bool:
    """Объявлено ли заключение оглавлением: независимое свидетельство."""
    wanted = [
        normalize_name(heading)
        for headings in policy.report_headings.values()
        for heading in headings
    ]
    for line in lines:
        stripped = line.strip()
        if not _CONTENTS_TAIL.search(stripped):
            continue
        normalized = normalize_name(stripped)
        if any(normalized.startswith(item) for item in wanted):
            return True
    return False


def _pages_before_forms(
    document: PdfDocument | None, limit: int
) -> tuple[int, ...]:
    """Страницы без текстового слоя, лежащие до первой формы."""
    if document is None:
        return ()
    last = document.page_at(max(0, limit - 1))
    return tuple(number for number in document.pages_without_text if number <= last)


def _unreadable_within(
    document: PdfDocument | None, pages: tuple[int, int] | None
) -> tuple[int, ...]:
    """Страницы без текстового слоя внутри найденных границ заключения."""
    if document is None or pages is None:
        return ()
    first, last = pages
    return tuple(
        number for number in document.pages_without_text if first <= number <= last
    )


def _opinion_of(block: list[str], policy: AuditPolicy):
    """Вид мнения по заголовку раздела; None — раздела нет.

    Перебор идёт в порядке справочника: сначала модифицированные виды,
    и лишь потом немодифицированный. «Мнение с оговоркой» начинается
    со слова «Мнение», и обратный порядок делал бы оговорку невидимой.
    """
    for kind in policy.opinions:
        for heading in kind.headings:
            wanted = normalize_name(heading)
            for line in block:
                normalized = normalize_name(line)
                if normalized == wanted or normalized.startswith(f"{wanted} "):
                    return kind
    return None


def _sections_of(block: list[str], policy: AuditPolicy) -> tuple[str, ...]:
    """Разделы-признаки, найденные в заключении."""
    found: list[str] = []
    for section in policy.sections:
        for heading in section.headings:
            wanted = normalize_name(heading)
            if any(normalize_name(line).startswith(wanted) for line in block):
                found.append(section.code)
                break
    return tuple(found)


def _signals_of(
    block: list[str], sections: tuple[str, ...], policy: AuditPolicy
) -> tuple[str, ...]:
    """Сигналы заключения: условие объявлено справочником, не кодом."""
    found: list[str] = []
    for signal in policy.signals:
        if signal.condition != "section_found" or signal.section not in sections:
            continue
        section = policy.section(signal.section)
        heading = next(
            (
                line
                for line in block
                if any(
                    normalize_name(line).startswith(normalize_name(item))
                    for item in section.headings
                )
            ),
            "",
        )
        lowered = heading.lower()
        if any(marker.lower() in lowered for marker in signal.markers):
            found.append(signal.code)
    return tuple(found)
