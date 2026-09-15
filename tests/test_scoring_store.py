"""Тесты записи оценки: разложение должно восстанавливаться из БД полностью."""

import pytest
from probes import FULL_BFO, read_probe

from finlib.db import execute, fetch_all, fetch_one
from finlib.metrics.engine import compute_all
from finlib.metrics.store import save_results
from finlib.normalize.loader import load_report_set
from finlib.quality.runner import run_checks
from finlib.scoring.engine import assess
from finlib.scoring.store import load_assessment, save_assessment
from finlib.sources.girbo import Organization, parse_report_sets
from finlib.utils import json_loads_decimal

INN = "7736050003"


@pytest.fixture
def prepared(db_conn):
    """Загружает пробу, прогоняет контроли и расчёт показателей."""
    execute("DELETE FROM organization WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute("DELETE FROM dq_log WHERE inn = %(i)s", {"i": INN}, conn=db_conn)
    execute("DELETE FROM metric_value WHERE inn = %(i)s", {"i": INN}, conn=db_conn)

    sets = parse_report_sets(json_loads_decimal(read_probe(FULL_BFO)), INN)
    org = Organization(inn=INN, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ")
    for report in sorted(sets, key=lambda item: item.report_year):
        run_checks(load_report_set(report, org, db_conn).src_file_id, db_conn)
    save_results(INN, compute_all(INN, db_conn), db_conn)
    return db_conn


def test_assessment_is_saved(prepared) -> None:
    """Оценка записывается вместе с версиями методик."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    row = fetch_one(
        "SELECT * FROM assessment WHERE inn = %(i)s", {"i": INN}, conn=prepared
    )
    assert row is not None
    assert row["class_code"] == result.class_code
    assert row["metrics_version"] and row["scoring_version"] and row["flags_version"]


def test_decomposition_is_complete(prepared) -> None:
    """Разложение восстанавливается целиком: группы, показатели, флаги."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    loaded = load_assessment(INN, result.report_date, prepared)
    assert loaded is not None
    assert len(loaded["groups"]) == len(result.groups)
    assert len(loaded["metrics"]) == len(result.metrics)
    assert len(loaded["flags"]) == len(result.flags)


def test_metric_decomposition_separates_level_and_dynamics(prepared) -> None:
    """Видно, чем вызван балл: состоянием или изменением."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    rows = fetch_all(
        "SELECT m.metric_code, m.level_score, m.dynamics_score, m.score, m.included, "
        "m.exclusion_reason FROM assessment_metric m "
        "JOIN assessment a ON a.id = m.assessment_id WHERE a.inn = %(i)s",
        {"i": INN},
        conn=prepared,
    )
    included = [row for row in rows if row["included"]]
    assert included
    assert any(row["level_score"] is not None for row in included), "уровень сохранён"
    assert all(row["dynamics_score"] is not None for row in included), "динамика сохранена"


def test_excluded_metrics_keep_reason(prepared) -> None:
    """У исключённого показателя видна причина исключения."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    rows = fetch_all(
        "SELECT m.metric_code, m.exclusion_reason FROM assessment_metric m "
        "JOIN assessment a ON a.id = m.assessment_id "
        "WHERE a.inn = %(i)s AND m.included = false",
        {"i": INN},
        conn=prepared,
    )
    assert rows
    assert all(row["exclusion_reason"] for row in rows)
    codes = {row["metric_code"] for row in rows}
    assert {"equity", "fin_leverage"} <= codes, "исключённые из балла показатели видны"


def test_group_weights_are_stored_both_ways(prepared) -> None:
    """Сохранены и номинальный вес группы, и фактический после нормировки."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    rows = fetch_all(
        "SELECT g.group_code, g.nominal_weight, g.effective_weight, g.metrics_used "
        "FROM assessment_group g JOIN assessment a ON a.id = g.assessment_id "
        "WHERE a.inn = %(i)s",
        {"i": INN},
        conn=prepared,
    )
    assert len(rows) == 5
    live = [row for row in rows if row["metrics_used"] > 0]
    assert sum(row["effective_weight"] for row in live) == pytest.approx(1, abs=0.001)


def test_stop_factor_is_visible_in_result(prepared) -> None:
    """Видно, какой стоп-фактор сработал и что он изменил."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    row = fetch_one(
        "SELECT class_before_stop, class_code, stop_factor_code, stop_factor_effect "
        "FROM assessment WHERE inn = %(i)s",
        {"i": INN},
        conn=prepared,
    )
    assert row is not None
    assert row["stop_factor_code"] == "weak_coverage"
    assert row["stop_factor_effect"] == "cap_at_class"
    assert row["class_before_stop"] is not None


def test_flag_text_is_stored(prepared) -> None:
    """Текст оговорки хранится готовым: заключению его собирать не нужно."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    row = fetch_one(
        "SELECT f.flag_code, f.message, f.affects_class FROM assessment_flag f "
        "JOIN assessment a ON a.id = f.assessment_id WHERE a.inn = %(i)s",
        {"i": INN},
        conn=prepared,
    )
    assert row is not None
    assert row["flag_code"] == "holding_structure"
    assert "холдинговой структуры" in row["message"]
    assert row["affects_class"] is False


def test_recalculation_replaces_decomposition(prepared) -> None:
    """Повторный расчёт переписывает разложение, а не копит его."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)
    first = len(load_assessment(INN, result.report_date, prepared)["metrics"])

    save_assessment(result, prepared)
    second = len(load_assessment(INN, result.report_date, prepared)["metrics"])

    assert first == second > 0


def test_confidence_reasons_are_stored(prepared) -> None:
    """Основания понижения уверенности сохранены текстом."""
    result = assess(INN, prepared)
    assert result is not None
    save_assessment(result, prepared)

    row = fetch_one(
        "SELECT confidence, confidence_reasons FROM assessment WHERE inn = %(i)s",
        {"i": INN},
        conn=prepared,
    )
    assert row is not None
    assert row["confidence"] in {"high", "medium", "low"}
    if row["confidence"] != "high":
        assert row["confidence_reasons"], "понижение без объяснения недопустимо"
