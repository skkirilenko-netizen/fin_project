"""Примечания: указатель, сверка с оглавлением и переход по ссылке из формы.

**Ссылка из строки формы — опора, а не мусор.** «Амортизация 6, 7»,
«Процентные расходы 9», «(прим. 21)» — номер при наименовании указывает,
в каком примечании стоит расшифровка величины. До сих пор он вычищался как
приклеенная сноска; здесь он читается как ссылка, и по ней примечание
находится точно, а не по совпадению наименования. Это то же решение, что
«раздел строки определяется ближайшим итогом ниже неё»: опора на строение
документа вместо угадывания по словам.

**Наименование примечания опорой быть не может.** Финансовые расходы стоят
примечанием 9 у Сегежи, 10 у ФосАгро и ЛСР, 11 у Автодора и 12 у Норникеля,
а называются «Финансовые доходы и расходы», «ФИНАНСОВЫЕ РАСХОДЫ, НЕТТО»
и «ФИНАНСОВЫЕ ДОХОДЫ/(РАСХОДЫ) И РЕЗЕРВЫ, НЕТТО».

**Сверка с оглавлением обязательна и выполняется всегда.** Оглавление —
независимый перечень примечаний, и расхождение с найденными заголовками
означает потерю: у Автодора и ЛСР есть страницы без текстового слоя, и
примечание, попавшее на такую страницу, ничем другим о себе не заявит.
Поэтому указатель хранит обе стороны — и чего не хватает в тексте, и чего
нет в оглавлении.

**Величина берётся только из названного примечания.** Поиск по наименованию
в пределах документа даёт не отсутствие числа, а чужое число: у Норникеля
«Амортизация дисконта по оценочным обязательствам» — финансовый расход,
а не износ, и слово то же. Это правило проверяется тестом, а не соглашением:
`lines_of` работает с границами примечания, и другого способа достать строку
примечания модуль не даёт.
"""

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_numbers import NotesPolicy, load_parsing_policy
from finlib.sources.pdf_text import PdfDocument

logger = logging.getLogger(__name__)

# Заголовок примечания: номер и наименование, начинающееся с буквы.
# «Примечание» перед номером необязательно — у разобранных эмитентов его нет
# ни у кого, но у других источников встречается.
_HEADING = re.compile(
    r"^(?:Примечание\s*)?(\d{1,2})\s*[.)]?\s+(?P<title>[^\W\d_][^\n]*?)\s*$"
)

# Запись оглавления: то же самое, но с номером страницы в конце, иногда
# через точки-выноски. Оглавление отличается от заголовка именно этим.
_CONTENTS = re.compile(
    r"^(?:Примечание\s*)?(\d{1,2})\s*[.)]?\s+(?P<title>[^\W\d_][^\n]*?)"
    r"[\s.…]*(?P<page>\d{1,3})\s*$"
)

# Ссылка на примечание при наименовании строки формы: хвост из номеров
# («Амортизация 6, 7») либо явное указание в скобках («(прим. 21)»).
_TAIL_REFERENCE = re.compile(r"(?<=\D)\s(\d{1,2}(?:\s*,\s*\d{1,2})*)\s*$")
_EXPLICIT_REFERENCE = re.compile(r"\(\s*прим(?:ечание)?\.?\s*(\d{1,2})\s*\)", re.I)


@dataclass(frozen=True, slots=True)
class Note:
    """Примечание: номер, наименование и его границы в тексте документа."""

    number: int
    title: str
    start: int
    end: int
    page: int = 0

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        return f"{self.number}. {self.title} (стр. {self.page})"


@dataclass(frozen=True, slots=True)
class ContentsEntry:
    """Запись оглавления: независимое свидетельство о существовании примечания."""

    number: int
    title: str
    page: int

    def describe(self) -> str:
        """Однострочное описание для отчёта."""
        return f"{self.number}. {self.title} (оглавление, стр. {self.page})"


