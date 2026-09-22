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
from pathlib import Path

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
    for inn in chosen:
        try:
            cbonds.fetch(
                METHOD,
                f"emissions_{inn}",
                filters=({"field": "emitent_inn", "operator": "eq", "value": inn},),
                limit=200,
            )
            done += 1
        except cbonds.CbondsError as failure:
            failed += 1
            logger.error("%s: %s", inn, str(failure)[:120])
    print(
        f"эмитентов {len(chosen)}: ответов получено {done}, отказов {failed}, "
        f"запросов {cbonds.pace.requested}, с диска {cbonds.pace.from_cache}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
