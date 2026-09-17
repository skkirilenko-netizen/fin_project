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
from dataclasses import dataclass, replace
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
    decisive_evidence,
    detect_grouping,
    drop_not_money_rows,
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
# Год допускает пробел внутри: текстовый слой рвёт числа. У эмитента,
# отчитывающегося в долларах, в шапке стоит «31 декабря 202 5», и дат
# в документе не находилось вовсе. Пробел допускается только там, где год
# стоит при месяце: отдельно взятое «202 5» годом не является.
_LONG_DATE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d\s?\d\s?\d\s?\d)",
    re.IGNORECASE,
)
_SHORT_DATE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")

# Голый год: подпись колонки, когда день и месяц названы один раз в шапке.
_YEAR = re.compile(r"(?<![\d.,])((?:19|20)\d{2})(?![\d.,])")


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
    grouping: Grouping | None = None,
    any_currency: bool = False,
) -> DocumentProfile | Rejection:
    """Определяет параметры документа либо отказывается его принимать.

    Порядок проверок — часть правила, а не деталь: текстовый слой, тип
    документа, периметр методики, валюта, единица, разделитель разрядов,
    отчётные даты, вид отчётности.

    `grouping` задаёт конвенцию вручную, и тогда определение её пропускается
    целиком. Это выход для документа, у которого разметка чисел не читается
    ни голосованием, ни арифметикой; способ называется в журнале, потому что
    доверие к нему иное — за него отвечает человек, а не документ.

    `any_currency` принимает отчётность в любой валюте. Валюта относится
    к **оценке**, а не к разбору: состав статей от неё не зависит, и разметка
    справочника по отчётности в долларах делается ровно так же. Отказ
    остаётся там, где считаются рублёвые показатели, а валюта представления
    хранится в профиле.
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
    if foreign is not None and not any_currency:
        return Rejection(
            CheckCode.FILE_CURRENCY_NOT_ROUBLE,
            policy.currency.reasons["not_rouble"],
            {"currency": foreign},
        )
    if foreign is None and not rouble:
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
    blocks = form_blocks(text, headings, policy)
    voting_lines: list[str] = []
    dropped_rows = 0
    for lines in blocks.values():
        kept, dropped = drop_not_money_rows(lines, policy.digit_grouping)
        voting_lines.extend(kept)
        dropped_rows += dropped

    voting, removed = ballot("\n".join(voting_lines))
    detection = detect_grouping(voting, policy.digit_grouping)
    if grouping is not None:
        detection = replace(
            detection, convention=grouping, reason=None, resolved_by="manual"
        )
        logger.warning(
            "конвенция %s задана вручную; определение по документу дало %s",
            grouping.value,
            detect_grouping(voting, policy.digit_grouping).describe(),
        )
    if removed or dropped_rows:
        logger.info(
            "голосование за конвенцию: исключено чисел %s; строк не в единице "
            "отчётности — %d",
            ", ".join(f"{name} — {count}" for name, count in sorted(removed.items())),
            dropped_rows,
        )

    if not detection.determined:
        # Бесспорная улика — число с обоими разделителями сразу: «11,266.5»
        # русской конвенцией не читается никак. Ищется по всему документу:
        # у ФосАгро такие числа стоят в таблице дивидендов, которую
        # голосование из выборки исключает, а в самих формах улик нет вовсе.
        resolved = resolve_by_both_separators(text, detection)
        if resolved is not None:
            detection = resolved

    if not detection.determined and policy.digit_grouping.arithmetic_resolution.enabled:
        # Сходимость итогов различает конвенции далеко не всегда: умножение
        # всех величин на тысячу сохраняет любое равенство сумм, и у ФосАгро
        # 445 912 + 217 976 = 663 888 сходится при обоих прочтениях. Помогает
        # она там, где прочтения дают разное число раскрытых величин.
        resolved = resolve_by_arithmetic(text, headings, policy, catalog, detection)
        if resolved is not None:
            detection = resolved

    if not detection.determined:
        reason = policy.digit_grouping.reasons[detection.reason]
        return Rejection(
            CheckCode.DIGIT_GROUPING_NOT_DETERMINED,
            reason,
            {"detection": detection.describe(), "excluded": removed},
        )

    # Отчётные даты стоят в шапках форм — «31 декабря 2025 года». По всему
    # документу их находятся десятки: сроки погашения займов, даты договоров,
    # события после отчётной даты. У ЛСР так извлекалась дата 28.07.2066,
    # и период, за который считались величины, оказывался выдуманным.
    #
    # Если в шапках дат нет, поиск расширяется до таблиц форм, но не дальше:
    # у Сегежи дата стоит в строке над таблицей, а не в шапке под заголовком.
    dates = _report_dates(
        " ".join(header_of(text, start, policy) for start in headings.values()),
        policy,
    ) or _report_dates(forms_text(text, headings, policy), policy)
    if not dates:
        return Rejection(
            CheckCode.FILE_PERIODS_NOT_DETERMINED,
            policy.periods.reasons["not_determined"],
        )

    profile = DocumentProfile(
        forms=forms,
        currency=foreign or "RUB",
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
        # Заголовок формы переносится, и ядро наименования разрывается
        # переносом: у Сегежи «О ФИНАНСОВОМ \nПОЛОЖЕНИИ» и «О ДВИЖЕНИИ
        # ДЕНЕЖНЫХ \nСРЕДСТВ». По одной строке такой заголовок не находится
        # вовсе, и формой становилось оглавление — там те же слова умещаются
        # в строку. Поэтому ядро ищется и в строке, склеенной со следующей.
        # Ограничение длины остаётся построчным: длинная фраза прозы
        # заголовком не становится ни сама, ни в склейке.
        # Строка оглавления отбрасывается целиком, вместе со своими склейками:
        # склеенная со следующей строкой оглавления, она перестаёт выглядеть
        # оглавлением — чисел в ней становится два, — и возвращалась
        # в заголовки через собственный же перенос.
        if _is_contents_entry(stripped):
            continue
        variants = [stripped]
        for ahead in range(1, policy.document_kind.heading_wrap_lines + 1):
            if index + ahead >= len(lines):
                break
            following = lines[index + ahead].strip()
            if not following or len(following) > limit:
                break
            variants.append(f"{stripped} {following}")
        # Оглавление отбрасывается признаком оглавления, а не выбором между
        # вхождениями: слова в нём те же самые, и по словам его от заголовка
        # не отличить. Отличает его номер страницы в конце при отсутствии
        # других чисел — у заголовка формы такого вида не бывает.
        lowered = [
            normalize_name(item) for item in variants if not _is_contents_entry(item)
        ]
        for code, cores in policy.document_kind.cores.items():
            if any(
                normalize_name(core) in variant for core in cores for variant in lowered
            ):
                candidates.setdefault(code, []).append(index)
                break

    found: dict[str, int] = {}
    for code, indexes in candidates.items():
        # Берётся **первое** вхождение, за которым таблица начинается сразу,
        # а не то, за которым строк таблицы больше всего.
        #
        # Наибольшее число строк выбирало не ту страницу: форма печатается
        # на нескольких, колонтитул повторяется на каждой, и у ЛСР баланс
        # занимал страницы 7 и 8 — выбирался колонтитул восьмой, а первая
        # половина баланса оставалась за блоком и уходила в отчёт о прибылях.
        #
        # Одного лишь «первое годное» тоже мало: у Автодора оглавление стоит
        # вплотную к формам, и таблица попадает в окно просмотра сразу за ним.
        # Отличает форму от оглавления расстояние: под заголовком формы стоят
        # единица измерения и шапка колонок, несколько строк, а за строкой
        # оглавления идут другие такие же строки и пустые.
        first = next(
            (item for item in indexes if _heads_a_table(lines, item, policy)), None
        )
        if first is not None:
            found[code] = starts[first]
    return found


def _heads_a_table(lines: list[str], index: int, policy: ParsingPolicy) -> bool:
    """Начинается ли под этой строкой таблица формы."""
    kind = policy.document_kind
    following = lines[index + 1 : index + 1 + kind.lookahead_lines]
    distance = next(
        (number for number, line in enumerate(following, 1) if _is_table_row(line)), None
    )
    if distance is None or distance > kind.heading_to_table_lines:
        return False
    return sum(1 for line in following if _is_table_row(line)) >= kind.min_table_rows


# Цифровая группа: подряд идущие цифры. Считаются именно группы, а не числа:
# разделитель разрядов и разделитель колонок здесь оба пробел, и «60 021
# 80 611» на этом этапе неразличимо — одна величина это или две. Группы
# считать можно и не зная конвенции, а числа — нет.
_DIGIT_RUN = re.compile(r"\d+")

# Номер пункта в начале строки: «6. Себестоимость реализованной продукции 18».
_LIST_MARKER = re.compile(r"^\s*\d{1,2}[.)]\s+")

# Чем кончается строка таблицы: величиной, величиной в скобках или прочерком
# на месте нераскрытой величины.
_ENDS_WITH_VALUE = re.compile(r"(?:\d[)%]*|[-–—])\s*$")

# Дата словами и цифрами: «31 декабря 2025 года», «15.04.2026». В строке
# таблицы дат не бывает, а в заголовке формы и в шапке колонок — бывают.
_DATE_IN_LINE = re.compile(
    r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}"
    r"|\d{1,2}\s+[А-Яа-яЁё]{3,8}\s+\d{4}"
    r"|\b(?:19|20)\d{2}\s*(?:год[а-я]*|г\.)",
    re.IGNORECASE,
)


def _is_table_row(line: str) -> bool:
    """Строка таблицы — не менее двух цифровых групп.

    Определение сузилось дважды, и оба раза по живым документам. Прежде
    величиной считалось любое число: оглавление проходило по номеру страницы
    («6. Себестоимость реализованной продукции 18»), и формой становилось
    оно, а не сама форма; а заголовок «ПО СОСТОЯНИЮ НА 31 ДЕКАБРЯ 2025 ГОДА»
    открывал таблицу прежде её первой строки, и многострочная шапка колонок
    у Сегежи вычерпывала весь допуск разрыва — баланс давал ноль строк.

    Поэтому из строки сначала вычитаются номер пункта и даты, и лишь потом
    считаются числа.
    """
    # Строка таблицы кончается величиной последнего периода, проза —
    # словом. Без этого условия за таблицу проходил абзац аудиторского
    # заключения: «…отчетов о прибылях и убытках за годы, закончившиеся
    # 31 декабря 2025, 2024 и 2023» — чисел в нём вдоволь, и формой
    # становился он, а не форма пятнадцатью строками ниже.
    if not _ENDS_WITH_VALUE.search(line):
        return False
    cleaned = _DATE_IN_LINE.sub(" ", _LIST_MARKER.sub("", line))
    runs = _DIGIT_RUN.findall(cleaned)
    # Двух групп мало: в оглавлении ЛСР номер страницы записан диапазоном
    # («о финансовом положении 7-8»), и оглавление проходило за таблицу.
    # У величины отчётности хотя бы одна группа от трёх цифр — у номера
    # страницы и номера примечания столько не бывает.
    return len(runs) >= 2 and any(len(run) >= 3 for run in runs)


# Номер страницы в конце строки оглавления: «… о финансовом положении 6»,
# «… о прибыли или убытке 7-8».
_PAGE_NUMBER = re.compile(r"\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*$")


def _is_contents_entry(text: str) -> bool:
    """Строка оглавления: номер страницы в конце и никаких других чисел."""
    page = _PAGE_NUMBER.search(text)
    if page is None:
        return False
    return not _DIGIT_RUN.search(text[: page.start()])


def _table_rows_after(lines: list[str], index: int, window: int) -> int:
    """Сколько строк с величинами идёт следом за строкой."""
    return sum(1 for line in lines[index + 1 : index + 1 + window] if _is_table_row(line))


def form_blocks(
    text: str, headings: dict[str, int], policy: ParsingPolicy
) -> dict[str, list[str]]:
    """Строки таблицы каждой формы: от заголовка до конца таблицы.

    **Блок кончается там, где кончается таблица**, а не там, где начинается
    следующая форма. Прежде он тянулся до следующего заголовка и захватывал
    примечания целиком: у ЛСР в «блоке баланса» оказывалось 698 строк вместо
    нескольких десятков. Раздутый блок портит и опознание — доля опознанных
    строк считается по мусору, — и голосование за конвенцию, потому что числа
    примечаний подают ложные улики.

    Конец таблицы виден по строкам без величин: подзаголовок раздела — одна
    такая строка, изредка две, а за таблицей идёт сплошной текст.

    **Разрыв страницы таблицу не кончает.** Форма печатается на нескольких
    страницах, и на переломе стоят колонтитул, номер страницы и надпись
    о пояснениях — у ЛСР девять строк без величин подряд, больше допуска.
    Блок баланса обрывался на «Итого активы», и вся сторона капитала
    и обязательств терялась молча. Перелом опознаётся по повтору заголовка
    той же формы, за которым снова идёт таблица: прозаическое упоминание
    формы в примечаниях этому признаку не отвечает.
    """
    if not headings:
        return {}

    lines = text.split("\n")
    starts: list[int] = []
    position = 0
    for line in lines:
        starts.append(position)
        position += len(line) + 1

    ordered = sorted(headings.items(), key=lambda item: item[1])
    gap_limit = policy.document_kind.table_end_gap
    blocks: dict[str, list[str]] = {}
    for index, (code, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else len(text)
        first = next(
            (number for number, offset in enumerate(starts) if offset >= start), 0
        )
        cores = tuple(
            normalize_name(core) for core in policy.document_kind.cores.get(code, ())
        )
        collected: list[str] = []
        gap = 0
        started = False
        for number, line in enumerate(lines[first:], first):
            if starts[first + len(collected)] >= end:
                break
            collected.append(line)
            if _is_table_row(line):
                started = True
                gap = 0
                continue
            if _continues_after_page_break(lines, number, cores, policy):
                # Таблица начинается заново, и над ней снова стоит шапка:
                # номер страницы, надпись о пояснениях, заголовки колонок
                # по строке на дату. У ЛСР их одиннадцать — больше допуска
                # разрыва, — поэтому счёт разрыва не просто обнуляется,
                # а откладывается до первой строки новой таблицы.
                gap, started = 0, False
                continue
            # Разрыв считается только внутри таблицы. До её первой строки
            # идёт шапка — наименование формы, единица, заголовки колонок,
            # каждый своей строкой; у ФосАгро их девять, и счёт разрыва
            # с начала обрывал блок прежде, чем таблица начиналась.
            if started:
                gap += 1
                if gap > gap_limit:
                    del collected[-gap:]
                    break
        blocks[code] = collected
    return blocks


def _continues_after_page_break(
    lines: list[str], index: int, cores: tuple[str, ...], policy: ParsingPolicy
) -> bool:
    """Продолжается ли та же форма на новой странице.

    Признак двойной: строка повторяет заголовок этой формы и за ней снова
    начинается таблица. Одного упоминания мало — в примечаниях форма
    называется прозой, и по одному упоминанию блок утёк бы в пояснения.
    """
    if not cores:
        return False
    lowered = normalize_name(lines[index])
    if not any(core in lowered for core in cores):
        return False
    return _heads_a_table(lines, index, policy)


def forms_text(
    text: str, headings: dict[str, int], policy: ParsingPolicy | None = None
) -> str:
    """Текст таблиц форм — выборка для голосования за конвенцию.

    Всё, что вне таблиц, — примечания, аудиторское заключение, оглавление —
    не участвует: чисел там больше, чем в формах, а денежных величин среди
    них почти нет.
    """
    policy = policy or load_parsing_policy()
    blocks = form_blocks(text, headings, policy)
    return "\n".join("\n".join(lines) for lines in blocks.values())


def resolve_by_both_separators(
    text: str, detection: GroupingDetection
) -> GroupingDetection | None:
    """Разрешает конвенцию числами, содержащими оба разделителя сразу.

    «11,266.5» — английская запись и никакая другая: один и тот же знак
    не бывает в одном числе и разрядным, и десятичным. Такие числа в споре
    не участвуют — они его решают, — поэтому достаточно, чтобы улики были
    только одной стороны.
    """
    russian, english = decisive_evidence(text)
    if bool(russian) == bool(english):
        return None
    winner = Grouping.RUSSIAN if russian else Grouping.ENGLISH
    logger.info(
        "конвенция %s: чисел с обоими разделителями сразу — русских %d, "
        "английских %d",
        winner.value,
        russian,
        english,
    )
    return replace(
        detection, convention=winner, reason=None, resolved_by="both_separators"
    )


def resolve_by_arithmetic(
    text: str,
    headings: dict[str, int],
    policy: ParsingPolicy,
    catalog: IfrsCatalog,
    detection: GroupingDetection,
) -> GroupingDetection | None:
    """Разрешает неоднозначность конвенции сходимостью итогов.

    Формы разбираются обеими конвенциями, и принимается та, при которой
    сходится больше итогов. Если не сходится ни одна либо сходятся обе
    одинаково — ответа нет, и отказ остаётся: арифметика не сказала ничего,
    а выбирать самим здесь и значит угадывать.

    Проверено на ФосАгро, где неоднозначны все величины форм: 445 912 +
    217 976 = 663 888 сходится только при запятой в роли разделителя
    разрядов.
    """
    from finlib.quality.totals import TotalVerdict, check_total
    from finlib.sources.ifrs_extract import extract

    # Даты нужны разбору, но на исход не влияют: сходимость проверяется
    # в пределах одного периода, а колонок у формы столько же при любой
    # конвенции.
    dates = _report_dates(
        " ".join(header_of(text, start, policy) for start in headings.values()),
        policy,
    )
    if not dates:
        return None

    rule = policy.digit_grouping.arithmetic_resolution
    scores: dict[Grouping, int] = {}
    for convention in (Grouping.RUSSIAN, Grouping.ENGLISH):
        found = extract(text, dates, convention, catalog)
        values = found.totals(dates[0])
        matched = 0
        for total in catalog.totals():
            outcome = check_total(
                total,
                values.get,
                lambda code: None,
                lambda amount: abs(amount) / Decimal(10_000) + Decimal(1),
            )
            if outcome.verdict is TotalVerdict.MATCHED:
                matched += 1
        scores[convention] = matched
        logger.info(
            "разрешение арифметикой: при конвенции %s сходится итогов %d",
            convention.value,
            matched,
        )

    best = max(scores, key=lambda item: scores[item])
    rival = max(item for item in scores if item is not best)
    if scores[best] < rule.min_totals or scores[best] == scores[rival]:
        return None
    return replace(
        detection, convention=best, reason=None, resolved_by="arithmetic"
    )


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
        found.add(date(int(year.replace(" ", "")), _MONTHS[month.lower()], int(day)))
    for match in _SHORT_DATE.finditer(text):
        day, month, year = match.groups()
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:  # pragma: no cover — нереальная дата в тексте
            continue
    # Отчётные даты идут рядом и приходятся на один и тот же день года:
    # «31 декабря 2025» и «31 декабря 2024». Прочие даты в шапке — подписание
    # отчётности, утверждение, события после отчётной даты — такой пары
    # не образуют. У Сегежи отчётной датой становилось 15.04.2026, день
    # подписания, и величины раскладывались по несуществующему периоду.
    by_day: dict[tuple[int, int], list[date]] = {}
    for item in found:
        by_day.setdefault((item.day, item.month), []).append(item)
    pairs = [items for items in by_day.values() if len(items) >= policy.periods.min_count]
    if pairs:
        best = max(pairs, key=lambda items: (len(items), max(items)))
        ordered = sorted(best, reverse=True)[: policy.periods.max_count]
        return tuple(ordered)

    # Полная дата в шапке может стоять одна, а колонки подписаны голыми
    # годами: у Норникеля «ЗА ГОДЫ, ЗАКОНЧИВШИЕСЯ 31 ДЕКАБРЯ 2025, 2024
    # И 2023», а над колонками «2025 2024 2023». Тогда день и месяц берутся
    # из единственной даты, а годы — из тех, что названы рядом. Выдумывания
    # здесь нет: и день с месяцем, и каждый год стоят в документе, соединяет
    # их сама формулировка шапки.
    if found:
        latest = max(found)
        span = range(latest.year - policy.periods.max_count + 1, latest.year + 1)
        years = {int(item) for item in _YEAR.findall(text) if int(item) in span}
        completed = {date(year, latest.month, latest.day) for year in years} | found
        if len(completed) >= policy.periods.min_count:
            return tuple(sorted(completed, reverse=True)[: policy.periods.max_count])

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
