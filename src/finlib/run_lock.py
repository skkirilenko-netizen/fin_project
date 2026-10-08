"""Замок планового прогона: кто сейчас ходит к источникам от имени дня.

**Снимок рейтингов не ходит в Cbonds, пока идёт `daily_run`** (решение
владельца 07.10.2026). Прогон 10:00 с медленными выпусками и паузой повтора
стадий доходит до 11:15–11:30, а в 11:30 агент снимка запускается сам:
два процесса к одному источнику делят предел частоты и суточную норму,
не зная друг о друге. Агент ждёт снятия замка и берёт файл снимка дня
с диска.

**Второй прогон при занятом замке отказывается** до записи в журнал
прогонов — с тем, кто замок держит.

Замок — `flock` на файле: процесс, упавший как угодно, снимает его сам,
и протухшего замка не бывает. В файле — кто держит: номер процесса, время
и команда. Снимок, запущенный изнутри самого прогона (`runpy`, тот же
процесс), замок прогона за чужой не считает.
"""

import fcntl
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import IO

from finlib.config import settings

LOCK = settings.output_dir / "daily_run.lock"


class RunInProgressError(RuntimeError):
    """Замок держит другой процесс: прогон уже идёт."""


def _said(handle: IO[str]) -> str:
    """Кто держит замок — словами из файла; пусто — не записано."""
    handle.seek(0)
    raw = handle.read().strip()
    try:
        found = json.loads(raw)
    except ValueError:
        return raw or "держатель не записан"
    return f"процесс {found.get('pid')} с {found.get('started')} ({found.get('what')})"


def _pid(handle: IO[str]) -> int | None:
    """Номер процесса-держателя из файла замка; None — не записан."""
    handle.seek(0)
    try:
        return int(json.loads(handle.read()).get("pid"))
    except (ValueError, TypeError, AttributeError):
        return None


@contextmanager
def hold(path: Path | None = None) -> Iterator[None]:
    """Держит замок на время блока; занят — `RunInProgressError` с держателем."""
    path = path or LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    # «a+», а не «w»: до взятия замка файл чужой, и стирать держателя нельзя.
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunInProgressError(f"прогон уже идёт: замок держит {_said(handle)}") from None
        try:
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "pid": os.getpid(),
                    "started": f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z}",
                    "what": " ".join(sys.argv)[:200],
                },
                handle,
                ensure_ascii=False,
            )
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def held_elsewhere(path: Path | None = None) -> str | None:
    """Держатель замка, если это другой процесс; None — замок свободен либо наш."""
    path = path or LOCK
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            if _pid(handle) == os.getpid():
                return None
            return _said(handle)
        fcntl.flock(handle, fcntl.LOCK_UN)
    return None


def wait_free(
    poll_s: float, wait_max_s: float, path: Path | None = None
) -> str | None:
    """Ждёт снятия чужого замка; None — свободен, иначе держатель по истечении предела."""
    deadline = time.monotonic() + wait_max_s
    while True:
        holder = held_elsewhere(path)
        if holder is None:
            return None
        if time.monotonic() >= deadline:
            return holder
        time.sleep(max(min(poll_s, deadline - time.monotonic()), 0.0))
