"""Дозапрос графиков: окно по дате обновления сверяется, а не принимается на веру."""

import sys

import pytest

from finlib.config import settings
from finlib.sources import cbonds

sys.path.insert(0, str(settings.base_dir / "scripts"))

import flows_fetch  # noqa: E402


def _answer(dates: list[str]):
    """Источник, отдающий выпуски с названными датами обновления."""

    def fetch(method: str, name: str, filters: tuple, limit: int, refresh: bool) -> dict:
        return {
            "items": [
                {"id": str(number), "updating_date": when}
                for number, when in enumerate(dates)
            ]
        }

    return fetch


def test_the_window_is_taken_when_the_source_applied_it(monkeypatch) -> None:
    """Все записи не раньше даты — отбор применён, идентификаторы взяты."""
    monkeypatch.setattr(flows_fetch.cbonds, "fetch", _answer(["2026-09-23", "2026-09-25"]))
    assert flows_fetch.changed_since("2026-09-22") == {"0", "1"}


def test_a_record_before_the_date_means_the_filter_was_skipped(monkeypatch) -> None:
    """Запись раньше даты — отбор пропущен молча, окно не принимается."""
    monkeypatch.setattr(flows_fetch.cbonds, "fetch", _answer(["2026-09-23", "2024-01-01"]))
    with pytest.raises(cbonds.FilterIgnoredError):
        flows_fetch.changed_since("2026-09-22")
