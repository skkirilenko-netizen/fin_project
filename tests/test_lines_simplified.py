"""Тесты упрощённого набора строк отчётности (приложение 5 к приказу 66н)."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from finlib.normalize.lines import (
    LinesCatalog,
    ReportingType,
    load_lines,
    normalize_name,
)

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"

SIMPLIFIED_BALANCE = ("1150", "1170", "1210", "1250", "1240", "1600",
                      "1300", "1410", "1450", "1510", "1520", "1550", "1700")
SIMPLIFIED_PROFIT = ("2110", "2120", "2330", "2340", "2350", "2410", "2400")


@pytest.fixture(scope="module")
def catalog() -> LinesCatalog:
    """Справочник, загруженный из methodology/lines.yaml."""
    return load_lines()


def test_simplified_set_is_complete(catalog: LinesCatalog) -> None:
    """Упрощённый набор содержит строки баланса и ОФР из приложения 5."""
    codes = catalog.codes(ReportingType.SIMPLIFIED)
    assert codes == set(SIMPLIFIED_BALANCE) | set(SIMPLIFIED_PROFIT)
    for code in SIMPLIFIED_BALANCE:
        assert catalog.require(code, ReportingType.SIMPLIFIED).form == "0710001"
    for code in SIMPLIFIED_PROFIT:
        assert catalog.require(code, ReportingType.SIMPLIFIED).form == "0710002"


def test_simplified_has_no_cash_flow_form(catalog: LinesCatalog) -> None:
    """ОДДС в упрощённый набор не входит: малые предприятия вправе его не представлять."""
    assert catalog.forms_of(ReportingType.SIMPLIFIED) == ("0710001", "0710002")
    assert "0710005" in catalog.forms_of(ReportingType.FULL)
    assert catalog.for_form("0710005", ReportingType.SIMPLIFIED) == ()


def test_reporting_types_match_schema_check() -> None:
    """Перечисление наборов не расходится с CHECK в sql/001_schema.sql."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    assert "reporting_type IN ('full', 'simplified')" in schema
    assert {kind.value for kind in ReportingType} == {"full", "simplified"}


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        ("1600", (("1150", "+"), ("1170", "+"), ("1210", "+"), ("1250", "+"), ("1240", "+"))),
        ("1700", (("1300", "+"), ("1410", "+"), ("1450", "+"),
                  ("1510", "+"), ("1520", "+"), ("1550", "+"))),
        # 2400 = 2110 − 2120 − 2330 + 2340 − 2350 − 2410
        ("2400", (("2110", "+"), ("2120", "-"), ("2330", "-"),
                  ("2340", "+"), ("2350", "-"), ("2410", "-"))),
    ],
)
def test_simplified_totals(
    catalog: LinesCatalog, total: str, expected: tuple[tuple[str, str], ...]
) -> None:
    """Состав итогов упрощённых форм задан отдельно от полного набора."""
    line = catalog.require(total, ReportingType.SIMPLIFIED)
    actual = tuple((component.code, component.op.value) for component in line.components)
    assert actual == expected


def test_shared_codes_declare_meaning(catalog: LinesCatalog) -> None:
    """Для каждого кода, встречающегося в обоих наборах, смысл объявлен явно."""
    checked = 0
    for line in catalog.for_type(ReportingType.SIMPLIFIED):
        if catalog.has(line.code, ReportingType.FULL):
            checked += 1
            assert line.same_meaning_as_full is not None, line.code
    assert checked == 20, "не нашлось общих кодов: проверять было нечего"


def test_sets_do_not_overlap_in_meaning(catalog: LinesCatalog) -> None:
    """Строки с одинаковым кодом либо совпадают по смыслу, либо различаются наименованием."""
    same, different = 0, 0
    for line in catalog.for_type(ReportingType.SIMPLIFIED):
        twin = catalog.get(line.code, ReportingType.FULL)
        if twin is None:
            continue
        if line.same_meaning_as_full:
            same += 1
            assert tuple(line.aggregates) == (line.code,), line.code
        else:
            different += 1
            assert normalize_name(line.name) != normalize_name(twin.name), line.code
            assert line.note, line.code
    # Обе ветви обязаны быть пройдены: иначе половина правила не проверена.
    assert same > 0 and different > 0, f"совпадающих {same}, отличающихся {different}"


