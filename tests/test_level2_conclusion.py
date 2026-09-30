"""Заключение уровня 2: полный состав разделов, величины сроков — по исходу сверки."""

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
                "loans_only": "Займы сошлись: {loans} / {reference}; не сверены {names}.",
                "failed": "Не сошлось с {against}: {value} / {reference} {unit}.",
                "no_carrying": "Графы нет ({against}); потоки как напечатаны.",
                "no_reference": "Опоры нет ({against}).",
                "not_read": "Сроков нет: {reason}.",
                "as_printed": "Графа «{label}» как напечатана.",
                "unread_rows": "Не прочитано: {names}.",
                "loans_label": "Займы",
                "with_debt_like_label": "Займы и долгоподобные ({names})",
                "debt_like_not_reconciled": "С долгоподобными ({names}) не приводятся.",
                "note_total_against": "итог примечания {note}",
            },
            "limitations": ["Класса нет."],
            "origin": "тест",
        }
    )


def _document(outcome: Check, debt_like: tuple[str, ...] = ()) -> Level2Document:
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
            "данные агрегатора", outcome, Decimal(110), Decimal(100), None, "385", "385",
            Decimal(100), debt_like,
        ),  # fmt: skip
        buckets=(
            PrintedBucket("до 1 года", Decimal(20), code="ifrs.debt_cf_due_m000_m012"),
            PrintedBucket(
                "от 1 до 5 лет", Decimal(100), as_printed=True, code="ifrs.debt_cf_due_m012_m060"
            ),
        ),
        buckets_with_debt_like=(
            PrintedBucket(
                "до 1 года", Decimal(25),
                code="ifrs.debt_cf_due_m000_m012 + ifrs.debt_like_cf_due_m000_m012",
            ),
        ),  # fmt: skip
        carrying_codes=("ifrs.debt_due_m000_plus", "ifrs.debt_like_due_m000_plus"),
    )


def test_a_missing_section_is_refused() -> None:
    """Раздел, не объявленный составом, исчез бы молча."""
    with pytest.raises(ValueError, match="разделы"):
        _composition(CODES[:-1])


def test_failed_reconciliation_prints_no_maturities() -> None:
    """Не сошлось — величины сроков не приводятся, и документ так и говорит."""
    part = Part("debt", "Долг")
    _debt(part, _document(Check.FAILED), _composition())
    assert "Не сошлось с данные агрегатора: 110 / 100 млн руб." in part.paragraphs
    assert all("cf_due" not in line.code for line in part.lines)


def test_passed_reconciliation_prints_flows_with_storage_codes() -> None:
    """Сошлось — потоки печатаются с кодом хранения, графа как напечатана названа."""
    part = Part("debt", "Долг")
    _debt(part, _document(Check.PASSED), _composition())
    codes = [line.code for line in part.lines]
    assert "ifrs.debt_cf_due_m000_m012" in codes and "ifrs.debt_cf_due_m012_m060" in codes
    assert "ifrs.debt_due_m000_plus" in codes
    assert "Графа «от 1 до 5 лет» как напечатана." in part.paragraphs


def test_without_carrying_flows_are_printed_with_a_caveat() -> None:
    """ЛСР: графы балансовой нет — сверки нет, потоки как напечатаны, без отказа."""
    part = Part("debt", "Долг")
    _debt(part, _document(Check.NO_CARRYING), _composition())
    assert "Графы нет (данные агрегатора); потоки как напечатаны." in part.paragraphs
    assert "ifrs.debt_cf_due_m000_m012" in [line.code for line in part.lines]


def test_debt_like_is_printed_as_a_second_value() -> None:
    """Автодор: две величины с перечнем добавленного; сумма без сверки потоками нет."""
    named = ("Концессионные соглашения",)
    part = Part("debt", "Долг")
    _debt(part, _document(Check.LOANS_ONLY, named), _composition())
    names = [line.name for line in part.lines]
    assert "Займы и долгоподобные (Концессионные соглашения), млн руб." in names
    assert "С долгоподобными (Концессионные соглашения) не приводятся." in part.paragraphs
    assert "ifrs.debt_cf_due_m000_m012" in [line.code for line in part.lines]
    assert all("debt_like_cf_due" not in line.code for line in part.lines)

    passed = Part("debt", "Долг")
    _debt(passed, _document(Check.PASSED, named), _composition())
    assert any("debt_like_cf_due" in line.code for line in passed.lines)


def test_debt_like_reconciled_with_the_document_balance_is_printed() -> None:
    """Решение 30.09.2026: у карантина сумма с долгоподобными сверяется
    с балансом документа; сошлось — вторая величина и её потоки печатаются."""
    from dataclasses import replace

    named = ("Концессионные соглашения",)
    extra = Reconciliation(
        "опора — баланс документа, комплект в карантине", Check.PASSED,
        Decimal(110), Decimal(110), None, "385", "385", Decimal(100), named,
    )  # fmt: skip
    document = replace(_document(Check.LOANS_ONLY, named), debt_like_check=extra)
    part = Part("debt", "Долг")
    _debt(part, document, _composition())
    assert (
        "Сошлось с опора — баланс документа, комплект в карантине: 110 / 110 млн руб."
        in part.paragraphs
    )
    assert any("debt_like_cf_due" in line.code for line in part.lines)
    # Без сверки с документом — как прежде: потоков суммы нет.
    failed = replace(extra, outcome=Check.NO_REFERENCE)
    part = Part("debt", "Долг")
    _debt(part, replace(document, debt_like_check=failed), _composition())
    assert all("debt_like_cf_due" not in line.code for line in part.lines)


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
