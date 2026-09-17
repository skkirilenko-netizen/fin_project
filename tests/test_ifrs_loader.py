"""Тесты записи комплекта МСФО в базу (задача 23-бис).

Здесь данные МСФО впервые встречаются с расчётным слоем: стандарт в ключах
на реальной записи, поведение контролей качества и приоритет периодов.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.db import execute, fetch_all, fetch_one
from finlib.normalize.ifrs_loader import load_extraction
from finlib.quality.runner import run_checks
from finlib.sources.ifrs_extract import extract
from finlib.sources.ifrs_inbox import identify
from finlib.sources.ifrs_numbers import Grouping
from finlib.sources.ifrs_review import review
from finlib.standards import Standard

INN = "7736050003"
DATES = (date(2024, 12, 31), date(2023, 12, 31))

BALANCE = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2024 года      31 декабря 2023 года
Основные средства                       700 000        650 000
Итого внеоборотные активы               700 000        650 000
Запасы                                  300 000        280 000
Денежные средства и их эквиваленты      500 000        430 000
Итого оборотные активы                  800 000        710 000
Итого активы                          1 500 000      1 360 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                200 000        160 000
Итого капитал                           600 000        560 000
Долгосрочные кредиты и займы            500 000        500 000
Итого долгосрочные обязательства        500 000        500 000
Краткосрочные кредиты и займы           400 000        300 000
Итого краткосрочные обязательства       400 000        300 000
Итого обязательства                     900 000        800 000
Итого капитал и обязательства         1 500 000      1 360 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 200 000      1 100 000
Себестоимость продаж                    (800 000)      (750 000)
Валовая прибыль                         400 000        350 000
Коммерческие расходы                     (40 000)       (35 000)
Административные расходы                 (60 000)       (55 000)
Операционная прибыль                    300 000        260 000
Финансовые доходы                        10 000          8 000
Финансовые расходы                       (50 000)       (48 000)
Прибыль до налогообложения              260 000        220 000
Расход по налогу на прибыль              (52 000)       (44 000)
Прибыль за период                       208 000        176 000
"""

HEADER = (
    "\n(в миллионах российских рублей)\n"
    "по состоянию на 31 декабря 2024 года и 31 декабря 2023 года\n"
    + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
)


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM src_file WHERE inn = %(i)s AND standard = 'ifrs'",
        {"i": INN},
        conn=db_conn,
    )
    execute(
        "DELETE FROM ifrs_line_confirmation WHERE inn = %(i)s", {"i": INN}, conn=db_conn
    )
    return db_conn


def prepared(text: str = BALANCE):
    """Документ, проведённый через приём, разбор и сверку."""
    profile = identify(text + HEADER)
    assert profile.accepted, getattr(profile, "reason", "")
    extraction = extract(text, DATES, Grouping.RUSSIAN)
    return extraction, profile, review(extraction, profile)


