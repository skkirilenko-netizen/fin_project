"""Ежедневный прогон целиком: доставка, маршрут, список, отчёт изменений.

**Один агент, а не пять.** Доставки идут по очереди в одном процессе — так
считается бюджет запросов и так виден отказ: `cbonds.pace.requested` живёт
в процессе, и разнесённые по скриптам доставки о расходе друг друга не знают.

**При отказе источника — ранняя остановка и запись, а не молчание.** Доставка
прекращается, прогон помечается неудавшимся, а маршрут всё равно строится
по тому, что на диске: список нужен и в день отказа. Но отказ при этом стоит
**в начале отчёта**, потому что «изменений нет» при недошедшей доставке
и «изменений нет» при полной — разные сведения, а выглядят одинаково.

**Не всякая доставка ежедневная.** Рейтинговое действие приходит днём, перевод
биржи — днём; перечень выпусков и отчётность так часто не меняются, и тратить
на них суточную норму запросов незачем. Частота объявлена у каждой доставки
вместе с причиной.

    uv run python scripts/daily_run.py            # полный прогон
    uv run python scripts/daily_run.py --dry      # без обращений к источникам
"""

import json
import logging
import runpy
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from routing_backfill_run import decision_values

from finlib.db import connection, execute, fetch_all
from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import routing_rows
from finlib.sources import cbonds
from finlib.sources.market import series as market_series
from finlib.version import code_version

logger = logging.getLogger(__name__)

# **Суточная норма запросов — предел источника, а не наше суждение.**
# Объявлена подпиской Cbonds: 10 000 обращений в сутки. Прогон проверяет
# её **до** запроса, а не после: превышение обрывает доставку до конца суток,
# и тогда день остаётся без данных вовсе.
DAILY_QUOTA = 10_000

# Запас, ниже которого доставка не начинается: стадия, начатая на остатке
# в сотню запросов, оборвётся на середине и оставит перечень наполовину
# обновлённым — хуже, чем не начатая.
RESERVE = 200

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output"


@dataclass(frozen=True, slots=True)
class Stage:
    """Доставка: чем занята, как часто нужна и что значит её отказ.

    `every` — через сколько дней доставка нужна снова; 1 — ежедневно.
    `blocking` — прекращает ли её отказ остальные доставки: отказ источника
    целиком прекращает, отказ одной доставки из многих — нет.
    """

    code: str
    name: str
    script: str
    every: int
    blocking: bool
    why: str


STAGES: tuple[Stage, ...] = (
    Stage(
        code="ratings",
        name="снимок рейтингов",
        script="scripts/ratings_snapshot.py",
        every=1,
        blocking=True,
        why=(
            "История рейтингов у подписки закрыта, и снимок — единственный "
            "путь к ней: пропущенный день не восстанавливается ничем."
        ),
    ),
    Stage(
        code="moex",
        name="сектор повышенного риска биржи",
        script="scripts/moex_fetch.py",
        every=1,
        blocking=False,
        why=(
            "Перевод в режим «Д» — событие дня с датой; биржа отвечает "
            "без подписки, и на суточную норму Cbonds это не тратится."
        ),
    ),
    # **Торги биржи — ежедневная доставка, и пропущенный день не добирается
    # задним числом дешевле, чем сразу**: глубина хранения у ISS годы, но
    # каждый день — отдельный запрос, и накопленный пропуск стоит столько же
    # дней. На суточную норму Cbonds это не тратится — источник другой.
    Stage(
        code="market",
        name="дневной срез торгов и кривая ОФЗ",
        script="scripts/moex_market_fetch.py",
        every=1,
        blocking=False,
        why=(
            "Рыночные основания маршрута считаются по ряду спредов и цен, "
            "и ряд этот наращивается днями: пропущенный день оставляет "
            "в нём дыру, а признак — подтверждение «7 из 10» — считает "
            "наблюдения, а не календарь."
        ),
    ),
    Stage(
        code="emissions",
        name="выпуски эмитентов",
        script="scripts/emissions_fetch.py",
        every=7,
        blocking=False,
        why=(
            "Перечень выпусков меняется размещениями и погашениями — "
            "не ежедневно; 977 запросов в день тратили бы десятую часть "
            "нормы на то, что почти не движется."
        ),
    ),
)


