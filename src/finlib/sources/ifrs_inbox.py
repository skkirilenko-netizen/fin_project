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
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from finlib.normalize.ifrs_lines import IfrsCatalog, load_ifrs_lines
from finlib.normalize.lines import normalize_name
from finlib.quality.codes import CheckCode
from finlib.sources.ifrs_numbers import (
    ColumnLayout,
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
    # Отчётные даты **каждой формы в отдельности**. В годовом комплекте они
    # у всех форм одни, в промежуточном — нет: МСФО (IAS) 34 требует
    # сравнивать отчёт о прибыли с тем же периодом прошлого года, а отчёт
    # о финансовом положении — с концом прошлого года. Колонок две у обеих
    # форм, а даты у вторых колонок разные, и одна пара дат на весь документ
    # кладёт настоящую величину под чужую дату.
    dates_by_form: dict[str, tuple[date, ...]] = field(default_factory=dict)
    # Формы, которые своих дат не объявили и взяли даты документа. Счётчик
    # стоит рядом: ноль унаследовавших форм и «дат никто не искал» — разные
    # сведения, и различать их должен журнал, а не память.
    inherited_dates: tuple[str, ...] = ()
    # Страницы без текстового слоя, попавшие внутрь форм. Не пустые страницы,
    # а страницы, содержимого которых мы не видим: у Автодора так потерялась
    # вся сторона пассива — баланс занимает страницы 8 и 9, слой есть только
    # у восьмой. Актив при этом сошёлся сам с собой, и ни один контроль
    # пропажи не заметил.
    pages_without_text: tuple[int, ...] = ()
    # Сколько страниц занимают формы: знаменатель доли потерянных страниц.
    # Без него ноль потерь неотличим от ненайденных форм — счётчик
    # проверенного стоит рядом со счётчиком сработавшего.
    form_pages: int = 0
    # Разметка граф каждой формы: сколько их и какие из них за период
    # комплекта. Граф бывает больше, чем отчётных дат, и различаются они
    # длительностью: у промежуточного ФосАгро отчёт о прибыли печатает
    # полугодие и квартал рядом. Без разметки брались последние графы,
    # то есть квартальные, — величины настоящие, период чужой.
    columns_by_form: dict[str, ColumnLayout] = field(default_factory=dict)
    # Формы, которые документ обещал, и откуда взят перечень. Обещание
    # проверяется отдельно от найденного: у СИБУРа страница отчёта о прибылях
    # без текстового слоя — 7-я из 60, — и первой найденной формой стал отчёт
    # о совокупном доходе. Потеря оказалась **до** окна форм, и признак
    # `pages_without_text` о ней молчал: форма, потерянная целиком, выглядит
    # как форма, которой в документе нет.
    expected_forms: tuple[str, ...] = ()
    expected_from: str = ""

    @property
    def missing_forms(self) -> tuple[str, ...]:
        """Формы, обещанные документом и не найденные в тексте.

        Пусто, если не найдено ни одной: тогда это не потеря отдельной формы,
        а документ, в котором форм нет вовсе, и об этом говорит приём.
        """
        if not self.forms:
            return ()
        return tuple(code for code in self.expected_forms if code not in self.forms)

    @property
    def accepted(self) -> bool:
        """Принят ли документ."""
        return True

    def dates_of(self, form_code: str) -> tuple[date, ...]:
        """Отчётные даты формы; у промежуточного баланса они свои.

        Правило то же, что у комплекта РСБУ (`sources.model.ReportSet.
        report_dates`): глубина и состав периодов — свойство формы, а не
        документа. В ГИР БО это видно по балансу, у которого периодов три,
        а у отчёта о финансовых результатах два; в МСФО — по промежуточной
        отчётности, где сравнительные колонки форм относятся к разным датам.
        """
        return self.dates_by_form.get(form_code) or self.report_dates

    def layout_of(self, form_code: str) -> ColumnLayout:
        """Разметка граф формы; у формы без разметки граф столько, сколько дат.

        Имя не `columns_of`: так называется чтение ячеек по координатам PDF
        (`PdfDocument.columns_of`), и это другое — там ячейки строки, здесь
        разметка граф формы.
        """
        dates = self.dates_of(form_code)
        return self.columns_by_form.get(form_code) or ColumnLayout(
            total=len(dates), taken=len(dates)
        )

    @property
    def all_dates(self) -> tuple[date, ...]:
        """Все отчётные даты комплекта, от свежей к ранней.

        Дат комплекта больше, чем дат любой его формы: у промежуточного
        комплекта их три — отчётная, конец прошлого года и то же полугодие
        прошлого года, — и выборка ранее загруженного обязана покрывать все.
        """
        found = set(self.report_dates)
        for item in self.dates_by_form.values():
            found.update(item)
        return tuple(sorted(found, reverse=True))

    def describe(self) -> str:
        """Однострочная сводка для журнала."""
        dates = ", ".join(f"{item:%d.%m.%Y}" for item in self.report_dates)
        own = "; ".join(
            f"{code.removeprefix('ifrs.')}: "
            + ", ".join(f"{item:%d.%m.%Y}" for item in found)
            for code, found in sorted(self.dates_by_form.items())
            if found != self.report_dates
        )
        return (
            f"формы: {len(self.forms)}, валюта {self.currency}, единица "
            f"{self.unit_code}, {self.grouping_detection.describe()}, "
            f"периоды: {dates}, вид отчётности: {self.reporting_kind.value}"
            + (f", даты форм врозь — {own}" if own else "")
            + (
                f", дат не объявили форм {len(self.inherited_dates)} из "
                f"{len(self.forms)}"
                if self.inherited_dates
                else ""
            )
            # Счётчик обещанного стоит рядом с найденным: «форм 3» само
            # по себе не говорит, все ли обещанные формы разобраны.
            + (
                f", обещано форм {len(self.expected_forms)} "
                f"({self.expected_from}), не найдено "
                f"{len(self.missing_forms)}"
                if self.expected_forms
                else ""
            )
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
    document: PdfDocument | None = None,
) -> DocumentProfile | Rejection:
    """Определяет параметры документа либо отказывается его принимать.

    Порядок проверок — часть правила, а не деталь: текстовый слой, тип
    документа, периметр методики, валюта, единица, разделитель разрядов,
    отчётные даты, вид отчётности.

    `grouping` задаёт конвенцию вручную, и тогда определение её пропускается
    целиком. Это выход для документа, у которого разметка чисел не читается
    ни голосованием, ни арифметикой; способ называется в журнале, потому что
    доверие к нему иное — за него отвечает человек, а не документ.

    `document` нужен одной проверке, которую по плоскому тексту сделать
    нельзя: не потеряна ли страница внутри форм. Страница без текстового
    слоя в плоском тексте неотличима от её отсутствия.

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
            {
                "detection": detection.describe(),
                "excluded": removed,
                # Сами числа и строки, в которых они стоят: отказ по конвенции
                # проверяется глазами, и по одним счётчикам сказать, чего улики
                # стоят, нельзя. «1.5» — улика за английскую конвенцию ровно
                # до тех пор, пока не видно, что это ставка процента.
                "evidence": detection.evidence(),
                "where": _evidence_lines(voting_lines, detection),
            },
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

    # Даты документа определены — теперь то же делается по каждой форме
    # в отдельности. Дат документа они не отменяют: отчётная дата у форм
    # одна, расходятся сравнительные.
    by_form, inherited = form_dates(
        text, headings, policy, detection.convention, dates
    )

    kind = _reporting_kind(lowered, policy)
    # Длительность периода комплекта берётся из отчётной даты правилом
    # методики показателей (`annualisation.months_from`). Правило одно
    # на весь проект: второе, заведённое здесь, однажды разошлось бы
    # с первым, и число месяцев у приёма и у расчёта стало бы разным.
    from finlib.metrics.ifrs import months_of

    months = months_of(dates[0], kind.value)
    by_columns, mismatched = form_columns(
        text, headings, policy, detection.convention, by_form, months
    )
    if mismatched and dates[0].month != months:
        # **Объявление сильнее умолчания.** Вид отчётности определяется
        # маркерами, и умолчание у него объявлено: полная. Но документ,
        # у которого формы сами назвали длительность граф, о своём периоде
        # заявил прямо — «за 6 месяцев, закончившихся 30 июня», — и
        # предпочесть этому наше умолчание значило бы отказать документу
        # за нашу же догадку. Длительность проверяется правилом отчётной
        # даты (`annualisation.months_from`), и если графы сходятся с ним,
        # комплект промежуточный, а маркера вида мы не знаем.
        #
        # Число месяцев берётся той же функцией, а не выражением рядом:
        # два способа посчитать одну величину расходятся, и расхождения
        # не видно, пока их не сравнить.
        retry = months_of(dates[0], ReportingKind.INTERIM.value)
        again, still = form_columns(
            text, headings, policy, detection.convention, by_form, retry
        )
        if not still:
            logger.warning(
                "вид отчётности принят умолчанием (%s), а формы объявили графы "
                "за %d мес.: комплект считается промежуточным",
                kind.value,
                retry,
            )
            kind, months, by_columns, mismatched = (
                ReportingKind.INTERIM,
                retry,
                again,
                (),
            )
    if mismatched:
        # Графы формы приведены за период иной длительности, чем период
        # комплекта: брать их значило бы выдать величины одного периода
        # за величины другого. Ошибка не ловится ничем — графа согласована
        # сама с собой, — поэтому отказ, а не выбор.
        return Rejection(
            CheckCode.FILE_COLUMN_SPAN_MISMATCH,
            policy.column_spans.reasons["span_mismatch"],
            {
                # Обе длительности, с которыми сверялись графы: принятая
                # по виду отчётности и данная отчётной датой. Они расходятся,
                # когда вид принят умолчанием, и по одной цифре не понять,
                # с чем именно графы не сошлись.
                "months": months,
                "report_month": dates[0].month,
                "forms": [
                    {
                        "form": code,
                        "spans": list(by_columns[code].spans),
                    }
                    for code in mismatched
                ],
            },
        )

    span = _form_span(document, text, headings, policy)
    promised, promised_from = expected_forms(text, headings, policy)
    profile = DocumentProfile(
        forms=forms,
        currency=foreign or "RUB",
        unit_code=unit,
        grouping=detection.convention,
        report_dates=dates,
        reporting_kind=kind,
        grouping_detection=detection,
        dates_by_form=by_form,
        inherited_dates=inherited,
        pages_without_text=_lost_pages(document, span),
        form_pages=(span[1] - span[0] + 1) if span is not None else 0,
        columns_by_form=by_columns,
        expected_forms=promised,
        expected_from=promised_from,
    )
    logger.info("документ принят: %s", profile.describe())
    if profile.pages_without_text:
        logger.warning(
            "внутри форм потеряны страницы без текстового слоя: %s",
            ", ".join(str(number) for number in profile.pages_without_text),
        )
    return profile


def _form_span(
    document: PdfDocument | None,
    text: str,
    headings: dict[str, int],
    policy: ParsingPolicy,
) -> tuple[int, int] | None:
    """Первая и последняя страница, занятые формами; None — форм не найдено."""
    if document is None or not headings:
        return None
    blocks = form_blocks(text, headings, policy)
    ends = [
        start + sum(len(line) + 1 for line in blocks.get(code, ()))
        for code, start in headings.items()
    ]
    first = document.page_at(min(headings.values()))
    last = document.page_at(max(ends) if ends else min(headings.values()))
    return first, last


def _lost_pages(
    document: PdfDocument | None,
    span: tuple[int, int] | None,
) -> tuple[int, ...]:
    """Страницы без текстового слоя, попавшие внутрь форм.

    Считаются только страницы между первой и последней формой: аудиторское
    заключение сканом — обычное дело и разбору не мешает, а страница
    посреди баланса означает потерю половины формы.
    """
    if document is None or span is None:
        return ()
    first, last = span
    return tuple(
        sorted(
            number
            for number in document.pages_without_text
            if first <= number <= last
        )
    )


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
        # **Оглавление опознаётся перечнем, а не отдельной строкой.** У Самолёта
        # первая строка «Содержания» — «Консолидированный отчет о прибыли или
        # убытке», перенос с номером страницы стоит ниже, и сама она признаку
        # строки оглавления не отвечает: номера в ней нет. Отличает её то,
        # что она стоит в перечне — под ней ещё четыре строки с номерами
        # страниц. Таблицу под таким заголовком изображали телефоны с бланка
        # аудитора, а настоящая форма не разбиралась вовсе.
        if _inside_contents_list(lines, index, policy):
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
        # **Заголовок формы сам строкой таблицы не бывает.** У Брусники ядро
        # нашлось в склейке «(по данным консолидированного отчета о движении |
        # денежных средств) 14, 15 1 538 1 141» — это строка сверки EBITDA
        # внутри другой формы, ссылающаяся на форму по имени. Блоком «отчёта
        # о движении денежных средств» становились десять строк примечания,
        # а настоящая форма стояла ниже и не разбиралась: фактов потока
        # у эмитента не было ни одного.
        lowered = [
            normalize_name(item)
            for item in variants
            if not _is_contents_entry(item) and not _is_table_row(item)
        ]
        for code, cores in policy.document_kind.cores.items():
            if any(
                normalize_name(core) in variant for core in cores for variant in lowered
            ):
                candidates.setdefault(code, []).append(index)
                break

    found = _chosen(candidates, lines, starts, policy)
    if not found:
        return found

    # **Заголовок формы не может стоять внутри примечаний.** У О'КЕЙ страница
    # с отчётом о прибыли или убытке не имеет текстового слоя, и заголовком
    # формы стала проза примечания — «Сравнительные показатели отчёта
    # о прибыли или убытке… были пересчитаны», за которой идёт таблица
    # прекращённой деятельности. Блоком формы становилось примечание целиком:
    # выручкой комплекта оказывалась выручка прекращённой деятельности,
    # 52 756 406 вместо настоящей, а одиннадцать строк примечания уходили
    # в очередь разметки основных форм.
    #
    # Отсчёт примечаний ведётся **от первой найденной формы**, а не от начала
    # документа: до форм нумерованным абзацем идёт аудиторское заключение,
    # и у Европлана «1. Мы не имели возможности получить достаточные
    # надлежащие аудиторские доказательства…» становилось первым примечанием.
    # Тогда внутри примечаний оказывались все формы сразу, и документ объявлялся
    # не отчётностью — при том, что формы в нём есть.
    from finlib.sources.ifrs_notes import first_note_start

    notes_at = first_note_start(text, policy.notes, min(found.values()))
    if notes_at is None:
        return found
    return _chosen(candidates, lines, starts, policy, notes_at)


def _chosen(
    candidates: dict[str, list[int]],
    lines: list[str],
    starts: list[int],
    policy: ParsingPolicy,
    limit: int | None = None,
) -> dict[str, int]:
    """Заголовок каждой формы: первое вхождение, за которым идёт таблица.

    Берётся **первое** вхождение, за которым таблица начинается сразу,
    а не то, за которым строк таблицы больше всего.

    Наибольшее число строк выбирало не ту страницу: форма печатается
    на нескольких, колонтитул повторяется на каждой, и у ЛСР баланс занимал
    страницы 7 и 8 — выбирался колонтитул восьмой, а первая половина баланса
    оставалась за блоком и уходила в отчёт о прибылях.

    Одного лишь «первое годное» тоже мало: у Автодора оглавление стоит
    вплотную к формам, и таблица попадает в окно просмотра сразу за ним.
    Отличает форму от оглавления расстояние: под заголовком формы стоят
    единица измерения и шапка колонок, несколько строк, а за строкой
    оглавления идут другие такие же строки и пустые.

    `limit` отсекает вхождения, стоящие внутри примечаний.
    """
    found: dict[str, int] = {}
    for code, indexes in candidates.items():
        listed = (
            indexes if limit is None else [item for item in indexes if starts[item] < limit]
        )
        first = next(
            (item for item in listed if _heads_a_table(lines, item, policy)), None
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


def is_table_row(line: str) -> bool:
    """Строка таблицы, а не проза, — то же определение, что у границы блока.

    Нужно указателю примечаний: заголовок примечания величин не несёт,
    а строка формы с номером примечания слева выглядит точно так же.
    У СИБУРа «4 Активы, предназначенные для продажи 12 605 -» — строка
    баланса, и указатель считал её примечанием 4; первое настоящее
    примечание у него начинается на шестьдесят тысяч знаков позже.
    Два определения «строки таблицы» неминуемо разошлись бы, поэтому
    определение одно.
    """
    return _is_table_row(line)


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


def expected_forms(
    text: str, headings: dict[str, int], policy: ParsingPolicy
) -> tuple[tuple[str, ...], str]:
    """Формы, которые документ обещал, и откуда взят перечень.

    **Обещание документа надёжнее нашего перечисления.** Форма, обещанная
    документом и не найденная в тексте, — это потеря, а не отсутствие: у СИБУРа
    страница отчёта о прибылях 7-я из 60 лишена текстового слоя, и признак
    потерянных страниц о ней молчал, потому что окно форм начинается с первой
    **найденной**.

    Порядок источников объявлен и есть часть правила:

    1. аудиторское заключение — оно перечисляет проверенную отчётность
       по составу, и это первоисточник;
    2. оглавление — когда заключение нечитаемо (скан, как у Автодора);
    3. обязательный состав МСФО (IAS) 1 — когда нет и оглавления.

    Перечень сужен до форм, которые методика разбирает: отчёт об изменениях
    капитала МСФО (IAS) 1 требует, а мы его не разбираем вовсе, и требовать
    его наличия значило бы объявлять потерю там, где её нет.
    """
    kind = policy.document_kind
    head = text[: min(headings.values())] if headings else text
    prose: set[str] = set()
    listed: set[str] = set()
    lines = [line.strip() for line in head.split("\n")]
    for index, stripped in enumerate(lines):
        if not stripped:
            continue
        # **Обещание тоже переносится по строкам, и в обеих его формах.**
        # В оглавлении у Самолёта «Консолидированный отчет о прибыли или
        # убытке | и прочем совокупном доходе 12», в аудиторском заключении
        # у него же «консолидированного отчета о прибыли или | убытке за 2025
        # год»: без склейки ядро не находится ни там, ни там, и обещание
        # теряется — то есть потеря формы остаётся незамеченной.
        variants = [stripped]
        for ahead in range(1, kind.heading_wrap_lines + 1):
            if index + ahead < len(lines) and lines[index + ahead]:
                variants.append(f"{stripped} {lines[index + ahead]}")
        found = {
            code
            for code, cores in kind.cores.items()
            for variant in variants
            if any(normalize_name(core) in normalize_name(variant) for core in cores)
        }
        if not found:
            continue
        # Строка оглавления опознаётся по любому своему написанию: перенос
        # уносит номер страницы во вторую строку, и по первой она выглядит
        # прозой.
        in_contents = any(_is_contents_entry(item) for item in variants)
        (listed if in_contents else prose).update(found)
    order = list(kind.cores)
    if len(prose) >= kind.min_forms:
        return tuple(code for code in order if code in prose), "audit_report"
    if len(listed) >= kind.min_forms:
        return tuple(code for code in order if code in listed), "contents"
    return tuple(order), "ias1"


def _inside_contents_list(
    lines: list[str], index: int, policy: ParsingPolicy
) -> bool:
    """Стоит ли строка в перечне оглавления: рядом другие строки с номерами.

    Признак строки оглавления по отдельности слаб — ему отвечает и номер
    страницы сам по себе, — а перечень однозначен: столько строк с номером
    в конце подряд бывает только в оглавлении.
    """
    kind = policy.document_kind
    following = lines[index + 1 : index + 1 + kind.contents_list_window]
    listed = sum(1 for line in following if _is_contents_entry(line.strip()))
    return listed >= kind.contents_list_entries


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
    # **Блок формы кончается там, где начинаются примечания**, даже если
    # таблица не кончилась: у примечаний свой справочник и своё назначение,
    # и строка примечания в очереди разметки форм — это величина, которую
    # разметят кодом основной формы и задвоят в итогах.
    from finlib.sources.ifrs_notes import first_note_start

    notes_at = first_note_start(text, policy.notes, min(headings.values())) or len(text)
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
            if starts[first + len(collected)] >= min(end, notes_at):
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


def _evidence_lines(
    lines: list[str], detection: GroupingDetection, limit: int = 6
) -> tuple[str, ...]:
    """Строки, в которых стоят числа-свидетельства обеих конвенций.

    Место важнее самого числа: «2.5» в строке ставки по займу и «2.5»
    в строке величины — разные вещи, и по числу отдельно от строки их
    не различить.
    """
    wanted = [(item, "англ.") for item in detection.english_samples]
    wanted += [(item, "рус.") for item in detection.russian_samples[:2]]
    found: list[str] = []
    for number, side in wanted:
        for line in lines:
            if number in line:
                found.append(f"[{side}] «{number}» в строке: {line.strip()[:90]}")
                break
        if len(found) >= limit:
            break
    return tuple(found)


def table_header_of(lines: list[str], grouping: Grouping) -> list[str]:
    """Шапка таблицы формы: строки от её заголовка до первой статьи.

    Границу объявляет строение таблицы, а не число строк: шапка кончается
    там, где начинается статья — строка с наименованием и величинами,
    не сложенная из слов шапки. Опора на структуру здесь обязательна,
    потому что подписи колонок сами выглядят строками с величинами:
    «В млн руб. Пояснения 2026 г. 2025 г.» несёт два числа, и по числам
    от статьи её не отличить.
    """
    from finlib.sources.ifrs_extract import is_table_header, split_row

    for index, line in enumerate(lines):
        name, values, _, _, _ = split_row(line, grouping)
        if values and name.strip() and not is_table_header(name):
            return lines[:index]
    return list(lines)


def form_dates(
    text: str,
    headings: dict[str, int],
    policy: ParsingPolicy,
    grouping: Grouping,
    fallback: tuple[date, ...],
) -> tuple[dict[str, tuple[date, ...]], tuple[str, ...]]:
    """Отчётные даты каждой формы и перечень форм, взявших даты документа.

    **Даты определяются по форме, а не по документу.** В годовом комплекте
    разницы нет, в промежуточном она есть всегда: отчёт о прибыли сравнивается
    с тем же периодом прошлого года, отчёт о финансовом положении — с концом
    прошлого года. Одна пара дат на весь документ кладёт величину баланса
    на 31 декабря под дату 30 июня: число настоящее, контроли сходятся,
    а периода такого у баланса нет.

    Форма, о своих датах умолчавшая, берёт даты документа, и это объявляется
    перечнем: молча унаследованная дата неотличима от прочитанной.
    """
    blocks = form_blocks(text, headings, policy)
    found: dict[str, tuple[date, ...]] = {}
    inherited: list[str] = []
    for code in headings:
        header = table_header_of(blocks.get(code, []), grouping)
        dates = _report_dates("\n".join(header), policy)
        if not dates:
            inherited.append(code)
            dates = fallback
        found[code] = dates
    logger.info(
        "отчётные даты по формам: %s; дат не объявили форм %d из %d",
        "; ".join(
            f"{code.removeprefix('ifrs.')} — "
            + ", ".join(f"{item:%d.%m.%Y}" for item in dates)
            for code, dates in sorted(found.items())
        ),
        len(inherited),
        len(headings),
    )
    return found, tuple(inherited)


def form_columns(
    text: str,
    headings: dict[str, int],
    policy: ParsingPolicy,
    grouping: Grouping,
    dates_by_form: dict[str, tuple[date, ...]],
    months: int,
) -> tuple[dict[str, ColumnLayout], tuple[str, ...]]:
    """Разметка граф каждой формы и перечень форм с чужой длительностью.

    **Граф в форме бывает больше, чем отчётных дат, и различаются они
    длительностью периода.** Промежуточный отчёт ФосАгро о прибыли или убытке
    печатает четыре графы — полугодие 2026, полугодие 2025, квартал 2026,
    квартал 2025. Брались последние две, то есть квартальные, и комплект
    за полугодие собирался из квартальных величин: ошибка тихая, потому что
    графа согласована сама с собой и все итоги по ней сходятся.

    Длительность читается из шапки таблицы словами (`column_spans.markers`),
    а длительность комплекта — из отчётной даты правилом методики. Графы
    берутся **объявленной длительности**, а не последние; форма, объявившая
    только чужую длительность, называется во втором возвращаемом значении —
    и документ отклоняется приёмом.

    **Порядок длительностей берётся из шапки, и поэтому требуется, чтобы
    они шли подряд.** Заголовок формы попадает в шапку вместе с подписями
    граф, а в нём длительность упоминается тоже: у ФосАгро «за три и шесть
    месяцев». Упоминания одной длительности, разорванные другой, означают,
    что порядок прочитан неверно, и тогда разметка не считается прочитанной
    вовсе: лучше блокирующая запись, чем взятые наугад графы.
    """
    blocks = form_blocks(text, headings, policy)
    found: dict[str, ColumnLayout] = {}
    mismatched: list[str] = []
    for code in headings:
        header = table_header_of(blocks.get(code, []), grouping)
        taken = len(dates_by_form.get(code, ()))
        spans = _column_spans(header, policy)
        total = max(_labelled_columns(header), taken)
        layout = ColumnLayout(
            total=total or taken, taken=taken, spans=spans, months=months
        )
        if spans and months not in spans:
            mismatched.append(code)
        elif spans and layout.wider:
            # Графы делятся на блоки по длительностям: сколько длительностей
            # объявлено, столько и блоков, и в каждом — по одной графе
            # на отчётную дату. Не делится — значит, шапку мы прочли неверно,
            # и разметка не прочитана: подгонять её нельзя.
            layout = (
                replace(layout, offset=spans.index(months) * taken)
                if taken and len(spans) * taken == total
                else replace(layout, spans=())
            )
        found[code] = layout
    logger.info(
        "графы форм: %s",
        "; ".join(
            f"{code.removeprefix('ifrs.')} — {item.describe()}"
            for code, item in sorted(found.items())
        ),
    )
    return found, tuple(mismatched)


def _column_spans(header: list[str], policy: ParsingPolicy) -> tuple[int, ...]:
    """Длительности граф в порядке объявления; пусто — шапка о них молчит."""
    text = normalize_name(" ".join(header))
    seen: list[tuple[int, int]] = []
    for marker, months in policy.column_spans.markers.items():
        start = 0
        needle = normalize_name(marker)
        while (place := text.find(needle, start)) != -1:
            seen.append((place, months))
            start = place + len(needle)
    ordered = [months for _, months in sorted(seen)]
    # Упоминания одной длительности обязаны идти подряд: «6, 6, 3» — это
    # заголовок формы и подписи двух блоков, а «6, 3, 6» означает, что порядок
    # прочитан неверно, и опираться на него нельзя.
    grouped: list[int] = []
    for months in ordered:
        if not grouped or grouped[-1] != months:
            if months in grouped:
                return ()
            grouped.append(months)
    return tuple(grouped)


def _labelled_columns(header: list[str], span: range = range(1900, 2101)) -> int:
    """Сколько граф подписано в шапке: наибольшее число годов в одной строке.

    Подписи граф стоят одной строкой — «2026 2025 2026 2025» у ФосАгро,
    «2025 2024 2023» у Норникеля. У формы, подписанной полными датами
    («30 июня 2026 года» и «31 декабря 2025 года» отдельными строками),
    такой строки нет, и число граф остаётся за отчётными датами.
    """
    best = 0
    for line in header:
        years = [int(item) for item in _YEAR.findall(line) if int(item) in span]
        best = max(best, len(years))
    return best


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

    # Пары одинаковых дня с месяцем не бывает у промежуточного баланса:
    # МСФО (IAS) 34 требует сравнивать отчёт о финансовом положении с концом
    # прошлого года, и колонки подписаны «30 июня 2026 г.» и «31 декабря
    # 2025 г.». Даты в шапке названы обе, и достраивать здесь нечего —
    # прежде шапка уходила в достройку голыми годами, и к двум настоящим
    # датам добавлялась выдуманная 30.06.2025, которой у баланса нет.
    if len(found) >= policy.periods.min_count:
        return tuple(sorted(found, reverse=True)[: policy.periods.max_count])

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
