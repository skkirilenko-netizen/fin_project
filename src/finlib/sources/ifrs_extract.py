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
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from itertools import product

from finlib.normalize.ifrs_lines import IfrsCatalog, IfrsPosition, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.sources.ifrs_claims import Claim, Fold, Folded, fold
from finlib.sources.ifrs_numbers import ColumnLayout, Grouping, parse_amount

logger = logging.getLogger(__name__)

# Ячейка таблицы читается по конвенции документа, и это не придирка:
# разделитель разрядов и разделитель колонок — оба пробелы, отличаются они
# только числом. «700 000        650 000» — это две величины, а не одна,
# и единственное, что их разделяет, — ширина промежутка. Внутри числа
# пробел ровно один, между колонками — два и более.
_NARROW_SPACE = "   "

# Сколько спорных строк раздела перебирается разом. Перебор растёт вдвое
# с каждой, а разделов с большим числом спорных строк в разобранных
# комплектах нет: у Сегежи их две, у прочих одна. Превышение — не повод
# гадать, а сигнал, что разбор колонок сломался целиком.
MAX_CONTESTED_ROWS = 6

# Прочерк на месте величины: в графе периода он означает ноль, а не пропуск
# колонки. Без него у строки оказывается одна величина вместо двух, и она
# ложится в чужой год: у Сегежи «Обязательства, относящиеся к опционным
# соглашениям 19 29 140 -» отдавала 29 140 за 2024 год вместо 2025.
#
# С прочерком строки РСБУ это не спорит: там прочерк означает нераскрытие
# и даёт NULL (инвариант 4). Здесь прочерк стоит **в графе периода основной
# формы**, где строка раскрыта, а её величина равна нулю, — это другое место
# и другое значение.
_NIL = r"(?<![\w)])[-–—](?![\w(])"

_CELL_BY_GROUPING: dict[Grouping, str] = {
    Grouping.RUSSIAN: (
        rf"\(?[-−]?\d{{1,3}}(?:[ {_NARROW_SPACE}]\d{{3}})*(?:,\d+)?\)?|{_NIL}"
    ),
    Grouping.ENGLISH: rf"\(?[-−]?\d{{1,3}}(?:,\d{{3}})*(?:\.\d+)?\)?|{_NIL}",
    Grouping.PLAIN: rf"\(?[-−]?\d+(?:[.,]\d+)?\)?|{_NIL}",
}


def _amount(text: str, grouping: Grouping) -> Decimal | None:
    """Величина ячейки; прочерк в графе периода — раскрытый ноль."""
    if re.fullmatch(r"[-–—]", text.strip()):
        return Decimal(0)
    return parse_amount(text, grouping)

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
    # Примечания, на которые ссылается строка формы. Ссылка — опора разбора
    # расшифровок: по номеру примечание находится точно, по наименованию —
    # с риском взять чужое число.
    note_reference: tuple[int, ...] = ()


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
    # Примечания, на которые ссылается строка формы.
    note_reference: tuple[int, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        """Устойчивый ключ строки: форма и место в ней."""
        return (self.form, self.index)

    @property
    def largest(self) -> Decimal:
        """Наибольшая по модулю величина строки — по ней считается доля."""
        return max((abs(item) for item in self.values), default=Decimal(0))


def _total_places_of(
    recognised: dict[int, IfrsPosition], form_code: str
) -> dict[str, int]:
    """Места итогов формы, опознанных справочником: код → номер строки."""
    return {
        position.code: index
        for index, position in recognised.items()
        # Итог остаётся итогом своей формы: позиция, объявившая вторую форму,
        # итогом там не становится — иерархию разделов задаёт своя форма.
        if position.is_total and position.form == form_code
    }


def nearest_total_below(index: int, places: dict[str, int]) -> str | None:
    """Код ближайшего итога ниже строки — итога её раздела.

    **Правило одно на все три места, где спрашивают про раздел.** Разбор
    разводит им одинаковые наименования и опровергает опознание, разметка
    определяет раздел строки в очереди, а ранее подтверждённое проверяет
    им притязание. Три выражения одного правила расходятся, и расхождения
    не видно, пока их не сравнить.

    Разными остаются **входы**, и это объявлено: у разбора итоги только
    опознанные справочником, у разметки к ним добавлены присвоенные
    человеком — у Норникеля итоги обоих разделов обязательств не подписаны
    вовсе, и без человека ближайшего итога ниже у строк пассива не находится.
    """
    below = [place for place in places.values() if place > index]
    if not below:
        return None
    nearest = min(below)
    return next(code for code, place in places.items() if place == nearest)


def materiality_base(form: str, catalog: IfrsCatalog) -> str | None:
    """Код позиции, которой мерится существенность строк этой формы.

    `None` — методика объявила, что базы у формы нет: у строки потока
    существенность не измеряется вовсе. Форма, о базе умолчавшая, справочник
    не загрузит, поэтому молчание и объявленное отсутствие здесь не путаются.
    """
    declared = catalog.materiality.bases.get(form)
    return declared.base if declared is not None else None


def materiality_share(
    row: UnrecognisedRow,
    catalog: IfrsCatalog,
    value_of: Callable[[str], Decimal | None],
) -> Decimal | None:
    """Мера существенности строки: её величина к базе своей формы.

    **Определение одно на всех, кто ею пользуется.** Экран сверки решает
    по ней, вынесена ли статья отдельной позицией, загрузчик пишет её
    в журнал подтверждений, а разметка сортирует очередь. Три выражения
    одной величины расходятся, и расхождения не видно, пока их не сравнить.

    **База своя у каждой формы, и у формы её может не быть вовсе.** Статья
    баланса соизмеряется с валютой баланса, строка отчёта о прибыли —
    с выручкой, а поток за период не доля ни от запаса, ни от оборота:
    у О'КЕЙ поступления от покупателей составляют 336,9 % валюты баланса —
    отношение верное, смысла в нём нет. Такая строка возвращает `None`,
    и это не ноль: ноль означал бы «несущественна», а `None` — «мерить нечем».

    `None` возвращается и там, где сама база не раскрыта: делить не на что.
    """
    base = materiality_base(row.form, catalog)
    if base is None:
        return None
    amount = value_of(base)
    if amount is None or amount == 0:
        return None
    return abs(row.largest) / abs(amount)


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
    # Строки, которые методика не использует осознанно: наименование, величины
    # и предмет из `ignored` справочника. Хранятся отдельно от неопознанных —
    # игнорируемое наименование это решение, неопознанное — недоработка.
    ignored: list[tuple[str, tuple[Decimal, ...], str]] = field(default_factory=list)
    # Место опознанной позиции в таблице: код → номер строки. Нужно
    # для иерархии итогов — в отчётности по МСФО слагаемые стоят **над**
    # своим итогом, и без места строки «ближайший итог ниже» не определить.
    recognised_at: dict[str, int] = field(default_factory=dict)
    # Строки, чья величина учтена в другой строке: сложена с ней или
    # повторяет её. Хранятся, а не выбрасываются: сложение — событие,
    # о котором должно быть известно, иначе оно неотличимо от затирания.
    merged: list[tuple[str, str, str]] = field(default_factory=list)
    # Позиции, за которые спорят несколько строк: величина не взята вовсе,
    # строки возвращены в неопознанные и уйдут в очередь разметки.
    contested: list[tuple[str, str]] = field(default_factory=list)
    # Разметка граф формы: сколько их в шапке, сколько берётся и с какой.
    # Хранится рядом с величинами, потому что по величинам её не увидеть:
    # квартальная графа, взятая за полугодовую, согласована сама с собой.
    layout: ColumnLayout | None = None
    # Строки, у которых величины отброшены без объяснения: граф больше,
    # чем берётся, а номером примечания отброшенное не является. Ноль таких
    # строк при неизвестном числе строк с величинами ничего не значит,
    # поэтому рядом стоит счётчик проверенного.
    dropped_values: list[tuple[str, tuple[Decimal, ...]]] = field(default_factory=list)
    rows_with_values: int = 0


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
    def merged(self) -> list[tuple[str, str, str]]:
        """Строки, чья величина учтена в другой строке того же кода."""
        return [item for form in self.forms.values() for item in form.merged]

    @property
    def contested(self) -> list[tuple[str, str]]:
        """Позиции, за которые спорят строки: величина не взята."""
        return [item for form in self.forms.values() for item in form.contested]

    @property
    def ignored(self) -> list[tuple[str, str, str]]:
        """Строки, не используемые методикой осознанно: форма, строка, предмет."""
        return [
            (form.code, name, subject)
            for form in self.forms.values()
            for name, _values, subject in form.ignored
        ]

    @property
    def dropped_values(self) -> list[tuple[str, str, tuple[Decimal, ...]]]:
        """Величины, отброшенные без объяснения: форма, строка, сами числа.

        Граф в строке больше, чем берётся, а номером примечания отброшенное
        не является. Прежде такие величины исчезали молча: у промежуточного
        ФосАгро так пропадали шестимесячные графы, а квартальные шли
        в комплект за полугодие.
        """
        return [
            (form.code, name, values)
            for form in self.forms.values()
            for name, values in form.dropped_values
        ]

    @property
    def rows_with_values(self) -> int:
        """Строк с величинами — знаменатель к числу строк с потерями."""
        return sum(form.rows_with_values for form in self.forms.values())

    @property
    def rows_total(self) -> int:
        """Строк таблиц всего — величин больше, и путать их нельзя."""
        return sum(form.rows_total for form in self.forms.values())

    @property
    def rows_recognised(self) -> int:
        """Строк, опознанных справочником."""
        return sum(form.rows_recognised for form in self.forms.values())

    def totals(
        self, report_date: date, catalog: IfrsCatalog | None = None
    ) -> dict[str, Decimal]:
        """Величины за период одним перечнем; позиция берётся из **своей** формы.

        **Один код в двух формах — два разных факта, и подменять они друг
        друга не вправе.** Плоский перечень оставлял последнюю встреченную
        величину, и у ПАО «Сегежа Групп» «Прочий финансовый результат, нетто»
        приходил из отчёта о движении денежных средств неденежной поправкой
        +9 660 вместо −9 660 отчёта о прибыли, а курсовые разницы −250 вместо
        +250. Цепочка прибыли расходилась на 18 820 у комплекта, в котором
        она сходится копейка в копейку, и комплект уходил в карантин
        за нашу ошибку.

        Своей формой считается та, которую объявляет справочник; величина
        из формы, названной в `also_in_forms`, заполняет пробел, но своей
        не подменяет.
        """
        catalog = catalog or load_ifrs_lines()
        found: dict[str, Decimal] = {}
        for form_code, form in self.forms.items():
            for item in form.values:
                if item.report_date != report_date:
                    continue
                position = catalog.get(item.code)
                own = position is not None and position.form == form_code
                if own or item.code not in found:
                    found[item.code] = item.value
        return found

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
            f"осознанно игнорируется {len(self.ignored)}, "
            f"итогов опознано структурой "
            f"{sum(len(form.totals_by_structure) for form in self.forms.values())}, "
            f"строк сложено с другими {len(self.merged)}, "
            f"спорных позиций {len(self.contested)}, "
            f"сносок под формами {len(self.notes)}"
        )


