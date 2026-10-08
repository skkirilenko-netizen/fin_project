"""Ежедневный прогон целиком: доставка, маршрут, список, отчёт изменений.

**Один агент, а не пять.** Доставки идут по очереди в одном процессе — так
считается бюджет запросов и так виден отказ: `cbonds.pace.requested` живёт
в процессе, и разнесённые по скриптам доставки о расходе друг друга не знают.

**Отказ доставки — запись, а не молчание и не остановка остальных.** Отказавшая
доставка оставляет свой слой вчерашним, остальные идут своим чередом
(с 28.09.2026; прежде отказ снимка рейтингов прекращал все), прогон
помечается неудавшимся, а маршрут строится по тому, что на диске: список
нужен и в день отказа. Но отказ при этом стоит
**в начале отчёта**, потому что «изменений нет» при недошедшей доставке
и «изменений нет» при полной — разные сведения, а выглядят одинаково.

**Не всякая доставка ежедневная.** Рейтинговое действие приходит днём, перевод
биржи — днём; перечень выпусков и отчётность так часто не меняются, и тратить
на них суточную норму запросов незачем. Частота объявлена у каждой доставки
вместе с причиной.

    uv run python scripts/daily_run.py            # полный прогон
    uv run python scripts/daily_run.py --dry      # без обращений к источникам
    uv run python scripts/daily_run.py --reason "пересчёт после правки"   # повтор дня

**Отчёт изменений прогона по расписанию не перезаписывается** (решение
владельца 29.09.2026). Он — `changes_<дата>.md`; всякий иной прогон пишет
свой рядом, `changes_<дата>_<ЧЧММ>.md`, и называет в шапке, повторный он или
внеплановый и почему (`--reason`; причина пишется и в журнал прогона).
29.09.2026 ручной повтор переписал утренний отчёт, и восстановить его
оказалось нечем: точки маршрута дня повтор тоже переписывает.
"""

import json
import logging
import runpy
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from routing_backfill_run import decision_values

from finlib import run_lock
from finlib.config import settings
from finlib.db import connection, execute, fetch_all
from finlib.schedule import is_scheduled
from finlib.scoring.routing import load_routing
from finlib.scoring.routing_store import routing_rows
from finlib.sources import cbonds
from finlib.sources.market import series as market_series
from finlib.sources.network import NetworkDownError, transient
from finlib.version import code_version, route_fingerprint

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
    """Доставка: чем занята, как часто нужна и из какого источника.

    `every` — через сколько дней доставка нужна снова; 1 — ежедневно.
    `source` — чью суточную норму она тратит: исчерпанная норма Cbonds
    прекращает доставки Cbonds, а биржу не касается.
    """

    code: str
    name: str
    script: str
    every: int
    source: str
    why: str
    # Доводы скрипта стадии: у среза торгов — окно в неделю по дням, иначе
    # скрипт спрашивал бы по умолчанию сегодня и каждый седьмой день назад,
    # а вчерашнего, ради которого стадия заведена, не спрашивал бы вовсе.
    args: tuple[str, ...] = ()


