"""Поздний снимок рейтинга не меняет историческую печать отчёта."""

import json
import sys
from datetime import date
from pathlib import Path

from finlib.config import settings
from finlib.scoring.routing import load_routing
from finlib.sources import cbonds_events

sys.path.insert(0, str(settings.base_dir / "eval"))
import change_report_run as report  # noqa: E402


def test_future_rating_cannot_rewrite_urgent_or_reason(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Поздняя коррекция с прежней датой рейтинга не попадает в прежний отчёт."""
    monkeypatch.setattr(cbonds_events, "SNAPSHOTS", tmp_path)
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    monkeypatch.setattr(cbonds_events, "credit_scales", lambda: frozenset({"1"}))
    monkeypatch.setattr(cbonds_events, "point_order", dict)
    monkeypatch.setattr(report, "risk_sectors", dict)
    monkeypatch.setattr(report, "_named", lambda inn: inn)
    entry = {"agency_name_rus": "Агентство", "scale_id": "1",
             "scale_point_name": "Withdrawn", "rating_date": "2026-10-02"}
    def save(day: str, point: str) -> None:
        """Сохраняет синтетический снимок только во временный каталог."""
        (tmp_path / f"{day}.json").write_text(json.dumps({
            "date": day, "issuers": {"1": [{**entry, "scale_point_name": point}]},
        }), encoding="utf-8")
    save("2026-10-02", "Withdrawn")
    routing = load_routing()
    before, until = date(2026, 10, 1), date(2026, 10, 2)
    row = {"grounds": [], "report_date": None}
    report._urgent(routing, {"1": row}, before, until)
    original = capsys.readouterr().out
    reason = report._why(routing, "1", before, until, set(), row, row, {"рейтинг"})
    assert "Withdrawn" in original and "Withdrawn" in reason
    save("2026-10-03", "ruAAA")
    report._urgent(routing, {"1": row}, before, until)
    assert capsys.readouterr().out == original
    assert report._why(routing, "1", before, until, set(), row, row, {"рейтинг"}) == reason
    assert cbonds_events.read_snapshot().on == date(2026, 10, 3)
    assert cbonds_events.read_snapshot(before).issuers == {}
