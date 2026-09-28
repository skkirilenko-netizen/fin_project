"""Утренняя сводка: прогон по расписанию и счёт чистых дней подряд."""

import sys
from datetime import datetime

from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "eval"))

import status_run as run  # noqa: E402


def _run(started: datetime, status: str = "done", failed: bool = False) -> dict:
    """Запись журнала прогонов."""
    return {
        "kind": "run",
        "started_at": started.astimezone(),
        "status": status,
        "sources": [{"status": "failed" if failed else "done"}],
    }


def test_only_a_weekday_start_at_ten_is_scheduled() -> None:
    """Ручной прогон днём и прогон в субботу по расписанию не считаются."""
    assert run._scheduled(_run(datetime(2026, 9, 28, 10, 0, 1)))
    assert not run._scheduled(_run(datetime(2026, 9, 28, 15, 23)))
    assert not run._scheduled(_run(datetime(2026, 9, 26, 10, 0, 1)))


def test_a_failed_delivery_or_a_missing_day_breaks_the_streak() -> None:
    """Счёт идёт назад по рабочим дням и обрывается на первом нечистом либо пустом."""
    runs = [
        _run(datetime(2026, 9, 25, 10, 0, 2)),
        _run(datetime(2026, 9, 24, 10, 0, 2), failed=True),
        _run(datetime(2026, 9, 28, 15, 0)),  # ручной — не в счёт
    ]
    # Суббота: счёт с пятницы.
    count, broke = run._streak(runs, datetime(2026, 9, 26, 12, 0).astimezone())
    assert count == 1 and broke.startswith("24.09.2026")
    # Вторник до 10:00: сегодняшний прогон ещё не наступил, счёт с понедельника.
    count, broke = run._streak(runs, datetime(2026, 9, 29, 1, 0).astimezone())
    assert count == 0 and broke.startswith("28.09.2026")
    # Тот же вторник после 10:00 без прогона — обрыв на нём самом.
    count, broke = run._streak(runs, datetime(2026, 9, 29, 11, 0).astimezone())
    assert count == 0 and broke.startswith("29.09.2026")