# **Ни одна доставка не останавливает остальные** (28.09.2026). Маршрут
# строится по диску, и доставка, не дошедшая сегодня, оставляет свой слой
# вчерашним, а не неверным: остановка остальных маршрут вернее не делает,
# она только делает вчерашними и их. Остановить стоило бы доставку,
# после отказа которой следующие записали бы несогласованное — такой нет:
#
# - рейтинги — неполный снимок пишется с перечнем незапрошенных, маршрут
#   берёт для них последнее наблюдение с его датой, остаток добирает агент
#   в 11:30 (`ratings_snapshot`, `cbonds_events.read_snapshot`);
# - дефолты, выпуски, графики, отчётность — каждая запись по эмитенту или
#   выпуску самостоятельна; графики спрашивают источник по дате обновления
#   сами и от доставки выпусков не зависят (`flows_since.json` сдвигается
#   только после полного прохода);
# - биржа — другой источник, от Cbonds не зависит ничем.
#
# 28.09.2026 неполный снимок рейтингов (пропала сеть) остановил весь прогон,
# и без дефолтов, выпусков и отчётности остался день, в котором они могли
# дойти. Прекращается только то, чему нечем платить: исчерпана суточная
# норма Cbonds — прекращаются доставки Cbonds, биржа идёт дальше.
# Недоставка при этом не молчит: прогон закрывается «failed» с перечнем
# недошедшего, и отчёт изменений ставит его первым.
STAGES: tuple[Stage, ...] = (
    Stage(
        code="ratings",
        name="снимок рейтингов",
        script="scripts/ratings_snapshot.py",
        every=1,
        source="cbonds",
        why=(
            "История рейтингов у подписки закрыта, и снимок — единственный "
            "путь к ней: пропущенный день не восстанавливается ничем."
        ),
    ),
    # **Перечень дефолтов — ежедневно**: дата события приходит только отсюда,
    # а весь перечень по стране стоит четырёх запросов. До 25.09.2026 стадии
    # не было вовсе, и перечень от 22.09 не обновлялся ничем.
    Stage(
        code="defaults",
        name="перечень дефолтов",
        script="scripts/defaults_fetch.py",
        every=1,
        source="cbonds",
        why=(
            "Дефолт — событие дня с датой, и корзину «Разбор» по нему маршрут "
            "даёт только по записи перечня: без ежедневной доставки дефолт, "
            "случившийся после последней, не виден вовсе."
        ),
    ),
    # **Выпуски — ежедневно отбором по дате обновления, по понедельникам —
    # полным обходом как контролем отбора** (решение владельца 25.09.2026).
    # Стоят раньше графиков: графики дозапрашиваются по новым выпускам.
    Stage(
        code="emissions",
        name="выпуски эмитентов",
        script="scripts/emissions_fetch.py",
        every=1,
        source="cbonds",
        why=(
            "Статус выпуска, признак дефолта и дата оферты меняются днём; "
            "отбор по дате обновления стоит единиц запросов, а полный обход "
            "977 — он раз в неделю и проверяет, не пропускает ли отбор."
        ),
    ),
    # **Графики платежей и оферты — дозапросом по изменившимся выпускам.**
    # Полный повтор — около 11 000 запросов, больше суточной нормы; окно
    # по дате обновления стоит единиц запросов плюс два на изменившийся выпуск.
    Stage(
        code="flows",
        name="графики платежей и оферты",
        script="scripts/flows_fetch.py",
        every=1,
        source="cbonds",
        why=(
            "Рефинансирование — самое частое основание «Внимания», и считается "
            "оно по графику: новый выпуск без графика и сдвинутая оферта "
            "меняют его в тот же день."
        ),
    ),
    # **Отчётность агрегатора — ежедневно** (решение владельца 25.09.2026):
    # МСФО справочником целиком (2 запроса), РСБУ отбором по дате создания
    # и обновления (единицы запросов плюс три на эмитента). ГИР БО остаётся
    # первоисточником РСБУ: приоритет источников при записи не меняется.
    Stage(
        code="reporting",
        name="отчётность агрегатора",
        script="scripts/reporting_fetch.py",
        every=1,
        source="cbonds",
        why=(
            "Новый комплект меняет основание маршрута, а в сезон раскрытия "
            "комплекты приходят каждый день; отбор по дате стоит единиц "
            "запросов, и ждать недели незачем."
        ),
    ),
    Stage(
        code="moex",
        name="сектор повышенного риска биржи",
        script="scripts/moex_fetch.py",
        every=1,
        source="moex",
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
        source="moex",
        why=(
            "Рыночные основания маршрута считаются по ряду спредов и цен, "
            "и ряд этот наращивается днями: пропущенный день оставляет "
            "в нём дыру, а признак — подтверждение «7 из 10» — считает "
            "наблюдения, а не календарь."
        ),
        args=("--depth-days", "7", "--step", "1"),
    ),
    # **Уровень листинга — то же, что рейтинги: истории у источника нет.**
    # ISS отдаёт только текущий уровень, и не начав копить снимки, мы
    # не получим историю смен никогда. Один запрос на весь снимок, подписки
    # не требует. Правил из него пока не делается — сперва надо, чтобы
    # накопилось, на чём мерить.
    Stage(
        code="listing",
        name="снимок уровня листинга акций",
        script="scripts/listing_snapshot.py",
        every=1,
        source="moex",
        why=(
            "История смен уровня котировального списка у источника "
            "отсутствует: он отдаёт только сегодняшнее значение. "
            "Пропущенный день не восстанавливается ничем."
        ),
    ),
)


