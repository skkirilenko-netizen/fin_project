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

# Комплект следующего года: сравнительная колонка повторяет отчётную колонку
# прежнего комплекта. Это и есть столкновение, ради которого существует
# правило приоритета, — и до сих пор оно ни разу не происходило на данных.
LATER = """
Консолидированный отчёт о финансовом положении
(в миллионах российских рублей)
Пояснения      31 декабря 2025 года      31 декабря 2024 года
Основные средства                       800 000        700 000
Итого внеоборотные активы               800 000        700 000
Запасы                                  350 000        300 000
Денежные средства и их эквиваленты      550 000        500 000
Итого оборотные активы                  900 000        800 000
Итого активы                          1 700 000      1 500 000
Акционерный капитал                     400 000        400 000
Нераспределённая прибыль                300 000        200 000
Итого капитал                           700 000        600 000
Долгосрочные кредиты и займы            600 000        500 000
Итого долгосрочные обязательства        600 000        500 000
Краткосрочные кредиты и займы           400 000        400 000
Итого краткосрочные обязательства       400 000        400 000
Итого обязательства                   1 000 000        900 000
Итого капитал и обязательства         1 700 000      1 500 000

Консолидированный отчёт о прибыли или убытке
(в миллионах российских рублей)
Выручка                               1 300 000      1 200 000
Себестоимость продаж                    (850 000)      (800 000)
Валовая прибыль                         450 000        400 000
Коммерческие расходы                     (45 000)       (40 000)
Административные расходы                 (65 000)       (60 000)
Операционная прибыль                    340 000        300 000
Финансовые доходы                        12 000         10 000
Финансовые расходы                       (52 000)       (50 000)
Прибыль до налогообложения              300 000        260 000
Расход по налогу на прибыль              (60 000)       (52 000)
Прибыль за период                       240 000        208 000
"""


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


def later_set(text: str = LATER):
    """Комплект следующего года: даты в шапках сдвинуты на год вперёд.

    Отчётный период прежнего комплекта приходит здесь сравнительной колонкой —
    ровно то столкновение, ради которого существует правило приоритета.
    Даты берутся из шапок форм самим приёмом: задать их в обход разбора
    значило бы проверить правило на данных, которых разбор не даёт.
    """
    document = text + HEADER.replace("2024 года и 31 декабря 2023", "2025 года и 31 декабря 2024")
    profile = identify(document)
    assert profile.accepted, getattr(profile, "reason", "")
    extraction = extract(document, profile.dates_by_form, Grouping.RUSSIAN)
    return extraction, profile, review(extraction, profile)