def extract(
    text: str,
    report_dates: tuple[date, ...] | Mapping[str, tuple[date, ...]],
    grouping: Grouping,
    catalog: IfrsCatalog | None = None,
    columns: Callable[[str], tuple[tuple[str, float], ...]] | None = None,
    layouts: Mapping[str, ColumnLayout] | None = None,
) -> Extraction:
    """Разбирает документ по формам справочника.

    report_dates и grouping приходят от приёма файла: разбирать числа,
    не зная конвенции, нельзя, а раскладывать их по периодам, не зная дат,
    не во что.

    **Даты передаются по формам, а не одной парой на документ.** В годовом
    комплекте разницы нет и достаточно кортежа; в промежуточном даты вторых
    колонок у форм разные, и общая пара кладёт величину баланса на конец
    года под дату полугодия. Словарь отдаёт приём файла
    (`DocumentProfile.dates_by_form`), кортеж остаётся для случая, когда
    даты у всех форм заведомо одни.

    `layouts` — разметка граф каждой формы от приёма файла
    (`DocumentProfile.columns_by_form`): сколько граф в шапке и какие из них
    за период комплекта. Без неё берутся последние графы, а у промежуточного
    ФосАгро последние — квартальные: в комплект за полугодие шли величины
    квартала, согласованные сами с собой и потому никаким контролем
    сходимости не отличимые от верных.

    `columns` отдаёт ячейки строки по координатам PDF. Это **свидетельство,
    а не догадка**: в плоском тексте разделитель разрядов и разделитель
    колонок — один и тот же пробел, а в координатах между «737» и «562»
    семьдесят три пункта, а внутри «737» девять. Там, где координаты есть,
    они решают, где кончается величина; где их нет — работают правила
    строения числа, и они остаются для текстовых выгрузок.
    """
    catalog = catalog or load_ifrs_lines()
    blocks = _split_by_forms(text, catalog)

    result = Extraction()
    for form_code, lines in blocks.items():
        dates = (
            report_dates.get(form_code, ())
            if isinstance(report_dates, Mapping)
            else report_dates
        )
        if not dates:
            # Форма без дат величин не даёт: раскладывать их по периодам
            # не во что. Молчать об этом нельзя — форма из документа
            # не исчезает, она остаётся пустой.
            logger.warning("форма %s: отчётные даты не определены", form_code)
            result.forms[form_code] = ExtractedForm(form_code)
            continue
        result.forms[form_code] = _extract_form(
            form_code,
            lines,
            dates,
            grouping,
            catalog,
            columns,
            layouts.get(form_code) if layouts else None,
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
    columns: Callable[[str], tuple[tuple[str, float], ...]] | None = None,
    layout: ColumnLayout | None = None,
) -> ExtractedForm:
    """Разбирает один блок формы: величины, неопознанные строки, сноски."""
    form = ExtractedForm(form_code, layout=layout)
    by_column = _columns_of_form(lines, report_dates, grouping, columns, layout)
    rows: list[tuple[str, tuple[Decimal, ...], int]] = []
    alternatives: dict[int, tuple[Decimal, ...]] = {}
    # Чтение, отбрасывающее первую группу цифр как номер примечания, живёт
    # отдельно от чтения по колонкам: оно **сильнее меняет строку** и потому
    # принимается только против арифметики раздела, а не против границы
    # «слагаемое не больше итога». У ЛСР «Прибыль за год 10 778 28 598»
    # по этой границе читалась бы как примечание 10 и прибыль 778.
    note_readings: dict[int, tuple[Decimal, ...]] = {}
    references: dict[int, tuple[int, ...]] = {}
    # Величины, отброшенные без объяснения, по месту строки: нарушением
    # они становятся только у строк, оставшихся статьями.
    losses: dict[int, tuple[Decimal, ...]] = {}
    tail_from = 0

    pending: list[str] = []
    letters = _single_letter_words()
    for index, line in enumerate(lines):
        name, values, alternative, reference, dropped = _split_row(
            line, grouping, len(report_dates), layout
        )
        # Слово, разорванное извлекателем, склеивается здесь: дальше
        # наименование идёт и в справочник, и в подтверждения, и в очередь
        # разметки, а разорванное не опознаётся ни там, ни там.
        name = glue_word_breaks(name, letters)
        # Координаты старше правил строения числа: они говорят, где кончается
        # колонка, а правила об этом только догадываются. Но величин от этого
        # не убывает: если ячейка не легла ни в одну колонку — у Норникеля
        # так вышло с «Прочими финансовыми активами», где первая из трёх
        # величин осталась за границами допуска, — чтение по координатам
        # отбрасывается целиком. Свидетельство о границе колонки не повод
        # потерять величину.
        by_coordinates = by_column.get(index)
        if by_coordinates and len(by_coordinates) >= len(values):
            values, alternative = by_coordinates, ((), ())
        if not values:
            # Строка без величин — либо заголовок раздела, либо начало
            # наименования, перенесённого вёрсткой. Какая именно, станет
            # видно на следующей строке с величинами.
            if name:
                pending.append(name)
            continue
        if alternative[0]:
            alternatives[len(rows)] = alternative[0]
        if alternative[1]:
            note_readings[len(rows)] = alternative[1]
        if reference:
            references[len(rows)] = reference
        if dropped:
            # Величины, отброшенные без объяснения: граф в строке больше,
            # чем берётся, а номером примечания отброшенное не является.
            # Складываются по месту строки и разбираются ниже, когда известен
            # отсев «не статья»: у шапки таблицы «Млн руб. Прим. 2025 2024»
            # лишнее число есть всегда, и потерей это не является.
            losses[len(rows)] = dropped
        rows.append((_joined(pending, name), values, index))
        # Знаменатель к числу строк с потерянными величинами: ноль потерь
        # при неизвестном числе строк с величинами ничего не означает.
        form.rows_with_values += 1
        pending.clear()
        tail_from = index + 1

    known = catalog.for_form(form_code)
    recognised: dict[int, IfrsPosition] = {}
    for position_index, (name, _, _) in enumerate(rows):
        found = catalog.match_by_name(name, form=form_code) if name else None
        if found is not None and found.occurs_in(form_code):
            recognised[position_index] = found

    _resolve_by_section(rows, recognised, catalog, form_code)

    _choose_reading_by_totals(rows, alternatives, note_readings, recognised, catalog)

    # Отсев «не статья» идёт **прежде** опознания итогов структурой.
    # Контрольная сумма без наименования равна сумме предшествующих строк
    # ровно так же, как итог раздела, и опознавалась итогом: у Сегежи строка
    # −88 378 под разбивкой убытка по акционерам становилась валовой
    # прибылью, хотя валовой прибыли в её отчёте нет вовсе. Порядок здесь
    # и есть правило: строка, дублирующая уже встреченную величину, итогом
    # раздела быть не может.
    dismissals = {
        index: found
        for index, (name, values, _) in enumerate(rows)
        if index not in recognised
        and (found := _auto_dismissal(name, values, rows[:index])) is not None
    }

    _name_totals_by_structure(rows, recognised, known, form, set(dismissals))
    # Второй проход по разделам: часть итогов опознаётся структурой, а не
    # наименованием, и в первый проход их ещё не было. У ФосАгро итоги обоих
    # разделов обязательств подписаны словами «Долгосрочные обязательства»
    # и «Краткосрочные обязательства» — справочник таких не знает, — и
    # «Торговая и прочая кредиторская задолженность» оставалась неопознанной
    # только потому, что разделу не на что было опереться.
    _resolve_by_section(rows, recognised, catalog, form_code)
    _retract_wrong_section(rows, recognised, form_code)

    # Несколько строк на одну позицию — сложение, повтор или спор, но
    # не затирание: словарь оставлял последнюю величину, и у ЛСР капитал
    # не сходился ровно на «Эмиссионный доход», затёртый «Добавочным».
    folded, carrier = _fold_claims(rows, recognised, form_code)
    for code, outcome in folded.items():
        if outcome.kind is not Fold.CONTESTED:
            continue
        form.contested.append((code, outcome.reason))
        for position_index in [
            index
            for index, position in recognised.items()
            if position.code == code
        ]:
            recognised.pop(position_index)

    form.rows_total = len(rows)
    # Потеря величины считается по строкам, оставшимся статьями: у шапки
    # таблицы и колонтитула лишнее число есть всегда, и молчание о них —
    # не умалчивание, а отсев, о котором объявлено отдельно
    # (`auto_dismissed`). Строка, которой мы не поняли вовсе, в счёт входит:
    # что отброшенное не было величиной, сказать нечем.
    form.dropped_values.extend(
        (rows[position_index][0], values)
        for position_index, values in sorted(losses.items())
        if position_index not in dismissals
    )
    for position_index, (name, values, _) in enumerate(rows):
        position = recognised.get(position_index)
        if position is None:
            dismissal = dismissals.get(position_index)
            if dismissal is not None:
                # Колонтитул и контрольная сумма — не статьи, и показывать их
                # человеку незачем. Важнее другое: в метрике общности они
                # искусственно завышали долю общих статей, потому что номер
                # страницы встречается у всех эмитентов.
                form.auto_dismissed.append((name, values, dismissal))
                continue
            ignored = catalog.ignored_subject(name)
            if ignored is not None:
                # Методика не использует этот предмет осознанно — прибыль
                # на акцию, число акций. Такая строка не идёт ни в очередь
                # разметки, ни в основания экрана сверки, но и не исчезает:
                # она считается отдельно от неопознанных, потому что
                # решение и недоработка — разные вещи.
                form.ignored.append((name, values, ignored.subject))
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
                    note_reference=references.get(position_index, ()),
                )
            )
            continue
        form.rows_recognised += 1
        form.recognised_at.setdefault(position.code, position_index)
        outcome = folded.get(position.code)
        if outcome is not None:
            if carrier.get(position.code) != position_index:
                form.merged.append((position.code, name.strip(), outcome.kind.value))
                continue
            values = outcome.values
        for report_date, value in zip(report_dates, values, strict=False):
            form.values.append(
                ExtractedValue(
                    position.code,
                    report_date,
                    value,
                    name.strip(),
                    references.get(position_index, ()),
                )
            )

    form.notes_under_form = _notes_after(lines[tail_from:])
    return form