def _spent() -> int:
    """Сколько запросов к источнику потрачено с начала процесса."""
    return cbonds.pace.requested


def _fresh(path: Path, every: int, today: date) -> bool:
    """Свежа ли доставка: моложе ли её файл объявленной частоты и полон ли он.

    **Неполный снимок дня — не свежая доставка.** Прерванный снимок рейтингов
    пишется с перечнем недошедших (`refused`), и повторный прогон того же дня
    обязан его добрать, а не счесть сделанным.
    """
    if not path.exists():
        return False
    when = date.fromtimestamp(path.stat().st_mtime)
    if (today - when).days >= every:
        return False
    if path.parent.name == "ratings":
        found = json.loads(path.read_text(encoding="utf-8"))
        return not found.get("refused")
    return True


def _run_stage(stage: Stage, dry: bool) -> dict:
    """Выполняет доставку и возвращает её исход вместе с расходом запросов."""
    before = _spent()
    started = time.monotonic()
    said: dict = {"code": stage.code, "name": stage.name, "every": stage.every}
    if dry:
        said |= {"status": "skipped", "why": "прогон без обращений к источникам"}
        return said
    written_before = _stamp(_marker(stage))
    argv = sys.argv
    try:
        sys.argv = [stage.script, *stage.args]
        runpy.run_path(str(ROOT / stage.script), run_name="__main__")
        said |= {"status": "done"}
    except SystemExit as stop:
        code = int(stop.code or 0)
        said |= {"status": "done" if code == 0 else "failed", "exit": code}
    except NetworkDownError as failure:
        # **Нет сети — не отказ источника**, и в журнале и отчёте это разные
        # записи: 28.09.2026 пропажа сети у машины была записана
        # «источник отказал», хотя до источника не дошёл ни один запрос.
        logger.exception("доставка «%s»: нет сети", stage.name)
        said |= {"status": "offline", "why": str(failure)[:200]}
    except Exception as failure:  # noqa: BLE001
        # В журнал процесса — целиком, с трассировкой: в `routing_run` идёт
        # только строка, и 25.09.2026 по ней было не понять, какой запрос
        # оборвался и где.
        logger.exception("доставка «%s» оборвалась", stage.name)
        said |= {
            "status": "failed",
            "error": f"{type(failure).__name__}: {failure}"[:200],
            # Таймаут или обрыв сети — стадию повторят через паузу
            # (`_retry`); 4xx и прочее повтор не лечит.
            "transient": transient(failure),
        }
    finally:
        sys.argv = argv
    said |= {
        "requests": _spent() - before,
        "seconds": round(time.monotonic() - started, 1),
    }
    return _honest(said, _marker(stage), written_before)


def _stamp(path: Path) -> float | None:
    """Время записи файла; None — файла нет."""
    return path.stat().st_mtime if path.exists() else None


def _honest(said: dict, marker: Path, written_before: float | None) -> dict:
    """«done» только у доставки, которая действительно положила данные дня.

    **Стадия, не обновившая свой файл, не доставила ничего, что бы ни
    вернул скрипт.** С 22.09.2026 стадия выпусков писала «done», не сделав
    ни одного запроса: ответы брались из кэша, и признаки дефолта застыли.
    Судится по самому файлу-результату, а не по счётчику запросов:
    у доставок биржи счётчика Cbonds нет вовсе. Файл не переписан —
    статус «cached» с датой того файла, что лежит.
    """
    if said.get("status") != "done":
        return said
    if _stamp(marker) is not None and _stamp(marker) != written_before:
        return said
    when = _stamp(marker)
    return said | {
        "status": "cached",
        "file_date": (
            f"{date.fromtimestamp(when):%Y-%m-%d}" if when is not None else None
        ),
        "why": (
            f"файл доставки не обновлён, лежит от {date.fromtimestamp(when):%d.%m.%Y}"
            if when is not None
            else "файла доставки нет вовсе"
        ),
    }


