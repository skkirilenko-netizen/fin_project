"""История корзин считается назад: маршрут на каждую дату за последний год.

**Десяти дней вперёд не ждём.** Маршрут пересчитывается на каждую дату сетки
по данным, известным **на ту дату**: отчётность — с даты раскрытия, события —
с даты события, рейтинговые действия — со своей даты. История получается
сразу, и на ней видно, как выглядит отчёт изменений в обычный день и как
часто корзины дребезжат.

**У пересчёта есть свойство, которого нет у наблюдения**: код и методика
на всём протяжении одни, поэтому всякое изменение в пересчитанной истории
**по определению от данных**. Это и делает её годной для проверки отчёта.

**И есть то, чего у него нет.** Признаки карточки истории не имеют вовсе —
статус эмитента, отрасль, признак финансирующей структуры, статус выпуска, —
и в пересчёт они не идут: сегодняшний признак дефолта, применённый к прошлому
году, объявил бы эмитента дефолтным весь год. Доля оснований, восстановленных
нынешними данными, печатается: пересчёт, не сказавший о своей неполноте,
выдаёт её за наблюдение.

    uv run python eval/routing_backfill_run.py            # без записи
    uv run python eval/routing_backfill_run.py --write    # с записью в историю

**Замер не считает сам**: корзину даёт боевая маршрутизация
(`scoring.routing_store.routing_rows`) с названной датой.
"""

import json
import logging
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, execute, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds_events import default_records  # noqa: E402
from finlib.sources.moex_risk import risk_sectors  # noqa: E402
from finlib.version import code_version  # noqa: E402

logger = logging.getLogger(__name__)

_RUN = """
INSERT INTO routing_run (kind, as_of, status, code_version, methodology, sources)
VALUES ('backfill', %(as_of)s, 'running', %(code)s, %(methodology)s, %(sources)s)
RETURNING id
"""

_DONE = """
UPDATE routing_run SET status = %(status)s, finished_at = now(), note = %(note)s
WHERE id = %(id)s
"""