def _fold_claims(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    form_code: str,
) -> tuple[dict[str, Folded], dict[str, int]]:
    """Сводит строки, претендующие на один код, и называет строку-носителя.

    Возвращается только то, за что спорят: код, на который легла ровно одна
    строка, сводить не с чем, и разбор его не трогает.
    """
    claims: dict[str, list[tuple[int, Claim]]] = {}
    for position_index, position in sorted(recognised.items()):
        name, values, _ = rows[position_index]
        claims.setdefault(position.code, []).append(
            (
                position_index,
                Claim(
                    name.strip(),
                    values,
                    form_code,
                    position.section,
                    (form_code, position_index),
                ),
            )
        )
    folded: dict[str, Folded] = {}
    carrier: dict[str, int] = {}
    for code, items in claims.items():
        if len(items) == 1:
            continue
        position = recognised[items[0][0]]
        outcome = fold(position, [claim for _, claim in items])
        folded[code] = outcome
        if outcome.accepted:
            carrier[code] = next(
                index for index, claim in items if claim is outcome.accepted[0]
            )
        logger.info("позиция %s: %s", code, outcome.reason)
    return folded, carrier


@lru_cache(maxsize=8)
def _cells_pattern(grouping: Grouping) -> re.Pattern[str]:
    """Как выглядит ячейка с величиной при этой конвенции записи чисел."""
    return re.compile(_CELL_BY_GROUPING[grouping])


def _auto_dismissal(
    name: str, values: tuple[Decimal, ...], earlier: list[tuple]
) -> str | None:
    """Почему строку можно отсеять без человека; None — нельзя.

    Два случая про строки без наименования. Одно число — колонтитул
    или номер страницы. Два числа, повторяющие ранее встреченную строку, —
    контрольная сумма разбивки: у Сегежи убыток печатается ещё раз под
    разбивкой «неконтролирующим долям участия», и в итог он войти не должен.

    Третий случай — шапка самой таблицы: «Прим. 2025 2024», «Млн руб. Прим.»,
    «ЗА ГОДЫ, ЗАКОНЧИВШИЕСЯ 31 ДЕКАБРЯ 2025, 2024 И 2023». Номер колонки
    и год — числа, и строка выглядела статьёй; в очереди она занимала место,
    а в недостаче итога давала слагаемое из ниоткуда.

    Строка с наименованием статьи так не отсеивается никогда: решение о ней
    принимает человек.
    """
    if _is_table_header(name):
        return "auto_table_header"
    if name.strip():
        return None
    if len(values) == 1:
        return "auto_not_item"
    for previous_name, previous_values, _ in earlier:
        if previous_values == values and previous_name.strip():
            return f"duplicate_of:{previous_name.strip()}"
    return None


# Из чего состоит шапка таблицы: подпись колонки примечаний, единица
# измерения и объявление периода. Ничем другим строка шапки не бывает,
# поэтому проверяется, что **всё** наименование сложено из этих кусков.
_HEADER_WORDS = (
    "прим",
    "примечание",
    "примечания",
    "приме",
    "чания",
    "поясн",
    "пояснение",
    "пояснения",
    "млн",
    "тыс",
    "руб",
    "год",
    "года",
    "годы",
    "году",
    "за",
    "в",
    "и",
    "на",
    "по",
    "состоянию",
    "закончившиеся",
    "закончившийся",
    "декабря",
    "долл",
    "сша",
)


def _is_table_header(name: str) -> bool:
    """Шапка таблицы, а не статья: наименование сложено только из её слов."""
    words = [word for word in re.split(r"[^\w-]+", normalize_name(name)) if word]
    if not words:
        return False
    # Числа в шапке — годы колонок и номер колонки примечаний, и они остаются
    # в наименовании: «Млн руб. Прим. 2025», «Приме- чания 2025». Отсечение
    # хвостовых чисел их не убирает — между ними стоят слова.
    return all(
        word.strip("-") in _HEADER_WORDS or word.isdigit() for word in words
    )


