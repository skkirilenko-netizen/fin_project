"""Синтетическая история в findb_test до окончательной печати отчёта."""

import sys
from datetime import date

import pytest

from finlib.config import settings
from finlib.db import PgConnection, execute
from finlib.scoring.routing import load_routing
from finlib.sources.cbonds_events import IssuerEvents

sys.path.insert(0, str(settings.base_dir / "eval"))
import change_report_run as report  # noqa: E402

BEFORE = date(2090, 1, 1)
AFTER = date(2090, 1, 2)


def _point(conn: PgConnection, inn: str, when: date, basket: str,
           grounds: list[str], all_grounds: list[str], fingerprint: str) -> None:
    """Пишет синтетическую точку внутри откатываемой тестовой транзакции."""
    execute(
        "INSERT INTO routing_history (inn, as_of, kind, basket, grounds, grounds_all, "
        "fingerprint, report_date) VALUES (%(inn)s, %(day)s, 'backfill', %(basket)s, "
        "%(grounds)s, %(all)s, %(fp)s, '2089-12-31')",
        {"inn": inn, "day": when, "basket": basket, "grounds": grounds,
         "all": all_grounds, "fp": fingerprint}, conn=conn,
    )


def test_history_to_report_keeps_senior_changes_and_perimeter(
    db_conn: PgConnection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Печать не теряет новые и ушедшие основания младшей корзины и вход/выход."""
    inns = [f"000000008{i}" for i in range(6)]
    execute("DELETE FROM routing_history WHERE inn = ANY(%(inns)s)", {"inns": inns},
            conn=db_conn)
    # 0 — ухудшение, 1 — улучшение; 2/3 — событие младшей корзины
    # при неизменном Разборе по рынку, 4/5 — вход/выход истории.
    for i, (old, new) in enumerate([
        ("attention", "review"), ("review", "attention"),
        ("review", "review"), ("review", "review"),
    ]):
        old_ground = "market_spread_extreme" if old == "review" else "refinancing_gap"
        new_ground = "market_spread_extreme" if new == "review" else "refinancing_gap"
        _point(db_conn, inns[i], BEFORE, old, [old_ground],
               [old_ground] + (["payment_missed"] if i == 3 else []), "old")
        _point(db_conn, inns[i], AFTER, new, [new_ground],
               [new_ground] + (["payment_missed"] if i == 2 else []), "new")
    _point(db_conn, inns[4], AFTER, "attention", ["refinancing_gap"], ["refinancing_gap"], "new")
    _point(db_conn, inns[5], BEFORE, "clear", [], [], "old")
    was = {inn: row for inn, row in report._read(db_conn, "backfill", BEFORE).items()
           if inn in inns}
    now = {inn: row for inn, row in report._read(db_conn, "backfill", AFTER).items() if inn in inns}
    monkeypatch.setattr(report, "events_of", lambda inn, **kwargs: IssuerEvents(inn=inn))
    monkeypatch.setattr(report, "risk_sectors", lambda: {})
    monkeypatch.setattr(report, "_named", lambda inn: f"Тест ({inn})")
    report._report(load_routing(), "backfill", BEFORE, AFTER, was, now, set(), BEFORE, [])
    text = capsys.readouterr().out
    assert "За сутки сменили корзину: 2 из 5" in text
    improved = next(line for line in text.splitlines() if f"({inns[1]}) |" in line)
    assert "ушло основание:" in improved and "p99" in improved
    assert "Новое основание без смены корзины: 1" in text
    assert "Ушло основание без смены корзины: 1" in text
    assert f"Тест ({inns[2]}): неплатёж" in text
    assert f"Тест ({inns[3]}): ушло основание: неплатёж" in text
    assert "Вошли в периметр: 1   Вышли: 1" in text
    assert f"вошёл Тест ({inns[4]})" in text
    assert f"вышел Тест ({inns[5]})" in text and "причина выхода из истории не установлена" in text
    assert "Новое основание без смены корзины: 1 (за неделю 01.01.2090 → 02.01.2090)" in text
    assert "Вошли в периметр: 1   Вышли: 1 (за неделю 01.01.2090 → 02.01.2090)" in text


def test_out_of_scope_return_names_the_removed_type_ground() -> None:
    """Возврат из очереди типов объясняется снятием типа, не текущим рейтингом."""
    before = {"basket": "out_of_scope",
              "grounds": ["out_of_scope_issuer", "rating_outlook_adverse"]}
    after = {"basket": "attention", "grounds": ["rating_outlook_adverse"]}
    assert report._decisive(load_routing(), before, after) == {"out_of_scope_issuer"}


def test_removed_payment_does_not_explain_a_removed_market_ground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ушедшие платежи не становятся причиной снятия рыночного основания."""
    monkeypatch.setattr(report, "events_of", lambda inn, **kwargs: IssuerEvents(inn=inn))
    monkeypatch.setattr(report, "risk_sectors", lambda: {})
    before = {"basket": "review", "report_date": BEFORE,
              "grounds": ["market_spread_extreme", "refinancing_gap"]}
    after = {"basket": "attention", "report_date": BEFORE,
             "grounds": ["rating_outlook_adverse"]}
    text = report._why_decisive(load_routing(), "0000000080", BEFORE, AFTER, before, after)
    assert "ушло основание:" in text and "p99" in text
    assert "по графику" not in text


def test_missing_daily_baseline_is_not_printed_as_zero_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Отсутствующая точка расписания явно блокирует суточное сравнение."""
    monkeypatch.setattr(report, "events_of", lambda inn, **kwargs: IssuerEvents(inn=inn))
    monkeypatch.setattr(report, "_named", lambda inn: inn)
    row = {"basket": "clear", "grounds": [], "grounds_all": [],
           "fingerprint": "same", "report_date": BEFORE}
    report._report(load_routing(), "backfill", BEFORE, AFTER,
                   {"0000000080": row}, {"0000000080": row}, set(), BEFORE, [], {})
    text = capsys.readouterr().out
    assert "Суточные смены не установлены" in text
    assert "За сутки сменили корзину: 0" not in text
