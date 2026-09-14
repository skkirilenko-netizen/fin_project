"""Тесты загрузки в fact_report. Идут в findb, транзакция откатывается всегда."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from probes import CORRECTED_BFO, FULL_BFO, SIMPLIFIED_BFO, read_probe

from finlib.db import execute, fetch_all, fetch_one
from finlib.normalize.lines import load_lines
from finlib.normalize.loader import PERIOD_RANK, build_facts, load_report_set, period_role
from finlib.sources.girbo import Organization, ReportSet, parse_report_sets
from finlib.utils import json_loads_decimal

FULL_INN = "7736050003"
SIMPLIFIED_INN = "2100010824"
CORRECTED_INN = "2522002003"


@pytest.fixture(autouse=True)
def clean_test_organizations(db_conn):
    """Убирает следы прежних прогонов по тестовым ИНН внутри той же транзакции.

    База findb рабочая, в ней может лежать реально загруженная отчётность тех
    же организаций. Удаление делается в транзакции теста и откатывается вместе
    с ней, поэтому настоящие данные не страдают.
    """
    execute(
        "DELETE FROM organization WHERE inn = ANY(%(inns)s)",
        {"inns": [FULL_INN, SIMPLIFIED_INN, CORRECTED_INN]},
        conn=db_conn,
    )
    execute(
        "DELETE FROM dq_log WHERE inn = ANY(%(inns)s)",
        {"inns": [FULL_INN, SIMPLIFIED_INN, CORRECTED_INN]},
        conn=db_conn,
    )
    return db_conn


def sets_from(probe, inn: str) -> list[ReportSet]:
    """Комплекты из сохранённой пробы."""
    return parse_report_sets(json_loads_decimal(read_probe(probe)), inn)


def org(inn: str) -> Organization:
    """Минимальные реквизиты организации."""
    return Organization(inn=inn, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ ПОЛНОЕ")


def by_year(sets: list[ReportSet], year: int) -> ReportSet:
    """Комплект за нужный год."""
    return next(s for s in sets if s.report_year == year)


def facts(conn, inn: str, **where: Any) -> list[dict[str, Any]]:
    """Строки fact_report по условиям."""
    sql = "SELECT * FROM fact_report WHERE inn = %(inn)s"
    params: dict[str, Any] = {"inn": inn}
    for key, value in where.items():
        sql += f" AND {key} = %({key})s"
        params[key] = value
    return fetch_all(sql + " ORDER BY report_date DESC, form_code, line_code", params, conn=conn)


def dq(conn, inn: str, check_code: str) -> list[dict[str, Any]]:
    """Записи журнала качества по коду контроля."""
    return fetch_all(
        "SELECT * FROM dq_log WHERE inn = %(inn)s AND check_code = %(code)s ORDER BY id",
        {"inn": inn, "code": check_code},
        conn=conn,
    )


# --- согласованность с БД ---------------------------------------------------


def test_period_rank_matches_database(db_conn) -> None:
    """Приоритет периода в коде и в схеме — одно и то же."""
    for role, rank in PERIOD_RANK.items():
        row = fetch_one("SELECT period_rank(%(r)s) AS rank", {"r": role}, conn=db_conn)
        assert row is not None and row["rank"] == rank


def test_period_role_from_date() -> None:
    """Роль периода выводится из отчётного года комплекта."""
    assert period_role(2025, date(2025, 12, 31)) == "current"
    assert period_role(2025, date(2024, 12, 31)) == "previous"
    assert period_role(2025, date(2023, 12, 31)) == "before_previous"
    with pytest.raises(ValueError, match="не относится к комплекту"):
        period_role(2025, date(2019, 12, 31))


# --- загрузка ---------------------------------------------------------------


def test_load_writes_facts(db_conn) -> None:
    """Комплект загружается, значения и статусы попадают в fact_report."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    result = load_report_set(report, org(FULL_INN), db_conn)

    assert result.src_file_id is not None
    assert result.facts_written == result.facts_total > 0
    rows = facts(db_conn, FULL_INN, report_date=date(2025, 12, 31), form_code="0710001")
    values = {row["line_code"]: row for row in rows}
    assert values["1600"]["value"] == Decimal("25736328136")
    assert values["1600"]["value_status"] == "ok"
    assert values["1600"]["period_role"] == "current"


