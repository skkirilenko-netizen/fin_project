"""Тесты прогона контролей: карантин, повторный прогон, реальные пробы."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from probes import CORRECTED_BFO, FULL_BFO, SIMPLIFIED_BFO, read_probe

from finlib.db import execute, fetch_all, fetch_one
from finlib.normalize.loader import load_report_set
from finlib.quality.runner import quarantined_src_files, run_checks
from finlib.sources.girbo import Organization, parse_report_sets
from finlib.utils import json_loads_decimal

FULL_INN = "7736050003"
SIMPLIFIED_INN = "2100010824"
CORRECTED_INN = "2522002003"
ALL_INNS = [FULL_INN, SIMPLIFIED_INN, CORRECTED_INN]


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute("DELETE FROM organization WHERE inn = ANY(%(i)s)", {"i": ALL_INNS}, conn=db_conn)
    execute("DELETE FROM dq_log WHERE inn = ANY(%(i)s)", {"i": ALL_INNS}, conn=db_conn)
    return db_conn


def load(probe, inn: str, year: int, conn) -> int:
    """Загружает один комплект и возвращает его идентификатор."""
    sets = parse_report_sets(json_loads_decimal(read_probe(probe)), inn)
    report = next(item for item in sets if item.report_year == year)
    org = Organization(inn=inn, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ")
    src_file_id = load_report_set(report, org, conn).src_file_id
    assert src_file_id is not None
    return src_file_id


def load_all(probe, inn: str, conn) -> list[int]:
    """Загружает все комплекты пробы от старых к новым."""
    sets = parse_report_sets(json_loads_decimal(read_probe(probe)), inn)
    org = Organization(inn=inn, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ")
    ids = []
    for report in sorted(sets, key=lambda item: item.report_year):
        result = load_report_set(report, org, conn)
        assert result.src_file_id is not None
        ids.append(result.src_file_id)
    return ids


def dq(conn, inn: str, check_code: str) -> list[dict[str, Any]]:
    """Записи журнала по коду контроля."""
    return fetch_all(
        "SELECT * FROM dq_log WHERE inn = %(inn)s AND check_code = %(c)s ORDER BY id",
        {"inn": inn, "c": check_code},
        conn=conn,
    )


def status_of(conn, src_file_id: int) -> dict[str, Any]:
    """Статус комплекта."""
    row = fetch_one(
        "SELECT status, quarantine_reason FROM src_file WHERE id = %(id)s",
        {"id": src_file_id},
        conn=db_conn_of(conn),
    )
    assert row is not None
    return row


def db_conn_of(conn):
    """Возвращает соединение как есть; вынесено для читаемости вызовов."""
    return conn


# --- реальные пробы ---------------------------------------------------------


def test_full_reporting_passes(db_conn) -> None:
    """Отчётность Газпрома проходит контроли: карантина нет."""
    for src_file_id in load_all(FULL_BFO, FULL_INN, db_conn):
        report = run_checks(src_file_id, db_conn)
        assert not report.quarantined, report.summary()
        assert not report.blocking_failures


def test_simplified_reporting_is_not_quarantined(db_conn) -> None:
    """Малое предприятие не уезжает в карантин из-за нашего отказа угадывать код."""
    for src_file_id in load_all(SIMPLIFIED_BFO, SIMPLIFIED_INN, db_conn):
        report = run_checks(src_file_id, db_conn)
        assert not report.quarantined, report.summary()

    # Раздел I проверить нельзя: код 1190 не привязан к строке.
    unverifiable = [
        item for item in run_checks(
            load(SIMPLIFIED_BFO, SIMPLIFIED_INN, 2024, db_conn), db_conn
        ).not_verifiable
    ]
    assert unverifiable
    assert any("1190" in item.message for item in unverifiable)


def test_real_broken_reporting_goes_to_quarantine(db_conn) -> None:
    """Отчётность с несходящимся балансом отправляется в карантин."""
    src_file_id = load(CORRECTED_BFO, CORRECTED_INN, 2025, db_conn)
    report = run_checks(src_file_id, db_conn)

    assert report.quarantined
    assert "balance_equality" in (report.quarantine_reason or "")
    assert status_of(db_conn, src_file_id)["status"] == "quarantine"
    assert src_file_id in quarantined_src_files(CORRECTED_INN, db_conn)


def test_comparative_failure_does_not_quarantine(db_conn) -> None:
    """Расхождение в сравнительной колонке видно, но расчёт не останавливает."""
    src_file_id = load(CORRECTED_BFO, CORRECTED_INN, 2021, db_conn)
    report = run_checks(src_file_id, db_conn)

    failures = [item for item in report.outcomes if item.status.value == "fail"]
    assert failures, "расхождение в сравнительной колонке не обнаружено"
    assert all(item.report_date != date(2021, 12, 31) for item in failures)
    assert not report.quarantined
    assert status_of(db_conn, src_file_id)["status"] == "loaded"


# --- карантин ---------------------------------------------------------------


def test_quarantine_is_lifted_when_checks_pass(db_conn) -> None:
    """Повторный прогон по исправным данным снимает ранее поставленный карантин."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    execute(
        "UPDATE src_file SET status = 'quarantine', quarantine_reason = 'прежний прогон' "
        "WHERE id = %(id)s",
        {"id": src_file_id},
        conn=db_conn,
    )

    report = run_checks(src_file_id, db_conn)

    assert not report.quarantined, report.summary()
    assert status_of(db_conn, src_file_id)["status"] == "loaded"
    assert status_of(db_conn, src_file_id)["quarantine_reason"] is None


