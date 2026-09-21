"""Ручная подача отчётности: разбор выгрузки ГИР БО в формате XLSX.

Источник отдаёт те же формы, что и веб-ресурс, но иначе: выгрузка — это книга
Excel, где первый лист несёт реквизиты организации, а каждый следующий — одну
форму отчётности. Разбор выверен на 71 выгрузке, лежащей в data/inbox.

**Организация, период и единица измерения берутся из содержимого файла.**
Имя файла не значит ничего: его назначает тот, кто выгружал, и проверить его
нечем. Все три величины в выгрузке есть: ИНН — и в реквизитах, и в шапке
каждой формы; период — в заголовках колонок; единица — строкой «Единица
измерения». Там же лежит номер выгрузки вида
`0710099_7736050003_2025_000_20260916`, который повторяет КНД, ИНН, отчётный
год и номер корректировки. Величина, которую не удалось определить или которая
расходится между местами, останавливает разбор: комплекта не возникает вовсе,
и в расчёт попасть нечему.

**Особенности формата, найденные на данных.**

- Ни номер строки шапки таблицы, ни номер колонки кода не постоянны: шапка
  стоит в строке 4 или 5, колонка «Код строки» — с 7-й по 18-ю. Поэтому
  таблица ищется по подписям, а не по координатам.
- Значение может стоять правее своей колонки заголовка: ячейки объединены,
  и границы объединения у шапки и у строк не совпадают. Значение ищется
  от колонки периода до колонки следующего периода.
- К наименованиям и значениям приклеены номера сносок: «Выручка7»,
  «(29 390)5». Без их снятия строка не опознаётся, а число не разбирается.
- Круглые скобки означают либо величину расхода (себестоимость, проценты
  к уплате — в справочнике это `in_brackets`), либо отрицательное число
  (убыток, отрицательный капитал). Что именно — говорит справочник строк,
  а не написание.
- На листе отчёта о финансовых результатах бывает вторая таблица —
  «Дополнительные строки», где код 2410 означает другое («Текущий налог
  на прибыль»). Разбирается только первая таблица листа, число пропущенных
  строк уходит в `src_file.meta`.
- Отчёт об изменениях капитала и отчёт о целевом использовании средств
  методикой не разбираются: их листы пропускаются так же.
"""

import logging
import re
import warnings
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from finlib.config import settings
from finlib.normalize.lines import LinesCatalog, ReportingType, load_lines
from finlib.quality.codes import CheckCode
from finlib.sources.cache import checksum
from finlib.sources.errors import SourceError
from finlib.sources.model import (
    KND_TO_REPORTING_TYPE,
    FormData,
    Organization,
    ReportSet,
    period_date,
)
from finlib.utils import ParseOutcome, parse_value

logger = logging.getLogger(__name__)

# Подписи, по которым в книге находится всё остальное.
CODE_HEADER = "Код строки"
NAME_HEADER = "Наименование показателя"
KND_LABEL = "Форма по КНД"
OKUD_LABEL = "Форма по ОКУД"
INN_LABEL = "ИНН"
UNIT_LABEL = "Единица измерения"
UPLOAD_LABEL = "Номер выгрузки информации"
FULL_NAME_LABEL = "Полное наименование юридического лица"
KPP_LABEL = "КПП"
OKPO_LABEL = "Код по ОКПО"
OKOPF_LABEL = "Организационно-правовая форма (по ОКОПФ)"
OKVED_LABEL = "Вид экономической деятельности по ОКВЭД 2"
OGRN_LABEL = "ОГРН/ОГРНИП"
ADDRESS_LABEL = "Местонахождение (адрес)"

# Номер выгрузки: КНД, ИНН, отчётный год, номер корректировки, дата выгрузки.
# «№» перед номером стоит не во всех выгрузках.
UPLOAD_NUMBER = re.compile(
    r"№?\s*(?P<knd>\d{7})_(?P<inn>\d{10}|\d{12})_(?P<year>\d{4})_"
    r"(?P<correction>\d{1,3})_(?P<uploaded>\d{8})\s*$"
)

# Код строки отчётности РСБУ; пятизначные расшифровки формой не разбираются.
LINE_CODE = re.compile(r"^\d{4}$")

