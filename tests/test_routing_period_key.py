"""Выборки маршрута по комплекту берут отчётную дату, а не год.

С тех пор как в ключ комплекта вошёл период, за год у эмитента бывает четыре
комплекта. Выборка по году смешивала их: 25.09.2026 АвтоМоё Опт ушла
в «Разбор» по провалу проверки нуля у двух квартальных комплектов, тогда как
годовой, по которому построен маршрут, проверку прошёл.
"""

from datetime import date

from finlib.db import execute, fetch_one
from finlib.scoring.routing_store import sources_of, zero_failed
from finlib.standards import Standard

INN = "0000000019"
ANNUAL = date(2025, 12, 31)
QUARTER = date(2025, 9, 30)
HALF = date(2025, 6, 30)


def _set(conn, period_end: date, status: str, unit: str) -> int:  # noqa: ANN001
    """Комплект агрегатора РСБУ на отчётную дату."""
    execute(
        "INSERT INTO organization (inn, name) VALUES (%(i)s, 'ТЕСТ') "
        "ON CONFLICT DO NOTHING",
        {"i": INN},
        conn=conn,
    )
    execute(
        "INSERT INTO src_file (inn, standard, report_year, period_end, source, "
        "reporting_type, unit_code, unit_source, status) VALUES (%(i)s, 'rsbu', "
        "%(y)s, %(p)s, 'cbonds', 'full', %(u)s, 'form_standard', %(s)s)",
        {"i": INN, "y": period_end.year, "p": period_end, "u": unit, "s": status},
        conn=conn,
    )
    return fetch_one(
        "SELECT id FROM src_file WHERE inn = %(i)s AND period_end = %(p)s",
        {"i": INN, "p": period_end},
        conn=conn,
    )["id"]


def test_an_interim_failure_is_not_carried_to_the_annual_set(db_conn) -> None:
    """Провал промежуточного комплекта не переносится на годовой."""
    _set(db_conn, ANNUAL, "loaded", "384")
    quarter = _set(db_conn, QUARTER, "quarantine", "384")
    execute(
        "INSERT INTO dq_log (src_file_id, inn, report_date, check_code, status, "
        "severity, message) VALUES (%(f)s, %(i)s, %(d)s, "
        "'cbonds_identity_mismatch', 'fail', 'blocking', 'ln1600 против ln1700')",
        {"f": quarter, "i": INN, "d": QUARTER},
        conn=db_conn,
    )
    failed = zero_failed(db_conn)
    assert (INN, "rsbu", QUARTER) in failed
    assert (INN, "rsbu", ANNUAL) not in failed


def test_the_unit_is_taken_from_the_set_of_the_route_date(db_conn) -> None:
    """Единица и вид берутся у комплекта той даты, по которой построен маршрут."""
    _set(db_conn, ANNUAL, "loaded", "384")
    _set(db_conn, HALF, "loaded", "385")
    found = sources_of(INN, Standard.RSBU, ANNUAL, db_conn)
    assert [row["unit_code"] for row in found] == ["384"]
