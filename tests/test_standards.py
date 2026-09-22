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
    # src_file, fact_report, metric_value, assessment и журнал ручных решений
    # о маршруте: всякая выборка по ИНН называет стандарт, и решение человека
    # о группе по МСФО о комплекте РСБУ не говорит.
    assert schema.count("standard IN ('rsbu', 'ifrs')") == 5
    assert {item.value for item in Standard} == {"rsbu", "ifrs"}


# --- ключи уникальности -------------------------------------------------------


def test_standard_is_part_of_every_key(db_conn) -> None:
    """Стандарт входит в ключ каждой таблицы фактов, показателей и оценки.

    Без него расчёт по одному стандарту молча затирал бы значения другого
    через ON CONFLICT, и ряд показателя собирался бы из точек двух разных
    отчётностей.
    """
    rows = fetch_all(
        """
        SELECT c.conrelid::regclass::text AS table_name,
               pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c
        WHERE c.contype = 'u'
          AND c.conrelid::regclass::text IN
              ('src_file', 'fact_report', 'metric_value', 'assessment')
        """,
        {},
        conn=db_conn,
    )
    assert len(rows) == 4, "не у всех таблиц есть ключ уникальности"
    for row in rows:
        assert "standard" in row["definition"], row["table_name"]


def test_assessment_parts_belong_to_one_standard(db_conn) -> None:
    """Разложение оценки принадлежит стандарту через саму оценку.

    Стандарт не дублируется в assessment_metric, assessment_group,
    assessment_signal и assessment_flag: он приходит по внешнему ключу,
    и ключ уникальности родителя его уже содержит. Хранить его вторым
    полем значило бы завести величину, которая может разойтись с родителем,
    — тот же случай, что с весами групп, которые хранились в разных единицах.
    """
    rows = fetch_all(
        """
        SELECT c.conrelid::regclass::text AS table_name,
               pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint c
        WHERE c.contype = 'f'
          AND c.conrelid::regclass::text IN
              ('assessment_metric', 'assessment_group', 'assessment_signal',
               'assessment_flag')
        """,
        {},
        conn=db_conn,
    )
    assert len(rows) == 4, "разложение оценки не связано с ней внешним ключом"
    for row in rows:
        assert "REFERENCES assessment(" in row["definition"], row["table_name"]


# --- правила обращения со стандартом ------------------------------------------


def test_base_standard_prefers_ifrs() -> None:
    """Для группы с консолидированной отчётностью база оценки — МСФО."""
    from finlib.standards import load_standards

    rule = load_standards().base_standard
    assert rule.choose({Standard.RSBU, Standard.IFRS}) is Standard.IFRS
    assert rule.choose({Standard.RSBU}) is Standard.RSBU
    assert rule.choose(set()) is None


def test_divergence_signal_is_declared_but_not_active() -> None:
    """Сигнал расхождения объявлен заготовкой: порога нет, и это сказано.

    Заготовка с объявленной причиной отличима от забытой строки справочника,
    а действующий сигнал без порога загрузиться не может вовсе.
    """
    from finlib.standards import load_standards

    divergence = load_standards().divergence
    assert divergence.active is False
    assert divergence.inactive_reason.strip()
    assert divergence.comparable_metrics


def test_active_divergence_without_threshold_is_rejected(tmp_path) -> None:
    """Объявить сигнал действующим, не дав порога, справочник не позволит."""
    import yaml
    from pydantic import ValidationError

    from finlib.standards import default_path, load_standards

    raw = yaml.safe_load(default_path().read_text(encoding="utf-8"))
    raw["divergence"]["active"] = True
    broken = tmp_path / "standards.yaml"
    broken.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    with pytest.raises(ValidationError, match="порога у него нет"):
        load_standards(broken)


def test_mixed_standards_block_the_metric() -> None:
    """Показатель из величин двух стандартов не считается вовсе.

    Смешение даёт число, которое выглядит настоящим и не значит ничего:
    чистый долг группы к выручке управляющей компании — не долговая
    нагрузка, а артефакт. Контроль отвергает показатель целиком, как
    и всякая другая нехватка: частичных вычислений в методике нет.
    """
    from finlib.metrics.definitions import load_metrics
    from finlib.metrics.engine import Baseline, MetricStatus, compute_metric
    from finlib.metrics.formula import NotCalculableReason
    from finlib.normalize.lines import ReportingType, load_lines
    from finlib.quality.periods import PeriodConfidence
    from finlib.quality.thresholds import load_thresholds

    metric = load_metrics().require("equity_ratio")  # 1300 / 1700
    values = {"1300": Decimal(400), "1700": Decimal(1000)}
    mixed = {"1300": Standard.IFRS.value, "1700": Standard.RSBU.value}

    catalog = load_lines()
    result = compute_metric(
        metric,
        ReportingType.FULL,
        PERIOD,
        values,
        Baseline(),
        PeriodConfidence.VERIFIED,
        load_thresholds(),
        mixed,
        catalog.measure_of,
    )
    assert result is not None
    assert result.status is MetricStatus.NOT_CALCULABLE
    assert result.reason_code == NotCalculableReason.MIXED_STANDARDS.value
    assert "ifrs" in result.reason and "rsbu" in result.reason

    # Те же величины одного стандарта считаются как обычно.
    same = dict.fromkeys(values, Standard.RSBU.value)
    ok = compute_metric(
        metric,
        ReportingType.FULL,
        PERIOD,
        values,
        Baseline(),
        PeriodConfidence.VERIFIED,
        load_thresholds(),
        same,
        catalog.measure_of,
    )
    assert ok is not None and ok.status is MetricStatus.OK


def test_mixing_check_reports_how_much_it_checked(db_conn, caplog) -> None:
    """Контроль смешения называет число проверенных показателей.

    Ноль отвергнутых при неизвестном числе проверок не означает ничего —
    правило ветки МСФО действует с первого дня.
    """
    import logging

    from finlib.metrics.engine import compute_all

    with caplog.at_level(logging.INFO, logger="finlib.metrics.engine"):
        compute_all("2100010824", db_conn)
    line = next(
        (item for item in caplog.messages if "контроль смешения" in item), None
    )
    assert line is not None, "контроль не сообщил о себе вовсе"
    assert "проверено показателей" in line
    checked = int(line.split("проверено показателей")[1].split(",")[0])
    assert checked > 0, "контроль не проверил ни одного показателя"


def test_fact_base_wording_differs_by_standard() -> None:
    """Обещание кодов строк не переносится на консолидированную отчётность."""
    from finlib.report.policy import load_policy

    section = load_policy().fact_base_section
    assert "кодами" in section.intro_text(Standard.RSBU)
    assert "унифицированной модели" in section.intro_text(Standard.IFRS)
    assert "утверждённых нормативным актом" in section.intro_text(Standard.IFRS)