_RUN = """
INSERT INTO routing_run (kind, as_of, status, code_version, methodology, sources)
VALUES ('run', %(as_of)s, 'running', %(code)s, %(methodology)s, %(sources)s)
RETURNING id
"""

def _open_run(today: date, methodology: str) -> int:
    """Пишет строку прогона при старте, до первой доставки.

    **Строка появляется в начале, а не в конце.** Прежде она писалась после
    доставок, и прогон, оборвавшийся на них, не оставлял в журнале ничего —
    «прогона не было» и «прогон упал» выглядели одинаково.
    """
    with connection() as conn:
        run_id = fetch_all(
            _RUN,
            {
                "as_of": today,
                "code": code_version(),
                "methodology": methodology,
                "sources": json.dumps([]),
            },
            conn=conn,
        )[0]["id"]
        conn.commit()
    return int(run_id)


def _close_failed(run_id: int, delivered: list[dict], failure: BaseException) -> None:
    """Закрывает строку оборвавшегося прогона на своём соединении.

    Соединение своё, потому что транзакция маршрута при обрыве откатывается,
    а запись об обрыве нужна именно тогда.
    """
    with connection() as conn:
        execute(
            _DONE,
            {
                "id": run_id,
                "status": "failed",
                "sources": json.dumps(delivered, ensure_ascii=False),
                "note": f"прогон оборвался: {type(failure).__name__}: {failure}"[:500],
            },
            conn=conn,
        )
        conn.commit()


_DONE = """
UPDATE routing_run
SET status = %(status)s, finished_at = now(), sources = %(sources)s, note = %(note)s
WHERE id = %(id)s
"""

# **Повтор дня пишется рядом с главной точкой, а не поверх неё** (решение
# владельца 29.09.2026): вид `repeat`, в ключе — прогон. Точку `run` пишет
# только прогон по расписанию; всякий иной — `repeat`, даже первый за день.
_POINT = """
INSERT INTO routing_history
       (run_id, inn, as_of, kind, standard, basket, subgroup, grounds,
        grounds_all, inputs, fingerprint, report_date)
VALUES (%(run)s, %(inn)s, %(as_of)s, %(kind)s, %(standard)s, %(basket)s,
        %(subgroup)s, %(grounds)s, %(grounds_all)s, %(inputs)s,
        %(fingerprint)s, %(report_date)s)
ON CONFLICT {target} DO UPDATE SET
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

# Ключ конфликта по виду точки: частичные уникальные индексы схемы.
POINT_TARGET = {
    "run": "(inn, as_of, kind) WHERE kind <> 'repeat'",
    "repeat": "(inn, as_of, run_id) WHERE kind = 'repeat'",
}

def point_sql(kind: str) -> str:
    """Запрос записи точки маршрута названного вида."""
    return _POINT.format(target=POINT_TARGET[kind])


def point_kind(scheduled: bool) -> str:
    """Вид точки прогона: `run` — прогон по расписанию, `repeat` — всякий иной.

    **Иной прогон — повтор, даже первый за день** (поправка владельца
    29.09.2026): иначе ручной прогон до планового занял бы место плановой
    точки, а аудит читает именно её.
    """
    return "run" if scheduled else "repeat"


def main() -> int:
    """Проводит день целиком под замком; замок занят — отказ до записи в журнал."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # **Второй прогон при идущем отказывается** (решение владельца 08.10.2026):
    # до строки `routing_run`, с тем, кто держит замок, и ненулевым исходом.
    try:
        with run_lock.hold():
            return _day()
    except run_lock.RunInProgressError as busy:
        logger.error("%s; этот запуск не выполнен", busy)
        return 2