def test_simplified_expenses_are_not_cost_of_sales(catalog: LinesCatalog) -> None:
    """Расходы по обычной деятельности не выдаются за себестоимость полной формы."""
    simplified = catalog.require("2120", ReportingType.SIMPLIFIED)
    full = catalog.require("2120", ReportingType.FULL)
    assert simplified.same_meaning_as_full is False
    assert simplified.aggregates == ("2120", "2210", "2220")
    assert full.aggregates == ()
    assert simplified.name != full.name
    assert "себестоимост" in simplified.note.casefold()


def test_aggregates_reference_full_set(catalog: LinesCatalog) -> None:
    """Укрупняемые коды существуют в полном наборе и не делятся между строками."""
    owners: dict[tuple[str, str], str] = {}
    for line in catalog.for_type(ReportingType.SIMPLIFIED):
        for code in line.aggregates:
            source = catalog.require(code, ReportingType.FULL)
            assert source.form == line.form
            assert (line.form, code) not in owners, code
            owners[(line.form, code)] = line.code


def test_canonical_code_is_allowed(catalog: LinesCatalog) -> None:
    """Канонический код входит в перечень допустимых кодов строки."""
    for line in catalog.for_type(ReportingType.SIMPLIFIED):
        assert line.code in line.code_allowed
        assert line.accepts_code(line.code)


def test_code_is_a_hint_not_a_key(catalog: LinesCatalog) -> None:
    """Укрупнённая строка опознаёт несколько кодов, строка полного набора — только свой."""
    simplified = catalog.require("1170", ReportingType.SIMPLIFIED)
    assert simplified.accepts_code("1190")
    assert simplified.accepts_code("1110")
    assert not catalog.require("1170", ReportingType.FULL).accepts_code("1190")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Материальные внеоборотные активы", "1150"),
        ("материальные внеоборотные активы", "1150"),
        ("  МАТЕРИАЛЬНЫЕ   ВНЕОБОРОТНЫЕ АКТИВЫ  ", "1150"),
        ("Нематериальные, финансовые и другие внеоборотные активы", "1170"),
        ("Финансовые и другие оборотные активы", "1240"),
        ("Расходы по обычной деятельности", "2120"),
        ("Расходы по обычным видам деятельности", "2120"),
    ],
)
def test_match_by_name(catalog: LinesCatalog, text: str, code: str) -> None:
    """Опознание упрощённой строки идёт по наименованию, а не по коду."""
    form = "0710001" if code.startswith("1") else "0710002"
    line = catalog.match_by_name(text, ReportingType.SIMPLIFIED, form)
    assert line is not None and line.code == code


def test_match_by_name_unknown(catalog: LinesCatalog) -> None:
    """Неопознанное наименование не подменяется ближайшим, возвращается None."""
    assert catalog.match_by_name("Выдуманная строка", ReportingType.SIMPLIFIED, "0710001") is None
    assert catalog.match_by_name("Запасы", ReportingType.SIMPLIFIED, "0710002") is None


def test_match_by_name_is_not_offered_for_full_set(catalog: LinesCatalog) -> None:
    """В полных формах наименования неоднозначны, опознание по ним не предлагается."""
    # «Заёмные средства» — это и 1410, и 1510 одной и той же формы.
    assert catalog.require("1410").name == catalog.require("1510").name
    with pytest.raises(ValueError, match="только для упрощённых форм"):
        catalog.match_by_name("Заёмные средства", ReportingType.FULL, "0710001")


def test_normalize_name_handles_yo_and_punctuation() -> None:
    """Нормализация снимает регистр, «ё» и пунктуацию."""
    assert normalize_name("Заёмные средства") == normalize_name("ЗАЕМНЫЕ, СРЕДСТВА")


