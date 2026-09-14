"""Тесты разделения стандартов отчётности. Ветка МСФО не реализована.

Проверяется только одно: модель данных не даёт смешать РСБУ и МСФО.
Строки по разным стандартам не затирают друг друга, а ряд показателя
строится в пределах одного стандарта.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.db import execute, fetch_all, fetch_one
from finlib.metrics.store import load_series
from finlib.standards import Standard

INN = "7736050003"
PERIOD = date(2025, 12, 31)


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute("DELETE FROM organization WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute("DELETE FROM metric_value WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute(
        "INSERT INTO organization (inn, name) VALUES (%(i)s, 'ТЕСТ')", {"i": INN}, conn=db_conn
    )
    return db_conn


def make_src_file(conn, standard: Standard, year: int = 2025) -> int:
    """Создаёт комплект заданного стандарта."""
    row = fetch_one(
        "INSERT INTO src_file (inn, standard, report_year, source) "
        "VALUES (%(i)s, %(s)s, %(y)s, 'file') RETURNING id",
        {"i": INN, "s": standard.value, "y": year},
        conn=conn,
    )
    assert row is not None
    return row["id"]


def add_fact(conn, src_file_id: int, standard: Standard, value: Decimal) -> None:
    """Добавляет факт по строке 1600 заданного стандарта."""
    execute(
        "INSERT INTO fact_report (src_file_id, inn, standard, report_date, form_code, "
        "line_code, source_line_code, value, value_status, period_role) "
        "VALUES (%(f)s, %(i)s, %(s)s, %(d)s, '0710001', '1600', '1600', %(v)s, 'ok', 'current')",
        {"f": src_file_id, "i": INN, "s": standard.value, "d": PERIOD, "v": value},
        conn=conn,
    )


def add_metric(conn, standard: Standard, value: Decimal) -> None:
    """Добавляет значение показателя заданного стандарта."""
    execute(
        "INSERT INTO metric_value (inn, standard, report_date, metric_code, value, status, "
        "confidence, methodology_version) "
        "VALUES (%(i)s, %(s)s, %(d)s, 'cur_liq', %(v)s, 'ok', 'verified', '1.0.0')",
        {"i": INN, "s": standard.value, "d": PERIOD, "v": value},
        conn=conn,
    )


# --- схема ------------------------------------------------------------------


def test_standard_defaults_to_rsbu(db_conn) -> None:
    """Умолчание — РСБУ: существующая ветка работает без изменений."""
    row = fetch_one(
        "INSERT INTO src_file (inn, report_year, source) VALUES (%(i)s, 2024, 'file') "
        "RETURNING standard",
        {"i": INN},
        conn=db_conn,
    )
    assert row is not None
    assert row["standard"] == "rsbu"


def test_ifrs_is_accepted_by_check(db_conn) -> None:
    """Значение ifrs заведено в CHECK заранее, хотя ветки ещё нет."""
    assert make_src_file(db_conn, Standard.IFRS) > 0


def test_unknown_standard_is_rejected(db_conn) -> None:
    """Посторонний стандарт схема не принимает."""
    with pytest.raises(Exception, match="standard"):
        execute(
            "INSERT INTO src_file (inn, standard, report_year, source) "
            "VALUES (%(i)s, 'gaap', 2025, 'file')",
            {"i": INN},
            conn=db_conn,
        )


def test_standards_coexist_in_src_file(db_conn) -> None:
    """За один год организация может раскрыть и РСБУ, и МСФО."""
    make_src_file(db_conn, Standard.RSBU)
    make_src_file(db_conn, Standard.IFRS)
    rows = fetch_all(
        "SELECT standard FROM src_file WHERE inn = %(i)s AND report_year = 2025",
        {"i": INN},
        conn=db_conn,
    )
    assert {row["standard"] for row in rows} == {"rsbu", "ifrs"}


def test_same_line_in_two_standards_does_not_collide(db_conn) -> None:
    """Одна и та же строка за один период существует в обоих стандартах."""
    add_fact(db_conn, make_src_file(db_conn, Standard.RSBU), Standard.RSBU, Decimal(100))
    add_fact(db_conn, make_src_file(db_conn, Standard.IFRS), Standard.IFRS, Decimal(200))

    rows = fetch_all(
        "SELECT standard, value FROM fact_report WHERE inn = %(i)s AND line_code = '1600'",
        {"i": INN},
        conn=db_conn,
    )
    assert {(row["standard"], row["value"]) for row in rows} == {
        ("rsbu", Decimal(100)),
        ("ifrs", Decimal(200)),
    }


def test_metric_values_do_not_overwrite_each_other(db_conn) -> None:
    """Расчёт по одному стандарту не затирает значения другого."""
    add_metric(db_conn, Standard.RSBU, Decimal("0.82"))
    add_metric(db_conn, Standard.IFRS, Decimal("1.35"))

    rows = fetch_all(
        "SELECT standard, value FROM metric_value WHERE inn = %(i)s AND metric_code = 'cur_liq'",
        {"i": INN},
        conn=db_conn,
    )
    assert len(rows) == 2
    assert {row["standard"] for row in rows} == {"rsbu", "ifrs"}


# --- ряды -------------------------------------------------------------------


def test_series_does_not_mix_standards(db_conn) -> None:
    """Ряд показателя строится в пределах одного стандарта."""
    add_metric(db_conn, Standard.RSBU, Decimal("0.82"))
    add_metric(db_conn, Standard.IFRS, Decimal("1.35"))

    rsbu = load_series(INN, "cur_liq", db_conn, Standard.RSBU)
    ifrs = load_series(INN, "cur_liq", db_conn, Standard.IFRS)

    assert [point.value for point in rsbu.points] == [Decimal("0.82")]
    assert [point.value for point in ifrs.points] == [Decimal("1.35")]
    assert rsbu.standard is Standard.RSBU
    assert ifrs.standard is Standard.IFRS


def test_series_defaults_to_rsbu(db_conn) -> None:
    """Без указания стандарта ряд строится по РСБУ."""
    add_metric(db_conn, Standard.RSBU, Decimal("0.82"))
    add_metric(db_conn, Standard.IFRS, Decimal("1.35"))
    assert load_series(INN, "cur_liq", db_conn).standard is Standard.RSBU
    assert [point.value for point in load_series(INN, "cur_liq", db_conn).points] == [
        Decimal("0.82")
    ]


def test_period_quality_is_split_by_standard(db_conn) -> None:
    """Доверие к периоду считается отдельно по каждому стандарту."""
    add_fact(db_conn, make_src_file(db_conn, Standard.RSBU), Standard.RSBU, Decimal(100))
    add_fact(db_conn, make_src_file(db_conn, Standard.IFRS), Standard.IFRS, Decimal(200))

    rows = fetch_all(
        "SELECT standard, lines_total FROM period_quality WHERE inn = %(i)s",
        {"i": INN},
        conn=db_conn,
    )
    assert len(rows) == 2, "представление смешало стандарты"
    assert all(row["lines_total"] == 1 for row in rows)


def test_enum_matches_schema_check() -> None:
    """Перечисление стандартов не расходится с CHECK в схеме."""
    schema = (Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql").read_text(
        encoding="utf-8"
    )
    assert schema.count("standard IN ('rsbu', 'ifrs')") == 3
    assert {item.value for item in Standard} == {"rsbu", "ifrs"}