def _spent() -> int:
    """Сколько запросов к источнику потрачено с начала процесса."""
    return cbonds.pace.requested


def _fresh(path: Path, every: int, today: date) -> bool:
    """Свежа ли доставка: моложе ли её файл объявленной частоты."""
    if not path.exists():
        return False
    when = date.fromtimestamp(path.stat().st_mtime)
    return (today - when).days < every


def _run_stage(stage: Stage, dry: bool) -> dict:
    """Выполняет доставку и возвращает её исход вместе с расходом запросов."""
    before = _spent()
    started = time.monotonic()
    said: dict = {"code": stage.code, "name": stage.name, "every": stage.every}
    if dry:
        said |= {"status": "skipped", "why": "прогон без обращений к источникам"}
        return said
    argv = sys.argv
    try:
        sys.argv = [stage.script]
        runpy.run_path(str(ROOT / stage.script), run_name="__main__")
        said |= {"status": "done"}
    except SystemExit as stop:
        code = int(stop.code or 0)
        said |= {"status": "done" if code == 0 else "failed", "exit": code}
    except Exception as failure:  # noqa: BLE001
        said |= {"status": "failed", "error": str(failure)[:200]}
    finally:
        sys.argv = argv
    said |= {
        "requests": _spent() - before,
        "seconds": round(time.monotonic() - started, 1),
    }
    return said


_RUN = """
INSERT INTO routing_run (kind, as_of, status, code_version, methodology, sources)
VALUES ('run', %(as_of)s, 'running', %(code)s, %(methodology)s, %(sources)s)
RETURNING id
"""

_DONE = """
UPDATE routing_run
SET status = %(status)s, finished_at = now(), sources = %(sources)s, note = %(note)s
WHERE id = %(id)s
"""

_POINT = """
INSERT INTO routing_history
       (run_id, inn, as_of, kind, standard, basket, subgroup, grounds,
        grounds_all, inputs, fingerprint, report_date)
VALUES (%(run)s, %(inn)s, %(as_of)s, 'run', %(standard)s, %(basket)s,
        %(subgroup)s, %(grounds)s, %(grounds_all)s, %(inputs)s,
        %(fingerprint)s, %(report_date)s)
ON CONFLICT (inn, as_of, kind) DO UPDATE SET
    run_id = EXCLUDED.run_id,
    standard = EXCLUDED.standard,
    basket = EXCLUDED.basket,
    subgroup = EXCLUDED.subgroup,
    grounds = EXCLUDED.grounds,
    grounds_all = EXCLUDED.grounds_all,
    inputs = EXCLUDED.inputs,
    fingerprint = EXCLUDED.fingerprint,
    report_date = EXCLUDED.report_date
"""


