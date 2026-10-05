"""Наименование в выборке маршрута: полное, краткое, затем ИНН."""

from datetime import date

import pytest

from finlib.db import fetch_all, fetch_one
from finlib.scoring import routing_store, rsbu_routing


@pytest.mark.parametrize("standard", ["ifrs", "rsbu"])
@pytest.mark.parametrize(
    ("full", "short", "expected"),
    [("Полное", "Краткое", "Полное"), ("  ", " Краткое ", "Краткое"),
     (None, "Краткое", "Краткое"), ("", "", "0000000097")],
)
def test_route_name_falls_back_without_updating_organization(
    db_conn, standard: str, full: str | None, short: str, expected: str,
) -> None:
    """Оба стандарта читают краткое имя при пустом полном и не меняют запись."""
    inn = "0000000097"
    params = {"inn": inn, "full": full, "short": short, "standard": standard}
    db_conn.cursor().execute("DELETE FROM organization WHERE inn = %(inn)s", params)
    db_conn.cursor().execute(
        "INSERT INTO organization (inn, name, short_name) VALUES (%(inn)s, %(full)s, %(short)s)",
        params,
    )
    source = fetch_one(
        "INSERT INTO src_file (inn, standard, report_year, period_end, source, status) "
        "VALUES (%(inn)s, %(standard)s, 2025, '2025-12-31', 'cbonds', 'loaded') RETURNING id",
        params, conn=db_conn,
    )
    assert source is not None
    db_conn.cursor().execute(
        "INSERT INTO fact_report (src_file_id, inn, standard, report_date, form_code, "
        "line_code, source_line_code, value, period_role) "
        "VALUES (%(source)s, %(inn)s, %(standard)s, '2025-12-31', 'balance', '1600', "
        "'1600', 1, 'current')",
        {**params, "source": source["id"]},
    )
    query = routing_store._LATEST if standard == "ifrs" else rsbu_routing._LATEST
    rows = fetch_all(query, {"as_of": None, "annual": 120, "interim": 60}, conn=db_conn)
    own = next(row for row in rows if row["inn"] == inn)
    assert own["report_date"] == date(2025, 12, 31)
    assert own["name"] == expected
    stored = fetch_one("SELECT name, short_name FROM organization WHERE inn = %(inn)s",
                       params, conn=db_conn)
    assert stored == {"name": full, "short_name": short}
