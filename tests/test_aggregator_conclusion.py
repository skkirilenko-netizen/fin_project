"""Базовое заключение по данным агрегатора: только агрегатор, состав полный, число с кодом."""

from datetime import date

import pytest
from docx import Document

from finlib.report.aggregator import (
    BaseConclusion,
    Line,
    NotAggregatorOnlyError,
    Part,
    build,
    render,
)
from finlib.report.policy import AggregatorConclusion
from finlib.scoring.routing import Verdict
from finlib.scoring.routing_store import RoutingRow

SECTIONS = [
    {"code": code, "title": code}
    for code in ("route", "values", "trend", "refinancing", "changes", "limits")
]


def _composition(sections: list[dict] = SECTIONS) -> AggregatorConclusion:
    """Состав из теста, а не из справочника: состав справочника на согласовании."""
    return AggregatorConclusion.model_validate(
        {
            "title": "Базовое заключение",
            "no_class": "Класс не присваивается.",
            "source": "Величины агрегатора.",
            "sections": sections,
            "limitations": ["Примечания не рассматривались."],
            "origin": "тест",
        }
    )


def test_a_missing_section_is_refused() -> None:
    """Раздел, не объявленный составом, исчез бы молча."""
    with pytest.raises(ValueError, match="разделы"):
        _composition(SECTIONS[:-1])


def test_a_row_with_document_values_is_not_level_one() -> None:
    """Строка, в величинах которой есть документ, базовым заключением не описывается."""
    row = RoutingRow(
        inn="9703024202",
        name="Сегежа",
        report_date=date(2026, 6, 30),
        verdict=Verdict("review", "Разбор", (), "approved"),
        computed=(),
        sources=("PDF", "Cbonds"),
    )
    with pytest.raises(NotAggregatorOnlyError):
        build(row, None, _composition(), date(2026, 9, 29), "Разбор")


def test_every_number_is_printed_with_its_code(tmp_path) -> None:  # noqa: ANN001
    """Строка с величиной печатается таблицей из трёх граф: наименование, значение, код."""
    said = BaseConclusion(
        "7840346335",
        "Группа Илим",
        date(2026, 9, 29),
        date(2025, 12, 31),
        ["Величины агрегатора."],
        [
            Part(
                "values",
                "Величины",
                lines=[Line("Чистый долг / EBITDA", "6,07", "net_debt_ebitda")],
            )
        ],
    )
    path = render(said, _composition(), tmp_path / "проба.docx", "ПРОБА")
    table = Document(str(path)).tables[0]
    assert [cell.text for cell in table.rows[1].cells] == [
        "Чистый долг / EBITDA",
        "6,07",
        "net_debt_ebitda",
    ]
