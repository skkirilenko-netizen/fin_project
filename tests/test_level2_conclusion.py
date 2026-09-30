"""Заключение уровня 2: полный состав разделов, величины сроков — только при сверке."""

from datetime import date
from decimal import Decimal

import pytest

from finlib.report.aggregator import Part
from finlib.report.level2 import Level2Document, _debt
from finlib.report.policy import Level2Conclusion
from finlib.sources.ifrs_debt_note import (
    Check,
    DebtNoteReading,
    Interval,
    MaturityRow,
    MaturityTable,
    PrintedBucket,
    Reconciliation,
    Refusal,
)
from finlib.sources.ifrs_notes import Note

CODES = ("route", "values", "trend", "refinancing", "changes", "audit", "debt", "limits")


def _composition(codes: tuple[str, ...] = CODES) -> Level2Conclusion:
    """Состав из теста, а не из справочника: состав уровня 2 на согласовании."""
    return Level2Conclusion.model_validate(
        {
            "title": "Заключение уровня 2",
            "source": "Агрегатор: {issuers}, {matched}, {compared}.",
            "document": "Документ {document} на {date}.",
            "sections": [{"code": code, "title": code} for code in codes],
            "quarantine": "Карантин: {reasons}.",
            "debt": {
                "basis": "Потоки, примечание {note}, {date}, {unit}.",
                "passed": "Сошлось с {against}: {value} / {reference} {unit}.",
                "with_lease": "С арендой ({against}).",
                "failed": "Не сошлось с {against}: {value} / {reference} {unit}.",
                "no_carrying": "Графы нет ({against}).",
                "no_reference": "Опоры нет ({against}).",
                "not_read": "Сроков нет: {reason}.",
                "as_printed": "Графа «{label}» как напечатана.",
                "unread_rows": "Не прочитано: {names}.",
            },
            "limitations": ["Класса нет."],
            "origin": "тест",
        }
    )


def _document(outcome: Check) -> Level2Document:
    """Документ с одной строкой долга и сверкой с заданным исходом."""
    table = MaturityTable(
        Note(23, "Управление рисками", 0, 0),
        date(2025, 12, 31),
        (Interval(0, 12, "до 1 года"), Interval(12, 60, "от 1 до 5 лет")),
        "carrying_total_buckets",
        (MaturityRow("Кредиты и займы", "debt", Decimal(100), Decimal(120),
                     (Decimal(20), Decimal(100))),),
    )  # fmt: skip
    return Level2Document(
        path="x.pdf",
        report_date=date(2025, 12, 31),
        unit="млн руб.",
        audit=None,
        audit_policy=None,
        debt=DebtNoteReading(table=table),
        reconciliation=Reconciliation(
            "данные агрегатора", outcome, Decimal(100), Decimal(90), None, "385", "385"
        ),
        buckets=(
            PrintedBucket("до 1 года", Decimal(20), code="within_1y"),
            PrintedBucket("от 1 до 5 лет", Decimal(100), as_printed=True, code="m12_60"),
        ),
    )


def test_a_missing_section_is_refused() -> None:
    """Раздел, не объявленный составом, исчез бы молча."""
    with pytest.raises(ValueError, match="разделы"):
        _composition(CODES[:-1])


def test_failed_reconciliation_prints_no_maturities() -> None:
    """Не сошлось — величины сроков не приводятся, и документ так и говорит."""
    part = Part("debt", "Долг")
    _debt(part, _document(Check.FAILED), _composition())
    assert "Не сошлось с данные агрегатора: 100 / 90 млн руб." in part.paragraphs
    assert all(not line.code.startswith("debt_maturity.within") for line in part.lines)


def test_passed_reconciliation_prints_buckets_with_codes() -> None:
    """Сошлось — корзины печатаются с кодом, пересекающая графа названа."""
    part = Part("debt", "Долг")
    _debt(part, _document(Check.PASSED), _composition())
    codes = [line.code for line in part.lines]
    assert "debt_maturity.within_1y" in codes and "debt_maturity.m12_60" in codes
    assert "Графа «от 1 до 5 лет» как напечатана." in part.paragraphs


def test_no_table_names_the_reason() -> None:
    """Таблицы нет — причина словами, чисел нет."""
    part = Part("debt", "Долг")
    document = Level2Document(
        "x.pdf", date(2025, 12, 31), "млн руб.", None, None,
        debt=DebtNoteReading(refusal=Refusal.NO_TABLE),
    )  # fmt: skip
    _debt(part, document, _composition())
    assert part.paragraphs == [f"Сроков нет: {Refusal.NO_TABLE.value}."]
    assert part.lines == []