@dataclass(frozen=True, slots=True)
class NoteIndex:
    """Указатель примечаний вместе с итогом сверки с оглавлением."""

    notes: tuple[Note, ...] = ()
    contents: tuple[ContentsEntry, ...] = ()
    # Объявлены оглавлением, но в тексте не найдены: это и есть потеря.
    missing: tuple[ContentsEntry, ...] = ()
    # Найдены в тексте, но оглавлением не объявлены: либо оглавление неполно,
    # либо за примечание принято что-то другое. И то и другое — сигнал.
    unexpected: tuple[Note, ...] = ()
    # Номер совпал, наименование разошлось: тоже сигнал, но другой —
    # примечание на месте, а опознано, возможно, не то.
    mismatched: tuple[Note, ...] = ()

    def get(self, number: int) -> Note | None:
        """Примечание по номеру; None — такого в тексте нет."""
        return next((item for item in self.notes if item.number == number), None)

    @property
    def has_contents(self) -> bool:
        """Нашлось ли оглавление: без него сверять не с чем."""
        return bool(self.contents)

    def describe(self) -> str:
        """Сводка со счётчиком проверенного рядом со счётчиком сработавшего."""
        if not self.has_contents:
            return (
                f"примечаний найдено {len(self.notes)}; оглавление не найдено, "
                "сверять не с чем"
            )
        return (
            f"примечаний найдено {len(self.notes)} из {len(self.contents)} "
            f"объявленных оглавлением; не найдено {len(self.missing)}, "
            f"нет в оглавлении {len(self.unexpected)}, "
            f"наименование разошлось у {len(self.mismatched)}"
        )


def index_notes(
    text: str,
    document: PdfDocument | None = None,
    policy: NotesPolicy | None = None,
    after: int = 0,
) -> NoteIndex:
    """Строит указатель примечаний и сверяет его с оглавлением.

    Сверка не отделена от построения намеренно: указатель без неё говорит
    только о найденном, а знать нужно и о ненайденном.

    `after` — смещение, после которого начинаются примечания: обычно конец
    форм. Без него примечаниями становятся нумерованные абзацы аудиторского
    заключения — у Европлана «1. Мы не имели возможности получить
    достаточные надлежащие аудиторские доказательства…».
    """
    policy = policy or load_parsing_policy().notes
    lines, offsets = _lines_with_offsets(text)
    pages = len(document.pages) if document is not None else 0
    contents, span = _contents_of(lines, offsets, policy, pages)
    notes, unexpected = _headings_of(
        lines, offsets, text, document, policy, contents, after, span
    )
    found = {item.number for item in notes}

    missing = tuple(item for item in contents if item.number not in found)
    declared = {item.number for item in contents}
    mismatched = tuple(
        item
        for item in notes
        if item.number in declared and not _same_title(item, contents, policy)
    )
    index = NoteIndex(notes, contents, missing, unexpected, mismatched)
    logger.info("указатель примечаний: %s", index.describe())
    return index


def lines_of(note: Note, text: str) -> tuple[str, ...]:
    """Строки одного примечания — и никакие другие.

    Единственный способ достать содержимое примечания, и это не удобство,
    а правило: поиск наименования по всему документу даёт чужое число,
    а не отсутствие числа. У Норникеля «амортизация дисконта по оценочным
    обязательствам» — финансовый расход, а не износ, и по слову «амортизация»
    она найдётся первой.
    """
    return tuple(
        line.strip() for line in text[note.start : note.end].split("\n") if line.strip()
    )


def find_in_note(
    index: NoteIndex, number: int, names: tuple[str, ...], text: str
) -> tuple[str, ...]:
    """Строки названного примечания, начинающиеся с одного из наименований.

    Примечания нет — пусто, и это отказ, а не ноль: показатель, которому
    величина нужна, не считается вовсе (то же правило, что в РСБУ для
    «Чистый долг / EBITDA» при отсутствии амортизации).
    """
    note = index.get(number)
    if note is None:
        logger.info("примечание %s в документе не найдено", number)
        return ()
    wanted = tuple(normalize_name(item) for item in names)
    found = []
    for line in lines_of(note, text):
        normalized = normalize_name(line)
        if any(normalized.startswith(item) for item in wanted):
            found.append(line)
    return tuple(found)