def glue_word_breaks(name: str, letters: frozenset[str]) -> str:
    """Склеивает слово, разорванное извлекателем пробелом.

    **Разрыв внутри куска, а не между кусками, поэтому координаты здесь
    не помогают.** У ФосАгро pypdf отдаёт «Г руппы» одним куском вместе
    с пробелом, и наименование «Себестоимость реализованной продукции
    Г руппы» не опознаётся ни справочником, ни ранее подтверждённым —
    а это 194 587, двадцать восемь процентов валюты баланса.

    Склеивается только то, что словом не бывает: `letters` — закрытый
    перечень однобуквенных слов из методики, и буква из него не склеивается
    никогда. Поэтому «В составе» и «С тавка» остаются как есть: отличить
    там разрыв от предлога нечем, а испорченное наименование хуже
    неопознанного — оно опознаётся **неверно**.
    """
    def glued(match: re.Match[str]) -> str:
        letter = match.group(1)
        return letter if letter.casefold() not in letters else match.group(0)

    return _LONE_LETTER.sub(glued, name)


# Одинокая буква перед словом: буква, отделённая от соседей границей слова,
# пробел и продолжение с буквы. Цифры сюда не попадают — «6 месяцев»
# разрывом слова не является.
_LONE_LETTER = re.compile(r"(?<![^\W\d_])([^\W\d_])\s+(?=[^\W\d_])")


@lru_cache(maxsize=1)
def _single_letter_words() -> frozenset[str]:
    """Однобуквенные слова методики — те, что склеивать нельзя."""
    from finlib.sources.ifrs_numbers import load_parsing_policy

    return frozenset(
        item.casefold() for item in load_parsing_policy().word_breaks.single_letter_words
    )


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
    texts = _strip_note_column(texts, periods)
    plain = _unglue_columns(texts, periods)
    if len(plain) >= periods:
        return plain
    # Колонок всё ещё меньше, чем периодов, — значит, склейка осталась,
    # и спереди к ней приклеен номер примечания: «Инвестиции в совместные
    # и ассоциированные компании 5 414 414» — это примечание 5 и две
    # величины по 414, а читалось как пять миллиардов при валюте баланса
    # в сто сорок одну тысячу.
    #
    # Отрезать номер сразу нельзя: у «1 500 000» первая группа тоже
    # однозначна, и остаток «500 000» делится ровно надвое — итог активов
    # превращался в пятьсот. Поэтому номер отрезается только тогда, когда
    # без него колонок не хватает.
    return _unglue_columns(texts, periods, note_number=True)


def _strip_note_column(texts: list[str], periods: int) -> list[str]:
    """Отделяет номер примечания, слипшийся с первой величиной строки.

    Колонка «Прим.» стоит слева от величин, и её номер прилипает к первой
    из них: у Сегежи «Добавочный капитал 19 116 179 35 122» — это
    примечание 19 и величины 116 179 и 35 122, а читалось как девятнадцать
    миллиардов при итоге капитала в 255.

    Опознаётся строением, а не догадкой: колонок ровно столько, сколько
    периодов, у первой на одну группу цифр больше, чем у остальных, лишняя
    группа — одна-две цифры, **и без неё величина становится сравнимой
    с соседним периодом**.

    Последнее условие и отличает номер от разряда. «1 000 000  930 000» —
    тоже три группы против двух, но величины соседних периодов различаются
    на восемь процентов, и отрезать там нечего; у Сегежи же 19 116 179
    против 35 122 — разница в пятьсот сорок пять раз, а после отсечения
    в три с половиной. Величина отчётности за смежные годы так не меняется.

    Порог разницы — три десятичных разряда, а не два: при двух под правило
    попадало «Прочие операционные доходы, нетто 1 393 49», где 1 393
    и 49 — настоящие величины смежных лет, и доход превращался в 393.
    """
    if len(texts) != periods or periods < 2:
        return texts
    split = [re.split(rf"[{_NARROW_SPACE} ]+", item.strip()) for item in texts]
    rest = max(len(item) for item in split[1:])
    if len(split[0]) != rest + 1 or len(split[0][0]) > 2:
        return texts
    whole = sum(len(group) for group in split[0])
    stripped = whole - len(split[0][0])
    neighbour = max(sum(len(group) for group in item) for item in split[1:])
    if whole - neighbour < 3 or abs(stripped - neighbour) >= whole - neighbour:
        return texts
    return [split[0][0], " ".join(split[0][1:]), *texts[1:]]


def _unglue_columns(
    texts: list[str], periods: int, note_number: bool = False
) -> list[str]:
    """Разрезает ячейки, считая или не считая первую группу номером примечания."""
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
        # Без номера примечания доля колонки должна быть не короче двух групп:
        # «60 021» при двух периодах — одна величина, а не шестьдесят и
        # двадцать один. Разделить её поровну можно, и правило, разрешающее
        # это, режет пополам каждую вторую строку баланса.
        columns = _split_evenly(groups, periods, min_size=2)
        if columns is not None:
            result.extend(columns)
            continue
        columns = (
            _split_evenly(groups[1:], periods, min_size=1)
            if note_number and len(groups) > 1 and len(groups[0]) <= 2
            else None
        )
        if columns is not None:
            result.append(groups[0])
            result.extend(columns)
            continue
        result.append(item)
    return result


def _split_evenly(groups: list[str], periods: int, min_size: int) -> list[str] | None:
    """Делит группы цифр поровну между колонками; None — не делятся.

    Склейка опознаётся по строению: число групп делится на число периодов,
    доля каждой колонки не короче `min_size` групп, и каждая доля сама
    по себе — правильно набранное число.
    """
    if len(groups) < periods * min_size or len(groups) % periods:
        return None
    size = len(groups) // periods
    parts = [groups[start : start + size] for start in range(0, len(groups), size)]
    if not all(_well_formed(part) for part in parts):
        return None
    return [" ".join(part) for part in parts]


def _well_formed(groups: list[str]) -> bool:
    """Складываются ли группы цифр в правильно набранное число."""
    if not groups or not 1 <= len(groups[0]) <= 3:
        return False
    return all(len(part) == 3 for part in groups[1:])


def split_row(
    line: str,
    grouping: Grouping,
    periods: int = 0,
    layout: ColumnLayout | None = None,
) -> tuple[
    str,
    tuple[Decimal, ...],
    tuple[Decimal, ...],
    tuple[int, ...],
    tuple[Decimal, ...],
]:
    """Разбор строки таблицы — один на формы и на примечания.

    Таблица примечания устроена так же, как таблица формы: наименование,
    колонки периодов, номер примечания сноской. Второй разбор того же
    неминуемо разошёлся бы с первым — это уже случалось с поиском
    заголовков форм, который приём и разбор делали порознь.
    """
    return _split_row(line, grouping, periods, layout)


def join_name(pending: list[str], name: str) -> str:
    """Склейка наименования, разорванного переносом, — та же, что в формах."""
    return _joined(pending, name)


def is_table_header(name: str) -> bool:
    """Шапка таблицы, а не статья, — то же определение, что у разбора форм.

    Нужна приёму: отчётные даты формы стоят в шапке её таблицы, и кончается
    шапка там, где начинается первая статья. Два определения одного и того же
    неминуемо разойдутся — это уже случалось с поиском заголовков форм.
    """
    return _is_table_header(name)


