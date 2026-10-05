"""События обязательства по полным снимкам и знанию на дату отчёта."""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.scoring.routing import load_routing
from finlib.sources import cbonds_events
from finlib.sources.cbonds_events import DefaultRecord, IssuerEvents
from finlib.sources.default_notifications import snapshots_at, timeline

sys.path.insert(0, str(settings.base_dir / "eval"))
import change_report_run as report  # noqa: E402


def _row(**changes: object) -> dict:
    """Синтетическое обязательство с пустыми датами объявления и исполнения."""
    return {"id": "record-1", "emission_id": "issue-1", "type_name_rus": "Купон",
            "status_name_rus": "Технический дефолт", "estimated_date": "2026-09-18",
            "default_date": "2026-10-02", "announcement_date": None,
            "actual_date": None, **changes}


def _write(root: Path, day: str, rows: list[dict], **meta: object) -> None:
    """Сохраняет явно синтетический снимок вне data проекта."""
    raw = {"items": rows, "count": len(rows), "total": len(rows), **meta}
    (root / f"defaults_ru_{day}.json").write_text(json.dumps(raw), encoding="utf-8")


def _events(root: Path, day: str) -> list[tuple[str, date]]:
    """Виды и первоначальные даты событий на дату отчёта."""
    until = date.fromisoformat(day)
    return [(item.kind, item.day) for item in timeline(snapshots_at(root, until), until)[0]]


@pytest.mark.parametrize("invalid", ["partial", "failed", "missing", "broken", "duplicate"])
def test_invalid_absence_is_not_evidence_of_first_appearance(
    tmp_path: Path, invalid: str,
) -> None:
    """Неуспешный или неполный снимок не образует отрицательную базу сравнения."""
    if invalid == "partial":
        _write(tmp_path, "2026-09-28", [], total=1)
    elif invalid == "failed":
        _write(tmp_path, "2026-09-28", [], success=False)
    elif invalid == "broken":
        (tmp_path / "defaults_ru_2026-09-28.json").write_text("{", encoding="utf-8")
    elif invalid == "duplicate":
        _write(tmp_path, "2026-09-28", [_row(), _row()])
    _write(tmp_path, "2026-09-29", [_row()])
    assert _events(tmp_path, "2026-09-29") == []


def test_missing_days_use_previous_successful_full_snapshot(tmp_path: Path) -> None:
    """Пропуски не дают точной даты появления; уведомление датируется наблюдением."""
    _write(tmp_path, "2026-09-25", [])
    _write(tmp_path, "2026-09-28", [], complete=False)
    _write(tmp_path, "2026-09-29", [_row()])
    assert _events(tmp_path, "2026-09-29") == [("first_seen", date(2026, 9, 29))]


def test_partial_status_is_not_a_transition(tmp_path: Path) -> None:
    """Переход фиксируется только в полном снимке после полного технического статуса."""
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-09-30", [_row(status_name_rus="Дефолт")], total=2)
    assert _events(tmp_path, "2026-09-30") == []
    _write(tmp_path, "2026-10-03", [_row(status_name_rus="Дефолт")])
    assert ("status_default", date(2026, 10, 3)) in _events(tmp_path, "2026-10-03")


def test_late_settlement_cannot_erase_grace_end(tmp_path: Path) -> None:
    """Поздно сообщённое исполнение раньше льготы не удаляет прежнее уведомление."""
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-10-02", [_row()])
    original = _events(tmp_path, "2026-10-02")
    _write(tmp_path, "2026-10-03", [_row(actual_date="2026-10-01")])
    assert _events(tmp_path, "2026-10-02") == original
    assert _events(tmp_path, "2026-10-03") == original
    corrections = timeline(snapshots_at(tmp_path, date(2026, 10, 3)), date(2026, 10, 3))[1]
    assert [(item.field, item.day) for item in corrections] == [("met", date(2026, 10, 3))]


def test_timely_known_settlement_suppresses_grace_notice(tmp_path: Path) -> None:
    """Исполнение, известное к концу льготы, не создаёт уведомление об её окончании."""
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-10-01", [_row(actual_date="2026-10-01")])
    assert _events(tmp_path, "2026-10-02") == []


def test_corrected_date_does_not_move_or_repeat_an_existing_event(tmp_path: Path) -> None:
    """Позднее исправление даты сохраняет первоначальную дату и единственный ключ."""
    _write(tmp_path, "2026-09-29", [_row()])
    original = _events(tmp_path, "2026-10-02")
    _write(tmp_path, "2026-10-03", [_row(default_date="2026-10-05")])
    assert _events(tmp_path, "2026-10-02") == original
    assert _events(tmp_path, "2026-10-06") == original
    assert len(timeline(snapshots_at(tmp_path, date(2026, 10, 6)), date(2026, 10, 6))[1]) == 1


def test_correction_before_event_uses_only_the_then_known_deadline(tmp_path: Path) -> None:
    """До наступления события действует уже известный уточнённый срок."""
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-10-01", [_row(default_date="2026-10-05")])
    assert _events(tmp_path, "2026-10-02") == []
    assert _events(tmp_path, "2026-10-05") == [("grace_end", date(2026, 10, 5))]


