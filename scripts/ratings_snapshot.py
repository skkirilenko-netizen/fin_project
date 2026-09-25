"""Ежедневный снимок рейтингов: без него истории понижений не будет никогда.

    uv run python scripts/ratings_snapshot.py            # снимок на сегодня
    uv run python scripts/ratings_snapshot.py --limit 50 # частичный, для пробы

**Метод источника отдаёт только последнее значение** (`…_maxdate`): по нему
видно, что рейтинг сейчас отозван, и не видно, что было до отзыва. У ЕвроТранса
все четыре агентства показывают `Withdrawn`, и прежнего значения не осталось
нигде. Поэтому снимок делается ежедневно и складывается на диск отдельным
файлом по дате: история берётся из последовательности снимков, а не из метода.

**Снимок дня не перезаписывается.** Файл — доказательная база: два прогона
в один день должны дать один файл, иначе «история» окажется историей наших
прогонов. Повторный прогон того же дня ничего не делает и говорит об этом;
`--refresh` переписывает намеренно.

**Эмитент без ответа снимок не обрывает.** Исчерпав повторы клиента, запрос
уходит в `refused` с причиной, и снимок идёт дальше: 25.09.2026 один таймаут
на 661-м эмитенте оставил день без снимка вовсе, а пропущенный день
не восстанавливается ничем. Повторный запуск того же дня дозапрашивает
только `refused`: наблюдения, уже лежащие в файле, не трогаются, а
дозапрошенные помечаются временем в `recovered`. Отказ многих подряд
(`cbonds_refused_in_row_max`) — уже не сбой запроса, а отказ источника,
и снимок прекращается: ответы по эмитентам лежат на диске, и следующий
запуск продолжит с места обрыва.

Запросов: один на эмитента. Перечень берётся из карточек справочника, а не
из базы: снимок нужен и по тем, у кого отчётности у нас пока нет.
"""

import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.config import settings  # noqa: E402
from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

CARDS = Path("data/raw/cbonds/emitents.json")
SNAPSHOTS = Path("data/raw/cbonds/ratings")
METHOD = "get_rating_emitent_maxdate"

# **Запись хранится полностью, и это решение от 22.09.2026.** Прежде снимок
# оставлял девять полей из сорока пяти — «остальное суть адреса агентств», —
# и это верно ровно до первого вопроса, которого мы не задавали: статуса
# наблюдения у рейтинга, например. Снимок единственное, что нельзя
# пересчитать, а урезать его значит принять методическое решение доставкой.
# Ненужные поля занимают место; потерянное поле не восстановить.
#
# Отсюда и резервная копия: один файл в день, полный, и достаточно копировать
# его (`scripts/snapshots_backup.py`).
DROPPED = (
    "agency_name_eng",
    "agency_name_ita",
    "agency_name_pol",
    "agency_site_eng",
    "agency_site_ita",
    "agency_site_pol",
    "agency_site_rus",
    "emitent_name_eng",
    "emitent_name_ita",
    "emitent_name_pol",
    "forecast_name_eng",
    "forecast_name_ita",
    "forecast_name_pol",
    "scale_name_eng",
    "scale_name_ita",
    "scale_name_pol",
    "scale_point_description_eng",
    "scale_point_description_ita",
    "scale_point_description_pol",
)


def issuers() -> dict[str, str]:
    """ИНН и наименование эмитентов справочника; пусто — карточек нет."""
    if not CARDS.exists():
        logger.error("карточек эмитентов на диске нет: %s", CARDS)
        return {}
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    return {inn: str(card.get("name_rus") or inn) for inn, card in cards.items()}


class SourceRefusedError(cbonds.CbondsError):
    """Эмитенты подряд без ответа: это отказ источника, а не сбой запроса."""


def take(chosen: list[str], today: date) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Записи рейтингов по эмитентам и причины, по которым ответа нет.

    Эмитент без ответа уходит в перечень отказов, снимок идёт дальше.
    Отказов подряд больше объявленного — `SourceRefusedError`.
    """
    snapshot: dict[str, list[dict]] = {}
    refused: dict[str, str] = {}
    in_row = 0
    for inn in chosen:
        try:
            found = cbonds.fetch(
                METHOD,
                f"ratings_{today:%Y-%m-%d}_{inn}",
                filters=({"field": "emitent_inn", "operator": "eq", "value": inn},),
                limit=50,
            )
        except (cbonds.CbondsError, httpx.TransportError) as failure:
            refused[inn] = f"{type(failure).__name__}: {failure}"[:120]
            logger.warning("рейтинги %s: ответа нет — %s", inn, refused[inn])
            in_row += 1
            if in_row >= settings.cbonds_refused_in_row_max:
                raise SourceRefusedError(
                    f"источник не ответил по {in_row} эмитентам подряд, "
                    f"последний {inn}: {refused[inn]}"
                ) from failure
            continue
        in_row = 0
        snapshot[inn] = [
            {key: value for key, value in item.items() if key not in DROPPED}
            for item in found.get("items", [])
        ]
    return snapshot, refused


def _complete(path: Path, today: date) -> int:
    """Снимок дня есть: дозапрашивает только эмитентов без ответа.

    Наблюдения, уже лежащие в файле, не трогаются — файл доказательная база;
    дозапрошенные дописываются с отметкой времени в `recovered`.
    """
    found = json.loads(path.read_text(encoding="utf-8"))
    missing = found.get("refused") or {}
    if not missing:
        print(
            f"снимок на {today} уже есть: {path}, эмитентов "
            f"{len(found.get('issuers', {}))}. Повторный прогон ничего "
            "не переписывает — файл доказательная база."
        )
        return 0
    got, still = take(list(missing), today)
    moment = f"{datetime.now():%H:%M}"
    found["issuers"].update(got)
    found["refused"] = still
    found.setdefault("recovered", {}).update({inn: moment for inn in got})
    path.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        f"{path}: дозапрошено {len(missing)}, получено {len(got)}, "
        f"без ответа осталось {len(still)}"
    )
    return 0


def main() -> int:
    """Делает снимок на сегодня; 1 — если снимать было нечем."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    today = date.today()
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    refresh = "--refresh" in sys.argv

    known = issuers()
    if not known:
        print("снимок не сделан: перечень эмитентов пуст, а выдумывать его нечем")
        return 1

    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOTS / f"{today:%Y-%m-%d}.json"
    if path.exists() and not refresh:
        return _complete(path, today)

    chosen = list(known)[:limit] if limit else list(known)
    snapshot, refused = take(chosen, today)

    path.write_text(
        json.dumps(
            {
                "date": f"{today:%Y-%m-%d}",
                "method": METHOD,
                "issuers": snapshot,
                "refused": refused,
                "requested": cbonds.pace.requested,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    with_rating = sum(1 for items in snapshot.values() if items)
    print(
        f"{path}: эмитентов {len(snapshot)}, из них с рейтингом {with_rating}, "
        f"отказов {len(refused)}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