def test_facts_are_written_with_the_ifrs_standard(db_conn) -> None:
    """Факты пишутся со стандартом ifrs и не смешиваются с РСБУ."""
    extraction, profile, decision = prepared()
    result = load_extraction(INN, extraction, profile, decision, db_conn)

    assert result.facts_written > 0
    rows = fetch_all(
        "SELECT standard, line_code, value, period_role FROM fact_report "
        "WHERE inn = %(i)s AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert rows, "фактов МСФО не записано"
    assert {row["standard"] for row in rows} == {"ifrs"}
    values = {row["line_code"]: row["value"] for row in rows}
    assert values["ifrs.total_assets"] == Decimal(1_500_000)


def test_period_roles_follow_the_column_order(db_conn) -> None:
    """Первая колонка — отчётный период, остальные сравнительные."""
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    roles = {
        (row["report_date"], row["line_code"]): row["period_role"]
        for row in fetch_all(
            "SELECT report_date, line_code, period_role FROM fact_report "
            "WHERE inn = %(i)s AND standard = 'ifrs' AND line_code = 'ifrs.total_assets'",
            {"i": INN},
            conn=db_conn,
        )
    }
    assert roles[(DATES[0], "ifrs.total_assets")] == "current"
    assert roles[(DATES[1], "ifrs.total_assets")] == "previous"


def test_ifrs_does_not_collide_with_rsbu(db_conn) -> None:
    """Величина по МСФО не затирает величину по РСБУ за ту же дату."""
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    rsbu = fetch_one(
        "SELECT count(*) AS n FROM fact_report "
        "WHERE inn = %(i)s AND standard = 'rsbu' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    ifrs = fetch_one(
        "SELECT count(*) AS n FROM fact_report "
        "WHERE inn = %(i)s AND standard = 'ifrs' AND report_date = %(d)s",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert ifrs["n"] > 0
    # Отчётность РСБУ этой организации в базе уже есть, и она на месте.
    assert rsbu["n"] > 0


def test_src_file_records_how_the_document_was_read(db_conn) -> None:
    """Комплект хранит вид отчётности, единицу и конвенцию записи чисел."""
    extraction, profile, decision = prepared()
    result = load_extraction(INN, extraction, profile, decision, db_conn)

    row = fetch_one(
        "SELECT standard, reporting_kind, unit_code, unit_source, digit_grouping, "
        "status FROM src_file WHERE id = %(id)s",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert row["standard"] == "ifrs"
    assert row["reporting_kind"] == "full"
    assert row["unit_code"] == "385"
    assert row["unit_source"] == "explicit"
    assert row["digit_grouping"] == "russian"
    assert row["status"] == "loaded"


def test_unconfirmed_extraction_goes_to_quarantine(db_conn) -> None:
    """Извлечение, не прошедшее сверку и не подтверждённое, в расчёт не идёт.

    Машина не знает, что перед ней: строка не опознана, а человек кода
    не присвоил. Пускать такое в расчёт — то же, что угадывать.
    """
    text = BALANCE + "\nЗадолженность Принципала                  50 000     40 000\n"
    extraction, profile, decision = prepared(text)
    assert not decision.automatic

    result = load_extraction(INN, extraction, profile, decision, db_conn)
    assert result.quarantined
    row = fetch_one(
        "SELECT status FROM src_file WHERE id = %(id)s",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert row["status"] == "quarantine"


def test_journal_records_counters_not_only_failures(db_conn) -> None:
    """В журнал уходит и число проверенного, а не только сработавшее."""
    extraction, profile, decision = prepared()
    result = load_extraction(INN, extraction, profile, decision, db_conn)

    rows = fetch_all(
        "SELECT check_code, message, details FROM dq_log WHERE src_file_id = %(id)s",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert rows
    summary = next(item for item in rows if item["check_code"] == "line_mapping")
    assert summary["details"]["rows_total"] > 0
    assert summary["details"]["totals_checked"] > 0
    assert "Опознано позиций" in summary["message"]


# --- сравнительные данные -------------------------------------------------------


def test_revision_against_previous_report_is_logged(db_conn) -> None:
    """Расхождение сравнительных данных с загруженными попадает в журнал.

    Один и тот же период приходит дважды: отчётным в своём комплекте
    и сравнительным в более позднем. Расхождение — содержательный сигнал
    о переклассификации.
    """
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    revised = BALANCE.replace(
        "Итого активы                          1 500 000      1 360 000",
        "Итого активы                          1 500 000      1 111 111",
    )
    again = prepared(revised)
    result = load_extraction(INN, *again, db_conn)

    assert result.revisions, "расхождение не замечено"
    assert any("ifrs.total_assets" in item for item in result.revisions)
    rows = fetch_all(
        "SELECT message FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'period_value_mismatch'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert rows, "расхождение не записано в журнал"


def test_comparative_value_does_not_overwrite_the_reported_one(db_conn) -> None:
    """Сравнительное значение не затирает отчётное.

    Правило приоритета то же, что в РСБУ: без него результат зависел бы
    от порядка загрузки комплектов.
    """
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)
    before = fetch_one(
        "SELECT value FROM fact_report WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND report_date = %(d)s AND line_code = 'ifrs.total_assets'",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )

    # Тот же период приходит сравнительным: он стоит второй колонкой.
    later = BALANCE.replace(
        "Итого активы                          1 500 000      1 360 000",
        "Итого активы                          1 900 000      1 500 000",
    )
    profile_later = identify(
        later
        + "\n(в миллионах российских рублей)\n"
        + "по состоянию на 31 декабря 2025 года и 31 декабря 2024 года\n"
        + "\nПримечания к консолидированной финансовой отчётности.\n" * 40
    )
    dates_later = (date(2025, 12, 31), date(2024, 12, 31))
    extraction_later = extract(later, dates_later, Grouping.RUSSIAN)
    decision_later = review(extraction_later, profile_later)
    load_extraction(INN, extraction_later, profile_later, decision_later, db_conn)

    after = fetch_one(
        "SELECT value, period_role FROM fact_report WHERE inn = %(i)s "
        "AND standard = 'ifrs' AND report_date = %(d)s "
        "AND line_code = 'ifrs.total_assets'",
        {"i": INN, "d": DATES[0]},
        conn=db_conn,
    )
    assert after["value"] == before["value"], "сравнительное значение затёрло отчётное"
    assert after["period_role"] == "current"


# --- подтверждённые статьи -------------------------------------------------------


def test_confirmed_item_is_saved_with_its_wording(db_conn) -> None:
    """Подтверждённая статья хранит наименование дословно и долю от активов."""
    text = BALANCE + "\nЗадолженность Принципала                 400 000    380 000\n"
    extraction, profile, decision = prepared(text)
    result = load_extraction(
        INN,
        extraction,
        profile,
        decision,
        db_conn,
        confirmed_by="аналитик",
        confirmations={"Задолженность Принципала": "ifrs.principal_receivable"},
    )
    assert result.confirmations == 1
    row = fetch_one(
        "SELECT code, source_name, share_of_assets, confirmed_by "
        "FROM ifrs_line_confirmation WHERE inn = %(i)s",
        {"i": INN},
        conn=db_conn,
    )
    assert row["code"] == "ifrs.principal_receivable"
    assert row["source_name"] == "Задолженность Принципала"
    assert row["share_of_assets"] > Decimal("0.05")
    assert row["confirmed_by"] == "аналитик"


def test_confirmation_lifts_the_quarantine(db_conn) -> None:
    """Подтверждённое человеком извлечение идёт в расчёт."""
    text = BALANCE + "\nЗадолженность Принципала                 400 000    380 000\n"
    extraction, profile, decision = prepared(text)
    result = load_extraction(
        INN,
        extraction,
        profile,
        decision,
        db_conn,
        confirmed_by="аналитик",
        confirmations={"Задолженность Принципала": "ifrs.principal_receivable"},
    )
    assert not result.quarantined


# --- контроли качества на данных МСФО --------------------------------------------


def test_rsbu_checks_are_not_run_against_an_ifrs_set(db_conn) -> None:
    """Контроли РСБУ к комплекту МСФО не применяются — и об этом есть запись.

    Они построены на формах и кодах строк РСБУ: равенство 1600 = 1700,
    состав разделов, цепочка прибыли. Прогнать их по комплекту МСФО значило
    бы получить полтора десятка ложных провалов и карантин на ровном месте.
    Молчание тоже не годится: комплект без записей выглядит проверенным.
    """
    extraction, profile, decision = prepared()
    result = load_extraction(INN, extraction, profile, decision, db_conn)

    report = run_checks(result.src_file_id, db_conn)
    assert report.outcomes == []
    assert not report.quarantined

    rows = fetch_all(
        "SELECT message FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'line_mapping'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert any("Контроли РСБУ не выполнялись" in row["message"] for row in rows)


def test_ifrs_set_does_not_reach_rsbu_metrics(db_conn) -> None:
    """Расчёт по РСБУ не подхватывает факты МСФО.

    Стандарт входит в выборку фактов, и показатель по РСБУ считается только
    по строкам РСБУ. Иначе смешение источников дало бы число, которое
    выглядит настоящим и не значит ничего.
    """
    from finlib.metrics.engine import load_period_values

    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    rsbu = load_period_values(INN, db_conn, Standard.RSBU)
    codes = {code for period in rsbu.values() for code in period.values}
    assert not any(code.startswith("ifrs.") for code in codes)

    ifrs = load_period_values(INN, db_conn, Standard.IFRS)
    ifrs_codes = {code for period in ifrs.values() for code in period.values}
    assert "ifrs.total_assets" in ifrs_codes
