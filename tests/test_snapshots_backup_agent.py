"""Агент резервной копии: диск не подключён — не сбой, успех — с проверкой описи.

Решение владельца 08.10.2026: копия на внешний SSD после планового прогона
(12:15 рабочих дней) и при подключении диска; без диска агент не падает
и пишет это в журнал, `make status` показывает дату последней успешной копии.
Все проверки — на синтетических временных файлах.
"""

import json
import plistlib
import sys
from datetime import datetime
from pathlib import Path

import pytest

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "scripts"))
sys.path.insert(0, str(settings.base_dir / "eval"))

import snapshots_backup as backup  # noqa: E402
import snapshots_backup_agent as agent  # noqa: E402
import status_run  # noqa: E402

PLIST = settings.base_dir / "scripts" / "ru.finanalysis.snapshots-backup.plist"


@pytest.fixture
def where(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Синтетический снимок, каталог копии и файл исхода во временном каталоге."""
    data = tmp_path / "data"
    ratings = data / "raw/cbonds/ratings"
    ratings.mkdir(parents=True)
    (ratings / "2090-01-01.json").write_text('{"issuers": {}}', encoding="utf-8")
    (ratings.parent / "defaults_ru_2090-01-01.json").write_text('{"items": []}', encoding="utf-8")
    monkeypatch.setattr(backup, "DATA", data)
    monkeypatch.setattr(agent, "STATUS", tmp_path / "output" / "snapshots_backup_status.json")
    monkeypatch.setattr(agent, "run_in_progress", lambda: False)
    target = tmp_path / "disk" / "fin-snapshots"
    target.mkdir(parents=True)
    return target


def _status() -> dict:
    return json.loads(agent.STATUS.read_text(encoding="utf-8"))


def test_no_disk_is_not_a_failure(where: Path, capsys) -> None:
    """Каталога назначения нет — исход 0, в журнале «диск не подключён», копии нет."""
    absent = where.parent / "unplugged" / "fin-snapshots"
    assert agent.main(["--to", str(absent)]) == 0
    assert "диск не подключён, копия не сделана" in capsys.readouterr().out
    assert _status()["last_outcome"] == "no_disk"
    assert "last_success" not in _status()
    assert not absent.exists()


def test_copy_is_verified_and_recorded(where: Path, capsys) -> None:
    """Успех — копия и `--verify` вернули 0; дата успеха и число файлов записаны."""
    assert agent.main(["--to", str(where)]) == 0
    out = capsys.readouterr().out
    assert "проверка описи (--verify)" in out and "файлов в описи 2" in out
    status = _status()
    assert status["last_outcome"] == "ok" and status["files"] == 2
    assert status["last_success"] == status["last_attempt"]
    assert backup.main(["--to", str(where), "--verify"]) == 0


def test_a_failed_verify_keeps_the_last_success(
    where: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка описи не прошла — исход 1, дата прежнего успеха не меняется."""
    assert agent.main(["--to", str(where)]) == 0
    before = _status()["last_success"]
    real = backup.main
    monkeypatch.setattr(
        agent.snapshots_backup, "main",
        lambda argv: 1 if "--verify" in argv else real(argv),
    )
    assert agent.main(["--to", str(where)]) == 1
    status = _status()
    assert status["last_outcome"] == "failed" and status["last_success"] == before


def test_a_running_daily_run_postpones_the_copy(
    where: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Идёт плановый прогон — копия откладывается без ошибки и без описи."""
    monkeypatch.setattr(agent, "run_in_progress", lambda: True)
    assert agent.main(["--to", str(where)]) == 0
    assert "копия отложена" in capsys.readouterr().out
    assert _status()["last_outcome"] == "run_in_progress"
    assert not (where / backup.MANIFEST).exists()


def test_the_agent_runs_after_the_daily_run_and_on_mount() -> None:
    """launchd: рабочие дни в 12:15, при подключении диска, путь копии параметром."""
    with PLIST.open("rb") as handle:
        plist = plistlib.load(handle)
    times = {(item["Weekday"], item["Hour"], item["Minute"])
             for item in plist["StartCalendarInterval"]}
    assert times == {(day, 12, 15) for day in range(1, 6)}
    assert plist["StartOnMount"] is True and plist["RunAtLoad"] is False
    args = plist["ProgramArguments"]
    assert args[args.index("--to") + 1] == "/Volumes/Transcend/fin-snapshots"
    assert "scripts/snapshots_backup_agent.py" in args
    assert plist["StandardOutPath"].endswith("data/output/snapshots_backup.log")


def test_status_prints_the_last_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сводка: дата последней успешной копии и неудачная попытка после неё."""
    monkeypatch.setattr(status_run, "OUTPUT", tmp_path)
    assert "не делалась" in status_run._backup()
    (tmp_path / "snapshots_backup_status.json").write_text(json.dumps({
        "last_success": datetime(2026, 10, 7, 12, 15, 3).astimezone().isoformat(),
        "files": 41,
        "last_attempt": datetime(2026, 10, 8, 12, 15, 2).astimezone().isoformat(),
        "last_outcome": "no_disk",
    }), encoding="utf-8")
    said = status_run._backup()
    assert "последняя успешная 07.10.2026 12:15, файлов в описи 41" in said
    assert "Последняя попытка 08.10.2026 12:15: диск не подключён" in said
