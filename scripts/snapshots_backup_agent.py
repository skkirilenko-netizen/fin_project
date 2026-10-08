"""Агент резервной копии снимков: копия на внешний диск и проверка описи.

    uv run python scripts/snapshots_backup_agent.py --to /Volumes/Transcend/fin-snapshots

Запускается launchd (`scripts/ru.finanalysis.snapshots-backup.plist`) после
планового прогона — 12:15 рабочих дней — и при подключении диска. Само
копирование и проверку делает `scripts/snapshots_backup.py`: здесь только
порядок, отказ без шума и запись исхода.

**Диска нет — не сбой.** Каталога назначения нет — в журнал «диск
не подключён, копия не сделана», исход 0: агент, падающий каждый день без
диска, учил бы не смотреть в его журнал. **Идёт плановый прогон — копия
откладывается**: снимок дня в это время дописывается, и копия недописанного
файла на следующем запуске дала бы «исходник изменился».

После копирования — `--verify`: независимая сверка описи и файлов копии.
Исход пишется в `data/output/snapshots_backup_status.json`; дату последней
успешной копии печатает `make status`. Успешна копия, у которой оба шага
вернули 0.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import snapshots_backup  # noqa: E402

STATUS = Path("data/output/snapshots_backup_status.json")

NO_DISK = "no_disk"
RUN_IN_PROGRESS = "run_in_progress"
FAILED = "failed"
OK = "ok"


def run_in_progress() -> bool:
    """Идёт ли плановый прогон: процесс `scripts/daily_run.py` жив."""
    found = subprocess.run(
        ["pgrep", "-f", "scripts/daily_run.py"], capture_output=True, check=False
    )
    return found.returncode == 0


def _say(text: str) -> None:
    """Строка журнала агента со временем: журнал копится, и без времени его не прочесть."""
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}", flush=True)


def _record(outcome: str, where: Path, files: int | None = None) -> None:
    """Пишет исход попытки; дата успеха меняется только успехом."""
    try:
        status = json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {}
    moment = datetime.now().astimezone().isoformat(timespec="seconds")
    status |= {"last_attempt": moment, "last_outcome": outcome, "where": str(where)}
    if outcome == OK:
        status |= {"last_success": moment, "files": files}
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Копирует и проверяет; диска нет либо идёт прогон — 0 и запись в журнал."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", type=Path, required=True)
    args = parser.parse_args(argv)
    where: Path = args.to
    if not where.is_dir():
        _say(f"диск не подключён, копия не сделана: каталога {where} нет")
        _record(NO_DISK, where)
        return 0
    if run_in_progress():
        _say("идёт плановый прогон, копия отложена до следующего запуска")
        _record(RUN_IN_PROGRESS, where)
        return 0
    _say(f"копия в {where}")
    if snapshots_backup.main(["--to", str(where)]) != 0:
        _say("копия не подтверждена: подробности выше")
        _record(FAILED, where)
        return 1
    _say("проверка описи (--verify)")
    if snapshots_backup.main(["--to", str(where), "--verify"]) != 0:
        _say("проверка описи не прошла: подробности выше")
        _record(FAILED, where)
        return 1
    files = len(snapshots_backup.read_manifest(where))
    _say(f"копия сделана и проверена: файлов в описи {files}")
    _record(OK, where, files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