def test_status_and_grace_are_independent_and_each_is_once(tmp_path: Path) -> None:
    """Переход в день льготы не поглощает событие льготы и не повторяется после отката."""
    _write(tmp_path, "2026-09-28", [])
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-10-02", [_row(status_name_rus="Дефолт")])
    _write(tmp_path, "2026-10-03", [_row()])
    _write(tmp_path, "2026-10-04", [_row(status_name_rus="Дефолт")])
    assert _events(tmp_path, "2026-10-04") == [
        ("first_seen", date(2026, 9, 29)), ("status_default", date(2026, 10, 2)),
        ("grace_end", date(2026, 10, 2))]


def test_records_of_one_issue_are_not_deduplicated_together(tmp_path: Path) -> None:
    """Ключ записи источника отличает два обязательства одного выпуска."""
    _write(tmp_path, "2026-09-28", [])
    _write(tmp_path, "2026-09-29", [_row(), _row(id="record-2")])
    assert len(_events(tmp_path, "2026-09-29")) == 2


def test_offer_grace_end_is_also_not_a_new_nonpayment(tmp_path: Path) -> None:
    """Льгота просроченной оферты даёт отдельный конец срока, не купонный переход статуса."""
    _write(tmp_path, "2026-09-29", [_row(type_name_rus="Оферта",
                                         status_name_rus="Просрочка исполнения оферты")])
    assert _events(tmp_path, "2026-10-02") == [("grace_end", date(2026, 10, 2))]


def test_report_rebuild_keeps_as_of_status_and_separate_corrections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Печать прошлого не читает будущее; уточнение печатается без нового срочного события."""
    _write(tmp_path, "2026-09-28", [])
    _write(tmp_path, "2026-09-29", [_row()])
    _write(tmp_path, "2026-10-02", [_row()])
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    record = DefaultRecord("issue-1", "Купон", "Дефолт", None, None, None, None, None)
    monkeypatch.setattr(report, "events_of", lambda inn: IssuerEvents(inn=inn, records=(record,)))
    monkeypatch.setattr(report, "_named", lambda inn: "Тест")
    report._urgent(load_routing(), {"test": {}}, date(2026, 10, 1), date(2026, 10, 2))
    original = capsys.readouterr().out
    assert "льготный срок закончился 02.10.2026" in original
    assert "снимок записи 02.10.2026, статус «Технический дефолт»" in original
    _write(tmp_path, "2026-10-03", [_row(actual_date="2026-10-01",
                                         default_date="2026-10-05")])
    report._urgent(load_routing(), {"test": {}}, date(2026, 10, 1), date(2026, 10, 2))
    assert capsys.readouterr().out == original
    report._urgent(load_routing(), {"test": {}}, date(2026, 10, 2), date(2026, 10, 3))
    updated = capsys.readouterr().out
    assert "Срочное за сутки (02.10.2026 → 03.10.2026): 0" in updated
    assert "Уточнения сведений источника" in updated
    assert "дата первоначального уведомления сохраняется" in updated


@pytest.mark.parametrize("coverage", ["missing", "stale", "failed_today", "no_baseline"])
def test_report_does_not_call_unknown_events_an_empty_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    coverage: str,
) -> None:
    """Недоставленный снимок или отсутствующая база не превращаются в сутки без событий."""
    if coverage in {"stale", "failed_today"}:
        _write(tmp_path, "2026-10-01", [])
    if coverage == "failed_today":
        _write(tmp_path, "2026-10-02", [], success=False)
    if coverage == "no_baseline":
        _write(tmp_path, "2026-10-02", [])
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    monkeypatch.setattr(report, "events_of", lambda inn: IssuerEvents(inn=inn))
    report._urgent(load_routing(), {"test": {}}, date(2026, 10, 1), date(2026, 10, 2))
    text = capsys.readouterr().out
    assert "По доступным сведениям срочных событий не выявлено" in text
    assert "ни одного события" not in text
    assert "сутки без дефолтов" not in text
    if coverage == "missing":
        assert "их события не установлены" in text
    elif coverage == "no_baseline":
        assert "до начала окна нет" in text
    else:
        assert "Полного снимка обязательств за 02.10.2026 нет" in text
        assert "последний полный снимок — 01.10.2026" in text


def test_stale_snapshot_still_reports_a_known_grace_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Пробел доставки не скрывает ранее известный срок и не подтверждает свежесть статуса."""
    _write(tmp_path, "2026-10-01", [_row()])
    monkeypatch.setattr(cbonds_events, "CACHE", tmp_path)
    row = DefaultRecord("issue-1", "Купон", "Технический дефолт", None, None, None, None, None)
    monkeypatch.setattr(report, "events_of", lambda inn: IssuerEvents(inn=inn, records=(row,)))
    monkeypatch.setattr(report, "_named", lambda inn: "Тест")
    report._urgent(load_routing(), {"test": {}}, date(2026, 10, 1), date(2026, 10, 2))
    text = capsys.readouterr().out
    assert "льготный срок закончился 02.10.2026" in text
    assert "последний доступный снимок записи 01.10.2026" in text
    assert "Полного снимка обязательств за 02.10.2026 нет" in text
