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

    Соседи хранятся рядом с наименованием: «Прочие» или «Итого» без контекста
    не опознать, а в форме такая строка стоит внутри раздела, и раздел виден
    по соседним строкам. У семи строк наименования нет вовсе — только
    величины, — и соседи для них единственная опора.
    """

    form: str
    source_name: str
    values: tuple[Decimal, ...]
    previous_name: str = ""
    next_name: str = ""
    # Место строки в таблице формы. Опознавать строку по наименованию нельзя:
    # у семи строк его нет вовсе, а «Прочие расходы» встречается в форме
    # дважды. По имени решение человека применялось не к той строке либо
    # не применялось вовсе — строка возвращалась в очередь, хотя её величина
    # уже была учтена в итоге.
    index: int = 0

    @property
    def key(self) -> tuple[str, int]:
        """Устойчивый ключ строки: форма и место в ней."""
        return (self.form, self.index)

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
    # Строки, отсеянные без участия человека: колонтитулы и контрольные суммы.
    # Хранятся, а не выбрасываются: из метрики общности их надо исключить
    # явно, а не тем, что их не видно.
    auto_dismissed: list[tuple[str, tuple[Decimal, ...], str]] = field(
        default_factory=list
    )


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
    from finlib.sources.ifrs_inbox import form_blocks, form_headings
    from finlib.sources.ifrs_numbers import load_parsing_policy

    policy = load_parsing_policy()
    headings = form_headings(text, catalog, policy)
    return form_blocks(text, headings, policy)


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

    pending: list[str] = []
    for index, line in enumerate(lines):
        name, values = _split_row(line, grouping, len(report_dates))
        if not values:
            # Строка без величин — либо заголовок раздела, либо начало
            # наименования, перенесённого вёрсткой. Какая именно, станет
            # видно на следующей строке с величинами.
            if name:
                pending.append(name)
            continue
        rows.append((_joined(pending, name), values, index))
        pending.clear()
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
            dismissal = _auto_dismissal(name, values, rows[:position_index])
            if dismissal is not None:
                # Колонтитул и контрольная сумма — не статьи, и показывать их
                # человеку незачем. Важнее другое: в метрике общности они
                # искусственно завышали долю общих статей, потому что номер
                # страницы встречается у всех эмитентов.
                form.auto_dismissed.append((name, values, dismissal))
                continue
            form.unrecognised.append(
                UnrecognisedRow(
                    form_code,
                    name.strip(),
                    values,
                    previous_name=rows[position_index - 1][0].strip()
                    if position_index
                    else "",
                    next_name=rows[position_index + 1][0].strip()
                    if position_index + 1 < len(rows)
                    else "",
                    index=position_index,
                )
            )
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


def _auto_dismissal(
    name: str, values: tuple[Decimal, ...], earlier: list[tuple]
) -> str | None:
    """Почему строку можно отсеять без человека; None — нельзя.

    Два случая, и оба про строки без наименования. Одно число — колонтитул
    или номер страницы. Два числа, повторяющие ранее встреченную строку, —
    контрольная сумма разбивки: у Сегежи убыток печатается ещё раз под
    разбивкой «неконтролирующим долям участия», и в итог он войти не должен.

    Строка с наименованием так не отсеивается никогда: решение о ней
    принимает человек.
    """
    if name.strip():
        return None
    if len(values) == 1:
        return "auto_not_item"
    for previous_name, previous_values, _ in earlier:
        if previous_values == values and previous_name.strip():
            return f"duplicate_of:{previous_name.strip()}"
    return None


def _looks_like_note_number(value: Decimal) -> bool:
    """Похожа ли величина на номер примечания, а не на сумму.

    Номера примечаний двузначные и целые. Величина отчётности такой тоже
    бывает, но не в одиночку: у статьи значение есть за каждый период.
    """
    return value == value.to_integral_value() and 0 < value <= MAX_NOTE_NUMBER


# Наибольший номер примечания, встреченный в разобранных комплектах, — сорок
# с небольшим. Округлено вверх с запасом.
MAX_NOTE_NUMBER = Decimal(99)


def _unglue(texts: list[str], periods: int, grouping: Grouping) -> list[str]:
    """Разрезает ячейку, в которую слиплись величины нескольких колонок.

    Разделитель разрядов и разделитель колонок — оба пробел, и различить их
    по ширине нельзя: у Автодора «856 349 835 020» — это 856 349 и 835 020,
    а прочитывалось как одно число в восемьсот пятьдесят шесть миллиардов
    при валюте баланса в полтора миллиона.

    Опора — число групп: четыре группы по три цифры при двух периодах делятся
    поровну. Делится только та ячейка, которая делится нацело; неровную
    не трогаем — угадывать, где граница, нельзя.
    """
    if grouping is not Grouping.RUSSIAN or periods < 2:
        return texts
    result: list[str] = []
    for item in texts:
        groups = re.split(rf"[{_NARROW_SPACE} ]+", item.strip())
        # Склейка опознаётся по строению: групп вдвое или более больше, чем
        # периодов, число групп делится на число периодов, и каждая доля
        # сама по себе — правильно набранное число: первая группа от одной
        # до трёх цифр, остальные ровно по три.
        #
        # Прежде требовалось, чтобы **все** группы были по три цифры, и
        # правило не срабатывало там, где первая группа короче: «64 582 109
        # 423» у Сегежи читалось как шестьдесят четыре миллиарда при валюте
        # баланса в сто сорок один, а «89 187 101 900» — как выручка в
        # восемьдесят девять миллиардов вместо восьмидесяти девяти тысяч.
        if len(groups) < 2 * periods or len(groups) % periods:
            result.append(item)
            continue
        size = len(groups) // periods
        if not all(
            _well_formed(groups[start : start + size])
            for start in range(0, len(groups), size)
        ):
            result.append(item)
            continue
        result.extend(
            " ".join(groups[start : start + size])
            for start in range(0, len(groups), size)
        )
    return result


def _well_formed(groups: list[str]) -> bool:
    """Складываются ли группы цифр в правильно набранное число."""
    if not groups or not 1 <= len(groups[0]) <= 3:
        return False
    return all(len(part) == 3 for part in groups[1:])


def _joined(pending: list[str], name: str) -> str:
    """Склеивает наименование, разорванное переносом строки.

    Вёрстка переносит длинные наименования, и величины остаются во второй
    части: «Авансы, выданные под строительство и» / «приобретение основных
    средств 7 083 8 818». Без склейки справочник получает обрывок —
    «приобретение основных средств», — а первая часть теряется вовсе.

    Продолжением считается строка, начинающаяся со строчной буквы либо
    оставляющая незакрытую скобку: заголовок раздела так не выглядит.
    Чужие строки к наименованию не липнут — заголовок «Активы» отбрасывается.
    """
    if not pending:
        return name
    parts: list[str] = []
    for item in reversed(pending):
        if not _CONTINUES.match(item) and not (parts or name[:1].islower()):
            break
        parts.append(item)
        if _CONTINUES.match(item):
            continue
        break
    if not parts:
        return name
    return " ".join([*reversed(parts), name]).strip()


# Строка выглядит незавершённой: кончается союзом, запятой, предлогом или
# открытой скобкой. Заголовок раздела так не кончается.
_CONTINUES = re.compile(r".*(?:[,(]|\bи|\bили|\bпо|\bна|\bв|\bот|\bдля|\bс)\s*$", re.I)


def _split_row(
    line: str, grouping: Grouping, periods: int = 0
) -> tuple[str, tuple[Decimal, ...]]:
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

    # Колонки склеиваются: разделитель разрядов и разделитель колонок — оба
    # пробел, и у «856 349 835 020» они неразличимы по ширине. Четыре группы
    # по три цифры при двух периодах — это две величины, а не одна
    # в восемьсот пятьдесят шесть миллиардов при валюте баланса в полтора.
    texts = _unglue([item.group() for item in tail], periods, grouping)

    parsed = tuple(
        value
        for value in (parse_amount(item, grouping) for item in texts)
        if value is not None
    )
    if not parsed:
        return stripped.strip(), ()

    # Колонок с величинами столько, сколько периодов. Всё, что левее, —
    # не величина: у ФосАгро это номер примечания, «Основные средства
    # 12 395,831 357,577», и без отсечения слева номер примечания стал бы
    # величиной за отчётный период.
    if periods and len(parsed) > periods:
        parsed = parsed[-periods:]
        tail = tail[-periods:] if len(tail) >= periods else tail
    # Одинокое двузначное целое при двух и более колонках — номер примечания,
    # а не величина: у ЛСР заголовок раздела «Собственный капитал 21» получал
    # величину 21 и попадал в очередь как статья. У настоящей статьи значение
    # есть за каждый период либо нет вовсе.
    if periods >= 2 and len(parsed) == 1 and _looks_like_note_number(parsed[0]):
        return stripped.strip(), ()

    name = stripped[: tail[0].start()].strip()
    # К наименованию липнут номер примечания, знак сноски и прочерк «нет
    # значения». Каждый из них ломает опознание целиком: «Денежные средства
    # и их эквиваленты 18» справочник не узнаёт, хотя без номера узнаёт.
    for pattern in (_NOTE_NUMBER, _FOOTNOTE_MARK, _TRAILING_DASH):
        name = pattern.sub("", name).strip()
    return name, parsed


# Хвостовое короткое число наименования — номер примечания, а не часть
# названия статьи. Четырёхзначное не трогаем: оно может быть годом в названии.
_NOTE_NUMBER = re.compile(r"[\s,]*\b\d{1,3}\s*$")

# Знак сноски: звёздочка или крестик, приклеенные к наименованию.
_FOOTNOTE_MARK = re.compile(r"[*†‡]+\s*$")

# Прочерк на месте величины: «Приобретение дочерних предприятий   -».
_TRAILING_DASH = re.compile(r"\s+[-–—]\s*$")


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