# Год в заголовке колонки периода: «На 31 декабря 2025 г.», «За 2024 г.»,
# «На 31 декабря 2023 года» — написание разнится, год в нём один.
PERIOD_YEAR = re.compile(r"(19|20)\d{2}")

# Номер сноски, приклеенный к значению в скобках: «(29 390)5».
VALUE_FOOTNOTE = re.compile(r"^(\(.*\))\s*\d{1,2}$")

# Номер сноски, приклеенный к наименованию строки: «Выручка7»,
# «Налоги и прибыль (доходы)9». Отделяется минимальный хвост из цифр:
# наименования строк отчётности цифрой не заканчиваются.
NAME_FOOTNOTE = re.compile(r"^(?P<name>.*?[^\d\s])\s*\d{1,2}$")

# Роль периода по порядку колонок: первая — отчётный период комплекта.
PERIOD_PREFIXES: tuple[str, ...] = ("current", "previous", "beforePrevious")

# Сколько первых строк листа реквизитов читать: подписи стоят в начале листа,
# ниже идут подписант и примечания.
_DETAILS_ROWS = 30

XLSX_SUFFIX = ".xlsx"


class InboxRejectedError(SourceError):
    """Файл разобрать нельзя, и причина названа кодом контроля.

    Ошибка несёт ИНН, если его удалось определить: без него запись в журнал
    качества не привязать к организации, и остаётся только отчёт загрузки.
    """

    def __init__(self, check_code: CheckCode, message: str, *, inn: str | None = None) -> None:
        super().__init__(message)
        self.check_code = check_code
        self.inn = inn


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """Разобранная выгрузка: организация, комплект и происхождение файла."""

    path: Path
    checksum: str
    organization: Organization
    report: ReportSet
    declared_unit: str
    skipped_sheets: dict[str, int] = field(default_factory=dict)
    skipped_rows: dict[str, int] = field(default_factory=dict)

    @property
    def meta(self) -> dict[str, Any]:
        """Происхождение комплекта для src_file.meta."""
        return {
            "file_name": self.path.name,
            "declared_unit": self.declared_unit,
            "period_depth": {
                code: form.depth for code, form in self.report.forms.items()
            },
            # Что в файле было, но разобрано не было: листы форм вне методики
            # и строки вторых таблиц листа.
            "skipped_sheets": self.skipped_sheets,
            "skipped_rows": self.skipped_rows,
            # Строки без кода с раскрытым значением: расшифровки строк с кодом
            # либо добавленные организацией строки. Перечень объясняет
            # расхождение итога раздела, если контроль сходимости его найдёт.
            "uncoded_rows": {
                code: list(form.uncoded)
                for code, form in self.report.forms.items()
                if form.uncoded
            },
        }


def _text(cell: object) -> str | None:
    """Содержимое ячейки как строка без переносов; пустая ячейка даёт None."""
    if cell is None:
        return None
    text = " ".join(str(cell).split())
    return text or None


def _cell(sheet: Worksheet, row: int, column: int) -> str | None:
    """Содержимое ячейки по координатам."""
    return _text(sheet.cell(row=row, column=column).value)


def strip_footnote(name: str) -> str:
    """Снимает номер сноски, приклеенный к наименованию строки.

    «Выручка7» — это «Выручка» со сноской 7, а не другое наименование.
    Наименования строк отчётности цифрой не заканчиваются, поэтому снятие
    однозначно.
    """
    match = NAME_FOOTNOTE.match(name)
    return match.group("name").strip() if match else name


def parse_amount(raw: str | None, *, in_brackets: bool) -> Decimal | None:
    """Разбирает ячейку значения; нераскрытое и неразбираемое дают None.

    Скобки — не знак, а способ печати. У расходной строки они означают
    величину расхода, и она хранится положительной (вычитание задаёт оператор
    в составе итога); у всякой другой строки — отрицательное число.
    """
    if raw is None:
        return None
    text = raw
    footnote = VALUE_FOOTNOTE.match(text)
    if footnote is not None:
        text = footnote.group(1)
    bracketed = text.startswith("(")
    if bracketed:
        text = text.strip("()").strip()
    parsed = parse_value(text)
    if parsed.outcome is ParseOutcome.INVALID:
        logger.warning("значение %r разобрать не удалось, считается нераскрытым", raw)
    if parsed.value is None:
        return None
    return -parsed.value if bracketed and not in_brackets else parsed.value


