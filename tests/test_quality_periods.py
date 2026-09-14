"""Тесты доверия к периоду: проверялся ли он блокирующими контролями."""

from datetime import date

import pytest
from probes import CORRECTED_BFO, FULL_BFO, read_probe

from finlib.db import execute
from finlib.normalize.loader import load_report_set
from finlib.quality.periods import PeriodConfidence, limitations, period_quality
from finlib.quality.runner import run_checks
from finlib.sources.girbo import Organization, parse_report_sets
from finlib.utils import json_loads_decimal

FULL_INN = "7736050003"
CORRECTED_INN = "2522002003"


@pytest.fixture(autouse=True)
def clean(db_conn):
    """Убирает следы прежних прогонов внутри транзакции теста."""
    execute(
        "DELETE FROM organization WHERE inn = ANY(%(i)s)",
        {"i": [FULL_INN, CORRECTED_INN]},
        conn=db_conn,
    )
    execute(
        "DELETE FROM dq_log WHERE inn = ANY(%(i)s)",
        {"i": [FULL_INN, CORRECTED_INN]},
        conn=db_conn,
    )
    return db_conn


def load(probe, inn: str, year: int, conn) -> int:
    """Загружает один комплект."""
    sets = parse_report_sets(json_loads_decimal(read_probe(probe)), inn)
    report = next(item for item in sets if item.report_year == year)
    org = Organization(inn=inn, girbo_id=1, short_name="ТЕСТ", full_name="ТЕСТ")
    src_file_id = load_report_set(report, org, conn).src_file_id
    assert src_file_id is not None
    return src_file_id


def test_reporting_period_is_verified(db_conn) -> None:
    """Отчётный период комплекта проверен своим комплектом."""
    load(FULL_BFO, FULL_INN, 2025, db_conn)
    quality = period_quality(FULL_INN, db_conn)
    own = quality[date(2025, 12, 31)]
    assert own.confidence is PeriodConfidence.VERIFIED
    assert own.has_own_report
    assert own.limitation is None


def test_comparative_only_period_is_flagged(db_conn) -> None:
    """Период, пришедший только сравнительной колонкой, помечается отдельно.

    Комплект за 2025 год приносит балансы на 31.12.2024 и 31.12.2023. Своих
    комплектов за эти периоды нет, блокирующие контроли по ним не выполнялись.
    """
    load(FULL_BFO, FULL_INN, 2025, db_conn)
    quality = period_quality(FULL_INN, db_conn)

    for report_date in (date(2024, 12, 31), date(2023, 12, 31)):
        item = quality[report_date]
        assert item.confidence is PeriodConfidence.COMPARATIVE_ONLY, report_date
        assert not item.has_own_report
        assert item.is_usable, "сравнительный период не запрещён, лишь помечен"
        assert item.limitation and "пониженным доверием" in item.limitation


def test_own_report_upgrades_confidence(db_conn) -> None:
    """Появление собственного комплекта переводит период в проверенные."""
    load(FULL_BFO, FULL_INN, 2025, db_conn)
    assert period_quality(FULL_INN, db_conn)[date(2024, 12, 31)].confidence is (
        PeriodConfidence.COMPARATIVE_ONLY
    )

    load(FULL_BFO, FULL_INN, 2024, db_conn)
    upgraded = period_quality(FULL_INN, db_conn)[date(2024, 12, 31)]
    assert upgraded.confidence is PeriodConfidence.VERIFIED
    assert upgraded.has_own_report


def test_quarantined_period_is_excluded(db_conn) -> None:
    """Период, чей комплект в карантине, из расчёта исключается."""
    src_file_id = load(CORRECTED_BFO, CORRECTED_INN, 2025, db_conn)
    assert run_checks(src_file_id, db_conn).quarantined

    item = period_quality(CORRECTED_INN, db_conn)[date(2025, 12, 31)]
    assert item.confidence is PeriodConfidence.QUARANTINED
    assert not item.is_usable
    assert item.limitation and "не включена" in item.limitation


def test_limitations_are_ready_for_conclusion(db_conn) -> None:
    """Оговорки собираются готовым текстом для раздела «Ограничения анализа»."""
    load(FULL_BFO, FULL_INN, 2025, db_conn)
    notes = limitations(FULL_INN, db_conn)
    assert len(notes) == 2, "оговорки по двум сравнительным периодам"
    assert all("контроли качества" in note or "контроли" in note for note in notes)


def test_confidence_matches_schema_check() -> None:
    """Перечисление доверия не расходится с CHECK в metric_value."""
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql").read_text(
        encoding="utf-8"
    )
    assert "confidence IN ('verified', 'comparative_only', 'quarantined')" in schema
    assert {item.value for item in PeriodConfidence} == {
        "verified",
        "comparative_only",
        "quarantined",
    }
