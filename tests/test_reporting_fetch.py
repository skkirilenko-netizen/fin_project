"""Отчётность агрегатора в прогоне: новый комплект и пересмотр различаются по базе."""

import io
import sys
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal

import pytest

from finlib.config import settings
from finlib.sources import cbonds

sys.path.insert(0, str(settings.base_dir / "scripts"))
sys.path.insert(0, str(settings.base_dir / "eval"))

import reporting_fetch  # noqa: E402
from change_report_run import _new_reporting  # noqa: E402

YEAR = date(2025, 12, 31)
HALF = date(2026, 6, 30)


def test_a_new_key_is_a_new_set_and_a_changed_value_is_a_revision() -> None:
    """Ключа не было — новый комплект; был и величина иная — пересмотр."""
    before = (
        {("1", YEAR)},
        {("1", YEAR): {("0710001", "1600"): Decimal(100)}},
    )
    after = (
        {("1", YEAR), ("1", HALF)},
        {
            ("1", YEAR): {("0710001", "1600"): Decimal(120)},
            ("1", HALF): {("0710001", "1600"): Decimal(130)},
        },
    )
    new, revised = reporting_fetch.compare(before, after, "rsbu")
    assert new == [
        {"inn": "1", "standard": "rsbu", "period_end": "2026-06-30", "kind": "промежуточный"}
    ]
    assert revised == [
        {"inn": "1", "standard": "rsbu", "period_end": "2025-12-31", "changed": 1}
    ]


def test_an_unchanged_set_is_neither() -> None:
    """Та же доставка второй раз — ни нового, ни пересмотра."""
    state = ({("1", YEAR)}, {("1", YEAR): {("0710001", "1600"): Decimal(100)}})
    assert reporting_fetch.compare(state, state, "ifrs") == ([], [])


def test_a_skipped_date_filter_is_refused(monkeypatch) -> None:
    """Запись раньше даты — отбор пропущен молча, окно не принимается."""

    def fetch(method: str, name: str, filters: tuple, limit: int, refresh: bool) -> dict:
        return {"items": [{"created_at": "2019-01-01 00:00:00"}]}

    monkeypatch.setattr(reporting_fetch.cbonds, "fetch", fetch)
    with pytest.raises(cbonds.FilterIgnoredError):
        reporting_fetch._window("get_report_rsbu_balance", "created_at", "2026-09-22", True)


def test_the_report_names_new_sets_and_revisions() -> None:
    """Раздел «Новая отчётность» называет оба рода с числами."""
    out = io.StringIO()
    with redirect_stdout(out):
        _new_reporting(
            {
                "since": "2026-09-24",
                "new": [{"inn": "1", "standard": "ifrs", "period_end": "2026-06-30",
                         "kind": "промежуточный"}],
                "revised": [{"inn": "2", "standard": "rsbu", "period_end": "2025-12-31",
                             "changed": 3}],
                "failed": 0,
            },
            names={"1": "Эмитент один", "2": "Эмитент два"},
        )
    text = out.getvalue()
    assert "новых комплектов **1**, пересмотров **1**" in text
    assert "Эмитент один" in text and "| 3 |" in text


def test_a_day_without_delivery_says_so() -> None:
    """Доставки не было — так и сказано, а не пустой раздел."""
    out = io.StringIO()
    with redirect_stdout(out):
        _new_reporting(None)
    assert "Доставки отчётности агрегатора за этот день не было" in out.getvalue()