def test_not_disclosed_is_null_not_zero(db_conn) -> None:
    """Нераскрытая строка пишется как NULL со статусом not_disclosed."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    row = fetch_one(
        "SELECT value, value_status FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1120'",
        {"inn": FULL_INN, "d": date(2025, 12, 31)},
        conn=db_conn,
    )
    assert row is not None
    assert row["value"] is None
    assert row["value_status"] == "not_disclosed"


def test_three_periods_from_one_set(db_conn) -> None:
    """Один комплект даёт три периода по балансу и два по остальным формам."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    balance_dates = {
        row["report_date"] for row in facts(db_conn, FULL_INN, form_code="0710001")
    }
    profit_dates = {row["report_date"] for row in facts(db_conn, FULL_INN, form_code="0710002")}
    assert balance_dates == {date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)}
    assert profit_dates == {date(2025, 12, 31), date(2024, 12, 31)}


def test_source_line_code_is_always_filled(db_conn) -> None:
    """source_line_code заполнен всегда: для полных форм равен line_code."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    rows = facts(db_conn, FULL_INN)
    assert rows
    assert all(row["source_line_code"] == row["line_code"] for row in rows)


def test_simplified_keeps_source_code(db_conn) -> None:
    """У упрощённой формы канонический и исходный коды различаются."""
    report = by_year(sets_from(SIMPLIFIED_BFO, SIMPLIFIED_INN), 2024)
    load_report_set(report, org(SIMPLIFIED_INN), db_conn)
    row = fetch_one(
        "SELECT line_code, source_line_code, value FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND source_line_code = '1230'",
        {"inn": SIMPLIFIED_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert row is not None
    assert row["line_code"] == "1240"
    assert row["source_line_code"] == "1230"


# --- идемпотентность --------------------------------------------------------


def test_second_load_creates_no_duplicates(db_conn) -> None:
    """Повторная загрузка того же комплекта не увеличивает число строк."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    before = len(facts(db_conn, FULL_INN))

    second = load_report_set(report, org(FULL_INN), db_conn)
    after = len(facts(db_conn, FULL_INN))

    assert after == before
    assert second.facts_written == 0
    assert second.facts_unchanged == second.facts_total


def test_second_load_writes_nothing_to_journal(db_conn) -> None:
    """Повторная загрузка без изменений не засоряет журнал и не трогает updated_at."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    stamps = {row["id"]: row["updated_at"] for row in facts(db_conn, FULL_INN)}

    load_report_set(report, org(FULL_INN), db_conn)

    assert dq(db_conn, FULL_INN, "fact_overwrite") == []
    assert {row["id"]: row["updated_at"] for row in facts(db_conn, FULL_INN)} == stamps


def test_changed_value_is_logged_with_previous(db_conn) -> None:
    """Изменившееся значение перезаписывается, прежнее уходит в dq_log."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    original = report.forms["0710001"].values[date(2025, 12, 31)]["1600"]

    report.forms["0710001"].values[date(2025, 12, 31)]["1600"] = original + 1000
    result = load_report_set(report, org(FULL_INN), db_conn)

    assert result.overwritten == 1
    records = dq(db_conn, FULL_INN, "fact_overwrite")
    assert len(records) == 1
    assert records[0]["previous_value"] == original
    assert records[0]["new_value"] == original + 1000
    assert records[0]["line_code"] == "1600"
    assert records[0]["status"] == "info"