def _day() -> int:
    """Проводит день целиком: доставка, маршрут, история, список, отчёт."""
    dry = "--dry" in sys.argv
    reason = (
        sys.argv[sys.argv.index("--reason") + 1] if "--reason" in sys.argv else ""
    )
    started = datetime.now().astimezone()
    today = date.today()
    routing = load_routing()
    delivered: list[dict] = []
    shortfall = ""
    # **Отпечаток прогона — содержимое методики и коммит кода маршрута**
    # (решение владельца 01.10.2026): объявленная версия «1.0.0» правкой
    # не поднималась, и отчёт изменений не видел наших правок.
    run_id = _open_run(
        today, json.dumps({"routing": routing.version, **route_fingerprint()})
    )
    said = _who(run_id, today, started, reason)
    try:
        rows, counts, shortfall = _deliver_and_route(
            run_id, today, dry, delivered, said
        )
    except BaseException as failure:
        logger.exception("прогон %s оборвался", today)
        _close_failed(run_id, delivered, failure)
        raise
    _publish(today, rows, counts, delivered, shortfall, started, said)
    return 1 if shortfall else 0


_SCHEDULED_TODAY = """
SELECT started_at FROM routing_run
WHERE kind = 'run' AND as_of = %(as_of)s AND id <> %(id)s
"""


def _who(run_id: int, today: date, started: datetime, reason: str) -> str:
    """Кто этот прогон: пусто — прогон по расписанию; иначе строка для журнала и шапки.

    Повторный — если у дня уже есть прогон по расписанию; внеплановый — если
    нет. Причина называется словами из `--reason`; без неё так и сказано.
    """
    if is_scheduled(started):
        return ""
    why = reason or "причина не названа"
    earlier = [
        row["started_at"]
        for row in fetch_all(_SCHEDULED_TODAY, {"as_of": today, "id": run_id})
        if is_scheduled(row["started_at"])
    ]
    if earlier:
        return (
            f"Повторный прогон дня в {started:%H:%M} (по расписанию был в "
            f"{min(earlier).astimezone():%H:%M}), причина — {why}"
        )
    return f"Внеплановый прогон в {started:%H:%M}, причина — {why}"


# Исходы стадии, при которых данных дня от неё нет. «cached» сюда не входит:
# стадия прошла, а нового у источника не было — это отчёт изменений
# называет сам, но прогон неудавшимся не делает.
UNDELIVERED: tuple[str, ...] = ("failed", "offline", "no_quota")


def _deliver(today: date, dry: bool, delivered: list[dict]) -> None:
    """Доставки дня по очереди; отказ одной остальные не останавливает.

    Исчерпана суточная норма Cbonds — доставки Cbonds дальше не начинаются
    (`no_quota`), доставки биржи идут: их норма другая.
    """
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
        if stage.source == "cbonds" and _spent() > DAILY_QUOTA - RESERVE:
            delivered.append(
                {
                    "code": stage.code,
                    "name": stage.name,
                    "status": "no_quota",
                    "why": "суточная норма запросов Cbonds исчерпана",
                }
            )
            continue
        delivered.append(_run_stage(stage, dry))
    _retry(dry, delivered)


def _retryable(item: dict) -> bool:
    """Повтор лечит только таймаут и обрыв сети: «нет сети» либо `transient`."""
    return item.get("status") == "offline" or (
        item.get("status") == "failed" and bool(item.get("transient"))
    )


def _retry(dry: bool, delivered: list[dict]) -> None:
    """Один повтор стадий, упавших по таймауту или обрыву сети, через паузу.

    **Повтор один и до закрытия прогона** (решение владельца 07.10.2026):
    05.10.2026 медленный Cbonds держался дольше пауз клиента, и день был
    потерян целиком. Пауза одна на все такие стадии (`stage_retry_pause_s`).
    Повтор докачивает недостающее, а не начинает заново: стадии берут
    сделанное сегодня с диска — снимок рейтингов дозапрашивает `refused`,
    выпуски не переспрашивают полученное сегодня. Не дошла и после
    повтора — прогон `failed`, как прежде. Первая попытка остаётся
    в записи стадии (`first`), число повторов — в `retries`.
    """
    again = [index for index, item in enumerate(delivered) if _retryable(item)]
    if dry or not again:
        return
    names = ", ".join(delivered[index]["name"] for index in again)
    logger.warning(
        "повтор через %.0f мин: %s", settings.stage_retry_pause_s / 60, names
    )
    time.sleep(settings.stage_retry_pause_s)
    by_code = {stage.code: stage for stage in STAGES}
    for index in again:
        first = delivered[index]
        stage = by_code[first["code"]]
        if stage.source == "cbonds" and _spent() > DAILY_QUOTA - RESERVE:
            said = {
                "code": stage.code,
                "name": stage.name,
                "status": "no_quota",
                "why": "суточная норма запросов Cbonds исчерпана",
            }
        else:
            said = _run_stage(stage, dry)
        delivered[index] = said | {
            "retries": 1,
            "first": {
                key: first[key]
                for key in ("status", "error", "why", "requests", "seconds")
                if key in first
            },
        }