@dataclass(frozen=True, slots=True)
class _Table:
    """Шапка таблицы формы: где стоят код, наименование и колонки периодов."""

    header_row: int
    code_column: int
    name_column: int
    periods: tuple[tuple[int, int], ...]  # (колонка, год)
    last_row: int


def _find_label(sheet: Worksheet, label: str, *, rows: int) -> tuple[int, int] | None:
    """Ищет подпись в первых строках листа и возвращает её координаты."""
    for row in range(1, min(sheet.max_row, rows) + 1):
        for column in range(1, sheet.max_column + 1):
            if _cell(sheet, row, column) == label:
                return row, column
    return None


def _value_right_of(sheet: Worksheet, row: int, column: int) -> str | None:
    """Первое непустое значение правее указанной ячейки в той же строке."""
    for right in range(column + 1, sheet.max_column + 1):
        found = _cell(sheet, row, right)
        if found is not None:
            return found
    return None


def _form_attribute(sheet: Worksheet, label: str) -> str | None:
    """Код формы из шапки листа: «Форма по ОКУД», «Форма по КНД»."""
    found = _find_label(sheet, label, rows=4)
    if found is None:
        return None
    return _value_right_of(sheet, *found)


def _sheet_inn(sheet: Worksheet) -> str | None:
    """ИНН из шапки листа формы: он напечатан вместе с подписью, одной ячейкой."""
    for row in range(1, min(sheet.max_row, 3) + 1):
        for column in range(1, sheet.max_column + 1):
            text = _cell(sheet, row, column)
            if text and text.startswith(f"{INN_LABEL} "):
                digits = text.split()[-1]
                return digits if digits.isdigit() else None
    return None


def _find_table(sheet: Worksheet) -> _Table | None:
    """Находит первую таблицу листа по подписям шапки.

    Вторая таблица листа — «Дополнительные строки» отчёта о финансовых
    результатах — не разбирается: там свои коды, и код 2410 означает в ней
    не ту строку, что в основной таблице.
    """
    header = _find_label(sheet, CODE_HEADER, rows=12)
    if header is None:
        return None
    header_row, code_column = header
    name = _find_label(sheet, NAME_HEADER, rows=12)
    if name is None:
        return None

    periods: list[tuple[int, int]] = []
    for column in range(code_column + 1, sheet.max_column + 1):
        title = _cell(sheet, header_row, column)
        if title is None:
            continue
        year = PERIOD_YEAR.search(title)
        if year is None:
            return None  # колонка периода без года: разбирать вслепую нельзя
        periods.append((column, int(year.group(0))))
    if not periods:
        return None

    last_row = sheet.max_row
    for row in range(header_row + 1, sheet.max_row + 1):
        if _cell(sheet, row, code_column) == CODE_HEADER:
            last_row = row - 1
            break
    return _Table(
        header_row=header_row,
        code_column=code_column,
        name_column=name[1],
        periods=tuple(periods),
        last_row=last_row,
    )


def _row_value(sheet: Worksheet, row: int, table: _Table, index: int) -> str | None:
    """Значение периода в строке; ищется до колонки следующего периода.

    Ячейки объединены, и границы объединения у шапки и у строк расходятся:
    заголовок стоит в колонке M, а значение — в N. Поиск идёт вправо
    до колонки следующего периода, поэтому чужое значение не подхватывается.
    """
    start = table.periods[index][0]
    limit = (
        table.periods[index + 1][0]
        if index + 1 < len(table.periods)
        else sheet.max_column + 1
    )
    for column in range(start, limit):
        found = _cell(sheet, row, column)
        if found is not None:
            return found
    return None


def _in_brackets(
    catalog: LinesCatalog,
    reporting_type: ReportingType,
    form_code: str,
    code: str,
    name: str,
) -> bool:
    """Печатается ли строка как величина расхода.

    Соглашение о знаке объявлено справочником, а не написанием в выгрузке:
    у одной и той же организации налог на прибыль в одном году напечатан
    в скобках, в другом — без них.
    """
    if reporting_type is ReportingType.FULL:
        line = catalog.get(code, reporting_type)
    else:
        line = catalog.match_by_name(name, reporting_type, form_code, source_code=code)
    return bool(line and line.in_brackets)


