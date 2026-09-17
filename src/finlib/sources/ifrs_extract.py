"""Извлечение основных форм МСФО в унифицированную модель статей.

Позиция опознаётся по наименованию через справочник синонимов: кодов строк,
утверждённых нормативным актом, в консолидированной отчётности нет.

**Итоги разделов не всегда подписаны.** У Норникеля итог внеоборотных
активов — просто число без наименования, у ФосАгро слово «Внеоборотные
активы» служит и заголовком раздела, и подписью итога. Поэтому опознание
итога не может опираться на наименование: опорой служит структура —
последняя числовая строка блока, равная сумме предшествующих строк того же
блока. Равенство здесь не контроль качества, а способ опознания: оно
отвечает на вопрос «что это за строка», а не «сошлась ли отчётность».

**Текст под формой извлекается наравне с таблицей.** У ЛСР сноской под
балансом сказано, что в состав денежных средств не включены средства
на счетах эскроу — 217 501 млн руб. Парсер таблиц её не увидит, а без неё
показатели ликвидности читаются неверно.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache

from finlib.normalize.ifrs_lines import IfrsCatalog, IfrsPosition, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_numbers import Grouping, parse_amount

logger = logging.getLogger(__name__)

# Ячейка таблицы читается по конвенции документа, и это не придирка:
# разделитель разрядов и разделитель колонок — оба пробелы, отличаются они
# только числом. «700 000        650 000» — это две величины, а не одна,
# и единственное, что их разделяет, — ширина промежутка. Внутри числа
# пробел ровно один, между колонками — два и более.
_NARROW_SPACE = "   "

_CELL_BY_GROUPING: dict[Grouping, str] = {
    Grouping.RUSSIAN: (
        rf"\(?[-−]?\d{{1,3}}(?:[ {_NARROW_SPACE}]\d{{3}})*(?:,\d+)?\)?"
    ),
    Grouping.ENGLISH: r"\(?[-−]?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?",
    Grouping.PLAIN: r"\(?[-−]?\d+(?:[.,]\d+)?\)?",
}

# Сноска под формой: строка, начинающаяся со звёздочки, решётки или
# «Примечание», либо предложение о составе статьи.
_FOOTNOTE_MARKERS = ("*", "**", "примечание", "в том числе", "включая", "не включ")


@dataclass(frozen=True, slots=True)
class ExtractedValue:
    """Величина статьи за один период."""

    code: str
    report_date: date
    value: Decimal
    source_name: str


@dataclass(frozen=True, slots=True)
class UnrecognisedRow:
    """Строка таблицы, которую справочник не опознал.

    Не теряется: экран сверки обязан показать её человеку, а до
    подтверждения комплект автоматически не проходит.
    """

    form: str
    source_name: str
    values: tuple[Decimal, ...]

    @property
    def largest(self) -> Decimal:
        """Наибольшая по модулю величина строки — по ней считается доля."""
        return max((abs(item) for item in self.values), default=Decimal(0))


@dataclass
class ExtractedForm:
    """Одна форма отчётности, разобранная в унифицированную модель."""

    code: str
    values: list[ExtractedValue] = field(default_factory=list)
    unrecognised: list[UnrecognisedRow] = field(default_factory=list)
    # Текст под таблицей формы: сноски о составе статей и ограничениях.
    notes_under_form: tuple[str, ...] = ()
    # Итоги, опознанные по структуре, а не по наименованию.
    totals_by_structure: tuple[str, ...] = ()
    # Строк таблицы всего и сколько из них опознано. Счётчик именно строк:
    # величин больше, потому что у строки столько величин, сколько периодов,
    # и смешение единиц счёта — повторяющийся источник ошибок.
    rows_total: int = 0
    rows_recognised: int = 0


@dataclass
class Extraction:
    """Итог разбора документа: формы, величины и всё, что не опознано."""

    forms: dict[str, ExtractedForm] = field(default_factory=dict)

    @property
    def values(self) -> list[ExtractedValue]:
        """Все извлечённые величины одним перечнем."""
        return [item for form in self.forms.values() for item in form.values]

    @property
    def unrecognised(self) -> list[UnrecognisedRow]:
        """Все неопознанные строки одним перечнем."""
        return [item for form in self.forms.values() for item in form.unrecognised]

    @property
    def notes(self) -> tuple[str, ...]:
        """Весь текст, извлечённый из-под форм."""
        return tuple(
            dict.fromkeys(
                note for form in self.forms.values() for note in form.notes_under_form
            )
        )

    @property
    def rows_total(self) -> int:
        """Строк таблиц всего — величин больше, и путать их нельзя."""
        return sum(form.rows_total for form in self.forms.values())

    @property
    def rows_recognised(self) -> int:
        """Строк, опознанных справочником."""
        return sum(form.rows_recognised for form in self.forms.values())

    def totals(self, report_date: date) -> dict[str, Decimal]:
        """Итоговые величины за период — вход для проверки правдоподобия."""
        return {
            item.code: item.value
            for item in self.values
            if item.report_date == report_date
        }

    def value_of(self, code: str, report_date: date) -> Decimal | None:
        """Величина статьи за период; None — статья не извлечена."""
        return next(
            (
                item.value
                for item in self.values
                if item.code == code and item.report_date == report_date
            ),
            None,
        )

    def describe(self) -> str:
        """Однострочная сводка со счётчиками проверенного."""
        return (
            f"форм разобрано {len(self.forms)}, величин извлечено "
            f"{len(self.values)}, строк не опознано {len(self.unrecognised)}, "
            f"итогов опознано структурой "
            f"{sum(len(form.totals_by_structure) for form in self.forms.values())}, "
            f"сносок под формами {len(self.notes)}"
        )


def extract(
    text: str,
    report_dates: tuple[date, ...],
    grouping: Grouping,
    catalog: IfrsCatalog | None = None,
) -> Extraction:
    """Разбирает документ по формам справочника.

    report_dates и grouping приходят от приёма файла: разбирать числа,
    не зная конвенции, нельзя, а раскладывать их по периодам, не зная дат,
    не во что.
    """
    catalog = catalog or load_ifrs_lines()
    blocks = _split_by_forms(text, catalog)

    result = Extraction()
    for form_code, lines in blocks.items():
        result.forms[form_code] = _extract_form(
            form_code, lines, report_dates, grouping, catalog
        )
    logger.info("разбор документа: %s", result.describe())
    return result


def _split_by_forms(text: str, catalog: IfrsCatalog) -> dict[str, list[str]]:
    """Делит документ на блоки по заголовкам форм.

    Заголовки ищет тот же код, что и приём документа (`ifrs_inbox.
    form_headings`): по ядру наименования и по тому, что следом идёт таблица.
    Два способа искать одно и то же неминуемо разойдутся — у ЛСР форма
    называется «Раскрываемый консолидированный отчет о финансовом положении»,
    приём её находил, а разбор нет, и комплект давал ноль опознанных строк.
    """
    from finlib.sources.ifrs_inbox import form_headings
    from finlib.sources.ifrs_numbers import load_parsing_policy

    headings = form_headings(text, catalog, load_parsing_policy())
    if not headings:
        return {}

    ordered = sorted(headings.items(), key=lambda item: item[1])
    blocks: dict[str, list[str]] = {}
    for index, (code, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else len(text)
        blocks[code] = text[start:end].split("\n")
    return blocks


def _extract_form(
    form_code: str,
    lines: list[str],
    report_dates: tuple[date, ...],
    grouping: Grouping,
    catalog: IfrsCatalog,
) -> ExtractedForm:
    """Разбирает один блок формы: величины, неопознанные строки, сноски."""
    form = ExtractedForm(form_code)
    rows: list[tuple[str, tuple[Decimal, ...], int]] = []
    tail_from = 0

    for index, line in enumerate(lines):
        name, values = _split_row(line, grouping)
        if not values:
            continue
        rows.append((name, values, index))
        tail_from = index + 1

    known = catalog.for_form(form_code)
    recognised: dict[int, IfrsPosition] = {}
    for position_index, (name, _, _) in enumerate(rows):
        found = catalog.match_by_name(name) if name else None
        if found is not None and found.form == form_code:
            recognised[position_index] = found

    _name_totals_by_structure(rows, recognised, known, form)

    form.rows_total = len(rows)
    for position_index, (name, values, _) in enumerate(rows):
        position = recognised.get(position_index)
        if position is None:
            form.unrecognised.append(UnrecognisedRow(form_code, name.strip(), values))
            continue
        form.rows_recognised += 1
        for report_date, value in zip(report_dates, values, strict=False):
            form.values.append(
                ExtractedValue(position.code, report_date, value, name.strip())
            )

    form.notes_under_form = _notes_after(lines[tail_from:])
    return form


@lru_cache(maxsize=8)
def _cells_pattern(grouping: Grouping) -> re.Pattern[str]:
    """Как выглядит ячейка с величиной при этой конвенции записи чисел."""
    return re.compile(_CELL_BY_GROUPING[grouping])


def _split_row(line: str, grouping: Grouping) -> tuple[str, tuple[Decimal, ...]]:
    """Делит строку таблицы на наименование и величины периодов.

    Величины ищутся в хвосте строки: наименование стоит слева и содержать
    чисел не обязано, а вот числа справа — это колонки периодов. Ячейка
    опознаётся по конвенции документа, иначе «700 000  650 000» слипается
    в одну величину.
    """
    stripped = line.rstrip()
    if not stripped.strip():
        return "", ()

    pattern = _cells_pattern(grouping)
    matches = list(pattern.finditer(stripped))
    if not matches:
        return stripped.strip(), ()

    # Хвост числовых ячеек: подряд идущие числа в конце строки. Число внутри
    # наименования («Примечание 12») колонкой не является.
    tail: list[re.Match[str]] = []
    position = len(stripped)
    for match in reversed(matches):
        between = stripped[match.end() : position].strip()
        if between:
            break
        tail.append(match)
        position = match.start()
    tail.reverse()
    if not tail:
        return stripped.strip(), ()

    parsed = tuple(
        value
        for value in (parse_amount(item.group(), grouping) for item in tail)
        if value is not None
    )
    if not parsed:
        return stripped.strip(), ()
    return stripped[: tail[0].start()].strip(), parsed


def _name_totals_by_structure(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    known: tuple[IfrsPosition, ...],
    form: ExtractedForm,
) -> None:
    """Опознаёт неподписанные итоги разделов по равенству сумме предшествующих.

    Наименования у такой строки либо нет вовсе (Норникель), либо оно занято
    заголовком раздела (ФосАгро). Единственная опора — структура: строка,
    равная сумме предшествующих строк блока, и есть его итог.

    Равенство проверяется по первому периоду: если строка окажется итогом
    по одному периоду и не окажется по другому, это не итог, а совпадение.
    """
    totals = [item for item in known if item.is_total]
    named: list[str] = []
    for index, (name, values, _) in enumerate(rows):
        if index in recognised or not values:
            continue
        preceding = [
            rows[earlier][1]
            for earlier in range(index)
            if earlier in recognised and not recognised[earlier].is_total
        ]
        if len(preceding) < 2:
            continue
        candidate = _matching_total(values, preceding, totals, recognised)
        if candidate is None:
            continue
        recognised[index] = candidate
        named.append(candidate.code)
        logger.info(
            "итог %s опознан структурой: подпись «%s» справочнику неизвестна",
            candidate.code,
            name or "отсутствует",
        )
    form.totals_by_structure = tuple(named)


def _matching_total(
    values: tuple[Decimal, ...],
    preceding: list[tuple[Decimal, ...]],
    totals: tuple[IfrsPosition, ...],
    recognised: dict[int, IfrsPosition],
) -> IfrsPosition | None:
    """Какой итог справочника описывает эта строка; None — ни один.

    Строка обязана равняться сумме предшествующих по всем периодам сразу:
    совпадение по одному периоду бывает случайным, по двум — уже нет.
    """
    periods = min(len(values), min(len(item) for item in preceding))
    if periods == 0:
        return None
    for position in range(periods):
        total = sum((item[position] for item in preceding), start=Decimal(0))
        if total != values[position]:
            return None
    taken = {item.code for item in recognised.values()}
    return next((item for item in totals if item.code not in taken), None)


def _notes_after(lines: list[str]) -> tuple[str, ...]:
    """Текст под таблицей формы: сноски о составе статей.

    Берутся только строки с маркером сноски, а не всякий текст под формой.
    Первая редакция считала сноской любое предложение с заглавной буквы
    и точкой — и на документе с колонтитулами насчитала восемьдесят четыре
    «сноски» из повторов одной строки. Широкая эвристика здесь хуже узкой:
    лишний текст уходит в заключение и выглядит содержательным.

    Повторы снимаются: одна и та же сноска печатается на каждой странице.
    """
    found: list[str] = []
    for line in lines:
        text = " ".join(line.split())
        if len(text) < 20:
            continue
        lowered = normalize_name(text)
        if any(normalize_name(mark) in lowered for mark in _FOOTNOTE_MARKERS):
            found.append(text)
    return tuple(dict.fromkeys(found))