def test_rerun_does_not_accumulate_results(db_conn) -> None:
    """Повторный прогон переписывает результаты контролей, а не копит их."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    first = run_checks(src_file_id, db_conn)
    count_first = len(fetch_all(
        "SELECT id FROM dq_log WHERE src_file_id = %(id)s AND check_code = 'section_sum'",
        {"id": src_file_id},
        conn=db_conn,
    ))

    second = run_checks(src_file_id, db_conn)
    count_second = len(fetch_all(
        "SELECT id FROM dq_log WHERE src_file_id = %(id)s AND check_code = 'section_sum'",
        {"id": src_file_id},
        conn=db_conn,
    ))

    assert count_first == count_second > 0
    assert first.counts == second.counts


def test_rerun_keeps_loader_history(db_conn) -> None:
    """Прогон контролей не стирает записи загрузчика: они история, а не снимок."""
    src_file_id = load(SIMPLIFIED_BFO, SIMPLIFIED_INN, 2024, db_conn)
    before = len(dq(db_conn, SIMPLIFIED_INN, "ambiguous_line_code"))
    assert before > 0

    run_checks(src_file_id, db_conn)
    run_checks(src_file_id, db_conn)

    assert len(dq(db_conn, SIMPLIFIED_INN, "ambiguous_line_code")) == before


def test_results_are_written_to_journal(db_conn) -> None:
    """Результаты контролей попадают в dq_log с уровнями из методики."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    run_checks(src_file_id, db_conn)

    rows = fetch_all(
        "SELECT check_code, status, severity, report_date FROM dq_log "
        "WHERE src_file_id = %(id)s ORDER BY check_code, report_date",
        {"id": src_file_id},
        conn=db_conn,
    )
    by_code = {row["check_code"] for row in rows}
    assert {"balance_equality", "section_sum", "profit_chain", "mandatory_fields"} <= by_code

    # Уровень контроля зависит от периода: блокирующим он остаётся только
    # за отчётный период комплекта, за сравнительные понижается. Строк
    # поэтому несколько, и брать первую из неупорядоченной выборки нельзя —
    # порядок в PostgreSQL не гарантирован, и тест мигал.
    passed = [
        row
        for row in rows
        if row["check_code"] == "balance_equality" and row["status"] == "pass"
    ]
    assert passed, "контроль равенства баланса не выполнялся"
    severities = {row["severity"] for row in passed}
    assert "blocking" in severities, "за отчётный период контроль обязан быть блокирующим"


def test_quarantine_blocks_calculation_list(db_conn) -> None:
    """Комплекты в карантине перечислимы: расчёт обязан их отбросить."""
    good = load(FULL_BFO, FULL_INN, 2025, db_conn)
    bad = load(CORRECTED_BFO, CORRECTED_INN, 2025, db_conn)
    run_checks(good, db_conn)
    run_checks(bad, db_conn)

    assert quarantined_src_files(FULL_INN, db_conn) == set()
    assert bad in quarantined_src_files(CORRECTED_INN, db_conn)


def test_unit_mismatch_is_reported(db_conn, caplog) -> None:
    """Иная единица измерения обесценивает пороги — об этом предупреждают."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    execute(
        "UPDATE src_file SET unit_code = '385' WHERE id = %(id)s",
        {"id": src_file_id},
        conn=db_conn,
    )
    with caplog.at_level("WARNING"):
        run_checks(src_file_id, db_conn)
    assert any("единица измерения" in record.message for record in caplog.records)


def test_summary_is_readable(db_conn) -> None:
    """Сводка контролей пригодна для вывода в CLI."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    summary = run_checks(src_file_id, db_conn).summary()
    assert FULL_INN in summary
    assert "расчёт разрешён" in summary


def test_decimal_preserved_in_details(db_conn) -> None:
    """Числа в журнале не превращаются в float."""
    src_file_id = load(FULL_BFO, FULL_INN, 2025, db_conn)
    run_checks(src_file_id, db_conn)
    row = fetch_one(
        "SELECT details FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'balance_equality' AND status = 'pass' LIMIT 1",
        {"id": src_file_id},
        conn=db_conn,
    )
    assert row is not None
    assert Decimal(row["details"]["1600"]) == Decimal("25736328136")
