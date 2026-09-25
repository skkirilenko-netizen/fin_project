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


def main() -> int:
    """Забирает выпуски по всем эмитентам справочника; 1 — если их нет."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not CARDS.exists():
        print("карточек эмитентов на диске нет: перечень брать негде")
        return 1
    cards = json.loads(CARDS.read_text(encoding="utf-8"))
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 0
    chosen = list(cards)[:limit] if limit else list(cards)
    done = failed = 0
    today = date.today()
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
        f"эмитентов {len(chosen)}: ответов получено {done}, отказов {failed}, "
        f"запросов {cbonds.pace.requested}, с диска {cbonds.pace.from_cache}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