def _joined(pending: list[str], name: str) -> str:
    """Склеивает наименование, разорванное переносом строки.

    Вёрстка переносит длинные наименования, и величины остаются во второй
    части: «Авансы, выданные под строительство и» / «приобретение основных
    средств 7 083 8 818». Без склейки справочник получает обрывок —
    «приобретение основных средств», — а первая часть теряется вовсе.

    Продолжением считается строка, начинающаяся со строчной буквы либо
    оставляющая незакрытую скобку: заголовок раздела так не выглядит.
    Чужие строки к наименованию не липнут — заголовок «Активы» отбрасывается.

    **Строка без наименования берёт предыдущую целиком.** Если у строки
    с величинами наименования нет вовсе, вся подпись осталась на строке
    выше: у ФосАгро «Авансы, выданные под строительство и приобретение
    основных средств» пришли безымянной строкой 7 083 / 8 818, и статья
    ушла в разметку неопознанной. Проверять там нечего — своего текста
    у строки нет, и взять его больше неоткуда.
    """
    if not pending:
        return name
    if not name.strip():
        return pending[-1].strip()
    # Склейка идёт, пока присоединённый кусок сам выглядит продолжением.
    # Прежде бралась одна строка, и наименование в три строки собиралось
    # наполовину: у Сегежи «Убыток от обесценения и другие расходы в связи
    # с изменением | долгосрочной стратегии и реорганизацией производственной
    # | деятельности Группы» давало две последние трети, и справочник
    # не опознавал ничего.
    parts: list[str] = []
    current = name
    for item in reversed(pending):
        if not current[:1].islower() and not _CONTINUES.match(item):
            break
        parts.append(item)
        current = item
    if not parts:
        return name
    return " ".join([*reversed(parts), name]).strip()


# Строка выглядит незавершённой: кончается союзом, запятой, предлогом или
# открытой скобкой. Заголовок раздела так не кончается.
_CONTINUES = re.compile(r".*(?:[,(]|\bи|\bили|\bпо|\bна|\bв|\bот|\bдля|\bс)\s*$", re.I)


# Насколько правые края ячеек одной колонки расходятся между строками.
# Величины выровнены по правому краю, но округление ширины знака и знак
# скобки дают разброс в несколько пунктов.
_COLUMN_SPREAD = 12.0


def _columns_of_form(
    lines: list[str],
    report_dates: tuple[date, ...],
    grouping: Grouping,
    columns: Callable[[str], tuple[tuple[str, float], ...]] | None,
    layout: ColumnLayout | None = None,
) -> dict[int, tuple[Decimal, ...]]:
    """Величины строк формы, разложенные по колонкам периодов, — по координатам.

    Колонка опознаётся по правому краю: величины выровнены по нему, и края
    ячеек одной колонки сходятся у всех строк формы. Колонок берётся столько,
    сколько их объявила шапка, и берутся **самые правые** — левее них стоит
    колонка примечаний, которая величиной не является.

    **Разметка граф нужна и здесь, а не только разбору по строению.** Координаты
    отвечают, где кончается колонка, но не отвечают, за какой период она
    приведена: у промежуточного ФосАгро самые правые графы — квартальные,
    и чтение по координатам подставляло их вместо шестимесячных строкам,
    где строение читалось хуже. Одна форма выходила собранной из двух разных
    периодов: выручка за полугодие, финансовые доходы за квартал.

    Строка, у которой ячейки не легли ни в одну колонку, здесь не возвращается
    вовсе: тогда работает разбор по строению числа. Молчаливой подстановки
    нет — есть либо свидетельство, либо его отсутствие.
    """
    if columns is None or len(report_dates) < 1:
        return {}

    cells: dict[int, list[tuple[Decimal, float]]] = {}
    for index, line in enumerate(lines):
        found = [
            (parsed, right)
            for text, right in columns(line)
            if (parsed := parse_amount(text, grouping)) is not None
        ]
        if found:
            cells[index] = found
    if not cells:
        return {}

    taken = len(report_dates)
    total = layout.total if layout is not None and layout.wider else taken
    offset = layout.offset if layout is not None and layout.wider else 0
    edges = _column_edges(
        [right for row in cells.values() for _, right in row], total
    )
    if len(edges) < total:
        return {}

    placed: dict[int, tuple[Decimal, ...]] = {}
    for index, row in cells.items():
        values: list[Decimal | None] = [None] * len(edges)
        for amount, right in row:
            nearest = min(range(len(edges)), key=lambda spot: abs(edges[spot] - right))
            if abs(edges[nearest] - right) <= _COLUMN_SPREAD and values[nearest] is None:
                values[nearest] = amount
        # Пропуск в середине разложить по периодам нечем: сдвиг влево отдал бы
        # величину чужому году. Такая строка остаётся разбору по строению.
        kept = [item for item in values if item is not None]
        if not kept or values[: len(kept)] != kept:
            continue
        if total == taken:
            placed[index] = tuple(kept)
            continue
        # Граф больше, чем берётся: наши стоят по месту, а не подряд с начала.
        # Строка, у которой наших граф нет, остаётся разбору по строению —
        # подставить вместо них соседние значило бы взять чужой период.
        window = values[offset : offset + taken]
        if all(item is not None for item in window):
            placed[index] = tuple(item for item in window if item is not None)
    return placed


def _column_edges(rights: list[float], periods: int) -> tuple[float, ...]:
    """Правые края колонок величин: самые правые скопления из всех."""
    clusters: list[list[float]] = []
    for right in sorted(rights):
        if clusters and right - clusters[-1][-1] <= _COLUMN_SPREAD:
            clusters[-1].append(right)
            continue
        clusters.append([right])
    # Колонка периода встречается у многих строк, случайное число — у одной.
    solid = [group for group in clusters if len(group) > 1] or clusters
    chosen = solid[-periods:]
    return tuple(sum(group) / len(group) for group in chosen)