def references_in(name: str, policy: NotesPolicy | None = None) -> tuple[int, ...]:
    """Номера примечаний, на которые ссылается строка формы.

    Две записи ссылки, обе с живых комплектов: номер хвостом при наименовании
    («Амортизация 6, 7») и явное указание в скобках («(прим. 21)»). Номер,
    больший наибольшего примечания, ссылкой не считается — это уже величина.
    """
    policy = policy or load_parsing_policy().notes
    found: list[int] = [
        int(number) for number in _EXPLICIT_REFERENCE.findall(name or "")
    ]
    tail = _TAIL_REFERENCE.search(_EXPLICIT_REFERENCE.sub("", name or "").strip())
    if tail is not None:
        found.extend(int(part) for part in re.split(r"\s*,\s*", tail.group(1)))
    return tuple(
        dict.fromkeys(item for item in found if 1 <= item <= policy.max_number)
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


def _contents_of(
    lines: list[str], offsets: list[int], policy: NotesPolicy, pages: int
) -> tuple[tuple[ContentsEntry, ...], tuple[int, int]]:
    """Записи оглавления; пусто — оглавления в документе нет.

    **Оглавление опознаётся как перечень, а не построчно.** Строка «номер,
    слова, число» встречается и в таблицах: «31 декабря 2024 года 168,443
    290,833» выглядит записью оглавления не хуже настоящей. Отличает
    оглавление то, что записи идут подряд, номера возрастают, а страницы
    не убывают и не выходят за пределы документа. Одна такая строка ничего
    не значит; десяток подряд — значит.
    """
    candidates: list[tuple[int, ContentsEntry]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if len(stripped) > policy.heading_max_length:
            continue
        match = _CONTENTS.match(stripped)
        if match is None:
            continue
        number = int(match.group(1))
        page = int(match.group("page"))
        if not 1 <= number <= policy.max_number:
            continue
        if pages and not 1 <= page <= pages:
            # Ссылка на страницу, которой в документе нет, — не оглавление,
            # а число из таблицы.
            continue
        title = _clean_title(match.group("title"))
        if not title or _has_digits(title):
            # В наименовании примечания цифр не бывает; в строке таблицы,
            # принятой за запись оглавления, они и стоят.
            continue
        candidates.append((index, ContentsEntry(number, title, page)))

    best, span = _longest_run(candidates, policy)
    if len(best) < policy.contents_min_entries:
        return (), (0, 0)
    return tuple(best), span


def _longest_run(
    candidates: list[tuple[int, ContentsEntry]], policy: NotesPolicy
) -> tuple[list[ContentsEntry], tuple[int, int]]:
    """Самая длинная цепочка записей подряд: возрастающие номера, растущие страницы.

    Возвращаются и границы цепочки в строках: строки самого оглавления
    заголовками примечаний быть не могут. У Автодора запись «20 Заемные
    средства и обязательства по долгосрочным инвестиционным и» переносится
    на вторую строку, номер страницы остаётся там, и без этого правила она
    становилась примечанием 20 — на тринадцатой странице, прежде примечания 1.
    Дальше цепочка требовала номеров больше двадцати, и девятнадцать
    примечаний терялись.
    """
    best: list[ContentsEntry] = []
    current: list[ContentsEntry] = []
    bounds = (0, 0)
    start_index = 0
    last_index = None
    for index, entry in candidates:
        # Разрыв номеров в оглавлении не ограничивается: длинное наименование
        # переносится на вторую строку, запись целиком не читается, и в цепочке
        # появляется пропуск. Оглавление держится не на сплошной нумерации,
        # а на том, что записи идут подряд и страницы не убывают.
        fits = (
            current
            and entry.number > current[-1].number
            and entry.page >= current[-1].page
            and last_index is not None
            and index - last_index <= policy.contents_min_entries
        )
        if fits:
            current = [*current, entry]
        else:
            current = [entry]
            start_index = index
        last_index = index
        if len(current) > len(best):
            best = current
            bounds = (start_index, index)
    return best, bounds


def _has_digits(title: str) -> bool:
    """Есть ли в наименовании цифры — признак строки таблицы, а не примечания."""
    return any(char.isdigit() for char in title)


def _headings_of(
    lines: list[str],
    offsets: list[int],
    text: str,
    document: PdfDocument | None,
    policy: NotesPolicy,
    contents: tuple[ContentsEntry, ...],
    after: int = 0,
    contents_span: tuple[int, int] = (0, 0),
) -> tuple[tuple[Note, ...], tuple[Note, ...]]:
    """Заголовки примечаний в тексте, по возрастанию номера.

    Номера обязаны возрастать: строка таблицы, начинающаяся с числа,
    иначе становится примечанием. Разрыв допускается — примечание могло
    не найтись вовсе, — но идти назад цепочка не может.

    Возвращаются две стороны: объявленные оглавлением и не объявленные им.
    Вторые не выбрасываются молча — оглавление бывает неполным, и знать
    об этом надо.
    """
    accepted: list[Note] = []
    undeclared: list[Note] = []
    declared = {item.number: item for item in contents}
    last = 0
    first, last_line = contents_span
    for index, line in enumerate(lines):
        if offsets[index] < after:
            continue
        if first <= index <= last_line:
            # Строка самого оглавления заголовком примечания не является.
            continue
        stripped = line.strip()
        if not stripped or len(stripped) > policy.heading_max_length:
            continue
        if _CONTENTS.match(stripped) is not None:
            continue
        match = _HEADING.match(stripped)
        if match is None:
            continue
        number = int(match.group(1))
        title = _clean_title(match.group("title"))
        if not title or not 1 <= number <= policy.max_number:
            continue
        if _is_continuation(title, policy):
            # Заголовок, повторённый на следующей странице: то же примечание,
            # а не новое. Границу оно не двигает.
            continue
        if number <= last:
            continue
        # Ограничение разрыва нужно там, где сверять не с чем: оно отсекает
        # прозу, начатую числом. Где оглавление есть, правилом служит оно —
        # номер объявлен, и наименование обязано совпасть.
        if not contents and number > last + policy.max_number_gap:
            continue
        # Заголовок примечания начинается с прописной буквы. Без этого
        # примечанием становилась проза, разорванная переносом: у Сегежи
        # «30. млн руб. (2024 год: 33 млн руб.)» занимало номер 30, и
        # настоящие примечания 28 и 29 после него уже не принимались.
        if not title[:1].isupper():
            continue
        entry = declared.get(number)
        if entry is not None and not _close(entry.title, title, policy):
            # Номер объявлен оглавлением, а наименование другое: это
            # не то примечание, а совпадение номера.
            continue
        if entry is None and (contents and _has_digits(title)):
            continue
        start = offsets[index]
        found = Note(
            number,
            title,
            start,
            len(text),
            document.page_at(start) if document is not None else 0,
        )
        accepted.append(found)
        last = number
        if entry is None:
            undeclared.append(found)
    return (
        tuple(
            Note(
                item.number,
                item.title,
                item.start,
                accepted[position + 1].start
                if position + 1 < len(accepted)
                else len(text),
                item.page,
            )
            for position, item in enumerate(accepted)
        ),
        tuple(undeclared),
    )


def _is_continuation(title: str, policy: NotesPolicy) -> bool:
    """Повторён ли заголовок на следующей странице."""
    lowered = title.lower()
    return any(marker.lower() in lowered for marker in policy.continuation_markers)


def _clean_title(title: str) -> str:
    """Наименование примечания без выносок и хвостовых номеров страниц."""
    return re.sub(r"[\s.…]+$", "", title).strip()


def _same_title(
    note: Note, contents: tuple[ContentsEntry, ...], policy: NotesPolicy
) -> bool:
    """Совпадает ли наименование в тексте с объявленным в оглавлении."""
    entry = next((item for item in contents if item.number == note.number), None)
    return entry is not None and _close(entry.title, note.title, policy)


def _close(declared: str, found: str, policy: NotesPolicy) -> bool:
    """Достаточно ли близки наименования: оглавление печатается с переносами.

    Сравнивается и начало: длинное наименование в оглавлении переносится
    на вторую строку, и целиком в запись не попадает — «Торговая и прочая
    дебиторская задолженность и расходы будущих периодов» против «Торговая
    и прочая дебиторская задолженность и расходы будущих периодов
    по доставке готовой продукции».
    """
    first = normalize_name(declared)
    second = normalize_name(found)
    if first.startswith(second) or second.startswith(first):
        return True
    ratio = SequenceMatcher(None, first, second).ratio()
    return ratio >= float(policy.title_match_ratio)
