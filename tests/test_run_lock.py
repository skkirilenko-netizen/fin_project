"""Замок планового прогона: второй прогон отказывается, снимок рейтингов ждёт.

Решения владельца 07.10 и 08.10.2026: снимок рейтингов в 11:30 не ходит
в Cbonds, пока идёт `daily_run`; второй `daily_run` при занятом замке
отказывается до записи в журнал прогонов, называя держателя.
"""

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from finlib import run_lock
from finlib.config import settings

sys.path.insert(0, str(settings.base_dir / "scripts"))

import daily_run  # noqa: E402
import ratings_snapshot  # noqa: E402

# Другой процесс берёт замок, говорит «взял» и держит до закрытия stdin.
HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from finlib import run_lock
    with run_lock.hold(Path(sys.argv[1])):
        print("взял", flush=True)
        sys.stdin.read()
    """
)


@pytest.fixture
def held(tmp_path: Path) -> Iterator[Path]:
    """Замок, который держит чужой процесс на время теста."""
    path = tmp_path / "daily_run.lock"
    other = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout is not None and other.stdout.readline().strip() == "взял"
        yield path
    finally:
        other.communicate("")


def test_a_free_lock_is_nobodys(tmp_path: Path) -> None:
    """Файла нет либо замок снят — держателя нет."""
    path = tmp_path / "daily_run.lock"
    assert run_lock.held_elsewhere(path) is None
    with run_lock.hold(path):
        pass
    assert run_lock.held_elsewhere(path) is None


def test_own_lock_is_not_foreign(tmp_path: Path) -> None:
    """Снимок изнутри прогона (тот же процесс) замок прогона чужим не считает."""
    path = tmp_path / "daily_run.lock"
    with run_lock.hold(path):
        assert run_lock.held_elsewhere(path) is None
        assert run_lock.wait_free(0.0, 0.0, path) is None


def test_a_second_run_is_refused_with_the_holder(held: Path) -> None:
    """Замок занят другим процессом — отказ с номером процесса держателя."""
    with (
        pytest.raises(run_lock.RunInProgressError, match="прогон уже идёт: замок держит процесс"),
        run_lock.hold(held),
    ):
        pass
    assert run_lock.held_elsewhere(held) is not None


def test_waiting_gives_up_at_the_limit(held: Path, monkeypatch) -> None:
    """Чужой замок не снят за предел ожидания — возвращается держатель."""
    monkeypatch.setattr(run_lock.time, "sleep", lambda pause: None)
    holder = run_lock.wait_free(1.0, 0.0, held)
    assert holder is not None and "процесс" in holder


def test_waiting_ends_when_the_run_ends(tmp_path: Path, monkeypatch) -> None:
    """Прогон закончился во время ожидания — ожидание кончается, держателя нет."""
    answers = iter(["процесс 1", "процесс 1", None])
    monkeypatch.setattr(run_lock, "held_elsewhere", lambda path=None: next(answers))
    pauses: list[float] = []
    monkeypatch.setattr(run_lock.time, "sleep", pauses.append)
    assert run_lock.wait_free(60.0, 3600.0, tmp_path / "x.lock") is None
    assert pauses == [60.0, 60.0]


def test_daily_run_refuses_before_the_journal(held: Path, monkeypatch, caplog) -> None:
    """Второй `daily_run` — код 2, строки `routing_run` нет, держатель в журнале."""
    monkeypatch.setattr(run_lock, "LOCK", held)
    opened: list[object] = []
    monkeypatch.setattr(daily_run, "_open_run", lambda *args: opened.append(args))
    monkeypatch.setattr(daily_run, "_day", lambda: opened.append("день") or 0)
    with caplog.at_level("ERROR"):
        assert daily_run.main() == 2
    assert opened == []
    assert "прогон уже идёт: замок держит процесс" in caplog.text


def test_ratings_snapshot_does_not_ask_while_the_run_goes(
    held: Path, monkeypatch, capsys
) -> None:
    """Снимок при идущем прогоне не обращается к Cbonds и по пределу выходит с ошибкой."""
    monkeypatch.setattr(run_lock, "LOCK", held)
    monkeypatch.setattr(settings, "run_lock_wait_max_s", 0.0)
    monkeypatch.setattr(run_lock.time, "sleep", lambda pause: None)
    asked: list[str] = []
    monkeypatch.setattr(ratings_snapshot.cbonds, "fetch", lambda *a, **k: asked.append("x"))
    monkeypatch.setattr(ratings_snapshot, "issuers", lambda: asked.append("перечень") or {})
    assert ratings_snapshot.main() == 1
    assert asked == []
    assert "в Cbonds не обращались" in capsys.readouterr().out
