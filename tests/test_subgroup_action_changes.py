"""Предписанное действие сохраняется в истории независимо от переименований."""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.config import settings
from finlib.db import PgConnection, execute
from finlib.metrics.ifrs import MetricValue
from finlib.scoring.routing import load_routing, route
from finlib.scoring.routing_store import RoutingRow
from finlib.sources.cbonds_events import IssuerEvents

sys.path.insert(0, str(settings.base_dir / "eval"))
import change_report_run as report  # noqa: E402
from routing_backfill_run import decision_values  # noqa: E402


def _point(subgroup: str, action: str, text: str) -> dict:
    """Точка с сохранённым смыслом и формулировкой действия на свой день."""
    return {"basket": "attention", "subgroup": subgroup, "inputs": {"action": {
        "code": action, "text": text, "subgroup": subgroup, "subgroup_name": subgroup}}}


def test_subgroup_change_prints_only_changed_action(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Смена смысла действия печатается, техническое переименование — нет."""
    monkeypatch.setattr(report, "_named", lambda inn: "Тест")
    before = _point("data_gap", "collect_data", "добрать данные")
    after = _point("value_risk", "attention_values", "смотреть")
    report._subgroup_changes(load_routing(), {"1": before}, {"1": after})
    printed = capsys.readouterr().out
    assert "сменилось действие при прежней корзине: 1 из 1" in printed
    assert "«добрать данные» → «смотреть»" in printed
    renamed = _point("new_technical_name", "collect_data", "другая редакция текста")
    report._subgroup_changes(load_routing(), {"1": before}, {"1": renamed})
    assert "сменилось действие при прежней корзине: 0 из 1" in capsys.readouterr().out


def test_missing_historical_action_is_explicit_not_guessed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Старая точка без кода не переинтерпретируется нынешней методикой."""
    old = {"basket": "attention", "subgroup": "data_gap", "inputs": {}}
    new = _point("value_risk", "attention_values", "смотреть")
    report._subgroup_changes(load_routing(), {"synthetic": old}, {"synthetic": new})
    text = capsys.readouterr().out
    assert "Смена действия не установлена" in text
    assert "сменилось действие при прежней корзине: 0" in text


def test_basket_change_is_not_duplicated_as_subgroup_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Смена корзины остаётся в своей таблице, не дублируется сообщением подгруппы."""
    old = _point("data_gap", "collect_data", "добрать данные")
    new = {**_point("event_risk", "review_event_first", "разбирать первым"), "basket": "review"}
    report._subgroup_changes(load_routing(), {"synthetic": old}, {"synthetic": new})
    assert "сменилось действие при прежней корзине: 0" in capsys.readouterr().out


def test_saved_action_survives_new_wording_of_the_policy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Повторная печать берёт исторические формулировки, не текст новой методики."""
    monkeypatch.setattr(report, "_named", lambda inn: "Тест")
    old = _point("data_gap", "collect_data", "добрать данные")
    new = _point("value_risk", "attention_values", "смотреть")
    policy = load_routing()
    changed = policy.model_copy(update={"baskets": tuple(
        basket.model_copy(update={"groups": tuple(
            group.model_copy(update={"name": "переименовано", "action": "новая редакция"})
            for group in basket.groups)}) for basket in policy.baskets)})
    report._subgroup_changes(policy, {"1": old}, {"1": new})
    original = capsys.readouterr().out
    report._subgroup_changes(changed, {"1": old}, {"1": new})
    assert capsys.readouterr().out == original


def test_route_to_history_to_report_preserves_action(
    db_conn: PgConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Боевой вердикт и общий писатель истории доходят до отдельной строки отчёта."""
    policy = load_routing()
    healthy = tuple(MetricValue(code=code, name=code, group="synthetic", in_scoring=True,
                               value=Decimal(value)) for code, value in [
        ("net_debt_ebitda", "1"), ("equity_ratio", "0.6"), ("cur_liq", "2.5")])
    old = route((), unit="тыс. руб.", quarantined=False, today=date(2026, 10, 2),
                latest_annual=date(2025, 12, 31), routing=policy)
    new = route(healthy, unit="тыс. руб.", quarantined=False, today=date(2026, 10, 3),
                latest_annual=date(2025, 12, 31), stop_factors=("negative_nwc",), routing=policy)
    assert old.basket == new.basket == "attention"
    assert old.action_codes[0] == "collect_data" and new.action_codes[0] == "attention_values"
    inn = "0000000071"
    days = (date(2091, 1, 1), date(2091, 1, 2))
    execute("DELETE FROM routing_history WHERE inn = %(inn)s", {"inn": inn}, conn=db_conn)
    for day, verdict in zip(days, (old, new), strict=True):
        row = RoutingRow(inn, "Синтетический эмитент", date(2025, 12, 31), verdict, ())
        inputs = decision_values(row)
        assert inputs["action"]["code"] == verdict.action_codes[0]
        execute(
            "INSERT INTO routing_history (inn, as_of, kind, basket, subgroup, grounds, "
            "grounds_all, inputs, fingerprint, report_date) VALUES (%(inn)s, %(day)s, "
            "'backfill', %(basket)s, %(group)s, %(grounds)s, %(grounds)s, "
            "%(inputs)s, 'synthetic', '2025-12-31')",
            {"inn": inn, "day": day, "basket": verdict.basket, "group": verdict.subgroup,
             "grounds": list(verdict.grounds), "inputs": json.dumps(inputs)}, conn=db_conn)
    was = {inn: report._read(db_conn, "backfill", days[0])[inn]}
    now = {inn: report._read(db_conn, "backfill", days[1])[inn]}
    monkeypatch.setattr(report.cbonds_events, "CACHE", tmp_path)
    monkeypatch.setattr(report, "events_of", lambda inn: IssuerEvents(inn=inn))
    monkeypatch.setattr(report, "_named", lambda inn: "Тест")
    report._report(policy, "backfill", days[0], days[1], was, now, set(), days[0], [], was)
    text = capsys.readouterr().out
    assert "За сутки сменили корзину: 0 из 1" in text
    assert "сменилось действие при прежней корзине: 1 из 1" in text
    assert "действие «добрать данные» → «смотреть»" in text
