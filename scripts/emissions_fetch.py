"""Выпуски эмитентов списка: статус, дефолт, погашения, оферты, поручитель.

    uv run python scripts/emissions_fetch.py [--limit N]

Один запрос на эмитента, ответы сохраняются на диск в исходном виде.
Событийный слой строится по ним: статус выпуска и признак неурегулированного
дефолта — единственные сведения о том, что случилось **между** отчётными
датами, а годовая отчётность их не видит по устройству.

Перечень берётся из карточек справочника: снимок нужен и по тем эмитентам,
у кого отчётности у нас пока нет.
"""

import json
import logging
import sys
from datetime import date
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

CARDS = Path("data/raw/cbonds/emitents.json")
METHOD = "get_emissions"


STATE = cbonds.CACHE / "emissions_state.json"


def changed_issuers(since: str, known: set[str]) -> set[str]:
    """ИНН эмитентов справочника, у которых выпуски обновлены с даты.

    Отбор по дате обновления источник применяет (проба 25.09.2026
    с отрицательным контролем), но клиент сверяет только `eq` — поэтому
    запись раньше даты здесь означает пропущенный отбор, и окно
    не принимается.
    """
    found = cbonds.fetch(
        METHOD,
        f"emissions_changed_{since}",
        filters=(
            {"field": "emitent_country", "operator": "eq", "value": "1"},
            {"field": "updating_date", "operator": "ge", "value": since},
        ),
        limit=1000,
        refresh=True,
    )
    items = found.get("items", [])
    early = [item for item in items if str(item.get("updating_date") or "")[:10] < since]
    if early:
        raise cbonds.FilterIgnoredError(
            f"отбор updating_date ≥ {since} не применён: {len(early)} записей раньше"
        )
    return {str(item.get("emitent_inn") or "") for item in items} & known


def _state() -> dict:
    """Дата прошлого прогона и эмитенты, обновлённые отбором с полного обхода."""
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"since": None, "by_delta": []}


def main() -> int:
    """Выпуски эмитентов справочника: отбором по дате, по понедельникам — все.

    **Ежедневно — отбором, по понедельникам — полным обходом как контролем
    отбора** (решение владельца 25.09.2026). Отбор стоит одного-двух запросов
    плюс по запросу на эмитента с изменившимися выпусками; полный обход —
    977. Полный обход сверяет себя с отбором: эмитент, чей ответ изменился,
    а отбор с прошлого полного обхода его не назвал, — пропуск отбора,
    и он печатается числом и перечнем.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not CARDS.exists():
        print("карточек эмитентов на диске нет: перечень брать негде")
        return 1
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    today = date.today()
    state = _state()
    full = "--full" in sys.argv or today.weekday() == 0 or not state["since"]
    if full:
        chosen = list(cards)
    else:
        delta = changed_issuers(state["since"], set(cards))
        chosen = sorted(delta)
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    chosen = chosen[:limit] if limit else chosen
    before = {
        inn: (cbonds.CACHE / f"emissions_{inn}.json").read_text(encoding="utf-8")
        for inn in chosen
        if full and (cbonds.CACHE / f"emissions_{inn}.json").exists()
    }
    done = failed = 0
    for inn in chosen:
        # **Обход обновляет, а не читает кэш.** Прежде ответ, лежавший
        # на диске, брался оттуда, и еженедельная стадия с 22.09.2026 не
        # сделала ни одного запроса: признаки дефолта и статусы выпусков
        # застыли. Ответ, полученный сегодня, не переспрашивается — прерванный
        # обход продолжается с места обрыва, а не начинается заново.
        path = cbonds.CACHE / f"emissions_{inn}.json"
        stale = not path.exists() or (
            date.fromtimestamp(path.stat().st_mtime) < today
        )
        try:
            cbonds.fetch(
                METHOD,
                f"emissions_{inn}",
                filters=({"field": "emitent_inn", "operator": "eq", "value": inn},),
                limit=200,
                refresh=stale,
            )
            done += 1
        except (cbonds.CbondsError, httpx.TransportError) as failure:
            # Сбой одного эмитента обход не обрывает: прежний ответ остаётся
            # на диске, и следующий обход его переспросит.
            failed += 1
            logger.error("%s: %s", inn, str(failure)[:120])
    print(
        f"{'полный обход' if full else 'отбор с ' + state['since']}: эмитентов "
        f"{len(chosen)}, ответов получено {done}, отказов {failed}, "
        f"запросов {cbonds.pace.requested}, с диска {cbonds.pace.from_cache}"
    )
    missed: list[str] = []
    if full:
        # Контроль отбора: изменился ответ, а отбор эмитента не называл.
        seen = set(state.get("by_delta", []))
        changed = [
            inn
            for inn, was in before.items()
            if _items(
                (cbonds.CACHE / f"emissions_{inn}.json").read_text(encoding="utf-8")
            )
            != _items(was)
        ]
        missed = [inn for inn in changed if inn not in seen]
        if state["since"]:
            print(
                f"контроль отбора: ответ изменился у {len(changed)} эмитентов, "
                f"из них отбор с прошлого полного обхода не назвал {len(missed)}"
                + (f": {', '.join(missed[:20])}" if missed else "")
            )
    (cbonds.CACHE / f"emissions_delta_{today}.json").write_text(
        json.dumps(
            {"full": full, "since": state["since"], "asked": chosen,
             "failed": failed, "missed_by_filter": missed},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    if not failed:
        STATE.write_text(
            json.dumps(
                {
                    "since": f"{today}",
                    "by_delta": [] if full else sorted(
                        set(state.get("by_delta", [])) | set(chosen)
                    ),
                }
            ),
            encoding="utf-8",
        )
    return 0


def _items(raw: str) -> list:
    """Записи ответа без служебных полей: сравнивается содержание, а не время."""
    return sorted(
        json.dumps(item, sort_keys=True, ensure_ascii=False)
        for item in json.loads(raw).get("items", [])
    )


if __name__ == "__main__":
    sys.exit(main())