def test_revision_against_previous_report_is_logged(db_conn) -> None:
    """Расхождение сравнительных данных с загруженными попадает в журнал.

    Один и тот же период приходит дважды: отчётным в своём комплекте
    и сравнительным в более позднем. Расхождение — содержательный сигнал
    о переклассификации.
    """
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    # Комплект следующего года пересмотрел сравнительную величину 2024 года.
    revised = LATER.replace(
        "Итого активы                          1 700 000      1 500 000",
        "Итого активы                          1 700 000      1 111 111",
    )
    result = load_extraction(INN, *later_set(revised), db_conn)

    assert result.collisions.checked, "сверять было не с чем — столкновения не было"
    assert result.revisions, "расхождение не замечено"
    assert any("ifrs.total_assets" in item for item in result.revisions)
    rows = fetch_all(
        "SELECT message FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'period_value_mismatch'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert rows, "расхождение не записано в журнал"


def test_sign_convention_is_not_counted_as_a_revision(db_conn) -> None:
    """Величина та же, знак обратный — это соглашение о печати, а не пересмотр.

    По коду `period_value_mismatch` считается интенсивность пересмотра
    отчётности. Расходная статья печатается то в скобках, то без них, и одна
    организация делает это в разные годы по-разному; считая такое пересмотром,
    сигнал мерил бы наше соглашение о знаке, а не эмитента.
    """
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    # Тот же расход за 2024 год, напечатанный в новом комплекте без скобок.
    # Проверено на РСБУ: одна и та же организация печатает налог на прибыль
    # в одном году в скобках, в другом без них.
    flipped = LATER.replace(
        "Расход по налогу на прибыль              (60 000)       (52 000)",
        "Расход по налогу на прибыль              (60 000)        52 000",
    )
    result = load_extraction(INN, *later_set(flipped), db_conn)

    assert not any("income_tax" in item for item in result.revisions)
    assert any("income_tax" in item.describe() for item in result.collisions.sign_only)
    rows = fetch_all(
        "SELECT check_code, line_code FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code IN ('sign_convention_mismatch', 'period_value_mismatch')",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert any(row["check_code"] == "sign_convention_mismatch" for row in rows)


def test_collision_counter_stands_next_to_the_findings(db_conn) -> None:
    """Число сверенных величин уходит в журнал рядом с числом расхождений.

    Ноль расхождений при неизвестном числе сверок не означает ничего:
    правило приоритета выглядело работающим, ни разу не сработав.
    """
    extraction, profile, decision = prepared()
    result = load_extraction(INN, extraction, profile, decision, db_conn)

    row = fetch_one(
        "SELECT message, details FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'period_priority'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert row is not None, "сводка столкновений не записана"
    assert row["details"]["checked"] == 0
    assert "ранее загруженных величин за эти периоды нет" in row["message"]


def test_reloading_the_same_set_is_an_overwrite_not_a_revision(db_conn) -> None:
    """Повторная загрузка того же комплекта — перезапись, а не пересмотр.

    Величину изменил наш разбор, а не эмитент, и приписывать ему правку
    парсера нельзя.
    """
    extraction, profile, decision = prepared()
    load_extraction(INN, extraction, profile, decision, db_conn)

    revised = BALANCE.replace(
        "Итого активы                          1 500 000      1 360 000",
        "Итого активы                          1 500 000      1 111 111",
    )
    result = load_extraction(INN, *prepared(revised), db_conn)

    assert not result.revisions
    assert result.collisions.rewritten
    rows = fetch_all(
        "SELECT check_code FROM dq_log WHERE src_file_id = %(id)s "
        "AND check_code = 'fact_overwrite'",
        {"id": result.src_file_id},
        conn=db_conn,
    )
    assert rows, "перезапись не записана в журнал"


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

    # Тот же период приходит сравнительным: он стоит второй колонкой,
    # и величина в ней другая.
    later = LATER.replace(
        "Итого активы                          1 700 000      1 500 000",
        "Итого активы                          1 700 000      1 111 111",
    )
    result = load_extraction(INN, *later_set(later), db_conn)
    assert result.collisions.checked, "столкновения не произошло — проверять нечего"
    assert result.collisions.kept_by_priority, "приоритет не сработал ни разу"

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
    """Подтверждённая статья хранит наименование дословно и меру существенности."""
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
        "SELECT code, source_name, materiality_share, confirmed_by "
        "FROM ifrs_line_confirmation WHERE inn = %(i)s",
        {"i": INN},
        conn=db_conn,
    )
    assert row["code"] == "ifrs.principal_receivable"
    assert row["source_name"] == "Задолженность Принципала"
    assert row["materiality_share"] > Decimal("0.05")
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


def test_confirmed_value_becomes_a_fact_with_its_recognition(db_conn) -> None:
    """Подтверждённая статья попадает в факты с пометкой источника опознания.

    Прежде факты писались только из строк, опознанных **справочником**,
    и статьи, о которых человек уже сказал, чем они являются, в расчёт
    не попадали вовсе: у Норникеля выпадали все 64 подтверждённые статьи,
    у Автодора — 39 из 40, включая две, в которых лежат 85 % активов.
    Снятый карантин при этом означал бы расчёт по неполным данным.
    """
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
    assert result.collisions.by_confirmation > 0, "фактов по подтверждению не записано"

    rows = fetch_all(
        "SELECT report_date, value, recognition, period_role FROM fact_report "
        "WHERE inn = %(i)s AND standard = 'ifrs' "
        "AND line_code = 'ifrs.principal_receivable' ORDER BY report_date DESC",
        {"i": INN},
        conn=db_conn,
    )
    assert [row["value"] for row in rows] == [Decimal(400_000), Decimal(380_000)]
    assert {row["recognition"] for row in rows} == {"confirmation"}
    # Роль периода та же, что у опознанных справочником: величина второй графы
    # сравнительная, и правило приоритета к ней применяется наравне.
    assert [row["period_role"] for row in rows] == ["current", "previous"]

    # Опознанное справочником помечено своей силой опознания, и графа считает
    # то, как называется.
    catalog = fetch_all(
        "SELECT DISTINCT recognition FROM fact_report WHERE inn = %(i)s "
        "AND standard = 'ifrs' AND line_code = 'ifrs.total_assets'",
        {"i": INN},
        conn=db_conn,
    )
    assert [row["recognition"] for row in catalog] == ["catalog"]


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