def _split_row(
    line: str,
    grouping: Grouping,
    periods: int = 0,
    layout: ColumnLayout | None = None,
) -> tuple[
    str,
    tuple[Decimal, ...],
    tuple[Decimal, ...],
    tuple[int, ...],
    tuple[Decimal, ...],
]:
    """Делит строку на наименование, величины, запасное чтение, ссылку и потерю.

    **Графы берутся объявленной длительности, а не последние.** `layout`
    говорит, сколько граф в форме и какие из них за период комплекта: у
    промежуточного ФосАгро отчёт о прибыли печатает полугодие и квартал рядом,
    и последние две графы — квартальные. Без разметки в комплект за полугодие
    попадали квартальные величины: число настоящее, период чужой, и ни один
    контроль сходимости этого не показывает.

    Пятое возвращаемое значение — величины, **отброшенные без объяснения**:
    строка несла больше граф, чем берётся, а номером примечания отброшенное
    не является. Это потеря, и она обязана быть названа: ноль потерь при
    неизвестном числе отброшенных величин ничего не значит.

    **Номер примечания не выбрасывается, а возвращается.** Он и есть ссылка
    на расшифровку: «Амортизация 6, 7», «Процентные расходы 9». Разбор
    и прежде отличал его от величины — иначе номер становился бы суммой, —
    но, отличив, терял. Опора на строение документа дороже того, чтобы
    её выкидывать: по ссылке примечание находится точно, а по наименованию
    даёт чужое число.

    Величины ищутся в хвосте строки: наименование стоит слева и содержать
    чисел не обязано, а вот числа справа — это колонки периодов. Ячейка
    опознаётся по конвенции документа, иначе «700 000  650 000» слипается
    в одну величину.

    Запасных чтений два, и оба — свидетельства о разметке, а не догадки.
    Первое: в каждой колонке стоит ровно одна группа цифр — «367 391»
    у Автодора это не триста шестьдесят семь тысяч, а 367 и 391 за два года.
    Второе: первая группа цифр — номер примечания, слипшийся с величиной, —
    «Авансы, выданные под внеоборотные активы 11 129 1 650» у Сегежи это
    примечание 11 и величины 129 и 1 650.

    Выбрать между чтениями по виду строки нельзя: номер примечания отделён
    от величины тем же одиночным пробелом, что и разряды. Это выбирает
    арифметика итога.
    """
    stripped = line.rstrip()
    if not stripped.strip():
        return "", (), (), (), ()

    pattern = _cells_pattern(grouping)
    matches = list(pattern.finditer(stripped))
    if not matches:
        return stripped.strip(), (), (), (), ()

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
        return stripped.strip(), (), (), (), ()

    # Колонки склеиваются: разделитель разрядов и разделитель колонок — оба
    # пробел, и у «856 349 835 020» они неразличимы по ширине. Четыре группы
    # по три цифры при двух периодах — это две величины, а не одна
    # в восемьсот пятьдесят шесть миллиардов при валюте баланса в полтора.
    cells = [item.group() for item in tail]
    texts = _unglue(cells, periods, grouping)
    # Запасных чтений два, и оба — свидетельства о разметке колонок,
    # а не догадки. Первое: в каждой колонке ровно одна группа цифр.
    # Второе: первая группа — номер примечания, слипшийся с величиной.
    # Выбирает между ними арифметика раздела, а не вид строки.
    by_columns = _by_single_groups(cells, periods, grouping)
    without_note = _without_leading_note(texts, periods, grouping)

    parsed = tuple(
        value
        for value in (_amount(item, grouping) for item in texts)
        if value is not None
    )
    if not parsed:
        return stripped.strip(), (), (), (), ()

    # Колонок с величинами столько, сколько периодов. Всё, что левее, —
    # не величина: у ФосАгро это номер примечания, «Основные средства
    # 12 395,831 357,577», и без отсечения слева номер примечания стал бы
    # величиной за отчётный период.
    reference: list[int] = []
    dropped: tuple[Decimal, ...] = ()
    if layout is not None and layout.wider and len(parsed) >= layout.total:
        # Граф в форме больше, чем отчётных дат, и шапка сказала, какие
        # из них за наш период. Берутся они, а не последние: у ФосАгро
        # последние — квартальные, и комплект за полугодие собирался
        # из величин квартала.
        #
        # Наименование при этом кончается там, где начинается **первая**
        # графа величин, а не наша: иначе величины чужой длительности
        # остались бы в наименовании, и справочник строку не узнал бы.
        note_cells = len(parsed) - layout.total
        reference.extend(
            int(value)
            for value in parsed[:note_cells]
            if _looks_like_note_number(value)
        )
        columns = parsed[note_cells:]
        parsed = columns[layout.offset : layout.offset + periods]
        tail = tail[note_cells:] if len(tail) > note_cells else tail
    elif periods and len(parsed) > periods:
        # Отсечённое слева — не величина, а номер примечания: он и есть
        # ссылка на расшифровку, и теряться ей незачем.
        surplus = parsed[:-periods]
        reference.extend(
            int(value) for value in surplus if _looks_like_note_number(value)
        )
        # Величина, отброшенная как номер примечания, но номером примечания
        # не являющаяся, — это потерянная графа. Молча отбросить её нельзя:
        # прежде так пропадали шестимесячные графы ФосАгро, а квартальные
        # шли в комплект за полугодие.
        #
        # **Потерей графы считается отброшенное числом не меньше, чем граф
        # берётся.** Одно лишнее число слева — это номер примечания, номер
        # страницы или число из прозы, затесавшейся в блок формы: у Брусники
        # так считалась потерей фраза «на страницах 13 - 54», у Самолёта —
        # телефон на бланке аудитора. Целая графа приходит колонкой, то есть
        # столькими числами, сколько периодов, — так было у ФосАгро.
        lost = tuple(value for value in surplus if not _looks_like_note_number(value))
        dropped = lost if len(lost) >= periods else ()
        parsed = parsed[-periods:]
        tail = tail[-periods:] if len(tail) >= periods else tail
    # Одинокое двузначное целое при двух и более колонках — номер примечания,
    # а не величина: у ЛСР заголовок раздела «Собственный капитал 21» получал
    # величину 21 и попадал в очередь как статья. У настоящей статьи значение
    # есть за каждый период либо нет вовсе.
    if periods >= 2 and len(parsed) == 1 and _looks_like_note_number(parsed[0]):
        return stripped.strip(), (), (), (), ()
    if not parsed:
        # Графы объявленной длительности в строке не оказалось вовсе: строка
        # несёт величины, но не наши. Величин у неё нет, а отброшенное
        # названо — по нему и сработает контроль.
        return stripped.strip(), (), (), (), dropped

    name = stripped[: tail[0].start()].strip()
    # К наименованию липнут номер примечания, знак сноски и прочерк «нет
    # значения». Каждый из них ломает опознание целиком: «Денежные средства
    # и их эквиваленты 18» справочник не узнаёт, хотя без номера узнаёт.
    # Номер при этом сохраняется: из наименования он убирается, из строки —
    # нет. «Амортизация 6, 7» ссылается на два примечания сразу.
    while name and (trailing := _NOTE_NUMBER.search(name)) is not None:
        # Ссылок бывает две: «Амортизация 6, 7» у ФосАгро. Отсекался прежде
        # только последний номер, и наименование оставалось «Амортизация 6» —
        # справочник такого не знает, и строка уходила в разметку.
        reference.extend(int(part) for part in re.findall(r"\d{1,3}", trailing.group()))
        name = _NOTE_NUMBER.sub("", name).strip()
    for pattern in (_FOOTNOTE_MARK, _TRAILING_DASH):
        name = pattern.sub("", name).strip()
    return (
        name,
        parsed,
        (
            by_columns if by_columns != parsed else (),
            without_note if without_note != parsed else (),
        ),
        tuple(dict.fromkeys(reference)),
        dropped,
    )


def _choose_reading_by_totals(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    alternatives: dict[int, tuple[Decimal, ...]],
    note_readings: dict[int, tuple[Decimal, ...]],
    recognised: dict[int, IfrsPosition],
    catalog: IfrsCatalog,
) -> None:
    """Выбирает чтение неоднозначной строки по итогу её раздела.

    «367 391» — одна величина или две, по написанию не отличить. Отличает
    арифметика: слагаемое не бывает больше своего итога. У Автодора «Прочие
    внеоборотные активы» при первом чтении дают 367 391 при итоге раздела
    9 178 — величина в сорок раз больше итога, в который входит. При втором
    чтении 367 и 391, и оба меньше итога.

    Схождения итога это не требует: часть слагаемых у эмитента может быть
    не опознана справочником, и точной суммы тогда нет вовсе. Требуется
    ровно то, что можно утверждать без справочника: слагаемое не больше
    итога. Чтение меняется только там, где первое чтение это нарушает,
    а второе нет, — иначе перебором подберётся что угодно.
    """
    if not alternatives and not note_readings:
        return
    by_code = {position.code: index for index, position in recognised.items()}
    for total in catalog.totals(form=None):
        place = by_code.get(total.code)
        if place is None:
            continue
        limits = rows[place][1]
        for component in total.components:
            index = by_code.get(component.code)
            if index is None or index not in alternatives:
                continue
            current, other = rows[index][1], alternatives[index]
            if _exceeds(current, limits) and not _exceeds(other, limits):
                logger.info(
                    "строка «%s» прочитана как %s: при чтении %s слагаемое"
                    " больше своего итога %s",
                    rows[index][0],
                    other,
                    current,
                    total.code,
                )
                rows[index] = (rows[index][0], other, rows[index][2])

    _fix_overflowing_sections(rows, alternatives, note_readings, by_code, catalog)

    # Тот же довод для строк, которых справочник не опознал. Итог раздела
    # для них неизвестен, но валюта баланса известна, и статья баланса больше
    # неё не бывает. У Сегежи «Текущая переплата по налогу на прибыль 160 123»
    # при валюте баланса 141 745 — это 160 и 123 за два года.
    root = _root_total(rows, by_code, catalog)
    if root is None:
        return
    for index, other in alternatives.items():
        if index in recognised:
            continue
        current = rows[index][1]
        if _exceeds(current, root) and not _exceeds(other, root):
            logger.info(
                "строка «%s» прочитана как %s: при чтении %s величина больше"
                " валюты баланса",
                rows[index][0],
                other,
                current,
            )
            rows[index] = (rows[index][0], other, rows[index][2])