def _catalog_dict(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Собирает минимальный справочник для негативных тестов."""
    return {
        "version": "test",
        "units": {
            "okei_code": "384",
            "name": "тыс. руб.",
            "multiplier": "1",
            "forms": ["0710001", "0710002"],
            "origin": "тестовый справочник",
            "names": {"384": "тыс. руб.", "385": "млн руб."},
            "names_origin": "тестовый справочник",
        },
        "measures": {
            "by_form": {"0710001": "stock", "0710002": "flow"},
            "annual_end": "12-31",
            "annual_end_origin": "тестовый справочник",
            "origin": "тестовый справочник",
        },
        "forms": {
            "0710001": {"name": "Бухгалтерский баланс"},
            "0710002": {"name": "Отчёт о финансовых результатах"},
        },
        "reporting_types": {
            "full": {"name": "Полная", "forms": ["0710001", "0710002"]},
            # Форма 0710002 намеренно не входит в упрощённый набор этого справочника.
            "simplified": {"name": "Упрощённая", "forms": ["0710001"]},
        },
        "lines": lines,
    }


def _full(code: str, name: str) -> dict[str, Any]:
    """Строка полного набора для негативных тестов."""
    return {"code": code, "name": name, "form": "0710001", "section": "I"}


def _simplified(code: str, name: str, **extra: Any) -> dict[str, Any]:
    """Строка упрощённого набора для негативных тестов."""
    line: dict[str, Any] = {
        "code": code,
        "reporting_type": "simplified",
        "name": name,
        "form": "0710001",
        "section": "Актив",
        "code_allowed": [code],
        "aggregates": [code],
    }
    line.update(extra)
    return line


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        # Код есть в обоих наборах, но смысл не объявлен.
        (
            [_full("1150", "Основные средства"), _simplified("1150", "Материальные активы")],
            "требуется явный признак",
        ),
        # Объявлено совпадение смысла, хотя строка укрупняет несколько кодов.
        (
            [
                _full("2120", "Себестоимость продаж"),
                _full("2210", "Коммерческие расходы"),
                _simplified(
                    "2120", "Расходы по обычной деятельности",
                    code_allowed=["2120", "2210"], aggregates=["2120", "2210"],
                    same_meaning_as_full=True,
                ),
            ],
            "объявлена совпадающей по смыслу",
        ),
        # Объявлено различие смысла, но наименование то же самое.
        (
            [
                _full("1150", "Основные средства"),
                _simplified(
                    "1150", "Основные  средства",
                    same_meaning_as_full=False, note="пояснение",
                ),
            ],
            "то же наименование",
        ),
        # Различие смысла объявлено без пояснения.
        (
            [
                _full("1150", "Основные средства"),
                _simplified("1150", "Материальные активы", same_meaning_as_full=False),
            ],
            "не снабжена пояснением",
        ),
        # Один и тот же код полного набора укрупнён дважды.
        (
            [
                _full("1110", "Нематериальные активы"),
                _full("1150", "Основные средства"),
                _simplified(
                    "1150", "Материальные активы",
                    code_allowed=["1150", "1110"], aggregates=["1150", "1110"],
                    same_meaning_as_full=False, note="пояснение",
                ),
                _simplified(
                    "1110", "Другие активы",
                    code_allowed=["1110"], aggregates=["1110"],
                    same_meaning_as_full=True,
                ),
            ],
            "укрупняется сразу строками",
        ),
        # Укрупняется код, которого нет в полном наборе.
        (
            [_simplified("1150", "Материальные активы")],
            "отсутствующий в полном наборе",
        ),
        # Канонический код вне перечня допустимых.
        (
            [
                _full("1150", "Основные средства"),
                _simplified("1150", "Материальные активы", code_allowed=["1190"]),
            ],
            "отсутствует в code_allowed",
        ),
        # Поля укрупнения у строки полного набора.
        (
            [{**_full("1150", "Основные средства"), "aggregates": ["1150"]}],
            "не может иметь полей укрупнения",
        ),
        # Форма не входит в набор.
        (
            [
                {
                    "code": "2110", "reporting_type": "simplified", "name": "Выручка",
                    "form": "0710002", "section": "Ф", "code_allowed": ["2110"],
                    "aggregates": ["2110"],
                }
            ],
            "не входит в набор",
        ),
    ],
)
def test_broken_simplified_catalog_rejected(lines: list[dict[str, Any]], message: str) -> None:
    """Нарушения в упрощённом наборе выявляются при загрузке справочника."""
    with pytest.raises(ValidationError, match=message):
        LinesCatalog.model_validate(_catalog_dict(lines))


def test_duplicate_names_within_form_rejected() -> None:
    """Тёзки, которых не развести кодом, — ошибка справочника.

    Перечни допустимых кодов пересекаются, поэтому по коду 1190 выбрать
    строку нельзя, а по наименованию — тем более. Молчаливый выбор запрещён.
    """
    lines = [
        _full("1150", "Основные средства"),
        _full("1190", "Прочие внеоборотные активы"),
        _simplified(
            "1150", "Материальные активы", code_allowed=["1150", "1190"],
            same_meaning_as_full=False, note="п",
        ),
        _simplified(
            "1190", "Другие активы", name_aliases=["Материальные активы"],
            code_allowed=["1190"], same_meaning_as_full=False, note="п",
        ),
    ]
    with pytest.raises(ValidationError, match="опознаёт сразу строки"):
        LinesCatalog.model_validate(_catalog_dict(lines))


def test_duplicate_names_resolved_by_code() -> None:
    """Тёзки с непересекающимися кодами допускаются, и разводит их код.

    Так устроен «БАЛАНС» упрощённой формы: итог актива и итог пассива
    подписаны одним словом, а код у них свой.
    """
    lines = [
        _full("1150", "Основные средства"),
        _full("1190", "Прочие внеоборотные активы"),
        _simplified("1150", "Материальные активы", same_meaning_as_full=False, note="п"),
        _simplified(
            "1190", "Другие активы", name_aliases=["Материальные активы"],
            same_meaning_as_full=False, note="п",
        ),
    ]
    catalog = LinesCatalog.model_validate(_catalog_dict(lines))
    simplified = ReportingType.SIMPLIFIED
    by_own = catalog.match_by_name("Материальные активы", simplified, "0710001", "1150")
    by_alias = catalog.match_by_name("Материальные активы", simplified, "0710001", "1190")
    assert by_own is not None and by_own.code == "1150"
    assert by_alias is not None and by_alias.code == "1190"
    # Без кода тёзки неразличимы, и выбор не делается.
    assert catalog.match_by_name("Материальные активы", simplified, "0710001") is None


def test_misprint_requires_reason() -> None:
    """Опечатка источника объявляется с причиной: иначе её примут за вариант нормы."""
    lines = [
        _full("1150", "Основные средства"),
        _simplified(
            "1150", "Материальные активы", same_meaning_as_full=False, note="п",
            name_misprints=[{"name": "Материальные актив"}],
        ),
    ]
    with pytest.raises(ValidationError):
        LinesCatalog.model_validate(_catalog_dict(lines))


def test_misprint_recognizes_line() -> None:
    """Объявленная опечатка опознаёт строку наравне с наименованием."""
    lines = [
        _full("1150", "Основные средства"),
        _simplified(
            "1150", "Материальные активы", same_meaning_as_full=False, note="п",
            name_misprints=[
                {"name": "Материальные актив", "reason": "опечатка шаблона выгрузки"}
            ],
        ),
    ]
    catalog = LinesCatalog.model_validate(_catalog_dict(lines))
    found = catalog.match_by_name("Материальные актив", ReportingType.SIMPLIFIED, "0710001")
    assert found is not None and found.code == "1150"
