"""Ежедневный снимок котировок. **Остановлен 22.09.2026, оставлен на месте.**

    uv run python scripts/quotes_snapshot.py             # снимок на сегодня
    uv run python scripts/quotes_snapshot.py --limit 50  # частичный, для пробы

**Из ежедневного расписания снят решением человека в тот же день, когда
заведён.** Причина не в нём: ISS Московской биржи отдаёт ту же историю торгов
глубже — 883 дня у выпуска ЕвроТранса против сорока у агрегатора, — бесплатно
и с уже посчитанным Z-спредом. Держать ради второго источника того же 383 МБ
в день незачем.

Модуль оставлен, а не удалён: снимок остаётся единственным путём к истории
Cbonds, если она однажды понадобится порознь от биржевой, — и удалённое
не отличить от забытого. Снимок за 22.09.2026 сделан и лежит на диске.

**Несобранное сегодня не восстановить.** `get_tradings_new` отдаёт последние
около сорока дней торгов, и отбор по дате окна не расширяет: на 01.03.2026
метод даёт те же записи, что и без отбора. Значит, история котировок —
тот же случай, что история рейтингов: её не будет, пока не начнём снимать.
Цена промедления измерена: у Кириллицы цена держалась 99+ % номинала
до 19.08.2026, 20.08 упала до 77, а погашение было должно состояться 22.08 —
сигнал пришёл за два дня и виден только в окне.

**Запись сохраняется полностью.** У рейтингов снимок хранит выбранные поля —
там остальное суть адреса агентств; здесь же 87 полей, и какое из них
понадобится рыночному слою, сегодня не известно. Урезать запись значило бы
принять методическое решение доставкой.

**Снимок дня не перезаписывается** — файл доказательная база, и два прогона
в один день обязаны дать один файл. `--refresh` переписывает намеренно.

**Берутся выпуски в обращении и размещаемые**: у погашенного котировок нет,
а запрос по нему тратит норму. Перечень — из карточек справочника, один
запрос на выпуск.
"""

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

CARDS = Path("data/raw/cbonds/emitents.json")
SNAPSHOTS = Path("data/raw/cbonds/quotes")
METHOD = "get_tradings_new"
WANTED = ("в обращении", "размещается")


def emissions() -> list[tuple[str, str, str]]:
    """Выпуски в обращении: ИНН, идентификатор, наименование."""
    if not CARDS.exists():
        logger.error("карточек эмитентов на диске нет: %s", CARDS)
        return []
    found: list[tuple[str, str, str]] = []
    for inn in json.loads(CARDS.read_text(encoding="utf-8")):
        for issue in issues_of(inn)[0]:
            if issue.status in WANTED and issue.emission_id:
                found.append((inn, issue.emission_id, issue.name))
    return found


def quotes(emission: str, today: date) -> list[dict] | None:
    """Котировки выпуска; None — ответа нет, причина в журнале.

    **Обрыв связи ответом не считается** и повторяется однажды: тысяча
    запросов идёт больше получаса, и падение на середине оставило бы снимок
    дня наполовину пустым — а по нему потом будут судить о рынке.
    """
    for attempt in (1, 2):
        try:
            found = cbonds.fetch(
                METHOD,
                f"quotes_{today:%Y-%m-%d}_{emission}",
                filters=(
                    {"field": "emission_id", "operator": "eq", "value": emission},
                ),
                limit=1000,
            )
        except cbonds.CbondsError as failure:
            logger.error("%s %s: %s", METHOD, emission, str(failure)[:110])
            return None
        except httpx.HTTPError as failure:
            logger.error(
                "%s %s: обрыв связи (%s), попытка %d",
                METHOD,
                emission,
                type(failure).__name__,
                attempt,
            )
            time.sleep(10.0)
            continue
        return found.get("items") or []
    return None


def main() -> int:
    """Делает снимок котировок на сегодня; 1 — если снимать было нечем."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = date.today()
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    refresh = "--refresh" in sys.argv

    wanted = emissions()
    if not wanted:
        print("снимок не сделан: выпусков в обращении не нашлось, а выдумывать нечего")
        return 1

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{today:%Y-%m-%d}.json"
    if path.exists() and not refresh:
        found = json.loads(path.read_text(encoding="utf-8"))
        print(
            f"снимок на {today} уже есть: {path}, выпусков "
            f"{len(found.get('emissions', {}))}. Повторный прогон ничего "
            "не переписывает — файл доказательная база."
        )
        return 0

    chosen = wanted[:limit] if limit else wanted
    snapshot: dict[str, dict] = {}
    refused: list[str] = []
    for inn, emission, name in chosen:
        records = quotes(emission, today)
        if records is None:
            refused.append(emission)
            continue
        snapshot[emission] = {"inn": inn, "name": name, "quotes": records}

    traded = sum(1 for item in snapshot.values() if item["quotes"])
    path.write_text(
        json.dumps(
            {
                "date": f"{today:%Y-%m-%d}",
                "method": METHOD,
                "emissions": snapshot,
                "refused": refused,
                "requested": cbonds.pace.requested,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        f"{path}: выпусков {len(snapshot)}, из них с котировками {traded}, "
        f"отказов {len(refused)}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