def _fix_overflowing_sections(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    alternatives: dict[int, tuple[Decimal, ...]],
    note_readings: dict[int, tuple[Decimal, ...]],
    by_code: dict[str, int],
    catalog: IfrsCatalog,
) -> None:
    """Правит чтение там, где сумма неотрицательных слагаемых больше итога.

    Утверждение жёсткое и потому пригодное для проверки: нераскрытое
    слагаемое сумму только увеличивает, поэтому у итога, все слагаемые
    которого неотрицательны, раскрытая часть больше итога не бывает.

    **Спорные строки раздела разбираются вместе, а не по одной.** Поодиночке
    ни одна из них итога не восстанавливает, и правка каждой откатывалась:
    у Сегежи в разделе внеоборотных активов спорны сразу две строки —
    «Гудвил 21 444» (примечание 21 и величина 444) и «Авансы, выданные под
    внеоборотные активы 11 129 1 650» (примечание 11, затем 129 и 1 650), —
    и раздел сходился только при обеих исправленных сразу. Это не единичный
    случай, а класс: номер примечания липнет к величине у каждого эмитента,
    печатающего примечания отдельной колонкой.

    Решает арифметика, и порядок доводов объявлен. Сначала ищется чтение,
    при котором раздел **сходится**: равенство суммы итогу — свидетельство
    сильное, и второго такого чтения обычно нет. Если сойтись не может —
    часть слагаемых не опознана справочником, и точной суммы нет вовсе, —
    годится чтение, при котором сумма итога хотя бы не превышает. И в том
    и в другом случае требуется **единственность**: два чтения, одинаково
    отвечающие доводу, означают, что довод ничего не выбрал, и строки
    уходят на экран сверки неразобранными. Перебором подбирается что угодно,
    поэтому неоднозначность — отказ, а не выбор большинством.

    К отчёту о прибылях правило неприменимо: там слагаемые знаковые,
    и превышение суммы над итогом — обычное дело.
    """
    for total in catalog.totals(form=None):
        place = by_code.get(total.code)
        if place is None:
            continue
        parts = [
            by_code[component.code]
            for component in total.components
            if component.code in by_code
        ]
        if not parts or any(
            value < 0 for index in parts for value in rows[index][1]
        ):
            continue
        movable = [
            index
            for index in parts
            if index in alternatives or index in note_readings
        ]
        if not movable:
            continue
        if len(movable) > MAX_CONTESTED_ROWS:
            # Перебор растёт с каждой спорной строкой. Разделов с таким
            # числом спорных строк в разобранных комплектах нет, и молчать
            # о встреченном нельзя: это сигнал, что разбор колонок сломался
            # целиком, а не в одной строке.
            logger.warning(
                "раздел %s: спорных строк %d, перебор не выполнялся",
                total.code,
                len(movable),
            )
            continue
        _choose_section_reading(
            rows, alternatives, note_readings, parts, place, movable, total.code
        )


def _choose_section_reading(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    alternatives: dict[int, tuple[Decimal, ...]],
    note_readings: dict[int, tuple[Decimal, ...]],
    parts: list[int],
    place: int,
    movable: list[int],
    total_code: str,
) -> None:
    """Выбирает чтение спорных строк раздела разом; при неоднозначности — ничего."""
    variants = [
        [
            rows[index][1],
            *([alternatives[index]] if index in alternatives else []),
            *([note_readings[index]] if index in note_readings else []),
        ]
        for index in movable
    ]
    matched: list[dict[int, tuple[Decimal, ...]]] = []
    fitting: list[dict[int, tuple[Decimal, ...]]] = []
    for combination in product(*variants):
        reading = dict(zip(movable, combination, strict=True))
        verdict = _section_fit(rows, parts, place, reading)
        if verdict is _Fit.MATCHED:
            matched.append(reading)
        elif verdict is _Fit.WITHIN:
            fitting.append(reading)

    chosen = matched[0] if len(matched) == 1 else (
        fitting[0] if not matched and len(fitting) == 1 else None
    )
    logger.info(
        "раздел %s: спорных строк %d, чтений перебрано %d, сошлось %d, "
        "не превышает итога %d",
        total_code,
        len(movable),
        math.prod(len(item) for item in variants),
        len(matched),
        len(fitting),
    )
    if chosen is None:
        return
    for index, values in chosen.items():
        if values == rows[index][1]:
            continue
        logger.info(
            "строка «%s» прочитана как %s: при чтении %s раздел %s не сходился",
            rows[index][0],
            values,
            rows[index][1],
            total_code,
        )
        rows[index] = (rows[index][0], values, rows[index][2])


class _Fit(StrEnum):
    """Как раскрытая часть раздела относится к его итогу."""

    # Сумма равна итогу в пределах допуска — свидетельство сильное.
    MATCHED = "matched"
    # Сумма итога не превышает: часть слагаемых может быть не опознана.
    WITHIN = "within"
    # Сумма больше итога хотя бы в одном периоде: чтение неверно.
    OVER = "over"


def _section_fit(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    parts: list[int],
    place: int,
    reading: dict[int, tuple[Decimal, ...]],
) -> _Fit:
    """Сходится ли раздел при этом чтении спорных строк — по всем периодам сразу.

    По одному периоду совпадение бывает случайным, поэтому требуется
    совпадение по всем: то же правило, по которому опознаётся неподписанный
    итог.
    """
    totals = rows[place][1]
    verdict = _Fit.MATCHED
    for period in range(len(totals)):
        computed = Decimal(0)
        for index in parts:
            values = reading.get(index, rows[index][1])
            if period < len(values):
                computed += values[period]
        total = totals[period]
        if computed > total + _tolerance(total):
            return _Fit.OVER
        if abs(computed - total) > _tolerance(total):
            verdict = _Fit.WITHIN
    return verdict


def _tolerance(total: Decimal) -> Decimal:
    """Допуск сходимости раздела: округление составителя, а не расхождение.

    Тот же вид, что у экрана сверки: доля от итога плюс единица. Величины
    приходят из документа целыми, и допуск нужен ровно на округление
    последнего разряда у составителя отчётности.
    """
    return abs(total) / Decimal(1000) + Decimal(1)


def _overflows(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    parts: list[int],
    place: int,
    period: int,
) -> bool:
    """Больше ли сумма раскрытых слагаемых, чем итог, за этот период."""
    if period >= len(rows[place][1]):
        return False
    computed = sum(
        (rows[index][1][period] for index in parts if period < len(rows[index][1])),
        Decimal(0),
    )
    return computed > rows[place][1][period]


def _root_total(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    by_code: dict[str, int],
    catalog: IfrsCatalog,
) -> tuple[Decimal, ...] | None:
    """Величины корневого итога формы — валюты баланса, если он раскрыт.

    Корневым считается итог, состоящий из одних итогов: «Итого активы»
    и «Итого капитал и обязательства». У отчёта о прибылях такого итога нет,
    и границы правдоподобия там нет тоже — выручка итогом не является.
    """
    for code, index in by_code.items():
        position = catalog.get(code)
        if position is None or not position.components:
            continue
        if all(
            (item := catalog.get(component.code)) is not None and item.is_total
            for component in position.components
        ):
            return rows[index][1]
    return None


def _exceeds(values: tuple[Decimal, ...], limits: tuple[Decimal, ...]) -> bool:
    """Превышает ли хоть одна величина итог своего периода по модулю."""
    return any(
        abs(value) > abs(limit) for value, limit in zip(values, limits, strict=False)
    )


def _without_leading_note(
    texts: list[str], periods: int, grouping: Grouping
) -> tuple[Decimal, ...]:
    """Чтение, при котором первая группа цифр — номер примечания.

    «Авансы, выданные под внеоборотные активы 11 129 1 650» — это примечание
    11 и величины 129 и 1 650, а читается как 11 129 и 1 650. Отсечение
    номера слева такой случай не ловит: величин ровно столько, сколько
    периодов, и лишней колонки, по которой номер опознаётся, здесь нет.

    Чтение предлагается, только когда колонок уже ровно столько, сколько
    периодов: иначе спор идёт о самой разметке колонок, и его решает первое
    запасное чтение. Номером считается целое от 1 до 99 — то же определение,
    по которому номер примечания отличается от величины в других местах
    разбора.
    """
    if periods < 1 or len(texts) != periods:
        return ()
    # Номер примечания не бывает ни отрицательным, ни в скобках. Без этой
    # оговорки у ЛСР «Курсовые разницы при пересчете из других валют
    # (9 202) 3 708» читались как примечание 9 и величина 202: знак терялся
    # вместе с первой группой цифр, и совокупный доход переставал сходиться.
    if texts[0].lstrip().startswith(("-", "−", "(")):
        return ()
    groups = re.findall(r"\d+", texts[0])
    if len(groups) < 2 or not _looks_like_note_number(Decimal(groups[0])):
        return ()
    head = texts[0]
    rest = head[head.index(groups[0]) + len(groups[0]) :]
    values = [_amount(rest, grouping)] + [
        _amount(item, grouping) for item in texts[1:]
    ]
    if any(value is None for value in values):
        return ()
    return tuple(value for value in values if value is not None)