def parse_form_sheet(
    sheet: Worksheet,
    form_code: str,
    report_year: int,
    reporting_type: ReportingType,
    catalog: LinesCatalog,
) -> tuple[FormData, int]:
    """Разбирает лист одной формы: значения по периодам, наименования строк.

    Возвращает форму и число строк, оставшихся во второй таблице листа.
    """
    table = _find_table(sheet)
    if table is None:
        raise InboxRejectedError(
            CheckCode.FILE_NOT_PARSED,
            f"на листе формы {form_code} не найдена таблица показателей: "
            f"нет подписей «{CODE_HEADER}» и «{NAME_HEADER}» или колонок периодов",
        )
    for _column, year in table.periods:
        if year > report_year:
            raise InboxRejectedError(
                CheckCode.FILE_PERIOD_NOT_DETERMINED,
                f"в форме {form_code} колонка периода за {year} год стоит в комплекте "
                f"за {report_year} год: состав периодов определить нельзя",
            )

    values: dict[date, dict[str, Decimal | None]] = {}
    names: dict[str, str] = {}
    uncoded: list[str] = []
    for row in range(table.header_row + 1, table.last_row + 1):
        code = _cell(sheet, row, table.code_column)
        if code is None or not LINE_CODE.match(code):
            _collect_uncoded(sheet, row, table, uncoded)
            continue
        name = strip_footnote(_cell(sheet, row, table.name_column) or "")
        if code in names:
            raise InboxRejectedError(
                CheckCode.FILE_NOT_PARSED,
                f"в форме {form_code} код строки {code} встречается дважды: "
                f"«{names[code]}» и «{name}». Какую строку считать этим кодом, "
                "выгрузка не говорит",
            )
        names[code] = name
        in_brackets = _in_brackets(catalog, reporting_type, form_code, code, name)
        for index, (_column, year) in enumerate(table.periods):
            raw = _row_value(sheet, row, table, index)
            period = date(year, 12, 31)
            values.setdefault(period, {})[code] = parse_amount(raw, in_brackets=in_brackets)

    skipped = sum(
        1
        for row in range(table.last_row + 1, sheet.max_row + 1)
        if (_cell(sheet, row, table.code_column) or "") and LINE_CODE.match(
            _cell(sheet, row, table.code_column) or ""
        )
    )
    return (
        FormData(form_code=form_code, values=values, names=names, uncoded=tuple(uncoded)),
        skipped,
    )


def _collect_uncoded(
    sheet: Worksheet, row: int, table: _Table, collected: list[str]
) -> None:
    """Запоминает строку без кода, у которой раскрыто значение.

    Такие строки в выгрузке двух родов, и различить их по разметке нельзя:
    расшифровка «в том числе» внутри строки с кодом и дополнительная строка,
    которую организация ввела сама. У одних выгрузок расшифровка сдвинута
    правее наименования, у других стоит в той же колонке.

    Расшифровка уже входит в свою строку с кодом, и грузить её значило бы
    посчитать величину дважды. Дополнительная строка, наоборот, входит в итог
    раздела, и её отсутствие итог разваливает — это и ловит контроль
    сходимости `section_sum`, называя расхождение. Различить их можно только
    арифметикой итога, поэтому здесь они лишь перечисляются: перечень попадает
    в `src_file.meta` и объясняет расхождение тому, кто будет разбираться.
    """
    name = _cell(sheet, row, table.name_column)
    if name is None:
        return
    name = strip_footnote(name)
    if not name or LINE_CODE.match(name) or name.isdigit():
        return  # строка нумерации колонок под шапкой таблицы
    disclosed = any(
        parse_amount(_row_value(sheet, row, table, index), in_brackets=False) is not None
        for index in range(len(table.periods))
    )
    if disclosed:
        collected.append(name)


@dataclass
class _Details:
    """Реквизиты с первого листа книги: подпись — значение."""

    values: dict[str, str] = field(default_factory=dict)

    def get(self, label: str) -> str | None:
        """Значение реквизита по подписи."""
        return self.values.get(label)


