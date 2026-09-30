"""Сроки погашения долга (уровень 2): примечание о долге и таблица сроков.

**Где лежат сроки — установлено на документах, а не предположено.** Разведка
30.09.2026 на шести эмитентах «Разбора» с PDF: в примечании «Кредиты
и займы» таблицы по корзинам срока нет ни у кого. Там годы погашения
по видам долга (О'КЕЙ, Самолёт, ЛСР) либо только деление на долгосрочную
и краткосрочную части (Брусника, Сегежа). Корзины стоят в разделе о риске
ликвидности примечания об управлении рисками (МСФО (IFRS) 7) —
**недисконтированными договорными потоками**, у пяти из шести с графой
балансовой стоимости рядом.

**Основа величины называется всегда.** Потоки по договору включают будущие
проценты, и с балансом их сумма не сойдётся никогда. Сверяется с балансом
только графа балансовой стоимости той же таблицы: её сумма по строкам долга
обязана равняться займам — документа у принятого комплекта, агрегатора
у комплекта в карантине (решение владельца 29.09.2026). Сошлось — значит
строки долга таблицы опознаны верно, и корзины потоков относятся к тому же
долгу. Графы балансовой стоимости нет (ЛСР) — сверять нечем, и это исход,
а не «прошло».

**Примечание о долге ищется по ссылке из строки формы**, как величины
примечаний (`ifrs_notes`). Примечание о рисках — по ссылке из текста
примечания о долге («…приведена в пояснении 26») и по наименованию
в указателе примечаний: наименование здесь — строение документа, слово
ищется только в заголовках указателя, а не по тексту.

**Строка таблицы читается арифметикой, а не пробелами.** Разряды у пяти
из шести разделены одиночным пробелом, как и графы: «79 153 108 165 -
31 451 15 656 35 794 25 264» читается однозначно только по тому, что итог
строки равен сумме корзин. Чтение, при котором равенство не выполняется,
отбрасывается; два выполняющихся — строка не прочитана, а не выбрана.

**Что считать долгом, какими корзинами печатать и с чем сверять — методика**
(`ifrs_note_lines.yaml`, `debt_maturity`). Её нет — разбор отказывается.
Как узнать таблицу в тексте — технический параметр (`ifrs_parsing.yaml`,
`maturity_table`).
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from finlib.normalize.ifrs_note_lines import DebtMaturity, MaturityBucket, MaturityRows
from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_extract import join_name
from finlib.sources.ifrs_notes import Note, NoteIndex, lines_of
from finlib.sources.ifrs_numbers import MaturityTablePolicy
from finlib.utils import marked_by

logger = logging.getLogger(__name__)

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}  # fmt: skip
_DATE = re.compile(r"(\d{1,2})\s*(" + "|".join(_MONTHS) + r")\s*(\d{4})", re.IGNORECASE)
_YEAR = re.compile(r"(?<!\d)(20\d\d)\s*год", re.IGNORECASE)
# Процентная ставка и диапазон ставок в строке таблицы: «13,90% - 20,95%».
_PERCENT = re.compile(r"(?:[-–]\s*)?\d+(?:[,.]\d+)?\s*%\**")
_DASHES = "-–—"


class Refusal(StrEnum):
    """Почему сроков нет: у каждого исхода свой текст для документа."""

    NO_POLICY = "состав сроков погашения в методике не утверждён"
    NO_TABLE = "таблица сроков погашения в примечаниях не найдена"
    NO_DEBT_ROWS = "в таблице сроков не опознано ни одной строки долга"


class Basis(StrEnum):
    """Основа величины сроков."""

    CARRYING = "балансовая стоимость"
    UNDISCOUNTED = "недисконтированные договорные потоки"


@dataclass(frozen=True, slots=True)
class Interval:
    """Графа сроков: границы в месяцах от отчётной даты и надпись шапки."""

    start: int
    end: int | None
    label: str

    def within(self, bucket: MaturityBucket) -> bool:
        """Лежит ли графа целиком внутри корзины печати."""
        if self.start < bucket.from_months:
            return False
        if bucket.to_months is None:
            return True
        return self.end is not None and self.end <= bucket.to_months


@dataclass(frozen=True, slots=True)
class MaturityRow:
    """Строка таблицы сроков: род, балансовая стоимость, итог и корзины."""

    name: str
    kind: str | None
    carrying: Decimal | None
    total: Decimal | None
    buckets: tuple[Decimal | None, ...]


@dataclass(frozen=True, slots=True)
class MaturityTable:
    """Таблица сроков на одну дату."""

    note: Note
    report_date: date | None
    intervals: tuple[Interval, ...]
    layout: str
    rows: tuple[MaturityRow, ...]
    # Строки с величинами, чтение которых не сошлось арифметикой.
    unread: tuple[str, ...] = ()

    @property
    def has_carrying(self) -> bool:
        """Есть ли у таблицы графа балансовой стоимости."""
        return "carrying" in self.layout

    def of_kind(self, kind: str | None) -> tuple[MaturityRow, ...]:
        """Строки названного рода; None — неопознанные."""
        return tuple(row for row in self.rows if row.kind == kind)

    def carrying_of(self, kind: str) -> Decimal | None:
        """Балансовая стоимость строк рода; None — графы нет либо строк нет."""
        rows = self.of_kind(kind)
        if not self.has_carrying or not rows:
            return None
        return sum((row.carrying or Decimal(0) for row in rows), Decimal(0))


@dataclass(frozen=True, slots=True)
class PrintedBucket:
    """Корзина печати: сумма граф внутри неё либо графа, пересекающая корзины."""

    name: str
    # Пусто — у всех строк графы прочерк.
    value: Decimal | None
    # Графа пересекает границу корзин и печатается как напечатана
    # (решение владельца 29.09.2026: «от 1 до 5 лет» не раскладывать).
    as_printed: bool = False
    # Код для графы «Код» документа: корзина методики либо границы графы.
    code: str = ""


@dataclass(frozen=True, slots=True)
class DebtNoteReading:
    """Итог чтения: примечание о долге, таблица сроков либо отказ."""

    references: tuple[int, ...] = ()
    debt_note: Note | None = None
    checked: tuple[Note, ...] = ()
    table: MaturityTable | None = None
    refusal: Refusal | None = None
    tables_seen: int = 0

    @property
    def basis(self) -> Basis | None:
        """Основа корзин: таблица риска ликвидности — всегда потоки по договору."""
        return Basis.UNDISCOUNTED if self.table is not None else None

    def describe(self) -> str:
        """Однострочная сводка для прогона."""
        note = (
            f"примечание о долге {self.debt_note.describe()}"
            if self.debt_note is not None
            else "примечание о долге по ссылке формы не найдено"
            + (" (ссылки нет)" if not self.references else "")
        )
        if self.table is None:
            return f"{note}; сроки: {self.refusal.value if self.refusal else 'нет'}"
        return (
            f"{note}; сроки — примечание {self.table.note.describe()}, "
            f"граф {len(self.table.intervals)}, строк долга "
            f"{len(self.table.of_kind('debt'))}"
        )


def _words(text: str) -> str:
    """Текст шапки одной строкой: переносы по слогам склеены, регистр снят."""
    joined = " ".join(text.split())
    joined = re.sub(r"(\w)-\s+(\w)", r"\1\2", joined)
    return joined.casefold().replace("ё", "е")


def header_intervals(text: str, policy: MaturityTablePolicy) -> tuple[Interval, ...]:
    """Графы сроков из шапки таблицы — по порядку; пусто — шапка не разобрана.

    Графы обязаны идти подряд от нуля: у каждой начало равно концу
    предыдущей. Иначе это не шапка сроков, и угадывать, какой графы
    не хватает, нельзя.
    """
    words = _words(text)
    number = "|".join(["\\d+", *map(re.escape, policy.number_words)])
    num = rf"(\d+|{number})"
    unit = "(" + "|".join(re.escape(item) for item in policy.unit_months) + r")\w*\.?"
    demand = "|".join(re.escape(item) for item in policy.on_demand_words)
    patterns = (
        ("demand_until",
         re.compile(rf"(?:до|по)\s+(?:{demand})\s+и\s+в\s+срок\s+до\s+{num}\s*{unit}")),
        ("demand", re.compile(rf"(?:до|по)\s+(?:{demand})")),
        ("from_to", re.compile(rf"от\s+{num}\s*(?:{unit})?\s*до\s+{num}\s*{unit}")),
        ("range", re.compile(rf"{num}\s*[{_DASHES}]\s*{num}\s*{unit}")),
        ("and_more", re.compile(rf"{num}\s*(?:{unit})?\s*и\s+более")),
        ("upto", re.compile(rf"(?:до|менее)\s+{num}\s*{unit}")),
        ("over", re.compile(rf"(?:свыше|более|больше)\s+{num}\s*{unit}")),
    )  # fmt: skip

    def value(token: str) -> int:
        return int(token) if token.isdigit() else policy.number_words[token]

    def months(amount: int, stem: str | None) -> int:
        factor = policy.unit_months[stem] if stem else Decimal(12)
        return int((Decimal(amount) * factor).quantize(Decimal(1), rounding=ROUND_HALF_UP))

    found: list[Interval] = []
    last_unit: str | None = None
    position = 0
    while True:
        hits = [
            (match.start(), order, kind, match)
            for order, (kind, pattern) in enumerate(patterns)
            if (match := pattern.search(words, position)) is not None
        ]
        if not hits:
            break
        _, _, kind, match = min(hits, key=lambda item: (item[0], item[1]))
        groups = match.groups()
        label = match.group(0)
        if kind == "demand":
            found.append(Interval(0, 0, label))
        elif kind == "demand_until":
            last_unit = groups[1]
            found.append(Interval(0, months(value(groups[0]), groups[1]), label))
        elif kind == "from_to":
            low, low_unit, high, high_unit = groups
            last_unit = high_unit
            start = months(value(low), low_unit or high_unit)
            found.append(Interval(start, months(value(high), high_unit), label))
        elif kind == "range":
            low, high, stem = groups
            last_unit = stem
            found.append(Interval(months(value(low), stem), months(value(high), stem), label))
        elif kind == "and_more":
            amount, stem = groups
            stem = stem or last_unit
            found.append(Interval(months(value(amount), stem), None, label))
        elif kind == "upto":
            amount, stem = groups
            last_unit = stem
            found.append(Interval(0, months(value(amount), stem), label))
        else:
            amount, stem = groups
            last_unit = stem
            found.append(Interval(months(value(amount), stem), None, label))
        position = match.end()
    if not found or found[0].start != 0:
        return ()
    for before, after in zip(found, found[1:], strict=False):
        if before.end is None or before.end != after.start:
            return ()
    return tuple(found)


def _dated(text: str) -> tuple[int, int, int] | int | None:
    """Последняя дата в тексте: день, месяц и год либо только год."""
    dates = _DATE.findall(text)
    if dates:
        day, month, year = dates[-1]
        return int(day), _MONTHS[month.casefold()], int(year)
    years = _YEAR.findall(text)
    return int(years[-1]) if years else None


def _as_date(found: tuple[int, int, int] | int | None, known: tuple[date, ...]) -> date | None:
    """Дата шапки среди отчётных дат комплекта; год без дня — по году."""
    if found is None:
        return None
    if isinstance(found, int):
        matches = [item for item in known if item.year == found]
        return matches[0] if len(matches) == 1 else None
    day, month, year = found
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _tail(line: str) -> tuple[str, list[str]]:
    """Наименование и хвост из групп цифр и прочерков; ставки вычищены."""
    cleaned = _PERCENT.sub(" ", line)
    tokens = cleaned.split()
    tail: list[str] = []
    while tokens and (tokens[-1].isdigit() or tokens[-1] in _DASHES):
        tail.insert(0, tokens.pop())
    return " ".join(tokens), tail


def _readings(tokens: list[str], count: int) -> list[tuple[Decimal | None, ...]]:
    """Все разбиения хвоста на `count` величин: разряды — группы по три цифры."""
    found: list[tuple[Decimal | None, ...]] = []

    def walk(at: int, taken: tuple[Decimal | None, ...]) -> None:
        left = count - len(taken)
        if left == 0:
            if at == len(tokens):
                found.append(taken)
            return
        if len(tokens) - at < left:
            return
        head = tokens[at]
        if head in _DASHES:
            walk(at + 1, (*taken, None))
            return
        digits = head
        walk(at + 1, (*taken, Decimal(digits)))
        if len(head) > 3:
            return
        step = at + 1
        while step < len(tokens) and len(tokens[step]) == 3 and tokens[step].isdigit():
            digits += tokens[step]
            step += 1
            walk(step, (*taken, Decimal(digits)))

    walk(0, ())
    return found


# Расположение граф итога и балансовой стоимости относительно корзин.
# Брусника, О'КЕЙ, Самолёт, Автодор: балансовая, итог, корзины; Сегежа:
# корзины, итог, балансовая; ЛСР: корзины, итог — графы балансовой нет.
LAYOUTS = (
    "carrying_total_buckets",
    "buckets_total_carrying",
    "buckets_total",
    "total_buckets",
)


def _fit(
    tokens: list[str], layout: str, width: int, tolerance: int
) -> list[tuple[Decimal | None, Decimal | None, tuple[Decimal | None, ...]]]:
    """Чтения строки при раскладке, у которых итог равен сумме корзин."""
    count = width + 1 + (1 if "carrying" in layout else 0)
    fitted = _fitted(_readings(tokens, count), layout, tolerance)
    if not fitted and tokens and tokens[0] in _DASHES:
        # Прочерк графы ставки перед величинами (ЛСР: «Торговая и прочая
        # кредиторская задолженность - 59 758 7 420 199 67 377»): величиной
        # он не является, и арифметика строки это подтверждает.
        fitted = _fitted(_readings(tokens[1:], count), layout, tolerance)
    return fitted


def _fitted(
    readings: list[tuple[Decimal | None, ...]], layout: str, tolerance: int
) -> list[tuple[Decimal | None, Decimal | None, tuple[Decimal | None, ...]]]:
    """Чтения, у которых итог строки равен сумме корзин при раскладке."""
    fitted = []
    for values in readings:
        if layout == "carrying_total_buckets":
            carrying, total, buckets = values[0], values[1], values[2:]
        elif layout == "buckets_total_carrying":
            carrying, total, buckets = values[-1], values[-2], values[:-2]
        elif layout == "buckets_total":
            carrying, total, buckets = None, values[-1], values[:-1]
        else:
            carrying, total, buckets = None, values[0], values[1:]
        # Для проверки арифметики прочерк — ноль (инвариант 4).
        summed = sum((item or Decimal(0) for item in buckets), Decimal(0))
        if total is None or abs(total - summed) > tolerance:
            continue
        fitted.append((carrying, total, tuple(buckets)))
    return fitted


def _strip_rate(name: str, policy: MaturityTablePolicy) -> str:
    """Наименование без слов ставки и валюты: «в руб.*» своим не считается."""
    words = "|".join(re.escape(normalize_name(word)) for word in policy.rate_words)
    return " ".join(re.sub(rf"(?<!\S)(?:{words})(?!\S)", " ", normalize_name(name)).split())


def _shown(name: str, policy: MaturityTablePolicy) -> str:
    """Наименование для печати: хвост ставки и валюты («в руб.*») отрезан."""
    words = "|".join(re.escape(word) for word in policy.rate_words)
    cut = re.split(rf"\s(?:{words})(?:\W|$)", f" {name}", maxsplit=1, flags=re.IGNORECASE)[0]
    return cut.strip() or name


def _glued(lines: list[str], own: str) -> str:
    """Наименование строки: перенос склеивается, пустое берёт строки выше."""
    if own:
        name = join_name(lines, own)
    elif not lines:
        return ""
    else:
        name = join_name(lines[:-1], lines[-1])
    # Перенос по слогам внутри наименования: «креди- торская» (Сегежа).
    return re.sub(r"(\w)-\s+(\w)", r"\1\2", name)


def _is_prose(line: str, policy: MaturityTablePolicy) -> bool:
    """Строка прозы: много слов и не шапка сроков.

    Шапка Брусники стоит одной строкой в двенадцать слов — «договору до 1
    года 1-2 года 2-3 года 3 и более млн руб.», — и по числу слов прозой
    выглядит; отличает её то, что графы сроков из неё читаются.
    """
    return (
        len(line.split()) >= policy.prose_min_words
        and not header_intervals(line, policy)
        and _dated(line) is None
    )


@dataclass
class _Draft:
    """Таблица в сборке: шапка, дата и сырые строки."""

    intervals: tuple[Interval, ...]
    report_date: date | None
    # Наименование, хвост величин и строки над ним: заголовок группы
    # («Необеспеченные банковские кредиты» у ЛСР) стоит отдельной строкой.
    rows: list[tuple[str, list[str], list[str]]] = field(default_factory=list)


def _header_end(buffer: list[str], policy: MaturityTablePolicy) -> int:
    """Номер последней строки шапки в буфере: дальше — наименование статьи."""
    last = -1
    for number, line in enumerate(buffer):
        if (
            _dated(line) is not None
            or header_intervals(line, policy)
            or marked_by(line, policy.header_words, _words)
        ):
            last = number
    return last


def read_tables(
    note: Note,
    text: str,
    known_dates: tuple[date, ...],
    rows: MaturityRows,
    policy: MaturityTablePolicy,
) -> tuple[MaturityTable, ...]:
    """Таблицы сроков одного примечания — по одной на дату.

    `rows` — роды строк методики (`MaturityRows`): по ним строка таблицы
    получает род, а строка из одних слов ставки наследует род заголовка
    группы над ней.
    """
    drafts: list[_Draft] = []
    current: _Draft | None = None
    buffer: list[str] = []
    for line in lines_of(note, text):
        name, tail = _tail(line)
        # Строка величин несёт корзины и итог — не меньше `min_buckets + 1`
        # значений; «0 – 30» из шапки Сегежи строкой не является.
        if len(tail) <= policy.min_buckets or not any(item.isdigit() for item in tail):
            buffer.append(line)
            continue
        # Шапка — строки после последней прозы: пояснение над таблицей
        # («…включая расчётные суммы процентных платежей») шапкой не является.
        prose = [i for i, item in enumerate(buffer) if _is_prose(item, policy)]
        head = buffer[prose[-1] + 1 :] if prose else buffer
        joined = "\n".join(buffer)
        intervals = header_intervals("\n".join(head), policy)
        if len(intervals) >= policy.min_buckets:
            buffer = head
            end = _header_end(buffer, policy)
            current = _Draft(intervals, _as_date(_dated("\n".join(buffer[: end + 1])), known_dates))
            drafts.append(current)
            names = buffer[end + 1 :]
        elif current is not None and _dated(joined) is not None:
            # Новая дата под прежней шапкой (Сегежа): таблица продолжается,
            # а наименование статьи начинается после строки с датой.
            dated_at = max(i for i, item in enumerate(buffer) if _dated(item) is not None)
            current = _Draft(current.intervals, _as_date(_dated(joined), known_dates))
            drafts.append(current)
            names = buffer[dated_at + 1 :]
        elif current is not None and prose:
            # Проза между строками — таблица кончилась.
            current = None
            buffer = []
            continue
        else:
            names = buffer
        if current is not None:
            current.rows.append((_glued(names, name), tail, names))
        buffer = []
    return tuple(
        table
        for draft in drafts
        if (table := _read_draft(note, draft, rows, policy)) is not None
    )


def _read_draft(
    note: Note, draft: _Draft, kinds: MaturityRows, policy: MaturityTablePolicy
) -> MaturityTable | None:
    """Раскладка таблицы — та, при которой прочитано больше строк; строки с родом."""
    width = len(draft.intervals)
    scores = []
    for layout in LAYOUTS:
        read = sum(
            1
            for _, tail, _ in draft.rows
            if len(_fit(tail, layout, width, policy.row_total_tolerance)) == 1
        )
        scores.append((read, layout))
    scores.sort(reverse=True)
    best, layout = scores[0]
    if best == 0 or (len(scores) > 1 and scores[1][0] == best):
        logger.info(
            "примечание %s: раскладка таблицы сроков не определена (%s)", note.number, scores
        )
        return None
    rows: list[MaturityRow] = []
    unread: list[str] = []
    group: tuple[str, str] | None = None
    for name, tail, above in draft.rows:
        fitted = _fit(tail, layout, width, policy.row_total_tolerance)
        for line in above:
            header = _strip_rate(line, policy)
            if header and (found := kinds.kind_of(header)) is not None:
                group = (_shown(line, policy), found)
        own = _strip_rate(name, policy)
        kind = kinds.kind_of(own) if own else None
        if kind is not None:
            name = _shown(name, policy)
        if not name or normalize_name(name).startswith(tuple(policy.total_words)):
            # Строка итога: без наименования (Автодор, Сегежа) либо «Итого».
            kind = "total"
        elif own and kind is not None:
            group = (name, kind)
        elif not own and group is not None:
            # Строка из одних слов ставки и валюты («в руб.*») — вид долга
            # заголовка группы над ней (ЛСР).
            name, kind = group[0], group[1]
        if len(fitted) != 1:
            unread.append(name or " ".join(tail))
            continue
        carrying, total, buckets = fitted[0]
        rows.append(MaturityRow(name, kind, carrying, total, buckets))
    return MaturityTable(
        note, draft.report_date, draft.intervals, layout, tuple(rows), tuple(unread)
    )


def debt_references(extraction: "object", found_in: tuple[str, ...]) -> tuple[int, ...]:
    """Номера примечаний, на которые ссылаются строки долга формы."""
    return tuple(
        sorted(
            {
                number
                for item in extraction.values  # type: ignore[attr-defined]
                if item.code in found_in
                for number in item.note_reference
            }
        )
    )


def read_debt_note(
    text: str,
    index: NoteIndex,
    extraction: "object",
    report_date: date,
    known_dates: tuple[date, ...],
    method: DebtMaturity | None,
    policy: MaturityTablePolicy,
) -> DebtNoteReading:
    """Примечание о долге и таблица сроков на отчётную дату — либо отказ."""
    if method is None:
        return DebtNoteReading(refusal=Refusal.NO_POLICY)
    references = debt_references(extraction, method.found_in)
    debt_note = next(
        (note for number in references if (note := index.get(number)) is not None), None
    )
    candidates: list[Note] = []
    if debt_note is not None:
        words = "|".join(re.escape(item) for item in policy.reference_words)
        body = "\n".join(lines_of(debt_note, text))
        for number in re.findall(rf"(?:{words})\w*\s+(\d{{1,2}})(?!\d)", body, re.IGNORECASE):
            note = index.get(int(number))
            if note is not None and note is not debt_note and note not in candidates:
                candidates.append(note)
    for note in index.notes:
        if marked_by(note.title, policy.risk_note_titles, _words) and note not in candidates:
            candidates.append(note)
    seen = 0
    fallback: Refusal = Refusal.NO_TABLE
    for note in candidates:
        tables = read_tables(note, text, known_dates, method.rows, policy)
        seen += len(tables)
        for table in tables:
            if table.report_date != report_date:
                continue
            if not table.of_kind("debt"):
                fallback = Refusal.NO_DEBT_ROWS
                continue
            return DebtNoteReading(
                references, debt_note, tuple(candidates), table, tables_seen=seen
            )
    return DebtNoteReading(
        references, debt_note, tuple(candidates), refusal=fallback, tables_seen=seen
    )


def printed_buckets(
    table: MaturityTable, method: DebtMaturity, kinds: tuple[str, ...] = ("debt",)
) -> tuple[PrintedBucket, ...]:
    """Корзины печати по строкам названных родов.

    Графы хранятся как напечатаны; в корзину методики они сводятся только
    при печати и только если лежат в ней целиком **и покрывают её целиком**:
    у Брусники «2–3 года» лежит внутри «от 2 до 5 лет», но под именем корзины
    была бы её часть, а «3 и более» пересекает границу — обе графы
    печатаются как есть. Код строки — тот же, что у хранимой графы: основа,
    род и границы в месяцах; у корзины — её границы.
    """
    rows = [row for row in table.rows if row.kind in kinds]
    printed: list[tuple[int, PrintedBucket]] = []
    taken: set[int] = set()

    def summed(numbers: list[int]) -> Decimal | None:
        # Прочерк не ноль (инвариант 4): графа, у всех строк которой прочерк,
        # печатается прочерком, а не нулём.
        values = [
            row.buckets[number]
            for row in rows
            for number in numbers
            if row.buckets[number] is not None
        ]
        return sum(values, Decimal(0)) if values else None

    def code(start: int, end: int | None) -> str:
        # Сумма родов — сумма хранимых кодов: своего кода у неё нет.
        return " + ".join(
            method.storage.code("undiscounted", kind, start, end) for kind in kinds
        )

    for bucket in method.buckets:
        inside = [
            number
            for number, interval in enumerate(table.intervals)
            if interval.within(bucket) and number not in taken
        ]
        if not inside or not _covers([table.intervals[i] for i in inside], bucket):
            continue
        taken.update(inside)
        printed.append(
            (
                bucket.from_months,
                PrintedBucket(
                    bucket.name, summed(inside),
                    code=code(bucket.from_months, bucket.to_months),
                ),
            )
        )  # fmt: skip
    for number, interval in enumerate(table.intervals):
        if number in taken:
            continue
        printed.append(
            (
                interval.start,
                PrintedBucket(
                    interval.label, summed([number]), as_printed=True,
                    code=code(interval.start, interval.end),
                ),
            )
        )  # fmt: skip
    return tuple(item for _, item in sorted(printed, key=lambda pair: pair[0]))


@dataclass(frozen=True, slots=True)
class StoredFact:
    """Величина к хранению: код (основа, род, границы) и величина графы."""

    code: str
    value: Decimal
    label: str


def stored_facts(table: MaturityTable, method: DebtMaturity) -> tuple[StoredFact, ...]:
    """Графы таблицы как напечатаны — по родам «займы» и «долгоподобные».

    Прочерк — не ноль (инвариант 4): графа, у всех строк рода которой стоит
    прочерк, к хранению не идёт. Балансовая стоимость строк рода хранится
    кодом всей шкалы — от нуля, открытой сверху.
    """
    found: list[StoredFact] = []
    for kind in ("debt", "debt_like"):
        rows = table.of_kind(kind)
        if not rows:
            continue
        for number, interval in enumerate(table.intervals):
            values = [row.buckets[number] for row in rows if row.buckets[number] is not None]
            if not values:
                continue
            found.append(
                StoredFact(
                    method.storage.code("undiscounted", kind, interval.start, interval.end),
                    sum(values, Decimal(0)),
                    interval.label,
                )
            )
        carrying = table.carrying_of(kind)
        if carrying is not None:
            found.append(
                StoredFact(
                    method.storage.code("carrying", kind, 0, None), carrying, "балансовая"
                )
            )
    return tuple(found)


def _covers(intervals: list[Interval], bucket: MaturityBucket) -> bool:
    """Покрывают ли графы корзину целиком — от её начала до конца."""
    edge = bucket.from_months
    for interval in sorted(intervals, key=lambda item: (item.start, item.end or 0)):
        if interval.start != edge and not (interval.start == 0 and interval.end == 0):
            return False
        if interval.end is None:
            return bucket.to_months is None
        edge = max(edge, interval.end)
    return bucket.to_months is not None and edge == bucket.to_months


class Check(StrEnum):
    """Исход сверки суммы строк таблицы с займами опоры."""

    PASSED = "сошлось"
    WITH_LEASE = "сошлось с займами и арендой"
    # Долгоподобные у опоры в займы не входят (агрегатор у Автодора: займы
    # 798 411 — ровно облигации), и сумма обеих величин сверена быть
    # не может; займы при этом сошлись.
    LOANS_ONLY = "сошлись займы; долгоподобные опорой не раскрыты"
    FAILED = "не сошлось"
    NO_CARRYING = "графы балансовой стоимости в таблице нет"
    NO_REFERENCE = "займов у опоры нет"


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """Сверка: с чем, исход и обе стороны в своих единицах.

    `table_value` — сумма обеих величин таблицы («займы + долгоподобные»),
    `loans` — одни займы; без долгоподобных строк они равны.
    """

    against: str
    outcome: Check
    table_value: Decimal | None = None
    reference: Decimal | None = None
    reference_with_lease: Decimal | None = None
    table_unit: str | None = None
    reference_unit: str | None = None
    loans: Decimal | None = None
    debt_like: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        """Сошлась ли сумма обеих величин — с оговоркой об аренде или без."""
        return self.outcome in (Check.PASSED, Check.WITH_LEASE)

    @property
    def loans_passed(self) -> bool:
        """Сошлись ли займы — суммой обеих величин либо сами по себе."""
        return self.passed or self.outcome is Check.LOANS_ONLY


def _total(values: dict[str, Decimal | None], codes: tuple[str, ...]) -> Decimal | None:
    """Сумма названных величин; хоть одной нет — суммы нет, а не частичная."""
    found = [values.get(code) for code in codes]
    if any(item is None for item in found):
        return None
    return sum(found, Decimal(0))  # type: ignore[arg-type]


def reconcile_debt(
    table: MaturityTable,
    table_unit: str,
    reference: dict[str, Decimal | None],
    reference_unit: str | None,
    against: str,
    method: DebtMaturity,
) -> Reconciliation:
    """Сумма балансовой стоимости «займы + долгоподобные» против займов опоры.

    Опора — баланс документа у принятого комплекта, займы агрегатора
    у комплекта в карантине (решение владельца 29.09.2026); какая — называет
    `against`. Сверяется сумма обеих величин (решение 30.09.2026); не сошлась,
    но сходится с «займы + аренда» — пройдено с оговоркой. Сумма не сошлась,
    а займы сами по себе сошлись — опора долгоподобных не включает, и это
    называется, а не выдаётся за расхождение. Допуск — одна единица более
    грубой стороны (`quality.reconcile.compare`).
    """
    from finlib.quality.reconcile import Outcome, Side, compare

    loans = table.carrying_of("debt")
    like = table.carrying_of("debt_like")
    names = tuple(row.name for row in table.of_kind("debt_like"))
    if loans is None:
        return Reconciliation(
            against, Check.NO_CARRYING, table_unit=table_unit, debt_like=names
        )
    ours = loans + (like or Decimal(0))
    borrowed = _total(reference, method.found_in)
    if borrowed is None:
        return Reconciliation(
            against, Check.NO_REFERENCE, ours, table_unit=table_unit,
            reference_unit=reference_unit, loans=loans, debt_like=names,
        )  # fmt: skip
    leased = _total(reference, method.lease_lines)
    with_lease = borrowed + leased if leased is not None else None
    date_ = table.report_date or date.min

    def matches(value: Decimal, target: Decimal | None) -> bool:
        if target is None:
            return False
        found = compare("debt", date_, Side(value, table_unit), Side(target, reference_unit))
        return found.outcome is Outcome.MATCH

    if matches(ours, borrowed):
        outcome = Check.PASSED
    elif matches(ours, with_lease):
        outcome = Check.WITH_LEASE
    elif like is not None and matches(loans, borrowed):
        outcome = Check.LOANS_ONLY
    else:
        outcome = Check.FAILED
    return Reconciliation(
        against, outcome, ours, borrowed, with_lease, table_unit, reference_unit,
        loans, names,
    )  # fmt: skip
