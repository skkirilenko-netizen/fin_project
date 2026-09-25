"""Графики платежей и оферты: дозапрос по выпускам, изменившимся с прошлого раза.

    uv run python scripts/flows_fetch.py                  # с даты прошлого прогона
    uv run python scripts/flows_fetch.py --since 2026-09-22

**Полный повтор не помещается в сутки.** Выпусков с графиком 5 493, запросов
на выпуск два (`get_flow_new`, `get_offert`) — около 11 000 при суточной
норме 10 000. Поэтому дозапрашиваются только изменившиеся: источник отбирает
выпуски по дате обновления записи (`updating_date ≥ дата`, проба 25.09.2026
с отрицательным контролем — отбор применяется), и выпуски без графика
на диске — сразу, какая бы ни была у них дата.

**Дата прошлого прогона хранится рядом с ответами** (`flows_since.json`)
и сдвигается только после того, как все запросы прошли: оборванный прогон
повторяет окно целиком, а не теряет его хвост.

**Файл дня** (`flows_delta_ГГГГ-ММ-ДД.json`) называет, что спрошено и почему,
и служит результатом стадии ежедневного прогона.
"""

import json
import logging
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = cbonds.CACHE
CARDS = CACHE / "emitents.json"
SINCE = CACHE / "flows_since.json"
# Выпуски, по которым нужны платежи: в обращении и размещаемые — для
# рефинансирования, с дефолтом — для истории событий. То же правило, что
# у полной доставки (`scripts/events_fetch.py`).
WANTED_STATUSES = ("в обращении", "размещается")


def changed_since(since: str) -> set[str]:
    """Идентификаторы выпусков российских эмитентов, обновлённых с даты."""
    found = cbonds.fetch(
        "get_emissions",
        f"emissions_changed_{since}",
        filters=(
            {"field": "emitent_country", "operator": "eq", "value": "1"},
            {"field": "updating_date", "operator": "ge", "value": since},
        ),
        limit=1000,
        refresh=True,
    )
    items = found.get("items", [])
    # Отбор ge клиент сам не сверяет — сверяется здесь: запись раньше даты
    # означает, что источник отбор пропустил, и окно ничего не говорит.
    early = [item for item in items if str(item.get("updating_date") or "")[:10] < since]
    if early:
        raise cbonds.FilterIgnoredError(
            f"отбор updating_date ≥ {since} не применён: {len(early)} записей раньше"
        )
    return {str(item.get("id")) for item in items}


def wanted(changed: set[str]) -> list[tuple[str, str, str]]:
    """Выпуски справочника, по которым спрашивать: изменившиеся и без графика."""
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    found: list[tuple[str, str, str]] = []
    for inn in cards:
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            if not (issue.status in WANTED_STATUSES or issue.default or issue.unsettled):
                continue
            missing = not (CACHE / f"flow_{issue.emission_id}.json").exists()
            if issue.emission_id in changed:
                found.append((inn, issue.emission_id, "обновлён"))
            elif missing:
                found.append((inn, issue.emission_id, "графика нет"))
    return found


def main() -> int:
    """Дозапрашивает графики и оферты; 1 — если справочника эмитентов нет."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not CARDS.exists():
        print("карточек эмитентов на диске нет: перечень брать негде")
        return 1
    today = date.today()
    since = (
        sys.argv[sys.argv.index("--since") + 1]
        if "--since" in sys.argv
        else json.loads(SINCE.read_text(encoding="utf-8"))["since"]
        if SINCE.exists()
        else f"{today}"
    )
    chosen = wanted(changed_since(since))
    asked: list[dict] = []
    failed = 0
    for inn, emission, why in chosen:
        for method, name, limit in (
            ("get_flow_new", f"flow_{emission}", 500),
            ("get_offert", f"offert_{emission}", 100),
        ):
            try:
                cbonds.fetch(
                    method,
                    name,
                    filters=({"field": "emission_id", "operator": "eq", "value": emission},),
                    limit=limit,
                    refresh=True,
                )
            except (cbonds.CbondsError, httpx.TransportError) as failure:
                failed += 1
                logger.error("%s %s: %s", method, emission, str(failure)[:120])
        asked.append({"inn": inn, "emission_id": emission, "why": why})
    (CACHE / f"flows_delta_{today}.json").write_text(
        json.dumps(
            {"since": since, "asked": asked, "failed": failed},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if not failed:
        SINCE.write_text(json.dumps({"since": f"{today}"}), encoding="utf-8")
    new = sum(1 for item in asked if item["why"] == "графика нет")
    print(
        f"графики с {since}: выпусков {len(asked)} (обновлённых {len(asked) - new}, "
        f"без графика {new}), отказов {failed}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