def _read_details(sheet: Worksheet) -> _Details:
    """Читает лист реквизитов: в строке слева подпись, справа значение.

    Первое вхождение подписи выигрывает: подпись «ИНН» на листе встречается
    дважды — у организации и у аудиторской организации ниже, — и организация
    стоит первой.
    """
    details = _Details()
    for row in range(1, sheet.max_row + 1):
        cells = [
            _cell(sheet, row, column)
            for column in range(1, sheet.max_column + 1)
        ]
        filled = [text for text in cells if text]
        if len(filled) < 2:
            continue
        label, value = filled[0], filled[1]
        details.values.setdefault(label, value)
    return details


def _upload_number(details: _Details, path: Path) -> re.Match[str]:
    """Разбирает номер выгрузки — единственное место, где есть номер корректировки."""
    raw = details.get(UPLOAD_LABEL)
    match = UPLOAD_NUMBER.match(raw) if raw else None
    if match is None:
        raise InboxRejectedError(
            CheckCode.FILE_NOT_PARSED,
            f"в файле {path.name} нет разборчивого номера выгрузки "
            f"(«{UPLOAD_LABEL}»): ни отчётный год, ни номер корректировки "
            "определить нечем",
        )
    return match


def _determine_inn(details: _Details, upload: re.Match[str], sheets: list[str | None]) -> str:
    """Определяет ИНН по содержимому и требует, чтобы места не расходились."""
    declared = {upload.group("inn")}
    from_details = details.get(INN_LABEL)
    if from_details:
        declared.add(from_details)
    declared.update(inn for inn in sheets if inn)
    if len(declared) != 1:
        raise InboxRejectedError(
            CheckCode.FILE_INN_NOT_DETERMINED,
            "ИНН организации в файле расходится между реквизитами, номером выгрузки "
            f"и шапками форм: {', '.join(sorted(declared))}",
        )
    inn = declared.pop()
    if not (inn.isdigit() and len(inn) in (10, 12)):
        raise InboxRejectedError(
            CheckCode.FILE_INN_NOT_DETERMINED,
            f"ИНН «{inn}» в файле не похож на настоящий: ожидается 10 или 12 цифр",
        )
    return inn


def _determine_unit(details: _Details, catalog: LinesCatalog, inn: str) -> str:
    """Проверяет объявленную единицу измерения по справочнику."""
    declared = details.get(UNIT_LABEL)
    if not declared:
        raise InboxRejectedError(
            CheckCode.UNIT_NOT_DETERMINED,
            f"в файле нет строки «{UNIT_LABEL}»: единица измерения не объявлена, "
            "а принимать её по умолчанию нельзя — ошибка в тысячу раз "
            "не ловится ни одним контролем",
            inn=inn,
        )
    if not catalog.units.matches_declared(declared):
        raise InboxRejectedError(
            CheckCode.UNIT_NOT_DETERMINED,
            f"единица измерения «{declared}» справочником не опознана: "
            f"методика знает «{catalog.units.name}», сопоставить объявленную "
            "с кодом ОКЕИ нечем",
            inn=inn,
        )
    return declared


def _organization(details: _Details, inn: str) -> Organization:
    """Собирает реквизиты организации с первого листа книги."""
    okopf = details.get(OKOPF_LABEL)
    return Organization(
        inn=inn,
        girbo_id=None,  # в выгрузке идентификатора ресурса нет
        short_name=None,  # короткого наименования выгрузка не содержит
        full_name=details.get(FULL_NAME_LABEL),
        ogrn=details.get(OGRN_LABEL),
        kpp=details.get(KPP_LABEL),
        okpo=details.get(OKPO_LABEL),
        okved=details.get(OKVED_LABEL),
        okopf=okopf,
        # Выгрузка называет это «Местонахождение (адрес)» — адресом и пишем:
        # прежде адрес ложился в графу региона, и документ печатал под словом
        # «Регион» дом с помещением.
        address=details.get(ADDRESS_LABEL),
    )