def _shortfall(delivered: list[dict]) -> str:
    """Что из доставок дня не дошло — строкой; пусто — дошло всё."""
    words = {"failed": "отказ", "offline": "нет сети", "no_quota": "нет нормы"}
    missed = [
        f"{item['name']} — {words[item['status']]}"
        for item in delivered
        if item.get("status") in UNDELIVERED
    ]
    return f"доставка неполна: {'; '.join(missed)}" if missed else ""


def _deliver_and_route(
    run_id: int, today: date, dry: bool, delivered: list[dict], said: str = ""
) -> tuple[list, dict, str]:
    """Доставки дня и маршрут; строку прогона закрывает вместе с точками истории.

    `delivered` наполняется по ходу: при обрыве строка прогона закрывается
    тем, что успело дойти.
    """
    _deliver(today, dry, delivered)
    shortfall = _shortfall(delivered)

    # **Маршрут строится и в день отказа.** Список нужен и тогда; но отказ
    # стоит в отчёте первым, а не молчит.
    with connection() as conn:
        # **Ряд спредов пересчитывается после доставки, а не читается
        # вчерашний.** Доставка кладёт новый срез, и маршрут, посчитанный
        # по старому ряду, объявил бы вчерашнее состояние сегодняшним.
        # Пересчёт идёт по диску и сети не касается вовсе.
        market_series(refresh=True)
        rows, counts = routing_rows(conn, today)
        # `said` пуст только у прогона по расписанию (`_who`).
        kind = point_kind(not said)
        insert = point_sql(kind)
        for row in rows:
            execute(
                insert,
                {
                    "run": run_id,
                    "kind": kind,
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
                # Недоставка делает прогон неудавшимся, хотя маршрут
                # построен: день, в котором данные дошли не все, чистым
                # прогоном не считается.
                "status": "failed" if shortfall else "done",
                "sources": json.dumps(delivered, ensure_ascii=False),
                # Повтор и его причина — в журнале прогона: сводка читает
                # их отсюда, а не из имени файла.
                "note": (f"{said}; " if said else "")
                + (shortfall or f"эмитентов {len(rows)}"),
            },
            conn=conn,
        )
        conn.commit()
    return rows, counts, shortfall


def report_path(today: date, started: datetime | None, said: str) -> Path:
    """Файл отчёта изменений: у прогона по расписанию свой, у прочих — со временем.

    **Главный отчёт дня не перезаписывается**: прогон не по расписанию пишет
    `changes_<дата>_<ЧЧММ>.md` всегда, даже если главного ещё нет, — иначе
    прогон по расписанию, пришедший позже, затёр бы его отчёт.
    """
    if not said or started is None:
        return OUT / f"changes_{today:%Y-%m-%d}.md"
    return OUT / f"changes_{today:%Y-%m-%d}_{started:%H%M}.md"


