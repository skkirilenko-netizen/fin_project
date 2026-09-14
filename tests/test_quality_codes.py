"""Тесты словаря кодов журнала качества."""

from pathlib import Path

from finlib.quality.codes import LOADER_SEVERITY, CheckCode, CheckStatus, Severity

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql"


def test_status_and_severity_match_schema() -> None:
    """Перечисления не расходятся с CHECK в sql/001_schema.sql."""
    schema = SCHEMA_SQL.read_text(encoding="utf-8")
    assert "status IN ('pass', 'fail', 'warning', 'info')" in schema
    assert "severity IN ('blocking', 'warning', 'info')" in schema
    assert {item.value for item in CheckStatus} == {"pass", "fail", "warning", "info"}
    assert {item.value for item in Severity} == {"blocking", "warning", "info"}


def test_unrecognized_line_is_visible_in_quality_summary() -> None:
    """Неопознанная строка журналируется на уровне, видном в сводке качества."""
    severity = LOADER_SEVERITY[CheckCode.LINE_NOT_RECOGNIZED]
    assert severity is Severity.WARNING
    assert severity is not Severity.INFO


def test_loader_codes_have_severity() -> None:
    """Каждая служебная запись загрузки имеет объявленный уровень."""
    loader_codes = {
        CheckCode.LINE_NOT_RECOGNIZED,
        CheckCode.UNKNOWN_LINE_CODE,
        CheckCode.FACT_OVERWRITE,
    }
    assert loader_codes <= set(LOADER_SEVERITY)