def parse_workbook(path: Path, catalog: LinesCatalog | None = None) -> ParsedFile:
    """Разбирает одну выгрузку XLSX в комплект отчётности.

    Всё, что нельзя определить по содержимому, останавливает разбор названной
    причиной: подставлять умолчания и разбирать имя файла запрещено.
    """
    catalog = catalog if catalog is not None else load_lines()
    try:
        with warnings.catch_warnings():
            # Выгрузка ГИР БО не содержит стилей по умолчанию, и openpyxl
            # сообщает об этом на каждой книге. К данным это отношения
            # не имеет, а вывод команды загрузки делает нечитаемым.
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            workbook = load_workbook(path, data_only=True)
    except Exception as exc:  # openpyxl поднимает разное на битых книгах
        raise InboxRejectedError(
            CheckCode.FILE_NOT_PARSED, f"файл {path.name} не читается как книга XLSX: {exc}"
        ) from exc

    sheets = workbook.worksheets
    if len(sheets) < 2:
        raise InboxRejectedError(
            CheckCode.FILE_NOT_PARSED,
            f"в файле {path.name} нет листов форм отчётности: "
            f"листов всего {len(sheets)}",
        )

    details = _read_details(sheets[0])
    upload = _upload_number(details, path)
    inn = _determine_inn(details, upload, [_sheet_inn(sheet) for sheet in sheets[1:]])
    declared_unit = _determine_unit(details, catalog, inn)

    knd = upload.group("knd")
    reporting_type = KND_TO_REPORTING_TYPE.get(knd)
    if reporting_type is None:
        raise InboxRejectedError(
            CheckCode.FILE_REPORTING_TYPE_UNKNOWN,
            f"код налогового документа {knd} неизвестен: набор строк отчётности "
            "определить нельзя",
            inn=inn,
        )
    report_year = int(upload.group("year"))

    forms: dict[str, FormData] = {}
    skipped_sheets: dict[str, int] = {}
    skipped_rows: dict[str, int] = {}
    known_forms = set(catalog.forms_of(reporting_type))
    for sheet in sheets[1:]:
        okud = _form_attribute(sheet, OKUD_LABEL)
        if okud is None:
            continue  # лист без кода формы формой не является
        if okud not in known_forms:
            # Отчёт об изменениях капитала и отчёт о целевом использовании
            # средств методикой не разбираются.
            skipped_sheets[okud] = skipped_sheets.get(okud, 0) + 1
            continue
        sheet_knd = _form_attribute(sheet, KND_LABEL)
        if sheet_knd != knd:
            raise InboxRejectedError(
                CheckCode.FILE_REPORTING_TYPE_UNKNOWN,
                f"код налогового документа в форме {okud} ({sheet_knd}) расходится "
                f"с номером выгрузки ({knd}): набор строк определить нельзя",
                inn=inn,
            )
        form, extra = parse_form_sheet(sheet, okud, report_year, reporting_type, catalog)
        _check_periods(form, report_year, okud, inn)
        forms[okud] = form
        if extra:
            skipped_rows[okud] = extra

    if not forms:
        raise InboxRejectedError(
            CheckCode.FILE_NOT_PARSED,
            f"в файле {path.name} нет ни одной формы из набора "
            f"«{catalog.reporting_types[reporting_type].name}»",
            inn=inn,
        )

    report = ReportSet(
        inn=inn,
        report_year=report_year,
        report_date=date(report_year, 12, 31),
        knd=knd,
        reporting_type=reporting_type,
        correction_version=int(upload.group("correction")),
        is_actual=True,  # уточняется по всем поданным файлам организации
        forms=forms,
    )
    return ParsedFile(
        path=path,
        checksum=checksum(path.read_bytes()),
        organization=_organization(details, inn),
        report=report,
        declared_unit=declared_unit,
        skipped_sheets=skipped_sheets,
        skipped_rows=skipped_rows,
    )


def _check_periods(form: FormData, report_year: int, form_code: str, inn: str) -> None:
    """Периоды формы обязаны идти подряд от отчётного года вглубь.

    Загрузка различает только отчётный, предыдущий и позапрошлый периоды;
    пропуск года или период старше комплекта означают, что колонки прочитаны
    неверно, и молча грузить такое нельзя.
    """
    expected = {period_date(report_year, prefix) for prefix in PERIOD_PREFIXES}
    unexpected = sorted(period for period in form.values if period not in expected)
    if unexpected:
        raise InboxRejectedError(
            CheckCode.FILE_PERIOD_NOT_DETERMINED,
            f"в форме {form_code} есть периоды вне комплекта {report_year} года: "
            + ", ".join(f"{period:%d.%m.%Y}" for period in unexpected),
            inn=inn,
        )


