"""Раскрытия уровня 2: ковенанты, залоги, поручительства, события после отчётной даты.

**Это проза, и величинами она не становится.** Разведка 30.09.2026 по шести
эмитентам: о ковенантах, залогах и поручительствах эмитент пишет абзацами,
и сумма в абзаце бывает балансовой стоимостью заложенного, лимитом кредита
или справедливой стоимостью поручительства — разными величинами под одним
словом. Поэтому абзац приводится дословно, с номером примечания и страницей,
как цитата аудиторского заключения; фактом он не пишется и в расчёт не идёт.

**Где искать — строением документа.** Абзац ищется только в названных
примечаниях: о долге — том же, что у сроков (по ссылке из строки займов,
при непрочитанном балансе — по наименованию из `balance_fallback`),
остальные — по наименованию в указателе и оглавлении. Примета без
названного примечания не ищется нигде (урок «структура документа надёжнее
поиска по тексту»): «меры ограничительного характера» в примечании
об отчитывающемся предприятии — о санкциях, а не о ковенантах.

**Нарушение ковенанта здесь не устанавливается.** Найденные приметы
нарушения называются как приметы; решение — за человеком, тот же порядок,
что у вида оговорки аудитора. Что считать приметой и где искать —
методика (`ifrs_note_lines.yaml`, `disclosures`); как делить текст
на абзацы — технический параметр (`ifrs_parsing.yaml`, `disclosure_text`).
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from finlib.normalize.ifrs_lines import Alias
from finlib.normalize.ifrs_note_lines import DebtMaturity, Disclosures
from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_notes import Note, NoteIndex
from finlib.sources.ifrs_numbers import DisclosureTextPolicy
from finlib.utils import markers_found

logger = logging.getLogger(__name__)

# Число в строке таблицы: разряд, дробь, скобки отрицательного, прочерк,
# процент. Хвост строки из таких лексем — признак строки таблицы.
_NUMBER = re.compile(r"^\(?-?[\d.,]+\)?%?$|^[-–—]$")
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}")


class Kind(StrEnum):
    """Предмет раскрытия; значение — ключ методики и предела цитаты."""

    COVENANTS = "covenants"
    PLEDGES = "pledges"
    GUARANTEES = "guarantees"
    SUBSEQUENT_EVENTS = "subsequent_events"


@dataclass(frozen=True, slots=True)
class Paragraph:
    """Абзац прозы примечания: текст, страница начала и заголовок над ним."""

    text: str
    page: int
    heading: str = ""


@dataclass(frozen=True, slots=True)
class Quote:
    """Дословный абзац: откуда он и что в нём. Обрезку делает печать."""

    note: int
    title: str
    page: int
    text: str


@dataclass(frozen=True, slots=True)
class DisclosureReading:
    """Итог по предмету: просмотренные примечания, цитаты, приметы.

    Пустые цитаты при просмотренных примечаниях — «абзацев нет», и это
    сведение о просмотренном, а не утверждение об отсутствии. Ни одного
    просмотренного — названного примечания в документе нет.
    """

    kind: Kind
    viewed: tuple[tuple[int, str], ...] = ()
    quotes: tuple[Quote, ...] = ()
    breach: tuple[str, ...] = ()
    reclassified: tuple[str, ...] = ()

    @property
    def note_missing(self) -> bool:
        """Не найдено ни одного примечания, в котором предмет ищется."""
        return not self.viewed

    def describe(self) -> str:
        """Однострочная сводка для прогона."""
        if self.note_missing:
            return "примечания нет"
        if not self.quotes:
            return f"абзацев нет (просмотрено {len(self.viewed)})"
        pages = ", ".join(f"{item.note}/стр. {item.page}" for item in self.quotes)
        tail = f"; приметы нарушения: {', '.join(self.breach)}" if self.breach else ""
        tail += f"; перенос в краткосрочные: {', '.join(self.reclassified)}" if (
            self.reclassified
        ) else ""
        return f"цитат {len(self.quotes)} ({pages}){tail}"


def colontitles(text: str, policy: DisclosureTextPolicy) -> frozenset[str]:
    """Строки, повторённые в документе колонтитулом, — приведённые к одному виду."""
    counted = Counter(" ".join(line.split()) for line in text.split("\n"))
    return frozenset(
        line
        for line, times in counted.items()
        if line and times >= policy.colontitle_min_repeats
    )


def _words(line: str) -> int:
    """Число слов из букв."""
    return len(_WORD.findall(line))


def _is_table(line: str, policy: DisclosureTextPolicy) -> bool:
    """Строка таблицы: хвост из величин подряд."""
    tokens = line.split()
    trailing = 0
    for token in reversed(tokens):
        if not _NUMBER.match(token):
            break
        trailing += 1
    return trailing >= policy.table_trailing_numbers


def _ends_sentence(line: str, policy: DisclosureTextPolicy) -> bool:
    """Кончается ли строка знаком конца предложения (кавычки и скобки не мешают)."""
    stripped = line.rstrip(" »\")")
    return bool(stripped) and stripped[-1] in policy.sentence_end


def _starts_new(line: str) -> bool:
    """Может ли строка начинать абзац: прописная, цифра, скобка, кавычка."""
    first = line[:1]
    return first.isupper() or first.isdigit() or first in "(«\"*"


def paragraphs_of(
    note: Note,
    text: str,
    page_at: object,
    policy: DisclosureTextPolicy,
    repeated: frozenset[str],
) -> tuple[Paragraph, ...]:
    """Абзацы прозы примечания; таблицы, колонтитулы и номера страниц отброшены.

    Первая строка — заголовок примечания, в абзацы не идёт. Абзац кончается
    на знаке конца предложения, если строка не во всю ширину и следующая
    может начинать абзац; предложение, не законченное строкой, продолжается
    через колонтитул и перелом страницы. Заголовок — короткая строка без
    знака конца после законченного предложения или таблицы.
    """
    raw = text[note.start : note.end].split("\n")
    lines: list[tuple[str, int]] = []
    offset = note.start
    for line in raw:
        cleaned = " ".join(line.split())
        at = offset
        offset += len(line) + 1
        if cleaned in policy.end_markers:
            break
        if not cleaned or cleaned in repeated or cleaned.isdigit():
            continue
        lines.append((cleaned, at))
    if not lines:
        return ()
    lines = lines[1:]
    # Ширина строки прозы — медиана, а не наибольшая: строка таблицы,
    # не опознанная таблицей, бывает вдвое длиннее абзаца.
    prose = sorted(
        len(line)
        for line, _ in lines
        if not _is_table(line, policy) and _words(line) >= policy.prose_min_words
    )
    full = float(policy.full_line_ratio) * (prose[len(prose) // 2] if prose else 0)

    found: list[Paragraph] = []
    current: list[str] = []
    start = 0
    heading = ""
    # Чем кончилась предыдущая строка: предложение не закончено («open»),
    # закончено на полной строке («soft» — абзац, скорее всего, идёт дальше)
    # либо на короткой («hard» — абзац кончился). Начало — «hard».
    state = "hard"

    def flush() -> None:
        nonlocal current
        if current:
            found.append(Paragraph(" ".join(current), page_at(start), heading))  # type: ignore[operator]
        current = []

    def is_heading(line: str, following: str) -> bool:
        return (
            _words(line) <= policy.heading_max_words
            and len(line) < full
            and not _ends_sentence(line, policy)
            and (
                # Нумерованный подраздел — заголовок при любой следующей
                # строке: у Самолёта под «(b) Сверка изменений…» шапка
                # таблицы со строчной буквы.
                bool(re.match(policy.enumerator, line, flags=re.IGNORECASE))
                or (
                    bool(following)
                    and (_starts_new(following) or following[:1] in policy.bullets)
                )
            )
        )

    for number, (line, at) in enumerate(lines):
        following = lines[number + 1][0] if number + 1 < len(lines) else ""
        if _is_table(line, policy):
            if state == "open" and current:
                # Предложение не закончено: строка с хвостом величин — его
                # продолжение («…на сумму 142 099» у Автодора), а короткая —
                # сноска с номером страницы на переломе («*См. Примечание 5.
                # 37» у Брусники), и абзац идёт дальше без неё.
                if _words(line) >= policy.prose_min_words:
                    current.append(line)
                continue
            flush()
            state = "hard"
            continue
        if current and (state == "open" or line[:1] in policy.bullets):
            current.append(line)
        elif is_heading(line, following):
            flush()
            heading = line
            state = "hard"
            continue
        elif current and (state == "soft" or not _starts_new(line)):
            current.append(line)
        elif _words(line) >= policy.prose_min_words and (
            len(line) >= full or _ends_sentence(line, policy)
        ):
            # Абзац начинается строкой во всю ширину либо сам укладывается
            # в строку; короткая строка без конца предложения — наименование
            # строки таблицы, чьи величины перенесены ниже («Необеспеченные
            # займы от связанных» у О'КЕЙ).
            flush()
            current = [line]
            start = at
        else:
            # Подпись вне прозы: «млн руб.», шапка графы.
            flush()
            state = "hard"
            continue
        if not _ends_sentence(line, policy):
            state = "open"
        else:
            state = "soft" if len(line) >= full else "hard"
    flush()
    return tuple(found)


def _title(note: Note, index: NoteIndex) -> str:
    """Полное наименование: из оглавления, иначе заголовок (он бывает обрезан)."""
    full = {entry.number: entry.title for entry in index.contents}
    return full.get(note.number, note.title)


def _named(index: NoteIndex, titles: tuple[Alias, ...]) -> tuple[Note, ...]:
    """Примечания с наименованием из перечня — полным, из оглавления."""
    wanted = {normalize_name(item.name) for item in titles}
    return tuple(
        note for note in index.notes if normalize_name(_title(note, index)) in wanted
    )


def debt_note_of(
    found: Note | None, index: NoteIndex, method: DebtMaturity | None
) -> Note | None:
    """Примечание о долге: по ссылке формы, иначе по наименованию запасной опоры."""
    if found is not None:
        return found
    if method is None or method.balance_fallback is None:
        return None
    named = _named(index, method.balance_fallback.note_titles)
    return named[0] if named else None


def _heading_matches(
    heading: str, markers: tuple[Alias, ...], policy: DisclosureTextPolicy
) -> bool:
    """Заголовок подраздела совпадает с приметой — без нумерации «(a)»."""
    bare = normalize_name(re.sub(policy.enumerator, "", heading, flags=re.IGNORECASE))
    return any(bare == normalize_name(item.name) for item in markers)


def _whole(paragraphs: tuple[Paragraph, ...]) -> str:
    """Проза примечания целиком: абзацы подряд, заголовок подраздела — перед своими."""
    parts: list[str] = []
    heading = ""
    for paragraph in paragraphs:
        if paragraph.heading and paragraph.heading != heading:
            parts.append(paragraph.heading)
            heading = paragraph.heading
        parts.append(paragraph.text)
    return " ".join(parts)


def read_disclosures(
    text: str,
    index: NoteIndex,
    page_at: object,
    debt_note: Note | None,
    method: Disclosures,
    policy: DisclosureTextPolicy,
) -> dict[Kind, DisclosureReading]:
    """Цитаты по четырём предметам — только из названных примечаний."""
    repeated = colontitles(text, policy)
    cache: dict[int, tuple[Paragraph, ...]] = {}

    def paragraphs(note: Note) -> tuple[Paragraph, ...]:
        if note.number not in cache:
            cache[note.number] = paragraphs_of(note, text, page_at, policy, repeated)
        return cache[note.number]

    def quote(note: Note, paragraph: Paragraph) -> Quote:
        return Quote(note.number, _title(note, index), paragraph.page, paragraph.text)

    def viewed(notes: tuple[Note, ...]) -> tuple[tuple[int, str], ...]:
        return tuple((note.number, _title(note, index)) for note in notes)

    result: dict[Kind, DisclosureReading] = {}
    in_debt = (debt_note,) if debt_note is not None else ()

    rule = method.covenants
    notes = in_debt if rule.in_debt_note else ()
    quotes = [
        quote(note, paragraph)
        for note in notes
        for paragraph in paragraphs(note)
        if markers_found(paragraph.text, rule.paragraph_markers, str.lower)
    ]
    joined = " ".join(item.text for item in quotes)
    result[Kind.COVENANTS] = DisclosureReading(
        Kind.COVENANTS,
        viewed(notes),
        tuple(quotes),
        markers_found(joined, tuple(item.name for item in rule.breach_markers), normalize_name)
        if joined
        else (),
        markers_found(
            joined, tuple(item.name for item in rule.reclassification_markers), normalize_name
        )
        if joined and rule.reclassification_markers
        else (),
    )

    pledges = method.pledges
    notes = in_debt if pledges.in_debt_note else ()
    result[Kind.PLEDGES] = DisclosureReading(
        Kind.PLEDGES,
        viewed(notes),
        tuple(
            quote(note, paragraph)
            for note in notes
            for paragraph in paragraphs(note)
            if markers_found(paragraph.text, pledges.paragraph_markers, str.lower)
            or (
                # Заголовок — запасная опора: абзац, который сам называет
                # другой предмет, ему не принадлежит. У Сегежи под «Активы,
                # переданные в качестве обеспечения» следом идёт абзац
                # «Ограничительные условия – …» без своего заголовка.
                paragraph.heading
                and _heading_matches(paragraph.heading, pledges.heading_markers, policy)
                and not markers_found(paragraph.text, rule.paragraph_markers, str.lower)
            )
        ),
    )

    guarantees = method.guarantees
    notes = (in_debt if guarantees.in_debt_note else ()) + tuple(
        note for note in _named(index, guarantees.note_titles) if note not in in_debt
    )
    result[Kind.GUARANTEES] = DisclosureReading(
        Kind.GUARANTEES,
        viewed(notes),
        tuple(
            quote(note, paragraph)
            for note in notes
            for paragraph in paragraphs(note)
            if markers_found(paragraph.text, guarantees.paragraph_markers, str.lower)
        ),
    )

    notes = _named(index, method.subsequent_events.note_titles)
    result[Kind.SUBSEQUENT_EVENTS] = DisclosureReading(
        Kind.SUBSEQUENT_EVENTS,
        viewed(notes),
        tuple(
            Quote(note.number, _title(note, index), note.page, _whole(found))
            for note in notes
            if (found := paragraphs(note))
        ),
    )
    for kind, reading in result.items():
        logger.info("раскрытие %s: %s", kind.value, reading.describe())
    return result