def test_status_change_counts_as_overwrite(db_conn) -> None:
    """Переход раскрытого значения в нераскрытое — событие, а не отсутствие события."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)

    report.forms["0710001"].values[date(2025, 12, 31)]["1600"] = None
    load_report_set(report, org(FULL_INN), db_conn)

    row = fetch_one(
        "SELECT value, value_status FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1600'",
        {"inn": FULL_INN, "d": date(2025, 12, 31)},
        conn=db_conn,
    )
    assert row is not None and row["value"] is None and row["value_status"] == "not_disclosed"
    assert len(dq(db_conn, FULL_INN, "fact_overwrite")) == 1


# --- приоритет периодов -----------------------------------------------------


def test_comparative_does_not_overwrite_current(db_conn) -> None:
    """Сравнительный период не затирает значение, пришедшее как отчётное."""
    sets = sets_from(FULL_BFO, FULL_INN)
    older = by_year(sets, 2024)
    newer = by_year(sets, 2025)

    load_report_set(older, org(FULL_INN), db_conn)
    authoritative = fetch_one(
        "SELECT value, period_role FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1600'",
        {"inn": FULL_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert authoritative is not None and authoritative["period_role"] == "current"

    # В комплекте 2025 года тот же период приходит сравнительным и с другим значением.
    newer.forms["0710001"].values[date(2024, 12, 31)]["1600"] = Decimal("1")
    result = load_report_set(newer, org(FULL_INN), db_conn)

    kept = fetch_one(
        "SELECT value, period_role FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1600'",
        {"inn": FULL_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert kept is not None
    assert kept["value"] == authoritative["value"], "сравнительное значение затёрло отчётное"
    assert kept["period_role"] == "current"
    assert result.facts_kept_by_priority > 0


def test_period_mismatch_is_logged(db_conn) -> None:
    """Расхождение сравнительного и отчётного значения фиксируется отдельным контролем."""
    sets = sets_from(FULL_BFO, FULL_INN)
    older, newer = by_year(sets, 2024), by_year(sets, 2025)
    load_report_set(older, org(FULL_INN), db_conn)

    original = newer.forms["0710001"].values[date(2024, 12, 31)]["1600"]
    newer.forms["0710001"].values[date(2024, 12, 31)]["1600"] = Decimal("1")
    load_report_set(newer, org(FULL_INN), db_conn)

    records = dq(db_conn, FULL_INN, "period_value_mismatch")
    assert records, "расхождение периодов не зафиксировано"
    record = next(r for r in records if r["line_code"] == "1600")
    assert record["previous_value"] == original
    assert record["new_value"] == Decimal("1")
    assert record["severity"] == "warning"
    assert record["details"]["kept_period_role"] == "current"
    assert record["details"]["rejected_period_role"] == "previous"


def test_load_order_does_not_change_result(db_conn) -> None:
    """Результат не зависит от порядка загрузки комплектов."""
    sets = sets_from(FULL_BFO, FULL_INN)
    older, newer = by_year(sets, 2024), by_year(sets, 2025)

    load_report_set(newer, org(FULL_INN), db_conn)
    load_report_set(older, org(FULL_INN), db_conn)
    forward = {
        (r["report_date"], r["form_code"], r["line_code"]): (r["value"], r["period_role"])
        for r in facts(db_conn, FULL_INN)
    }

    db_conn.rollback()

    load_report_set(older, org(FULL_INN), db_conn)
    load_report_set(newer, org(FULL_INN), db_conn)
    backward = {
        (r["report_date"], r["form_code"], r["line_code"]): (r["value"], r["period_role"])
        for r in facts(db_conn, FULL_INN)
    }

    assert forward == backward


def test_current_overwrites_comparative(db_conn) -> None:
    """Отчётное значение старше: оно замещает ранее загруженное сравнительное."""
    sets = sets_from(FULL_BFO, FULL_INN)
    newer, older = by_year(sets, 2025), by_year(sets, 2024)

    load_report_set(newer, org(FULL_INN), db_conn)
    before = fetch_one(
        "SELECT period_role FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1600'",
        {"inn": FULL_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert before is not None and before["period_role"] == "previous"

    load_report_set(older, org(FULL_INN), db_conn)
    after = fetch_one(
        "SELECT period_role FROM fact_report "
        "WHERE inn = %(inn)s AND report_date = %(d)s AND line_code = '1600'",
        {"inn": FULL_INN, "d": date(2024, 12, 31)},
        conn=db_conn,
    )
    assert after is not None and after["period_role"] == "current"


# --- корректировки ----------------------------------------------------------


def test_correction_supersedes_previous_version(db_conn) -> None:
    """Загрузка корректировки снимает признак актуальности с прежней версии."""
    report = by_year(sets_from(CORRECTED_BFO, CORRECTED_INN), 2024)

    stale = ReportSet(
        inn=report.inn,
        girbo_bfo_id=report.girbo_bfo_id,
        report_year=report.report_year,
        report_date=report.report_date,
        knd=report.knd,
        reporting_type=report.reporting_type,
        correction_version=0,
        is_actual=True,
        forms=report.forms,
    )
    load_report_set(stale, org(CORRECTED_INN), db_conn)
    load_report_set(report, org(CORRECTED_INN), db_conn)

    rows = fetch_all(
        "SELECT correction_version, is_actual FROM src_file "
        "WHERE inn = %(inn)s AND report_year = 2024 ORDER BY correction_version",
        {"inn": CORRECTED_INN},
        conn=db_conn,
    )
    assert [(r["correction_version"], r["is_actual"]) for r in rows] == [(0, False), (1, True)]


def test_both_versions_are_kept_in_src_file(db_conn) -> None:
    """Обе версии остаются в src_file: ключ различает их номером корректировки."""
    report = by_year(sets_from(CORRECTED_BFO, CORRECTED_INN), 2024)
    stale = ReportSet(
        inn=report.inn,
        girbo_bfo_id=report.girbo_bfo_id,
        report_year=report.report_year,
        report_date=report.report_date,
        knd=report.knd,
        reporting_type=report.reporting_type,
        correction_version=0,
        is_actual=True,
        forms=report.forms,
    )
    load_report_set(stale, org(CORRECTED_INN), db_conn)
    load_report_set(report, org(CORRECTED_INN), db_conn)

    row = fetch_one(
        "SELECT count(*) AS n FROM src_file WHERE inn = %(inn)s AND report_year = 2024",
        {"inn": CORRECTED_INN},
        conn=db_conn,
    )
    assert row is not None and row["n"] == 2


# --- коды, не попавшие в расчёт ---------------------------------------------


def test_unknown_codes_are_logged(db_conn) -> None:
    """Коды вне справочника логируются и попадают в отчёт о загрузке."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    result = load_report_set(report, org(FULL_INN), db_conn)

    assert result.unknown_codes, "неизвестные коды не обнаружены"
    logged = {row["line_code"] for row in dq(db_conn, FULL_INN, "unknown_line_code")}
    assert "1105" in logged
    assert "13101" in logged
    assert result.has_warnings


