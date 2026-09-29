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


def test_the_reconciliation_share_comes_from_the_table(db_conn) -> None:  # noqa: ANN001
    """Доля совпавших берётся из записанной сверки, а без сверки сборка отказывается.

    Решение владельца 29.09.2026: «176 из 183», вписанное строкой в методику,
    пережило бы перемер сверки.
    """
    from finlib.db import execute
    from finlib.report.aggregator import ReconciliationMissingError, _source

    composition = _composition().model_copy(
        update={"source": "На {issuers} эмитентах совпало {matched} из {compared}."}
    )
    execute("DELETE FROM source_reconciliation", conn=db_conn)
    with pytest.raises(ReconciliationMissingError):
        _source(composition, db_conn)
    for code, outcome, role in (
        ("ifrs.revenue", "совпало", "reporting"),
        ("ifrs.cash", "расходится", "reporting"),
        ("ifrs.ppe", "нет в документе", "reporting"),
        ("ifrs.inventories", "совпало", "comparative"),
    ):
        execute(
            "INSERT INTO source_reconciliation (inn, document_path, report_date, "
            "period_role, line_code, aggregator_kind, outcome, code_version) "
            "VALUES ('7838360491', 'x.pdf', %(d)s, %(r)s, %(c)s, 'exact', %(o)s, 'тест')",
            {"d": date(2025, 12, 31), "r": role, "c": code, "o": outcome},
            conn=db_conn,
        )
    # Сравнительная колонка и «нет в документе» в меру не идут.
    assert _source(composition, db_conn) == "На 1 эмитентах совпало 1 из 2."