def inbox_dir() -> Path:
    """Каталог ручной подачи по умолчанию."""
    return settings.data_dir / "inbox"


def files_in(directory: Path | None = None) -> list[Path]:
    """Выгрузки в каталоге подачи, в порядке имён."""
    root = directory if directory is not None else inbox_dir()
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.iterdir()
        if path.is_file()
        and path.suffix.casefold() == XLSX_SUFFIX
        and not path.name.startswith("~$")  # временные файлы Excel
    )


@dataclass
class InboxScan:
    """Лёгкий обзор каталога: какой файл чьей организации.

    Полный разбор книги стоит около трети секунды на файл, и разбирать весь
    каталог ради одной организации незачем. Обзор читает только первый лист
    и только затем, чтобы разложить файлы по ИНН; всё остальное — разбор,
    и он делается по требованию.
    """

    by_inn: dict[str, list[Path]] = field(default_factory=dict)
    # Файлы, у которых ИНН не прочитался или разошёлся с номером выгрузки:
    # к организации их не отнести, и в лёгком обзоре они стоят отдельно.
    unattributed: list[Path] = field(default_factory=list)

    @property
    def inns(self) -> list[str]:
        """Организации, отчётность которых подана."""
        return sorted(self.by_inn)


def _scan_file(path: Path) -> str | None:
    """Читает ИНН с первого листа книги, не разбирая её целиком."""
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            workbook = load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # книга не читается вовсе — разбор скажет почему
        logger.warning("файл %s не читается при обзоре каталога: %s", path.name, exc)
        return None
    try:
        sheet = workbook.worksheets[0] if workbook.worksheets else None
        if sheet is None:
            return None
        # Объявленные размеры листа в выгрузке не соответствуют содержимому,
        # и без сброса читается только первая колонка.
        sheet.reset_dimensions()
        # Подпись «ИНН» на листе встречается дважды: у организации и ниже
        # у аудиторской организации. Организация стоит первой.
        own: str | None = None
        upload: str | None = None
        for row in sheet.iter_rows(min_row=1, max_row=_DETAILS_ROWS, values_only=True):
            filled = [text for text in (_text(value) for value in row) if text]
            if len(filled) < 2:
                continue
            if filled[0] == INN_LABEL and own is None:
                own = filled[1]
            elif filled[0] == UPLOAD_LABEL and upload is None:
                match = UPLOAD_NUMBER.match(filled[1])
                upload = match.group("inn") if match is not None else None
        if own is not None and upload is not None and own != upload:
            return None  # расхождение разберёт полный разбор и назовёт причину
        return own or upload
    finally:
        workbook.close()


def scan_directory(directory: Path | None = None) -> InboxScan:
    """Раскладывает выгрузки каталога по организациям, не разбирая их целиком."""
    scan = InboxScan()
    for path in files_in(directory):
        inn = _scan_file(path)
        if inn is None:
            scan.unattributed.append(path)
            continue
        scan.by_inn.setdefault(inn, []).append(path)
    return scan


def mark_actual(files: list[ParsedFile]) -> list[ParsedFile]:
    """Расставляет признак актуальной корректировки по поданным файлам.

    Источник, сообщающий номер актуальной корректировки, недоступен, поэтому
    актуальной считается наибольшая поданная корректировка года. Признак
    уточняется ещё раз при загрузке — по корректировкам, уже лежащим в базе:
    подача старой версии не должна отменять загруженную новую.
    """
    latest: dict[tuple[str, int], int] = {}
    for item in files:
        key = (item.report.inn, item.report.report_year)
        latest[key] = max(latest.get(key, -1), item.report.correction_version)
    marked: list[ParsedFile] = []
    for item in files:
        key = (item.report.inn, item.report.report_year)
        actual = item.report.correction_version == latest[key]
        if actual == item.report.is_actual:
            marked.append(item)
            continue
        report = ReportSet(
            inn=item.report.inn,
            report_year=item.report.report_year,
            report_date=item.report.report_date,
            knd=item.report.knd,
            reporting_type=item.report.reporting_type,
            correction_version=item.report.correction_version,
            is_actual=actual,
            girbo_bfo_id=item.report.girbo_bfo_id,
            forms=item.report.forms,
        )
        marked.append(
            ParsedFile(
                path=item.path,
                checksum=item.checksum,
                organization=item.organization,
                report=report,
                declared_unit=item.declared_unit,
                skipped_sheets=item.skipped_sheets,
                skipped_rows=item.skipped_rows,
            )
        )
    return marked


