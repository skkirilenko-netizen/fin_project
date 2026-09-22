"""Поручители, платежи и оферты выпусков: доставка на диск.

    uv run python scripts/events_fetch.py [--limit N]
                                         [--only guarantors|flows|defaults]

Три метода, открытые источником 22.09.2026:

- `get_emission_guarantors` — поручители, отбор по эмитенту: один запрос
  на эмитента;
- `get_flow_new` — платежи выпуска (купоны, погашение), отбор по выпуску:
  один запрос на выпуск. Берутся выпуски в обращении и размещаемые — по ним
  считается рефинансирование, — и выпуски с дефолтом: по пропущенному платежу
  видна **дата события**, которой в самом выпуске нет;
- `get_offert` — оферты выпуска, отбор по выпуску.

**Оферты забираются по всем выпускам в обращении, и это решила контрольная
выборка.** Сперва они запрашивались только у выпусков, объявивших дату
в записи (`offert_date_put`), — расчёт был на то, что запись называет
ближайшую оферту сама. Контрольная выборка тех, что даты не объявили, нашла
оферты у четырёх выпусков, а сверка объявленных — случай, где запись
называет **не ближайшую**: у «Русбонд-Удобрения, 001Р-СПВБ-01» объявлено
29.03.2027, а метод даёт 28.09.2026, то есть внутри годового окна. Экономия
на запросах стоила бы потери ближайшей оферты — той самой величины, ради
которой всё и считается.

**Ответы кладутся на диск в исходном виде**, повторный прогон сети не дёргает.
Источник держит 30 запросов в минуту и объявляет это сам; прогон выжидает.
"""

import json
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")
CARDS = CACHE / "emitents.json"
# Выпуски, по которым нужны платежи: в обращении и размещаемые — для
# рефинансирования, с дефолтом — для даты события.
WANTED_STATUSES = ("в обращении", "размещается")


def ask(method: str, name: str, key: str, value: str, limit: int) -> bool:
    """Один запрос с повтором; False — ответа нет, причина в журнале.

    **Обрыв связи доставку не прекращает.** Тысяча запросов идёт больше часа,
    и падение на девятисотом означало бы, что час потрачен впустую: ответы
    лежат на диске, а перечень недошедших — нигде. Отказ источника и обрыв
    различаются: первое сведение о запросе, второе о сети.
    """
    for attempt in (1, 2):
        try:
            cbonds.fetch(
                method,
                name,
                filters=({"field": key, "operator": "eq", "value": value},),
                limit=limit,
            )
            return True
        except cbonds.CbondsError as failure:
            logger.error("%s %s: %s", method, name, str(failure)[:100])
            return False
        except httpx.HTTPError as failure:
            logger.error(
                "%s %s: обрыв связи (%s), попытка %d",
                method,
                name,
                type(failure).__name__,
                attempt,
            )
            time.sleep(10.0)
    return False


def emissions(defaults_only: bool = False) -> list[tuple[str, str, str]]:
    """Выпуски для платежей: ИНН, идентификатор, наименование.

    `defaults_only` оставляет выпуски с дефолтом: по ним нужна **дата
    события**, и их девять десятков против тысячи двухсот в обращении —
    правило давности проверяется, не дожидаясь всей доставки.
    """
    found: list[tuple[str, str, str]] = []
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    for inn in cards:
        path = CACHE / f"emissions_{inn}.json"
        if not path.exists():
            continue
        issues, _ = issues_of(inn)
        by_name = {item.name: item for item in issues}
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            name = str(item.get("document_rus") or item.get("isin_code") or "")
            issue = by_name.get(name)
            marked = issue is not None and (issue.defaulted or issue.settled_default)
            status = str(item.get("status_name_rus") or "").strip().lower()
            if defaults_only:
                if marked:
                    found.append((inn, str(item["id"]), name))
                continue
            if status in WANTED_STATUSES or marked:
                found.append((inn, str(item["id"]), name))
    return found


def guarantors(limit: int) -> None:
    """Забирает поручителей по каждому эмитенту справочника."""
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    chosen = list(cards)[:limit] if limit else list(cards)
    done = 0
    for inn in chosen:
        emitent = str(cards[inn].get("id") or "")
        if not emitent:
            continue
        done += ask(
            "get_emission_guarantors",
            f"guarantors_{inn}",
            "emitent_id",
            emitent,
            100,
        )
    print(f"поручители: эмитентов {done} из {len(chosen)}")


def flows(limit: int, defaults_only: bool) -> None:
    """Забирает платежи выпусков и оферты там, где они нужны."""
    wanted = emissions(defaults_only=defaults_only)
    chosen = wanted[:limit] if limit else wanted
    paid = offers = 0
    for _, emission, _ in chosen:
        paid += ask(
            "get_flow_new", f"flow_{emission}", "emission_id", emission, 500
        )
        if defaults_only:
            # Оферта говорит о рефинансировании, а не о дате дефолта.
            continue
        offers += ask(
            "get_offert", f"offert_{emission}", "emission_id", emission, 100
        )
    print(f"платежи: выпусков {paid} из {len(chosen)}; оферты: запрошено {offers}")


def main() -> int:
    """Забирает поручителей и платежи; 1 — если справочника эмитентов нет."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not CARDS.exists():
        print("карточек эмитентов на диске нет: перечень брать негде")
        return 1
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    only = sys.argv[sys.argv.index("--only") + 1] if "--only" in sys.argv else ""

    if only in ("", "guarantors"):
        guarantors(limit)
    if only in ("", "flows", "defaults"):
        flows(limit, defaults_only=only == "defaults")

    print(
        f"запросов к источнику {cbonds.pace.requested}, ответов с диска "
        f"{cbonds.pace.from_cache}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