def _publish(
    today: date,
    rows: list,
    counts: dict,
    delivered: list[dict],
    shortfall: str,
    started: datetime | None = None,
    said: str = "",
) -> None:
    """Список, выгрузка, карточки и отчёт изменений по записанному маршруту."""
    # Список, выгрузка и карточки — тем же кодом, что руками: второй путь
    # к странице разошёлся бы с первым.
    #
    # **Порядок здесь часть дела.** Список ставит ссылку на карточку только
    # тогда, когда файл её лежит на диске, а карточка ссылается на свежайший
    # список этого запуска. Карточки получают его путь явно, до появления
    # файла. HTML публикуется после успешной сборки карточек: новые эмитенты
    # тоже получают ссылки, а отказ сборки не выдаётся за готовый список.
    # Отчёт и CSV сохраняются до списка.
    report = report_path(today, started, said)
    html_out = report.with_name(report.name.replace("changes_", "watchlist_")).with_suffix(".html")
    csv_out = html_out.with_suffix(".csv")
    # Проверка в генераторе HTML остаётся последней защитой. Здесь отказ
    # нужен до CSV и карточек, иначе спутники уже опубликованного списка
    # изменятся раньше, чем генератор обнаружит существующую страницу.
    if html_out.exists():
        raise ValueError(f"сохранённый HTML не переписывается: {html_out}; выберите новый запуск")
    argv = sys.argv
    try:
        sys.argv = ["change_report_run.py", "--kind", "run", "--output", str(report)] + (
            ["--note", said] if said else []
        )
        runpy.run_path(
            str(ROOT / "eval" / "change_report_run.py"), run_name="__main__"
        )
    except SystemExit as stop:
        if stop.code:
            raise
    finally:
        sys.argv = argv

    for name, args in (
        ("watchlist_csv.py", ("--out", str(csv_out))),
        ("issuer_card_run.py", ("--all", "--watchlist", str(html_out))),
        ("watchlist_run.py", ("--out", str(html_out), "--report", str(report),
                             "--csv", str(csv_out))),
    ):
        argv = sys.argv
        try:
            sys.argv = [name, *args]
            runpy.run_path(str(ROOT / "eval" / name), run_name="__main__")
        except SystemExit as stop:
            if stop.code:
                raise
        finally:
            sys.argv = argv

    print(f"\nПрогон {today}: эмитентов {len(rows)}, запросов {_spent()}")
    for said in delivered:
        print(f"  {said['name']}: {said['status']}", end="")
        if said.get("requests"):
            print(f", запросов {said['requests']}", end="")
        print()
    if shortfall:
        print(f"  НЕДОСТАВКА: {shortfall}")
    print(f"  отчёт изменений: {report}")
    print(f"  строк маршрута: {counts.get('эмитентов', 0)}")


def _marker(stage: Stage) -> Path:
    """Файл, по возрасту которого видно, когда доставка была последней.

    У каждой доставки он свой и настоящий — не метка о запуске, а сам
    её результат: метка сказала бы «прогон был», а нужен ответ «данные есть».
    """
    if stage.code == "ratings":
        return ROOT / "data" / "raw" / "cbonds" / "ratings" / f"{date.today():%Y-%m-%d}.json"
    if stage.code == "defaults":
        return ROOT / "data" / "raw" / "cbonds" / f"defaults_ru_{date.today():%Y-%m-%d}.json"
    if stage.code == "flows":
        return ROOT / "data" / "raw" / "cbonds" / f"flows_delta_{date.today():%Y-%m-%d}.json"
    if stage.code == "reporting":
        return (
            ROOT / "data" / "raw" / "cbonds"
            / f"reporting_delta_{date.today():%Y-%m-%d}.json"
        )
    if stage.code == "moex":
        return ROOT / "data" / "raw" / "moex" / "bonds_traded.json"
    # Срез торгов вчерашнего дня: сегодняшнего у биржи ещё нет, и ждать
    # его — значит не доставить ничего.
    if stage.code == "market":
        return (
            ROOT / "data" / "raw" / "moex"
            / f"xsec_{date.today() - timedelta(days=1):%Y-%m-%d}.json"
        )
    # Снимок листинга — сегодняшний: он о текущем состоянии, а не о торгах
    # вчерашнего дня, и ждать вчера от него незачем.
    if stage.code == "listing":
        return ROOT / "data" / "raw" / "moex" / "listing" / f"{date.today():%Y-%m-%d}.json"
    return ROOT / "data" / "raw" / "cbonds" / f"emissions_delta_{date.today():%Y-%m-%d}.json"


if __name__ == "__main__":
    sys.exit(main())
