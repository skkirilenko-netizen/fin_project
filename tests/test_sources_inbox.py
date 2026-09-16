"""Тесты ручной подачи: разбор выгрузки XLSX и загрузка поданного комплекта.

Выгрузки реальных организаций в репозиторий не коммитятся, поэтому книги
здесь собираются на лету. Собираются они по образцу настоящих: те же подписи,
те же сдвиги колонок между шапкой и значениями, те же сноски, приклеенные
к наименованиям и числам, та же вторая таблица на листе ОФР.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from finlib.db import execute, fetch_all
from finlib.normalize.lines import ReportingType, load_lines
from finlib.normalize.loader import load_report_set
from finlib.quality.codes import CheckCode
from finlib.quality.runner import run_checks
from finlib.sources.inbox import (
    InboxRejectedError,
    InboxSource,
    parse_amount,
    parse_workbook,
    scan_directory,
    strip_footnote,
)
from finlib.sources.model import SourceKind
from finlib.standards import Standard

INN = "7736050003"
TEST_INN = "5001000018"  # ИНН с верным контрольным разрядом, в наборе не занят

# Реквизиты первого листа: подпись слева, значение в колонке I — как в выгрузке.
DETAILS_COLUMN = 9


def _details(
    sheet: Any,
    *,
    inn: str | None = INN,
    knd: str = "0710099",
    year: int = 2025,
    correction: str = "000",
    unit: str | None = "Тыс. руб.",
    upload: str | None = None,
) -> None:
    """Заполняет лист реквизитов организации."""
    rows: list[tuple[str, str | None]] = [
        ("Информация из Государственного информационного ресурса", None),
        ("Дата формирования информации", "16.09.2026"),
        (
            "Номер выгрузки информации",
            upload
            if upload is not None
            else f"№ {knd}_{inn or '0000000000'}_{year}_{correction}_20260916",
        ),
        ("Полное наименование юридического лица", "Общество с ограниченной ответственностью «Т»"),
        ("ИНН", inn),
        ("КПП", "997250001"),
        ("Код по ОКПО", "00040778"),
        ("Вид экономической деятельности по ОКВЭД 2", "61.10.1"),
        ("Местонахождение (адрес)", "Москва"),
        ("Единица измерения", unit),
        ("ОГРН/ОГРНИП", "1027700070518"),
        # Ниже реквизитов организации стоит ИНН аудитора: он не должен
        # приниматься за ИНН организации.
        ("Наименование аудиторской организации", "ООО «Аудит»"),
        ("ИНН", "7701017140"),
    ]
    for index, (label, value) in enumerate(rows, start=1):
        sheet.cell(row=index, column=1, value=label)
        if value is not None:
            sheet.cell(row=index, column=DETAILS_COLUMN, value=value)


def _form_sheet(
    sheet: Any,
    *,
    okud: str,
    knd: str = "0710099",
    header: str = "На 31 декабря 2025 г.",
    periods: tuple[str, ...] = ("На 31 декабря 2025 г.2", "На 31 декабря 2024 г.3"),
    rows: tuple[tuple[str | None, str, tuple[str | None, ...]], ...] = (),
    inn: str = INN,
    code_column: int = 10,
    name_column: int = 4,
    value_shift: int = 1,
) -> None:
    """Заполняет лист формы: шапка, колонки периодов и строки показателей.

    `value_shift` сдвигает значения правее колонки заголовка периода — так
    в настоящих выгрузках расходятся границы объединённых ячеек.
    """
    sheet.cell(row=1, column=3, value=f"ИНН   {inn}")
    sheet.cell(row=1, column=code_column + 8, value="Форма по КНД")
    sheet.cell(row=1, column=code_column + 12, value=knd)
    sheet.cell(row=2, column=code_column + 8, value="Форма по ОКУД")
    sheet.cell(row=2, column=code_column + 12, value=okud)
    sheet.cell(row=3, column=1, value=header)

    sheet.cell(row=5, column=name_column, value="Наименование показателя")
    sheet.cell(row=5, column=code_column, value="Код строки")
    period_columns = [code_column + 2 + index * 4 for index in range(len(periods))]
    for column, title in zip(period_columns, periods, strict=True):
        sheet.cell(row=5, column=column, value=title)
    sheet.cell(row=6, column=name_column, value="2")
    sheet.cell(row=6, column=code_column, value="3")

    for offset, (code, name, values) in enumerate(rows, start=7):
        sheet.cell(row=offset, column=name_column, value=name)
        if code is not None:
            sheet.cell(row=offset, column=code_column, value=code)
        for column, value in zip(period_columns, values, strict=True):
            if value is not None:
                sheet.cell(row=offset, column=column + value_shift, value=value)


BALANCE_ROWS = (
    ("1150", "Основные средства", ("100", "90")),
    ("1100", "Итого по разделу I", ("100", "90")),
    ("1250", "Денежные средства и денежные эквиваленты", ("(50)", "70")),
    ("1200", "Итого по разделу II", ("(50)", "70")),
    ("1600", "БАЛАНС (актив)", ("50", "160")),
    ("1310", "Уставный капитал5", ("10", "10")),
    ("1320", "Собственные акции, выкупленные у акционеров", ("(3)6", "(-)")),
    ("1300", "Итого по разделу III", ("7", "10")),
    ("1520", "Кредиторская задолженность", ("43", "150")),
    ("1500", "Итого по разделу V", ("43", "150")),
    ("1700", "БАЛАНС (пассив)", ("50", "160")),
)

PROFIT_ROWS = (
    ("2110", "Выручка4", ("1 000", "900")),
    ("2120", "Себестоимость продаж", ("(600)5", "(500)")),
    ("2100", "Валовая прибыль (убыток)", ("400", "400")),
    ("2400", "Чистая прибыль (убыток)", ("(120)", "80")),
)


def _workbook(path: Path, **details: Any) -> Path:
    """Собирает книгу полной отчётности и кладёт её в указанный файл."""
    book = Workbook()
    _details(book.active, **details)
    book.active.title = "Сведения об организации"
    shared = {
        "inn": details.get("inn") or INN,
        "knd": details.get("knd", "0710099"),
    }
    _form_sheet(
        book.create_sheet("Бухгалтерский баланс"),
        okud="0710001",
        rows=BALANCE_ROWS,
        **shared,
    )
    _form_sheet(
        book.create_sheet("Отчет о финансовых результатах"),
        okud="0710002",
        periods=("За 2025 г.2", "За 2024 г.3"),
        rows=PROFIT_ROWS,
        **shared,
    )
    book.save(path)
    return path


# --- разбор ячеек -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "in_brackets", "expected"),
    [
        ("1 000", False, Decimal("1000")),
        ("(600)", False, Decimal("-600")),  # скобки у обычной строки — минус
        ("(600)", True, Decimal("600")),  # у расходной — величина расхода
        ("(600)5", True, Decimal("600")),  # со сноской
        ("(29 390)5", True, Decimal("29390")),
        ("-", False, None),
        ("(-)", False, None),
        ("(-)2", False, None),
        ("0.48", False, Decimal("0.48")),
        (None, False, None),
        ("нечисло", False, None),
    ],
)
def test_parse_amount(raw: str | None, in_brackets: bool, expected: Decimal | None) -> None:
    """Скобки, сноски и прочерки разбираются по соглашению справочника."""
    assert parse_amount(raw, in_brackets=in_brackets) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Выручка7", "Выручка"),
        ("Налоги и прибыль (доходы)9", "Налоги и прибыль (доходы)"),
        ("Прочее10", "Прочее"),
        ("Чистая прибыль (убыток)", "Чистая прибыль (убыток)"),
        ("БАЛАНС", "БАЛАНС"),
    ],
)
def test_strip_footnote(raw: str, expected: str) -> None:
    """Номер сноски отделяется от наименования, а само наименование не режется."""
    assert strip_footnote(raw) == expected


# --- разбор книги -----------------------------------------------------------


def test_parse_workbook_reads_content_not_file_name(tmp_path: Path) -> None:
    """Организация, период и единица берутся из содержимого, а не из имени файла."""
    path = _workbook(tmp_path / "как-угодно-названный-файл.xlsx")
    parsed = parse_workbook(path)
    assert parsed.report.inn == INN
    assert parsed.report.report_year == 2025
    assert parsed.report.report_date == date(2025, 12, 31)
    assert parsed.report.knd == "0710099"
    assert parsed.report.reporting_type is ReportingType.FULL
    assert parsed.report.correction_version == 0
    assert parsed.declared_unit == "Тыс. руб."
    assert parsed.organization.okved == "61.10.1"
    # ИНН аудитора ниже по листу за организацию не принимается.
    assert parsed.organization.inn == INN


def test_parse_workbook_values_follow_the_sign_convention(tmp_path: Path) -> None:
    """Расходная строка хранится величиной расхода, убыток — со знаком."""
    parsed = parse_workbook(_workbook(tmp_path / "выгрузка.xlsx"))
    profit = parsed.report.forms["0710002"].values[date(2025, 12, 31)]
    balance = parsed.report.forms["0710001"].values[date(2025, 12, 31)]
    assert profit["2120"] == Decimal("600")  # себестоимость: in_brackets
    assert profit["2400"] == Decimal("-120")  # убыток: знак сохраняется
    assert balance["1320"] == Decimal("3")  # контр-статья капитала
    assert balance["1250"] == Decimal("-50")  # отрицательный остаток
    assert parsed.report.forms["0710001"].values[date(2024, 12, 31)]["1320"] is None


def test_parse_workbook_keeps_line_names(tmp_path: Path) -> None:
    """Наименования строк сохраняются: упрощённые формы опознаются по ним."""
    parsed = parse_workbook(_workbook(tmp_path / "выгрузка.xlsx"))
    names = parsed.report.forms["0710002"].names
    assert names["2110"] == "Выручка"  # сноска снята
    assert names["2120"] == "Себестоимость продаж"


def test_second_table_on_the_sheet_is_not_parsed(tmp_path: Path) -> None:
    """Вторая таблица листа ОФР не разбирается: код 2410 значит в ней другое."""
    book = Workbook()
    _details(book.active)
    book.active.title = "Сведения об организации"
    _form_sheet(book.create_sheet("Бухгалтерский баланс"), okud="0710001", rows=BALANCE_ROWS)
    profit = book.create_sheet("Отчет о финансовых результатах")
    _form_sheet(
        profit,
        okud="0710002",
        periods=("За 2025 г.2", "За 2024 г.3"),
        rows=(*PROFIT_ROWS, ("2410", "Налог на прибыль", ("(7)", "(5)"))),
    )
    # Ниже основной таблицы — «Дополнительные строки» со своей шапкой.
    profit.cell(row=14, column=1, value="Дополнительные строки отчета о финансовых результатах")
    profit.cell(row=15, column=4, value="Наименование показателя")
    profit.cell(row=15, column=10, value="Код строки")
    profit.cell(row=15, column=12, value="За 2025 г.")
    profit.cell(row=16, column=4, value="Текущий налог на прибыль")
    profit.cell(row=16, column=10, value="2410")
    profit.cell(row=16, column=13, value="(99)")
    path = tmp_path / "выгрузка.xlsx"
    book.save(path)

    parsed = parse_workbook(path)
    values = parsed.report.forms["0710002"].values[date(2025, 12, 31)]
    # В полной форме 2410 приходит со знаком, поэтому расход отрицателен;
    # значение (99) второй таблицы в комплект не попадает вовсе.
    assert values["2410"] == Decimal("-7"), "значение берётся из основной таблицы"
    assert parsed.meta["skipped_rows"] == {"0710002": 1}


def test_uncoded_rows_with_values_are_listed(tmp_path: Path) -> None:
    """Строка без кода, несущая значение, попадает в происхождение комплекта.

    Расшифровку строки с кодом и добавленную организацией строку разметка
    не различает; перечень объясняет расхождение итога раздела.
    """
    book = Workbook()
    _details(book.active)
    book.active.title = "Сведения об организации"
    _form_sheet(
        book.create_sheet("Бухгалтерский баланс"),
        okud="0710001",
        rows=(*BALANCE_ROWS, (None, "Средства в банках", ("4 510", "-"))),
    )
    _form_sheet(
        book.create_sheet("Отчет о финансовых результатах"),
        okud="0710002",
        periods=("За 2025 г.2", "За 2024 г.3"),
        rows=PROFIT_ROWS,
    )
    path = tmp_path / "выгрузка.xlsx"
    book.save(path)
    parsed = parse_workbook(path)
    assert parsed.meta["uncoded_rows"] == {"0710001": ["Средства в банках"]}


# --- отказы -----------------------------------------------------------------


def test_reject_without_inn(tmp_path: Path) -> None:
    """Нет ИНН — комплекта не возникает."""
    path = _workbook(tmp_path / "без-инн.xlsx", inn=None)
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.FILE_INN_NOT_DETERMINED
    assert found.value.inn is None, "привязать запись журнала не к чему"


def test_reject_when_inn_disagrees(tmp_path: Path) -> None:
    """Расхождение ИНН между реквизитами и номером выгрузки — отказ, а не выбор."""
    path = _workbook(
        tmp_path / "спор.xlsx", upload=f"№ 0710099_{TEST_INN}_2025_000_20260916"
    )
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.FILE_INN_NOT_DETERMINED


def test_reject_without_period(tmp_path: Path) -> None:
    """Нет разборчивого номера выгрузки — отчётный период определить нечем."""
    path = _workbook(tmp_path / "без-периода.xlsx", upload="выгрузка")
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.FILE_NOT_PARSED


def test_reject_without_unit(tmp_path: Path) -> None:
    """Единица измерения не объявлена — комплект не грузится."""
    path = _workbook(tmp_path / "без-единицы.xlsx", unit=None)
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.UNIT_NOT_DETERMINED
    assert found.value.inn == INN, "ИНН известен, запись журнала привязать есть к чему"


def test_reject_unknown_unit(tmp_path: Path) -> None:
    """Единица, которой справочник не знает, не подменяется известной."""
    path = _workbook(tmp_path / "миллионы.xlsx", unit="Млн руб.")
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.UNIT_NOT_DETERMINED


def test_reject_unknown_knd(tmp_path: Path) -> None:
    """Неизвестный КНД — набор строк отчётности определить нельзя."""
    path = _workbook(tmp_path / "чужой-кнд.xlsx", knd="0710000")
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.FILE_REPORTING_TYPE_UNKNOWN


def test_reject_duplicate_code_in_form(tmp_path: Path) -> None:
    """Один код дважды в одной таблице — разбирать вслепую нельзя."""
    book = Workbook()
    _details(book.active)
    book.active.title = "Сведения об организации"
    _form_sheet(
        book.create_sheet("Бухгалтерский баланс"),
        okud="0710001",
        rows=(*BALANCE_ROWS, ("1150", "Основные средства прочие", ("5", "5"))),
    )
    path = tmp_path / "дубль.xlsx"
    book.save(path)
    with pytest.raises(InboxRejectedError) as found:
        parse_workbook(path)
    assert found.value.check_code is CheckCode.FILE_NOT_PARSED


# --- каталог подачи ---------------------------------------------------------


def test_scan_groups_files_by_organization(tmp_path: Path) -> None:
    """Обзор каталога раскладывает выгрузки по ИНН, не разбирая их целиком."""
    _workbook(tmp_path / "первый.xlsx")
    _workbook(tmp_path / "второй.xlsx", inn=TEST_INN)
    scan = scan_directory(tmp_path)
    assert scan.inns == sorted([INN, TEST_INN])
    assert not scan.unattributed


def test_latest_correction_is_actual(tmp_path: Path) -> None:
    """Актуальной считается наибольшая поданная корректировка года."""
    _workbook(tmp_path / "версия-0.xlsx", correction="000")
    _workbook(tmp_path / "версия-1.xlsx", correction="001")
    source = InboxSource(tmp_path)
    versions = {
        item.report.correction_version: item.report.is_actual
        for item in source.files_for(INN)
    }
    assert versions == {0: False, 1: True}


# --- загрузка поданного комплекта ------------------------------------------


@pytest.fixture
def clean(db_conn):
    """Убирает следы прежних прогонов по тестовому ИНН внутри транзакции теста."""
    execute("DELETE FROM organization WHERE inn = %(i)s", {"i": TEST_INN}, conn=db_conn)
    execute("DELETE FROM dq_log WHERE inn = %(i)s", {"i": TEST_INN}, conn=db_conn)
    return db_conn


def test_loaded_set_says_where_it_came_from(tmp_path: Path, clean) -> None:
    """Комплект из файла помечен источником и объявленной единицей измерения."""
    parsed = parse_workbook(_workbook(tmp_path / "выгрузка.xlsx", inn=TEST_INN))
    load_report_set(
        parsed.report,
        parsed.organization,
        clean,
        raw_path=str(parsed.path),
        checksum=parsed.checksum,
        source=SourceKind.FILE,
        unit_source=load_lines().units.source_for(parsed.report.form_codes),
        meta_extra=parsed.meta,
    )
    rows = fetch_all(
        "SELECT source, unit_code, raw_path, checksum, meta FROM src_file "
        "WHERE inn = %(i)s",
        {"i": TEST_INN},
        conn=clean,
    )
    assert len(rows) == 1
    assert rows[0]["source"] == "file"
    assert rows[0]["unit_code"] == "384"
    assert rows[0]["checksum"] == parsed.checksum
    assert rows[0]["meta"]["file_name"] == "выгрузка.xlsx"


def _simplified_workbook(path: Path, extra_value: str | None) -> Path:
    """Упрощённая выгрузка с чужой строкой в балансе.

    Чужая строка — «Целевые средства» набора для некоммерческих организаций:
    справочником она не опознаётся. Значение у неё задаёт тест.
    """
    book = Workbook()
    _details(book.active, inn=TEST_INN, knd="0710096")
    book.active.title = "Сведения об организации"
    _form_sheet(
        book.create_sheet("Бухгалтерский баланс"),
        okud="0710001",
        knd="0710096",
        periods=("На 31 декабря 2025 г.2", "На 31 декабря 2024 г.3"),
        inn=TEST_INN,
        rows=(
            ("1150", "Материальные внеоборотные активы2", ("100", "90")),
            ("1250", "Денежные средства и денежные эквиваленты", ("50", "70")),
            ("1600", "БАЛАНС", ("150", "160")),
            ("1300", "Капитал", ("150", "160")),
            ("1350", "Целевые средства", (extra_value, "-")),
            ("1700", "БАЛАНС", ("150", "160")),
        ),
    )
    _form_sheet(
        book.create_sheet("Отчет о финансовых результатах"),
        okud="0710002",
        knd="0710096",
        periods=("За 2025 г.2", "За 2024 г.3"),
        inn=TEST_INN,
        rows=(
            ("2110", "Выручка7", ("1 000", "900")),
            ("2120", "Расходы по обычной деятельности8", ("(600)", "(500)")),
            ("2330", "Проценты по уплате", ("(-)", "(-)")),
            ("2340", "Прочие доходы", ("0", "0")),
            ("2350", "Прочие расходы", ("(0)", "(0)")),
            ("2410", "Налоги и прибыль (доходы)9", ("(80)", "(60)")),
            ("2400", "Чистая прибыль (убыток)", ("320", "340")),
        ),
    )
    book.save(path)
    return path


def test_simplified_lines_are_matched_by_name(tmp_path: Path) -> None:
    """Строка упрощённой формы опознаётся по наименованию, включая опечатки."""
    parsed = parse_workbook(_simplified_workbook(tmp_path / "упрощ.xlsx", "-"))
    catalog = load_lines()
    profit = parsed.report.forms["0710002"]
    matched = {
        code: catalog.match_by_name(name, ReportingType.SIMPLIFIED, "0710002", code)
        for code, name in profit.names.items()
    }
    assert matched["2330"] is not None and matched["2330"].code == "2330"
    assert matched["2410"] is not None and matched["2410"].code == "2410"
    # Тёзки «БАЛАНС» разводятся кодом.
    balance = parsed.report.forms["0710001"]
    assert catalog.match_by_name(
        balance.names["1600"], ReportingType.SIMPLIFIED, "0710001", "1600"
    ).code == "1600"
    assert catalog.match_by_name(
        balance.names["1700"], ReportingType.SIMPLIFIED, "0710001", "1700"
    ).code == "1700"


def _load_simplified(path: Path, conn) -> int:
    """Загружает упрощённый комплект из файла и возвращает идентификатор."""
    parsed = parse_workbook(path)
    result = load_report_set(
        parsed.report,
        parsed.organization,
        conn,
        raw_path=str(parsed.path),
        checksum=parsed.checksum,
        source=SourceKind.FILE,
        standard=Standard.RSBU,
        meta_extra=parsed.meta,
    )
    assert result.src_file_id is not None
    return result.src_file_id


def _not_recognized(conn, src_file_id: int) -> list[dict[str, Any]]:
    """Записи журнала о неопознанных строках комплекта."""
    return fetch_all(
        "SELECT severity, status, line_code, new_value FROM dq_log "
        "WHERE src_file_id = %(id)s AND check_code = %(code)s",
        {"id": src_file_id, "code": CheckCode.LINE_NOT_RECOGNIZED.value},
        conn=conn,
    )


def test_unrecognized_empty_line_only_warns(tmp_path: Path, clean) -> None:
    """Неопознанная строка без значения — пробел справочника, не остановка."""
    src_file_id = _load_simplified(_simplified_workbook(tmp_path / "пусто.xlsx", "-"), clean)
    records = _not_recognized(clean, src_file_id)
    assert [row["severity"] for row in records] == ["warning"]
    assert not run_checks(src_file_id, clean).quarantined


def test_unrecognized_line_with_value_blocks(tmp_path: Path, clean) -> None:
    """Неопознанная строка с ненулевым значением — тихая потеря данных.

    Значение в отчётности есть, в расчёт оно не попадёт, и контроль сходимости
    его не хватится: строка не входит ни в один проверяемый итог. Поэтому
    комплект в расчёт не идёт.
    """
    src_file_id = _load_simplified(
        _simplified_workbook(tmp_path / "со-значением.xlsx", "42"), clean
    )
    records = _not_recognized(clean, src_file_id)
    assert [row["severity"] for row in records] == ["blocking"]
    assert records[0]["status"] == "fail"
    assert records[0]["new_value"] == Decimal("42.000")
    report = run_checks(src_file_id, clean)
    assert report.quarantined
    assert CheckCode.LINE_NOT_RECOGNIZED.value in (report.quarantine_reason or "")


def test_unrecognized_zero_line_only_warns(tmp_path: Path, clean) -> None:
    """Раскрытый ноль потерей данных не является."""
    src_file_id = _load_simplified(_simplified_workbook(tmp_path / "ноль.xlsx", "0"), clean)
    assert [row["severity"] for row in _not_recognized(clean, src_file_id)] == ["warning"]
    assert not run_checks(src_file_id, clean).quarantined


def test_repeated_load_does_not_multiply_records(tmp_path: Path, clean) -> None:
    """Повторная загрузка того же комплекта не множит записи о сопоставлении."""
    path = _simplified_workbook(tmp_path / "повтор.xlsx", "-")
    first = _load_simplified(path, clean)
    second = _load_simplified(path, clean)
    assert first == second
    assert len(_not_recognized(clean, first)) == 1