_POINT = """
INSERT INTO routing_history
       (run_id, inn, as_of, kind, standard, basket, subgroup, grounds,
        grounds_all, inputs, fingerprint, report_date)
VALUES (%(run)s, %(inn)s, %(as_of)s, 'backfill', %(standard)s, %(basket)s,
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


# **Дни, когда становится виден промежуточный комплект** (фаза 5-бис): с него
# меняется база маршрута, и точка сетки ставится на этот день — иначе смена
# базы датировалась бы ближайшей неделей. День — появление записи у агрегатора,
# срок закона — только там, где его нет; правило то же, что у выборки базы
# (`routing_store._LATEST_INTERIM`).
_INTERIM_VISIBLE = """
SELECT DISTINCT COALESCE(
    (s.meta->>'disclosed_on')::date,
    (s.meta->'cbonds'->>'created_at')::date,
    s.period_end + CASE s.standard WHEN 'ifrs' THEN %(ifrs)s ELSE %(rsbu)s END
) AS day
FROM src_file s
WHERE s.is_actual AND s.status <> 'quarantine' AND s.reporting_kind = 'interim'
"""


def interim_days(start: date, today: date) -> set[date]:
    """Дни видимости промежуточных комплектов внутри окна пересчёта."""
    from finlib.standards import Standard

    known = load_routing().history.known_from
    with connection() as conn:
        rows = fetch_all(
            _INTERIM_VISIBLE,
            {
                "ifrs": known.days(Standard.IFRS, interim=True),
                "rsbu": known.days(Standard.RSBU, interim=True),
            },
            conn=conn,
        )
    return {row["day"] for row in rows if start <= row["day"] <= today}


def grid(today: date, step: int, depth: int) -> tuple[tuple[date, ...], int]:
    """Сетка дат пересчёта: неделя плюс точка на каждую дату события.

    **Дневное разрешение есть только у дефолтов и переводов биржи**, и там оно
    и нужно: у отчётности четыре точки в год, у рейтингов — единицы действий.
    Считать год по дням ради отчётности значило бы тратить час на разрешение,
    которого в данных нет.
    """
    start = today - timedelta(days=depth)
    weekly = {start + timedelta(days=shift) for shift in range(0, depth + 1, step)}
    weekly.add(today)
    # Даты событий: дефолт и перевод биржи датированы днём, и точка ставится
    # ровно на него — иначе событие видно неделей позже, чем случилось.
    events: set[date] = set()
    # У неплатежа два дня: объявление (с него виден неплатёж) и конец
    # льготного срока (с него — дефолт); точка ставится на оба.
    for records in default_records().values():
        for item in records:
            for day in (item.known_on, item.moment):
                if day is not None and start <= day <= today:
                    events.add(day)
    for entry in risk_sectors().values():
        if entry.since is not None and start <= entry.since <= today:
            events.add(entry.since)
    events |= interim_days(start, today)
    # Число точек события считается после сведения: день события, попавший
    # на неделю сетки, второй точкой не становится, и приписывать его
    # событиям значило бы посчитать одну точку дважды.
    return tuple(sorted(weekly | events)), len(events - weekly)


def decision_values(row) -> dict:  # noqa: ANN001
    """Величины, которыми решение получено, — строками.

    **Исход без величин не отвечает на вопрос «а если порог другой».**
    Калибровка фазы 6 задаст истории десятки таких вопросов, и каждый стоил бы
    обхода года по сорок минут. Строками потому, что `Decimal` в JSON иначе
    становится `float`, а денежные величины у нас только `Decimal`
    (инвариант 5).

    Величины берутся у строки маршрута, а не считаются здесь заново: второй
    путь к покрытию разошёлся бы с первым.
    """
    found: dict = {
        "metrics": {code: str(value) for code, value in row.values.items()},
        "unit": row.unit,
    }
    if row.cash is not None:
        found["cash"] = str(row.cash)
    money = row.refinance
    if money is not None:
        found["refinance"] = {
            "due": str(money.due) if money.due is not None else None,
            "offered": str(money.offered) if money.offered is not None else None,
            "cash": str(money.cash) if money.cash is not None else None,
            "unit": money.unit,
            "days": money.days,
        }
    return found


def main() -> int:
    """Пересчитывает историю корзин назад и печатает её сводку."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    write = "--write" in sys.argv
    routing = load_routing()
    rule = routing.history
    today = date.today()
    dates, on_events = grid(today, rule.step_days, rule.depth_days)

    print("# История корзин: пересчёт назад\n")
    print(
        f"Глубина {rule.depth_days} дней, шаг {rule.step_days}; точек сетки "
        f"{len(dates)}, из них на даты событий {on_events}. "
        f"Дата известности отчётности — сроком закона "
        f"(`{rule.known_from.status}`): РСБУ годовая "
        f"{rule.known_from.rsbu_annual_days} дней, МСФО годовая "
        f"{rule.known_from.ifrs_annual_days}, промежуточная "
        f"{rule.known_from.ifrs_interim_days}.\n"
    )
    if not write:
        print("**Без `--write` история не записывается.**\n")

    seen: dict[str, str] = {}
    moves = Counter()
    by_date: list[tuple[date, int, int]] = []
    run_id = None
    with connection() as conn:
        if write:
            run_id = fetch_all(
                _RUN,
                {
                    "as_of": today,
                    "code": code_version(),
                    "methodology": f'{{"routing": "{routing.version}"}}',
                    "sources": "{}",
                },
                conn=conn,
            )[0]["id"]
            # Строка прогона фиксируется до первой точки: точки на неё
            # ссылаются, и незаписанный прогон оставил бы их без ссылки.
            conn.commit()
        # **Память прохода, а не кэш расчёта.** Показатели зависят от пары
        # «эмитент, отчётная дата», а не от дня маршрута: без неё один и тот же
        # комплект считался бы полсотни раз подряд, и год обходился бы
        # семь часов вместо получаса.
        memo: dict = {}
        for moment in dates:
            rows, _ = routing_rows(conn, moment, as_of=moment, memo=memo)
            changed = 0
            for row in rows:
                key = row.verdict.basket
                was = seen.get(row.inn)
                if was is not None and was != key:
                    changed += 1
                    moves[row.inn] += 1
                seen[row.inn] = key
                if write:
                    execute(
                        _POINT,
                        {
                            "run": run_id,
                            "inn": row.inn,
                            "as_of": moment,
                            "standard": (
                                row.standard.value if row.standard else None
                            ),
                            "basket": row.verdict.basket,
                            "subgroup": row.verdict.subgroup,
                            "grounds": list(row.verdict.grounds),
                            # **Все сработавшие основания, а не только
                            # называющие корзину**: у эмитента в «Разборе»
                            # основание рефинансирования корзину не называет,
                            # и ряд по `grounds` объявил бы его несрабатывающим.
                            "grounds_all": sorted(
                                {item.ground for item in row.verdict.findings}
                            ),
                            "inputs": json.dumps(decision_values(row)),
                            "fingerprint": row.fingerprint,
                            "report_date": row.report_date,
                        },
                        conn=conn,
                    )
            by_date.append((moment, len(rows), changed))
            # **Точка истории записывается целиком и своей датой.** Пересчёт
            # года идёт больше часа, и одна транзакция на всё означала бы,
            # что обрыв на двухсотой точке уносит и первые сто девяносто
            # девять. Дата — естественная единица: половины точки не бывает.
            conn.commit()
            logger.info("%s: строк %d, смен корзины %d", moment, len(rows), changed)
        if write:
            execute(
                _DONE,
                {
                    "id": run_id,
                    "status": "done",
                    "note": f"точек {len(dates)}, эмитентов {len(seen)}",
                },
                conn=conn,
            )

    total = sum(changed for _, _, changed in by_date[1:])
    print("\n## Сводка\n")
    print(
        f"Точек {len(dates)}, эмитентов {len(seen)}, смен корзины всего "
        f"**{total}**. Первая точка сменой не считается: сравнивать её не с чем.\n"
    )
    print("| Эмитентов сменили корзину | Сколько раз |")
    print("|---|---|")
    spread = Counter(moves.values())
    for times, count in sorted(spread.items()):
        print(f"| {count} | {times} |")
    never = len(seen) - len(moves)
    print(f"| {never} | ни разу |")
    print(
        f"\n**Сменили корзину хотя бы раз {len(moves)} из {len(seen)}**, "
        f"не менялись ни разу {never}.\n"
    )
    print(
        "Чем именно восстановлена история — своими датами или нынешними "
        "данными — считает замер по записанным основаниям "
        "(`eval/history_measure_run.py`): вопрос один, и второй ответ на него "
        "разошёлся бы с первым.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