class InboxSource:
    """Источник ручной подачи: каталог выгрузок вместо ответа ресурса.

    Интерфейс повторяет `GirboSource` намеренно: комплект, поданный файлом,
    проходит ту же загрузку, те же контроли и тот же расчёт.
    """

    def __init__(
        self, directory: Path | None = None, catalog: LinesCatalog | None = None
    ) -> None:
        self.directory = directory if directory is not None else inbox_dir()
        self._catalog = catalog if catalog is not None else load_lines()
        self._scan: InboxScan | None = None
        self._parsed: dict[Path, ParsedFile] = {}
        self._rejected: dict[Path, InboxRejectedError] = {}

    def __enter__(self) -> "InboxSource":
        """Вход в контекст; закрывать нечего, но вызов симметричен источнику ресурса."""
        return self

    def __exit__(self, *exc: object) -> None:
        """Выход из контекста."""
        return None

    def scan(self) -> InboxScan:
        """Лёгкий обзор каталога; делается один раз за время жизни источника."""
        if self._scan is None:
            self._scan = scan_directory(self.directory)
        return self._scan

    def parse(self, path: Path) -> ParsedFile | None:
        """Разбирает файл, запоминая и разбор, и отказ; None — файл отклонён."""
        if path in self._parsed:
            return self._parsed[path]
        if path in self._rejected:
            return None
        try:
            parsed = parse_workbook(path, self._catalog)
        except InboxRejectedError as exc:
            logger.warning("файл %s отклонён: %s", path.name, exc)
            self._rejected[path] = exc
            return None
        self._parsed[path] = parsed
        return parsed

    def files_for(self, inn: str) -> list[ParsedFile]:
        """Выгрузки одной организации, от раннего года к позднему."""
        found = [
            parsed
            for path in self.scan().by_inn.get(inn, [])
            if (parsed := self.parse(path)) is not None
        ]
        return sorted(
            mark_actual(found),
            key=lambda item: (item.report.report_year, item.report.correction_version),
        )

    def rejections(self, inn: str | None = None) -> list[tuple[Path, InboxRejectedError]]:
        """Отклонённые файлы: все разобранные к этому моменту либо одной организации."""
        rejected = sorted(self._rejected.items())
        if inn is None:
            return rejected
        return [(path, exc) for path, exc in rejected if exc.inn == inn]

    def fetch_report_sets(self, inn: str) -> tuple[Organization, list[ParsedFile]]:
        """Отдаёт реквизиты и все поданные комплекты организации."""
        files = self.files_for(inn)
        if not files:
            raise InboxRejectedError(
                CheckCode.FILE_NOT_PARSED,
                f"в каталоге подачи {self.directory} нет разобранных выгрузок "
                f"по ИНН {inn}",
                inn=inn,
            )
        return _merged_organization(files), files


def _merged_organization(files: list[ParsedFile]) -> Organization:
    """Реквизиты по самой свежей выгрузке, недостающие — по более ранним.

    Состав реквизитов между годами разнится: ОКВЭД есть не во всех выгрузках.
    Берём свежее, пропуски закрываем прежним.
    """
    ordered = sorted(files, key=lambda item: item.report.report_year, reverse=True)
    merged = ordered[0].organization
    for item in ordered[1:]:
        merged = Organization(
            inn=merged.inn,
            girbo_id=merged.girbo_id or item.organization.girbo_id,
            short_name=merged.short_name or item.organization.short_name,
            full_name=merged.full_name or item.organization.full_name,
            ogrn=merged.ogrn or item.organization.ogrn,
            kpp=merged.kpp or item.organization.kpp,
            okpo=merged.okpo or item.organization.okpo,
            okved=merged.okved or item.organization.okved,
            okopf=merged.okopf or item.organization.okopf,
            region=merged.region or item.organization.region,
            address=merged.address or item.organization.address,
        )
    return merged
