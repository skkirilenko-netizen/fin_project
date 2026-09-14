"""Тесты справочника строк форм РСБУ."""

from typing import Any

import pytest
from pydantic import ValidationError

from finlib.normalize.lines import LinesCatalog, Operator, Sign, load_lines

# Минимальный перечень кодов из постановки задачи.
REQUIRED_CODES: dict[str, tuple[str, ...]] = {
    "0710001": (
        "1110", "1120", "1130", "1140", "1150", "1160", "1170", "1180", "1190", "1100",
        "1210", "1220", "1230", "1240", "1250", "1260", "1200", "1600",
        "1310", "1320", "1340", "1350", "1360", "1370", "1300",
        "1410", "1420", "1430", "1450", "1400",
        "1510", "1520", "1530", "1540", "1550", "1500", "1700",
    ),
    "0710002": (
        "2110", "2120", "2100", "2210", "2220", "2200",
        "2310", "2320", "2330", "2340", "2350", "2300", "2410", "2400",
    ),
    "0710005": (
        "4110", "4120", "4100", "4210", "4220", "4200",
        "4310", "4320", "4300", "4400", "4450", "4500",
    ),
}


@pytest.fixture(scope="module")
def catalog() -> LinesCatalog:
    """Справочник, загруженный из methodology/lines.yaml."""
    return load_lines()


def test_catalog_loads(catalog: LinesCatalog) -> None:
    """Справочник читается, версия и формы заданы."""
    assert catalog.version
    assert set(catalog.forms) == {"0710001", "0710002", "0710005"}
    assert len(catalog.lines) == len(catalog.codes)


@pytest.mark.parametrize(("form", "codes"), REQUIRED_CODES.items())
def test_required_codes_present(catalog: LinesCatalog, form: str, codes: tuple[str, ...]) -> None:
    """Все коды из постановки задачи есть в справочнике и отнесены к своей форме."""
    for code in codes:
        line = catalog.require(code)
        assert line.form == form, f"строка {code} отнесена к форме {line.form}"
        assert line.name


def test_component_codes_exist(catalog: LinesCatalog) -> None:
    """Все коды в составе итоговых строк существуют в справочнике."""
    for line in catalog.lines:
        for component in line.components:
            assert catalog.has(component.code), (
                f"в составе {line.code} указан отсутствующий код {component.code}"
            )


def test_totals_have_components(catalog: LinesCatalog) -> None:
    """Состав есть у всех итоговых строк и только у них."""
    for line in catalog.lines:
        assert bool(line.components) == line.is_total, f"строка {line.code}"
    assert len(catalog.totals()) >= 14


def test_components_belong_to_same_form(catalog: LinesCatalog) -> None:
    """Итог не собирается из строк другой формы."""
    for line in catalog.totals():
        for component in line.components:
            assert catalog.require(component.code).form == line.form


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        # Сходимость баланса.
        ("1600", (("1100", "+"), ("1200", "+"))),
        ("1700", (("1300", "+"), ("1400", "+"), ("1500", "+"))),
        # Собственные акции уменьшают капитал.
        ("1300", (("1310", "+"), ("1320", "-"), ("1340", "+"),
                  ("1350", "+"), ("1360", "+"), ("1370", "+"))),
        # Цепочка прибыли из контролей задачи 5.
        ("2100", (("2110", "+"), ("2120", "-"))),
        ("2200", (("2100", "+"), ("2210", "-"), ("2220", "-"))),
        # Итог движения денежных средств.
        ("4400", (("4100", "+"), ("4200", "+"), ("4300", "+"))),
    ],
)
def test_known_compositions(
    catalog: LinesCatalog, total: str, expected: tuple[tuple[str, str], ...]
) -> None:
    """Состав ключевых итоговых строк соответствует формам отчётности."""
    line = catalog.require(total)
    actual = tuple((component.code, component.op.value) for component in line.components)
    assert actual == expected


def test_expense_lines_are_subtracted(catalog: LinesCatalog) -> None:
    """Строки в круглых скобках входят в итог с минусом."""
    for line in catalog.totals():
        for component in line.components:
            if catalog.require(component.code).in_brackets:
                assert component.op is Operator.MINUS, (
                    f"{component.code} в составе {line.code} должна вычитаться"
                )


def test_result_lines_allow_negative(catalog: LinesCatalog) -> None:
    """Финансовые результаты и сальдо могут быть отрицательными."""
    for code in ("1370", "1300", "2100", "2200", "2300", "2400", "4100", "4200", "4300", "4400"):
        assert catalog.require(code).sign is Sign.ANY


def test_require_unknown_code_raises(catalog: LinesCatalog) -> None:
    """Неизвестный код не подменяется молча."""
    assert catalog.get("9999") is None
    assert not catalog.has("9999")
    with pytest.raises(KeyError):
        catalog.require("9999")


def test_for_form_and_totals(catalog: LinesCatalog) -> None:
    """Выборки по форме согласованы со справочником."""
    balance = catalog.for_form("0710001")
    assert {line.code for line in balance} >= set(REQUIRED_CODES["0710001"])
    assert all(line.form == "0710002" for line in catalog.totals("0710002"))


def _catalog_dict(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Собирает минимальный справочник для негативных тестов."""
    return {
        "version": "test",
        "forms": {"0710001": {"name": "Бухгалтерский баланс"}},
        "lines": lines,
    }


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (
            [
                {"code": "1110", "name": "А", "form": "0710001", "section": "I"},
                {"code": "1110", "name": "Б", "form": "0710001", "section": "I"},
            ],
            "встречается дважды",
        ),
        (
            [
                {
                    "code": "1100", "name": "Итого", "form": "0710001", "section": "I",
                    "is_total": True, "components": [{"code": "1110", "op": "+"}],
                }
            ],
            "отсутствующий код",
        ),
        (
            [
                {
                    "code": "1100", "name": "Итого", "form": "0710001", "section": "I",
                    "is_total": True, "components": [],
                }
            ],
            "без состава",
        ),
        (
            [
                {
                    "code": "1110", "name": "А", "form": "0710001", "section": "I",
                    "components": [{"code": "1120", "op": "+"}],
                },
                {"code": "1120", "name": "Б", "form": "0710001", "section": "I"},
            ],
            "не итоговая",
        ),
        (
            [
                {
                    "code": "1100", "name": "Итого", "form": "0710001", "section": "I",
                    "is_total": True, "components": [{"code": "1100", "op": "+"}],
                }
            ],
            "состав самой себя",
        ),
        (
            [{"code": "1110", "name": "А", "form": "0710009", "section": "I"}],
            "неизвестную форму",
        ),
    ],
)
def test_broken_catalog_rejected(lines: list[dict[str, Any]], message: str) -> None:
    """Нарушения целостности справочника выявляются при загрузке."""
    with pytest.raises(ValidationError, match=message):
        LinesCatalog.model_validate(_catalog_dict(lines))


def test_cycle_in_components_rejected() -> None:
    """Взаимные ссылки итоговых строк не проходят проверку."""
    lines = [
        {
            "code": "1100", "name": "А", "form": "0710001", "section": "I",
            "is_total": True, "components": [{"code": "1200", "op": "+"}],
        },
        {
            "code": "1200", "name": "Б", "form": "0710001", "section": "II",
            "is_total": True, "components": [{"code": "1100", "op": "+"}],
        },
    ]
    with pytest.raises(ValidationError, match="цикл в составе"):
        LinesCatalog.model_validate(_catalog_dict(lines))