def _by_single_groups(
    cells: list[str], periods: int, grouping: Grouping
) -> tuple[Decimal, ...]:
    """Чтение строки при допущении «одна группа цифр — одна колонка».

    У Автодора «Прочие внеоборотные активы   367 391» — это 367 и 391 за два
    года, а не триста шестьдесят семь тысяч: итог внеоборотных активов у него
    9 178, и величина в сорок раз больше итога своего раздела туда не входит.
    Отличить такую строку от настоящих «367 391» нельзя ничем, кроме
    арифметики, поэтому чтение не подменяет основное, а идёт рядом с ним.
    """
    if grouping is not Grouping.RUSSIAN or periods < 2:
        return ()
    groups: list[tuple[str, bool]] = []
    for cell in cells:
        negative = cell.strip().startswith("(") or cell.strip().startswith(("-", "−"))
        body = cell.strip().strip("()").lstrip("-−").strip()
        if "," in body or "." in body:
            return ()
        if not body.replace(" ", "").replace(_NARROW_SPACE, "").isdigit():
            # Прочерк и прочее нечисловое: цифровых групп здесь нет,
            # и запасного чтения по группам для такой строки не существует.
            return ()
        groups.extend((item, negative) for item in re.split(rf"[{_NARROW_SPACE} ]+", body))
    if len(groups) == periods + 1 and len(groups[0][0]) <= 2:
        # Слева стоит номер примечания: «Отложенные налоговые активы 26 346 169».
        groups = groups[1:]
    if len(groups) != periods or any(not 1 <= len(item) <= 3 for item, _ in groups):
        return ()
    return tuple(
        Decimal(f"-{item}") if negative else Decimal(item) for item, negative in groups
    )


# Хвостовое короткое число наименования — номер примечания, а не часть
# названия статьи. Четырёхзначное не трогаем: оно может быть годом в названии.
# Номер бывает с буквой пункта — у ФосАгро «…по финансовой деятельности,
# нетто 25 (в)», — и без неё номер остаётся в наименовании целиком.
_NOTE_NUMBER = re.compile(r"[\s,]*\b\d{1,3}(?:\s*\([а-яa-z]\))?\s*$", re.IGNORECASE)

# Знак сноски: звёздочка или крестик, приклеенные к наименованию.
_FOOTNOTE_MARK = re.compile(r"[*†‡]+\s*$")

# Прочерк на месте величины: «Приобретение дочерних предприятий   -».
_TRAILING_DASH = re.compile(r"\s+[-–—]\s*$")


def _name_totals_by_structure(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    known: tuple[IfrsPosition, ...],
    form: ExtractedForm,
    dismissed: set[int] | None = None,
) -> None:
    """Опознаёт неподписанные итоги разделов по равенству сумме предшествующих.

    Наименования у такой строки либо нет вовсе (Норникель), либо оно занято
    заголовком раздела (ФосАгро). Единственная опора — структура: строка,
    равная сумме предшествующих строк блока, и есть его итог.

    Равенство проверяется по первому периоду: если строка окажется итогом
    по одному периоду и не окажется по другому, это не итог, а совпадение.
    """
    totals = [item for item in known if item.is_total]
    dismissed = dismissed or set()
    named: list[str] = []
    for index, (name, values, _) in enumerate(rows):
        if index in recognised or index in dismissed or not values:
            continue
        # Раздел — это строки между предыдущим итогом и этой строкой, и в него
        # входят строки, справочником не опознанные: у Норникеля из шести
        # строк внеоборотных активов две справочнику неизвестны, и сумма
        # одних опознанных с итогом не сходилась. Начало отсчёта — первая
        # опознанная строка формы: до неё идут остатки шапки, у которых
        # величины взяты из подписи колонок.
        opened = _section_start(rows, recognised, index)
        if opened is None:
            continue
        preceding = [
            rows[earlier][1]
            for earlier in range(opened, index)
            if not (earlier in recognised and recognised[earlier].is_total)
            and rows[earlier][1]
        ]
        if len(preceding) < 2:
            continue
        inside = {
            recognised[earlier].code
            for earlier in range(opened, index)
            if earlier in recognised
        }
        candidate = _matching_total(values, preceding, totals, recognised, inside)
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


# Разделы баланса, в которых статья стоит и не стоит нигде больше: оборотный
# актив не бывает внеоборотным, долгосрочное обязательство — краткосрочным.
# Прочие разделы (итоги, капитал, ОПУ, потоки) так не противопоставлены.
_EXCLUSIVE_SECTIONS = frozenset(
    {"non_current_assets", "current_assets", "non_current_liabilities", "current_liabilities"}
)


def _resolve_by_section(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    catalog: IfrsCatalog,
    form_code: str,
) -> None:
    """Опознаёт строки, чьё наименование повторяется в разных разделах.

    «Кредиты и займы» стоят в балансе дважды, и различает их раздел: строка
    выше итога долгосрочных обязательств — долгосрочная, выше итога
    краткосрочных — краткосрочная. Раздел берётся от ближайшего итога **ниже**
    строки, по тому же правилу, по которому строится иерархия итогов:
    в МСФО слагаемые стоят над своим итогом.

    Проход второй, а не первый, потому что итоги разделов опознаются
    однозначно и должны быть уже на местах.
    """
    places = _total_places_of(recognised, form_code)
    for index, (name, _, _) in enumerate(rows):
        if index in recognised or not name or not catalog.ambiguous_name(name):
            continue
        code = nearest_total_below(index, places)
        if code is None:
            continue
        below = places[code]
        found = catalog.match_by_name(
            name, section=recognised[below].section, form=form_code
        )
        if found is not None and found.occurs_in(form_code):
            recognised[index] = found
            logger.info(
                "строка «%s» опознана по разделу %s как %s",
                name,
                recognised[below].section,
                found.code,
            )


def _retract_wrong_section(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    form_code: str,
) -> None:
    """Снимает опознание строки, стоящей не в своём разделе.

    Раздел не только разводит одинаковые наименования — он и опровергает
    опознание. У ЛСР «Торговая и прочая дебиторская задолженность» стоит
    во внеоборотных активах, а справочник знает эту строку оборотной,
    и в расчёт уходило 1 410 вместо 215 664. Статья оборотных активов
    не стоит в разделе внеоборотных ни у кого, поэтому такое опознание
    снимается, а строка идёт человеку.

    Проход последний: итоги разделов к этому времени опознаны и по
    наименованию, и структурой, иначе ближайшим итогом ниже оказался бы
    итог чужого раздела.
    """
    places = _total_places_of(recognised, form_code)
    for index, position in list(recognised.items()):
        if position.is_total or position.section not in _EXCLUSIVE_SECTIONS:
            continue
        code = nearest_total_below(index, places)
        if code is None:
            continue
        below = places[code]
        if recognised[below].section not in _EXCLUSIVE_SECTIONS:
            continue
        if recognised[below].section != position.section:
            logger.info(
                "опознание строки «%s» как %s снято: строка стоит в разделе %s",
                rows[index][0],
                position.code,
                recognised[below].section,
            )
            del recognised[index]


def _section_start(
    rows: list[tuple[str, tuple[Decimal, ...], int]],
    recognised: dict[int, IfrsPosition],
    index: int,
) -> int | None:
    """С какой строки идёт раздел, кончающийся этой; None — раздел не начат."""
    closed = [
        earlier
        for earlier in range(index)
        if earlier in recognised and recognised[earlier].is_total
    ]
    if closed:
        return closed[-1] + 1
    opened = [earlier for earlier in range(index) if earlier in recognised]
    return opened[0] if opened else None


def _matching_total(
    values: tuple[Decimal, ...],
    preceding: list[tuple[Decimal, ...]],
    totals: tuple[IfrsPosition, ...],
    recognised: dict[int, IfrsPosition],
    inside: set[str],
) -> IfrsPosition | None:
    """Какой итог справочника описывает эта строка; None — ни один.

    Строка обязана равняться сумме предшествующих по всем периодам сразу:
    совпадение по одному периоду бывает случайным, по двум — уже нет.

    **Равенства суммы мало: итог обязан узнавать свой состав.** Прежде
    брался первый незанятый итог справочника, какой попадётся, и подходил
    он по одному лишь равенству. У Автодора «Финансовые доходы (нетто)»
    равны сумме двух предшествующих строк — это промежуточный итог, кода
    у него в справочнике нет, — и он был назван чистой прибылью: 11 373
    вместо 7 636. Поэтому среди опознанных строк **этого раздела** обязана
    быть хотя бы одна из состава итога.
    """
    periods = min(len(values), min(len(item) for item in preceding))
    if periods == 0:
        return None
    for position in range(periods):
        total = sum((item[position] for item in preceding), start=Decimal(0))
        if total != values[position]:
            return None
    taken = {item.code for item in recognised.values()}
    return next(
        (
            item
            for item in totals
            if item.code not in taken
            and any(part.code in inside for part in item.components)
        ),
        None,
    )


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
