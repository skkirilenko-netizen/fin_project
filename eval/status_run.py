"""Утренняя сводка одной командой: прошёл ли прогон и что он принёс.

    uv run python eval/status_run.py      # make status

**Только чтение.** Всё, что здесь печатается, уже записано другими: журнал
прогонов (`routing_run`), снимок рейтингов на диске, отчёт изменений дня.
Сводка ничего не пересчитывает — второй счёт «Срочного» или смен корзины
разошёлся бы с отчётом, который читает человек.

**Прогон по расписанию отличается от ручного временем старта**; правило
одно на сводку и ежедневный прогон — `finlib.schedule`.

**День засчитывается по прогону по расписанию этого дня** (решение владельца
29.09.2026). Повторные ручные прогоны того же дня счёт не обрывают и не
засчитываются, но показываются строкой с причиной из журнала: повтор — это
сведение о дне, а не его исход.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from finlib.db import fetch_all
from finlib.schedule import SCHEDULED_AT, is_scheduled

# За сколько дней назад показывать повторные прогоны: неделя — тот же
# горизонт, что у отчёта изменений.
REPEATS_DAYS = 7

RATINGS = Path("data/raw/cbonds/ratings")
OUTPUT = Path("data/output")

_RUNS = """
SELECT id, kind, as_of, status, started_at, finished_at, sources, note, code_version
FROM routing_run ORDER BY id DESC
"""


def _local(moment: datetime | None) -> datetime | None:
    """Время журнала в часовом поясе машины."""
    return moment.astimezone() if moment is not None else None


def _scheduled(run: dict) -> bool:
    """Стартовал ли прогон по расписанию: рабочий день, 10:00 и не позже допуска."""
    return run["kind"] == "run" and is_scheduled(run["started_at"])


def _repeats(runs: list[dict], now: datetime) -> list[str]:
    """Повторные прогоны дней, у которых есть прогон по расписанию: строками.

    Повторный не засчитывается и счёт не обрывает; причина берётся из журнала
    (`routing_run.note`), куда её пишет ежедневный прогон с `--reason`.
    """
    scheduled_days = {
        _local(run["started_at"]).date() for run in runs if _scheduled(run)
    }
    edge = now.date() - timedelta(days=REPEATS_DAYS)
    found: list[str] = []
    for run in sorted(runs, key=lambda item: item["started_at"]):
        started = _local(run["started_at"])
        if (
            run["kind"] != "run"
            or _scheduled(run)
            or started.date() not in scheduled_days
            or started.date() < edge
        ):
            continue
        found.append(
            f"{started:%d.%m}: повторный прогон {started:%H:%M}, "
            f"{run['status']} — {run.get('note') or 'причина не названа'}"
        )
    return found


def _clean(run: dict) -> bool:
    """Чистый прогон: завершён и ни одна доставка не отказала."""
    deliveries = run["sources"] or []
    return run["status"] == "done" and all(
        item.get("status") == "done" for item in deliveries
    )


def _running_today(runs: list[dict], now: datetime) -> dict | None:
    """Сегодняшний прогон по расписанию, который ещё идёт; None — такого нет.

    **Идущий прогон не чистый и не упавший — он не кончился.** Доставка
    рейтингов упирается в предел Cbonds и тянется за полдень, и сводка,
    снятая в это время, обрывала счёт на сегодняшнем дне. Прогон, начатый
    в другой день и так и не закрытый, сюда не относится: он оборвался.
    """
    today = [
        run
        for run in runs
        if _scheduled(run) and _local(run["started_at"]).date() == now.date()
    ]
    if any(_clean(run) for run in today):
        return None
    return next((run for run in today if run["status"] == "running"), None)


def _streak(runs: list[dict], now: datetime) -> tuple[int, str]:
    """Сколько рабочих дней подряд, считая назад, был чистый прогон по расписанию.

    Рабочий день без прогона по расписанию обрывает счёт так же, как упавший:
    отсутствие прогона — тоже не устойчивость. Сегодняшний день в счёт идёт,
    только если время прогона уже прошло и сам прогон завершён: идущий
    сегодняшний счёт не обрывает, а откладывает.
    """
    by_day: dict[date, list[dict]] = {}
    for run in runs:
        if _scheduled(run):
            by_day.setdefault(_local(run["started_at"]).date(), []).append(run)
    day = now.date()
    if now.time() < SCHEDULED_AT or day.weekday() >= 5 or _running_today(runs, now):
        day -= timedelta(days=1)
    count = 0
    while True:
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        found = by_day.get(day, [])
        if not found:
            return count, f"{day:%d.%m.%Y}: прогона по расписанию нет"
        if not any(_clean(run) for run in found):
            return count, f"{day:%d.%m.%Y}: прогон по расписанию не чистый"
        count += 1
        day -= timedelta(days=1)


def _changes() -> tuple[str, str, str, str]:
    """Срочное, опоздавшее и смены корзины из свежайшего отчёта изменений: файл, три числа.

    **Главный отчёт дня — отчёт прогона по расписанию** (`changes_<дата>.md`);
    повторный прогон пишет свой рядом (`changes_<дата>_<ЧЧММ>.md`). Берётся
    свежайшая дата, у неё — главный отчёт, а если его нет — последний
    повторный, и это сказано в имени.

    **Счётчик раздела — строки, то есть записи**: одна строка на запись
    источника с её переходами, у «Срочного» — ещё и рейтинговые действия.
    Сводка печатает его тем же словом, а не «событиями»: переходов в строке
    бывает несколько.

    **Смены корзины — заголовок «За сутки сменили корзину: N из M»**
    (`eval/change_report_run.py`); прежний «Сменили корзину: N из M» читается
    у старых отчётов. Без суточной точки сравнения отчёт пишет «Суточные
    смены не установлены» — это не ноль и не «раздела нет», и сводка
    говорит так же.
    """
    dated = re.compile(r"changes_(\d{4}-\d{2}-\d{2})(?:_(\d{4}))?\.md$")
    found = sorted(
        (match.group(1), match.group(2) is None, match.group(2) or "", path)
        for path in OUTPUT.glob("changes_*.md")
        if (match := dated.match(path.name))
    )
    if not found:
        return "отчёта изменений нет", "—", "—", "—"
    day = found[-1][0]
    mine = [item for item in found if item[0] == day]
    main_report = next((item for item in mine if item[1]), None)
    chosen = main_report or mine[-1]
    text = chosen[3].read_text(encoding="utf-8")
    urgent = re.search(r"^## Срочное[^:]*:\s*(\d+)", text, re.M)
    late = re.search(r"^## Доставлено с опозданием:\s*(\d+)", text, re.M)
    moved = re.search(
        r"^## (?:За сутки с|С)менили корзину:\s*(\d+)\s*из\s*(\d+)", text, re.M
    )
    unset = re.search(r"^## Суточные смены не установлены", text, re.M)
    label = chosen[3].name
    if main_report is None:
        label += " (повторного прогона: отчёта прогона по расписанию нет)"
    elif len(mine) > 1:
        label += f" (и повторных рядом: {len(mine) - 1})"
    return (
        label,
        urgent.group(1) if urgent else "раздела нет",
        late.group(1) if late else "раздела нет",
        f"{moved.group(1)} из {moved.group(2)}" if moved
        else "не установлены: суточной точки нет" if unset
        else "раздела нет",
    )


# Исходы попытки агента копии словами (`scripts/snapshots_backup_agent.py`).
BACKUP_OUTCOMES = {
    "no_disk": "диск не подключён, копия не сделана",
    "run_in_progress": "шёл плановый прогон, копия отложена",
    "failed": "копия не подтверждена — см. data/output/snapshots_backup.log",
}


def _backup() -> str:
    """Строка о резервной копии снимков: дата последней успешной и неудачная попытка после неё."""
    try:
        status = json.loads((OUTPUT / "snapshots_backup_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "**Резервная копия снимков** не делалась: исхода агента копии нет."
    success = status.get("last_success")
    said = (
        f"**Резервная копия снимков**: последняя успешная "
        f"{datetime.fromisoformat(success):%d.%m.%Y %H:%M}, файлов в описи "
        f"{status.get('files')}."
        if success
        else "**Резервная копия снимков**: успешной ещё не было."
    )
    outcome = status.get("last_outcome")
    if outcome and outcome != "ok" and status.get("last_attempt"):
        said += (
            f" Последняя попытка {datetime.fromisoformat(status['last_attempt']):%d.%m.%Y %H:%M}: "
            f"{BACKUP_OUTCOMES.get(outcome, outcome)}."
        )
    return said


def main() -> int:
    """Печатает утреннюю сводку."""
    today = date.today()
    runs = fetch_all(_RUNS, {})
    print(f"# Сводка на {datetime.now():%d.%m.%Y %H:%M}\n")
    last = next((run for run in runs if run["kind"] == "run"), None)
    if last is None:
        print("Прогонов в журнале нет.")
        return 1
    started, finished = _local(last["started_at"]), _local(last["finished_at"])
    deliveries = last["sources"] or []
    if isinstance(deliveries, str):
        deliveries = json.loads(deliveries)
    spent = sum(item.get("requests") or 0 for item in deliveries)
    print(
        f"**Последний прогон** № {last['id']} на {last['as_of']:%d.%m.%Y}: "
        f"{last['status']}, {'по расписанию' if _scheduled(last) else 'ручной'}, "
        f"{started:%d.%m %H:%M} — "
        + (f"{finished:%H:%M}" if finished else "не завершён")
        + f", запросов {spent}. {last['note'] or ''}\n"
    )
    if last["as_of"] != today:
        print(f"Прогона за {today:%d.%m.%Y} нет.\n")
    print("| Доставка | Состояние | Запросов | Секунд | Причина |")
    print("|---|---|---|---|---|")
    for item in deliveries:
        print(
            f"| {item.get('name')} | {item.get('status')} | {item.get('requests', '—')} "
            f"| {item.get('seconds', '—')} | {item.get('error') or ''} |"
        )
    snapshot = RATINGS / f"{today:%Y-%m-%d}.json"
    if snapshot.exists():
        taken = json.loads(snapshot.read_text(encoding="utf-8"))
        issuers = taken.get("issuers") or taken.get("snapshot") or {}
        print(
            f"\n**Снимок рейтингов за сегодня** есть: эмитентов {len(issuers)}, "
            f"отказов {len(taken.get('refused') or {})}."
        )
    else:
        print(f"\n**Снимка рейтингов за {today:%d.%m.%Y} нет.**")
    name, urgent, late, moved = _changes()
    print(f"\n**Отчёт изменений** {name}: срочное — строк {urgent}, доставлено "
          f"с опозданием — записей {late}, сменили корзину {moved}.")
    print(f"\n{_backup()}")
    now =datetime.now().astimezone()
    count, broke = _streak(runs, now)
    running = _running_today(runs, now)
    pending = (
        f"сегодняшний идёт с {_local(running['started_at']):%H:%M}, счёт по завершении; "
        if running is not None
        else ""
    )
    print(
        f"\n**Чистых прогонов по расписанию подряд: {count}** (ручные не считаются; "
        f"{pending}счёт оборвался — {broke})."
    )
    repeats = _repeats(runs, now)
    if repeats:
        print("\nПовторные прогоны (не засчитываются и счёт не обрывают):\n")
        for line in repeats:
            print(f"- {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
