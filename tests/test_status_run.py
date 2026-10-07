"""Утренняя сводка: прогон по расписанию и счёт чистых дней подряд."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

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


def test_a_repeated_manual_run_neither_counts_nor_breaks() -> None:
    """День засчитывается по прогону по расписанию; повторный показан строкой.

    29.09.2026: № 25 по расписанию done, затем ручной повтор в 12:02 после
    исправления актуальности комплектов. Повтор — сведение о дне, а не его
    исход: засчитанный день остаётся засчитанным, и упавший повтор его
    не портит.
    """
    repeat = _run(datetime(2026, 9, 29, 12, 2), failed=True)
    repeat["note"] = "повторный прогон: причина — пересчёт после is_actual"
    runs = [
        _run(datetime(2026, 9, 29, 10, 0, 1)),
        repeat,
        _run(datetime(2026, 9, 28, 10, 0, 1)),
    ]
    now = datetime(2026, 9, 29, 13, 0).astimezone()
    count, broke = run._streak(runs, now)
    assert count == 2 and broke.startswith("25.09.2026")
    lines = run._repeats(runs, now)
    assert len(lines) == 1
    assert "12:02" in lines[0] and "пересчёт после is_actual" in lines[0]
    # Ручной прогон дня без прогона по расписанию повтором не назван:
    # повторять было нечего, и день по-прежнему не засчитан.
    lonely = [_run(datetime(2026, 9, 28, 15, 0))]
    assert run._repeats(lonely, now) == []
    count, broke = run._streak(lonely, now)
    assert count == 0 and broke.startswith("29.09.2026")


def test_a_running_scheduled_run_defers_the_count() -> None:
    """Идущий сегодняшний прогон счёт не обрывает: считается со вчерашнего дня.

    Доставка рейтингов упирается в предел Cbonds и тянется за полдень, и сводка
    в это время объявляла день «не чистым».
    """
    runs = [
        _run(datetime(2026, 9, 29, 10, 0, 2), status="running"),
        _run(datetime(2026, 9, 28, 10, 0, 2)),
        _run(datetime(2026, 9, 25, 10, 0, 2)),
    ]
    now = datetime(2026, 9, 29, 11, 30).astimezone()
    count, broke = run._streak(runs, now)
    assert count == 2 and broke.startswith("24.09.2026")
    assert run._running_today(runs, now) is runs[0]
    # Незакрытый прогон прошлого дня — оборвавшийся, а не идущий.
    stale = [_run(datetime(2026, 9, 28, 10, 0, 2), status="running")]
    assert run._running_today(stale, datetime(2026, 9, 29, 11, 0).astimezone()) is None
    count, broke = run._streak(stale, datetime(2026, 9, 29, 11, 0).astimezone())
    assert count == 0 and broke.startswith("29.09.2026")


def test_counters_are_read_as_printed_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сводка берёт счётчики строк из заголовков отчёта, не пересчитывая переходы."""
    (tmp_path / "changes_2090-01-03.md").write_text(
        "## Срочное за сутки (02.01.2090 → 03.01.2090): 1\n"
        "- Тест: купон, выпуск e, запись r: запись впервые обнаружена 02.01.2090; "
        "льготный срок закончился 03.01.2090\n"
        "## Доставлено с опозданием: 2\n- a\n- b\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run, "OUTPUT", tmp_path)
    assert run._changes() == ("changes_2090-01-03.md", "1", "2", "раздела нет")
