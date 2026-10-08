"""Отказ одной доставки остальные не останавливает.

28.09.2026 неполный снимок рейтингов (у машины пропала сеть) остановил весь
прогон, и без дефолтов, выпусков, отчётности и биржи остался день, в котором
они могли дойти. Маршрут строится по диску: недошедшая доставка оставляет
свой слой вчерашним, а не неверным, и остановка остальных вернее его
не делает.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "scripts"))

import daily_run  # noqa: E402

TODAY = date(2026, 9, 28)


@pytest.fixture
def _stages(monkeypatch) -> list[str]:
    """Доставки не ходят в сеть: рейтинги отказывают, остальные проходят."""
    ran: list[str] = []

    def run(stage: daily_run.Stage, dry: bool) -> dict:
        ran.append(stage.code)
        status = "offline" if stage.code == "ratings" else "done"
        return {"code": stage.code, "name": stage.name, "status": status}

    monkeypatch.setattr(daily_run, "_run_stage", run)
    monkeypatch.setattr(daily_run, "_fresh", lambda path, every, today: False)
    # «Нет сети» повторяется через паузу (`_retry`): здесь пауза не ждётся.
    monkeypatch.setattr(daily_run.time, "sleep", lambda pause: None)
    return ran


def test_no_stage_stops_the_others(_stages: list[str]) -> None:
    """Рейтинги без сети — дефолты, выпуски, отчётность и биржа всё равно идут."""
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    # Все по порядку, затем один повтор рейтингов без сети.
    assert _stages == [stage.code for stage in daily_run.STAGES] + ["ratings"]
    assert _stages[0] == "ratings"


def test_an_undelivered_stage_fails_the_run_by_name(_stages: list[str]) -> None:
    """Недоставка не молчит: прогон неудавшийся, недошедшее названо с причиной."""
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    said = daily_run._shortfall(delivered)
    assert said == "доставка неполна: снимок рейтингов — нет сети"
    assert daily_run._shortfall([{"name": "проба", "status": "done"}]) == ""
    assert daily_run._shortfall([{"name": "проба", "status": "cached"}]) == ""


def test_an_exhausted_quota_stops_only_its_source(
    _stages: list[str], monkeypatch
) -> None:
    """Нет нормы Cbonds — доставки Cbonds не начинаются, биржа идёт."""
    monkeypatch.setattr(daily_run, "_spent", lambda: daily_run.DAILY_QUOTA)
    delivered: list[dict] = []
    daily_run._deliver(TODAY, False, delivered)
    moex = [stage.code for stage in daily_run.STAGES if stage.source == "moex"]
    assert _stages == moex and moex
    skipped = [item for item in delivered if item["status"] == "no_quota"]
    assert {item["code"] for item in skipped} == {
        stage.code for stage in daily_run.STAGES if stage.source == "cbonds"
    }


def test_every_stage_names_a_known_source() -> None:
    """Источник объявлен у каждой доставки: по нему судится норма."""
    assert {stage.source for stage in daily_run.STAGES} == {"cbonds", "moex"}


def test_a_partial_ratings_snapshot_is_not_fresh(tmp_path: Path) -> None:
    """Прерванный снимок дня добирается повторным прогоном, а не считается сделанным."""
    folder = tmp_path / "ratings"
    folder.mkdir()
    path = folder / f"{date.today():%Y-%m-%d}.json"
    path.write_text(
        json.dumps({"issuers": {"1": []}, "refused": {"2": "не запрошен: проба"}}),
        encoding="utf-8",
    )
    assert not daily_run._fresh(path, 1, date.today())
    path.write_text(json.dumps({"issuers": {"1": []}, "refused": {}}), encoding="utf-8")
    assert daily_run._fresh(path, 1, date.today())