def main() -> int:
    """Проводит день целиком: доставка, маршрут, история, список, отчёт."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    dry = "--dry" in sys.argv
    today = date.today()
    routing = load_routing()
    delivered: list[dict] = []
    stopped = ""

    for stage in STAGES:
        if _fresh(_marker(stage), stage.every, today):
            delivered.append(
                {
                    "code": stage.code,
                    "name": stage.name,
                    "status": "fresh",
                    "why": f"доставка моложе {stage.every} дн.",
                }
            )
            continue
        if _spent() > DAILY_QUOTA - RESERVE:
            stopped = f"суточная норма запросов исчерпана на доставке «{stage.name}»"
            delivered.append(
                {"code": stage.code, "name": stage.name, "status": "no_quota"}
            )
            break
        said = _run_stage(stage, dry)
        delivered.append(said)
        if said["status"] == "failed" and stage.blocking:
            stopped = f"источник отказал на доставке «{stage.name}»"
            break

    # **Маршрут строится и в день отказа.** Список нужен и тогда; но отказ
    # стоит в отчёте первым, а не молчит.
    with connection() as conn:
        run_id = fetch_all(
            _RUN,
            {
                "as_of": today,
                "code": code_version(),
                "methodology": json.dumps({"routing": routing.version}),
                "sources": json.dumps(delivered, ensure_ascii=False),
            },
            conn=conn,
        )[0]["id"]
        conn.commit()
        # **Ряд спредов пересчитывается после доставки, а не читается
        # вчерашний.** Доставка кладёт новый срез, и маршрут, посчитанный
        # по старому ряду, объявил бы вчерашнее состояние сегодняшним.
        # Пересчёт идёт по диску и сети не касается вовсе.
        market_series(refresh=True)
        rows, counts = routing_rows(conn, today)
        for row in rows:
            execute(
                _POINT,
                {
                    "run": run_id,
                    "inn": row.inn,
                    "as_of": today,
                    "standard": row.standard.value if row.standard else None,
                    "basket": row.verdict.basket,
                    "subgroup": row.verdict.subgroup,
                    "grounds": list(row.verdict.grounds),
                    # Перечень всех сработавших и величины решения пишет
                    # тот же код, что и пересчёт: два способа записать одну
                    # историю разошлись бы составом, и сравнить их было бы
                    # нечем — наблюдение с пересчётом не сравнивается.
                    "grounds_all": sorted(
                        {item.ground for item in row.verdict.findings}
                    ),
                    "inputs": json.dumps(decision_values(row)),
                    "fingerprint": row.fingerprint,
                    "report_date": row.report_date,
                },
                conn=conn,
            )
        execute(
            _DONE,
            {
                "id": run_id,
                "status": "failed" if stopped else "done",
                "sources": json.dumps(delivered, ensure_ascii=False),
                "note": stopped or f"эмитентов {len(rows)}",
            },
            conn=conn,
        )
        conn.commit()

    # Список и выгрузка — тем же кодом, что руками: второй путь к странице
    # разошёлся бы с первым.
    for name in ("watchlist_run.py", "watchlist_csv.py"):
        argv = sys.argv
        try:
            sys.argv = [name]
            runpy.run_path(str(ROOT / "eval" / name), run_name="__main__")
        except SystemExit:
            pass
        finally:
            sys.argv = argv

    report = OUT / f"changes_{today:%Y-%m-%d}.md"
    argv, out = sys.argv, sys.stdout
    try:
        sys.argv = ["change_report_run.py", "--kind", "run"]
        with report.open("w", encoding="utf-8") as handle:
            sys.stdout = handle
            runpy.run_path(
                str(ROOT / "eval" / "change_report_run.py"), run_name="__main__"
            )
    except SystemExit:
        pass
    finally:
        sys.argv, sys.stdout = argv, out

    print(f"\nПрогон {today}: эмитентов {len(rows)}, запросов {_spent()}")
    for said in delivered:
        print(f"  {said['name']}: {said['status']}", end="")
        if said.get("requests"):
            print(f", запросов {said['requests']}", end="")
        print()
    if stopped:
        print(f"  ОСТАНОВКА: {stopped}")
    print(f"  отчёт изменений: {report}")
    print(f"  строк маршрута: {counts.get('эмитентов', 0)}")
    return 1 if stopped else 0


def _marker(stage: Stage) -> Path:
    """Файл, по возрасту которого видно, когда доставка была последней.

    У каждой доставки он свой и настоящий — не метка о запуске, а сам
    её результат: метка сказала бы «прогон был», а нужен ответ «данные есть».
    """
    if stage.code == "ratings":
        return ROOT / "data" / "raw" / "cbonds" / "ratings" / f"{date.today():%Y-%m-%d}.json"
    if stage.code == "moex":
        return ROOT / "data" / "raw" / "moex" / "bonds_traded.json"
    # Срез торгов вчерашнего дня: сегодняшнего у биржи ещё нет, и ждать
    # его — значит не доставить ничего.
    if stage.code == "market":
        return (
            ROOT / "data" / "raw" / "moex"
            / f"xsec_{date.today() - timedelta(days=1):%Y-%m-%d}.json"
        )
    return ROOT / "data" / "raw" / "cbonds" / "emissions_7736050003.json"


if __name__ == "__main__":
    sys.exit(main())
