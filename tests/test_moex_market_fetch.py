"""Пустой срез за рабочий день — недоставка, а не «торгов не было».

22–25.09.2026 срезы, спрошенные до публикации итогов дня, легли на диск
пустыми и брались оттуда: четыре торговых дня выпали из ряда молча.
"""

import datetime as dt
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from finlib.config import settings
from finlib.sources import moex

sys.path.insert(0, str(settings.base_dir / "scripts"))

import moex_market_fetch  # noqa: E402


class _Friday(dt.date):
    """«Сегодня» — пятница 25.09.2026."""

    @classmethod
    def today(cls) -> "dt.date":
        return cls(2026, 9, 25)


def test_an_empty_weekday_slice_is_not_kept(tmp_path: Path, monkeypatch) -> None:
    """Пустой рабочий день с диска удаляется и назван; выходной — нет."""
    monkeypatch.setattr(moex, "CACHE", tmp_path)
    monkeypatch.setattr(moex_market_fetch, "CURVES", tmp_path / "curves.json")
    monkeypatch.setattr(moex_market_fetch, "date", _Friday)

    def paged(path: str, name: str, block: str, params: dict) -> list:
        (tmp_path / f"{name}.json").write_text(json.dumps({block: []}), encoding="utf-8")
        return []

    monkeypatch.setattr(moex_market_fetch.moex, "paged", paged)
    monkeypatch.setattr(moex_market_fetch, "curve_of", lambda day: None)
    monkeypatch.setattr(sys, "argv", ["x", "--depth-days", "1", "--step", "1"])
    out = io.StringIO()
    with redirect_stdout(out):
        moex_market_fetch.main()
    # Пятница 25.09 и четверг 24.09 — рабочие: пустой ответ не хранится.
    assert not (tmp_path / "xsec_2026-09-25.json").exists()
    assert not (tmp_path / "xsec_2026-09-24.json").exists()
    assert "рабочих дней без итогов торгов 2" in out.getvalue()


def test_an_empty_weekend_slice_is_kept(tmp_path: Path, monkeypatch) -> None:
    """Суббота без торгов — данные: хранится и не переспрашивается."""
    monkeypatch.setattr(moex, "CACHE", tmp_path)
    monkeypatch.setattr(moex_market_fetch, "CURVES", tmp_path / "curves.json")

    class _Sunday(dt.date):
        @classmethod
        def today(cls) -> "dt.date":
            return cls(2026, 9, 27)

    monkeypatch.setattr(moex_market_fetch, "date", _Sunday)

    def paged(path: str, name: str, block: str, params: dict) -> list:
        (tmp_path / f"{name}.json").write_text(json.dumps({block: []}), encoding="utf-8")
        return []

    monkeypatch.setattr(moex_market_fetch.moex, "paged", paged)
    monkeypatch.setattr(moex_market_fetch, "curve_of", lambda day: None)
    monkeypatch.setattr(sys, "argv", ["x", "--depth-days", "1", "--step", "1"])
    with redirect_stdout(io.StringIO()):
        moex_market_fetch.main()
    assert (tmp_path / "xsec_2026-09-27.json").exists()
    assert (tmp_path / "xsec_2026-09-26.json").exists()