def test_unknown_codes_are_not_written_as_facts(db_conn) -> None:
    """Неизвестный код в fact_report не попадает."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    codes = {row["line_code"] for row in facts(db_conn, FULL_INN)}
    assert "1105" not in codes
    assert "13101" not in codes


def test_build_facts_maps_every_known_code() -> None:
    """Все известные коды формы превращаются в факты по каждому периоду."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    built = build_facts(report, load_lines())
    assert built.facts
    assert not built.ambiguous
    assert not built.conflicts
    assert built.unknown
    roles = {fact.period_role for fact in built.facts}
    assert roles == {"current", "previous", "before_previous"}


def test_one_aggregated_line_is_filled_by_one_code(db_conn) -> None:
    """На укрупнённую строку претендует несколько кодов; побеждает раскрывший значение."""
    report = by_year(sets_from(SIMPLIFIED_BFO, SIMPLIFIED_INN), 2024)
    values = report.forms["0710001"].values[date(2024, 12, 31)]
    # 1220, 1230, 1240 и 1260 — все «Финансовые и другие оборотные активы».
    assert values["1230"] is not None
    assert values["1220"] is None and values["1240"] is None

    load_report_set(report, org(SIMPLIFIED_INN), db_conn)
    rows = facts(db_conn, SIMPLIFIED_INN, report_date=date(2024, 12, 31), line_code="1240")
    assert len(rows) == 1, "укрупнённая строка задвоилась"
    assert rows[0]["value"] == Decimal("202")
    assert rows[0]["source_line_code"] == "1230"


def test_conflicting_codes_are_not_guessed(db_conn) -> None:
    """Если значение раскрыли сразу два кода одной строки, строка не грузится."""
    report = by_year(sets_from(SIMPLIFIED_BFO, SIMPLIFIED_INN), 2024)
    values = report.forms["0710001"].values[date(2024, 12, 31)]
    values["1220"] = Decimal("50")  # раскрыт второй код той же укрупнённой строки

    result = load_report_set(report, org(SIMPLIFIED_INN), db_conn)

    assert result.line_conflicts >= 1
    assert facts(db_conn, SIMPLIFIED_INN, report_date=date(2024, 12, 31), line_code="1240") == []
    records = [
        r for r in dq(db_conn, SIMPLIFIED_INN, "ambiguous_line_code") if r["line_code"] == "1240"
    ]
    assert records
    assert set(records[0]["details"]["source_codes"]) == {"1220", "1230"}
    assert records[0]["severity"] == "warning"


def test_ambiguous_source_code_is_logged(db_conn) -> None:
    """Код 1190 упрощённой формы не разносится наугад и попадает в журнал."""
    report = by_year(sets_from(SIMPLIFIED_BFO, SIMPLIFIED_INN), 2024)
    result = load_report_set(report, org(SIMPLIFIED_INN), db_conn)

    assert "1190" in result.ambiguous_codes.get("0710001", ())
    logged = {row["line_code"] for row in dq(db_conn, SIMPLIFIED_INN, "ambiguous_line_code")}
    assert "1190" in logged


# --- целостность транзакции -------------------------------------------------


def test_everything_rolls_back_together(db_conn) -> None:
    """Откат снимает и организацию, и комплект, и факты — частичного состояния нет."""
    report = by_year(sets_from(FULL_BFO, FULL_INN), 2025)
    load_report_set(report, org(FULL_INN), db_conn)
    assert facts(db_conn, FULL_INN)

    db_conn.rollback()

    assert facts(db_conn, FULL_INN) == []
    assert (
        fetch_one(
            "SELECT count(*) AS n FROM src_file WHERE inn = %(inn)s",
            {"inn": FULL_INN},
            conn=db_conn,
        )["n"]
        == 0
    )
    assert (
        fetch_one(
            "SELECT count(*) AS n FROM organization WHERE inn = %(inn)s",
            {"inn": FULL_INN},
            conn=db_conn,
        )["n"]
        == 0
    )
