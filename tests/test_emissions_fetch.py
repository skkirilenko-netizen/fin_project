"""Выпуски отбором по дате: окно сверяется, и берутся только эмитенты справочника."""

import sys

import pytest

from finlib.config import settings
from finlib.sources import cbonds

sys.path.insert(0, str(settings.base_dir / "scripts"))

import emissions_fetch  # noqa: E402


def _answer(rows: list[tuple[str, str]]):
    """Источник, отдающий выпуски: ИНН эмитента и дата обновления."""

    def fetch(method: str, name: str, filters: tuple, limit: int, refresh: bool) -> dict:
        return {
            "items": [
                {"emitent_inn": inn, "updating_date": when} for inn, when in rows
            ]
        }

    return fetch


def test_only_issuers_of_the_list_are_taken(monkeypatch) -> None:
    """Выпуск чужого эмитента в окно попадает, в дозапрос — нет."""
    monkeypatch.setattr(
        emissions_fetch.cbonds,
        "fetch",
        _answer([("1", "2026-09-24"), ("2", "2026-09-25")]),
    )
    assert emissions_fetch.changed_issuers("2026-09-24", {"1", "3"}) == {"1"}


def test_a_skipped_filter_is_refused(monkeypatch) -> None:
    """Запись раньше даты — отбор пропущен молча, окно не принимается."""
    monkeypatch.setattr(
        emissions_fetch.cbonds, "fetch", _answer([("1", "2019-01-01")])
    )
    with pytest.raises(cbonds.FilterIgnoredError):
        emissions_fetch.changed_issuers("2026-09-24", {"1"})
