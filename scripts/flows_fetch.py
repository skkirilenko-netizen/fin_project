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

**Обновлённый — не значит изменившийся по существу** (`methodology/delivery.yaml`,
`flows.substantive_fields`). Запись выпуска обновляется по поводам, графика
не касающимся; перезабирается выпуск, у которого с прошлой доставки сменилось
поле перечня. Отпечаток полей на момент доставки хранится рядом
(`flows_basis.json`); выпуск без отпечатка перезабирается, как прежде.
"""

import hashlib
import json
import logging
import sys
from datetime import date
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.config import settings  # noqa: E402
from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = cbonds.CACHE
CARDS = CACHE / "emitents.json"
SINCE = CACHE / "flows_since.json"
BASIS = CACHE / "flows_basis.json"


def substantive_fields() -> tuple[str, ...]:
    """Поля записи выпуска, смена которых требует перезабрать график."""
    rules = yaml.safe_load(
        (settings.methodology_dir / "delivery.yaml").read_text(encoding="utf-8")
    )
    return tuple(rules["flows"]["substantive_fields"])


def digest(record: dict, fields: tuple[str, ...]) -> str:
    """Отпечаток существенных полей записи выпуска."""
    said = json.dumps({key: record.get(key) for key in fields}, sort_keys=True)
    return hashlib.sha256(said.encode("utf-8")).hexdigest()[:16]
# Выпуски, по которым нужны платежи: в обращении и размещаемые — для
# рефинансирования, с дефолтом — для истории событий. То же правило, что
# у полной доставки (`scripts/events_fetch.py`).
WANTED_STATUSES = ("в обращении", "размещается")


def changed_since(since: str) -> dict[str, dict]:
    """Записи выпусков российских эмитентов, обновлённых с даты: выпуск → запись."""
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
    return {str(item.get("id")): item for item in items}


def wanted(
    changed: dict[str, dict], basis: dict[str, str], fields: tuple[str, ...]
) -> tuple[list[tuple[str, str, str]], int]:
    """Выпуски справочника, по которым спрашивать, и сколько обновлённых пропущено.

    Спрашиваются изменившиеся по существу и выпуски без графика на диске.
    Обновлённый выпуск, у которого отпечаток существенных полей тот же,
    что при прошлой доставке, не спрашивается.
    """
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    found: list[tuple[str, str, str]] = []
    skipped = 0
    for inn in cards:
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            if not (issue.status in WANTED_STATUSES or issue.default or issue.unsettled):
                continue
            missing = not (CACHE / f"flow_{issue.emission_id}.json").exists()
            record = changed.get(issue.emission_id)
            if missing:
                found.append((inn, issue.emission_id, "графика нет"))
            elif record is not None:
                if basis.get(issue.emission_id) == digest(record, fields):
                    skipped += 1
                    continue
                found.append((inn, issue.emission_id, "обновлён"))
    return found, skipped


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
    fields = substantive_fields()
    basis = json.loads(BASIS.read_text(encoding="utf-8")) if BASIS.exists() else {}
    changed = changed_since(since)
    chosen, skipped = wanted(changed, basis, fields)
    asked: list[dict] = []
    failed = 0
    for inn, emission, why in chosen:
        before = failed
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
        # Отпечаток пишется только у доставленного целиком: выпуск с отказом
        # должен перезабраться и завтра.
        if failed == before and emission in changed:
            basis[emission] = digest(changed[emission], fields)
    BASIS.write_text(json.dumps(basis, sort_keys=True), encoding="utf-8")
    (CACHE / f"flows_delta_{today}.json").write_text(
        json.dumps(
            {"since": since, "asked": asked, "skipped": skipped, "failed": failed},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if not failed:
        SINCE.write_text(json.dumps({"since": f"{today}"}), encoding="utf-8")
    new = sum(1 for item in asked if item["why"] == "графика нет")
    print(
        f"графики с {since}: выпусков {len(asked)} (обновлённых по существу "
        f"{len(asked) - new}, без графика {new}), обновлённых не по существу "
        f"{skipped} — не спрошены, отказов {failed}, запросов {cbonds.pace.requested}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
