"""Доставка с ISS: перечень торгуемых облигаций и карточки выпусков риска.

    uv run python scripts/moex_fetch.py

**Перевод выпуска в сектор повышенного риска — событие, и биржа его
датирует.** У ЕвроТранса режим TQCB кончается 05.08.2026, TQRD начинается
06.08.2026: это не наше суждение о выпуске, а решение биржи с датой.

Два запроса разного рода. Перечень торгуемых облигаций отдаётся целиком
одним ответом и говорит, в каком режиме выпуск торгуется **сейчас**; когда
он туда переведён, стоит в карточке выпуска — по запросу на выпуск.
Карточки берутся только у тех, кто в режиме риска: у прочих даты перевода
нет, и спрашивать о ней нечего.

**Ответы кладутся на диск** (`data/raw/moex/`), повторный прогон сети
не дёргает. ISS предела частоты не объявляет, и мы держим свой.
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import moex  # noqa: E402
from finlib.sources.moex_risk import RISK_BOARDS, TRADED  # noqa: E402

logger = logging.getLogger(__name__)

CBONDS = Path("data/raw/cbonds")


def isin_to_issue() -> dict[str, tuple[str, str]]:
    """ISIN выпусков справочника: ISIN → (ИНН, наименование выпуска)."""
    cards = json.loads((CBONDS / "emitents.json").read_text(encoding="utf-8"))
    found: dict[str, tuple[str, str]] = {}
    for inn in cards:
        path = CBONDS / f"emissions_{inn}.json"
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            code = str(item.get("isin_code") or "").strip()
            if code:
                found[code] = (inn, str(item.get("document_rus") or code))
    return found


def main() -> int:
    """Забирает перечень и карточки; 1 — если источник не ответил."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        # **Перечень — текущее состояние, и он переспрашивается каждый раз.**
        # С диска он устаревает: с 22.09 по 25.09.2026 стадия брала его
        # из кэша и писала «done», а перевод в сектор риска не был бы виден.
        answer = moex.fetch(
            "engines/stock/markets/bonds/securities.json",
            TRADED,
            {"iss.meta": "off", "iss.only": "securities"},
            refresh=True,
        )
    except moex.MoexError as failure:
        print(f"перечень торгуемых облигаций не получен: {failure}")
        return 1
    traded = moex.rows(answer, "securities")
    ours = isin_to_issue()
    risky = [
        item
        for item in traded
        if str(item.get("BOARDID")) in RISK_BOARDS
        and str(item.get("ISIN") or "") in ours
    ]
    print(
        f"торгуемых облигаций {len(traded)}, из них в режимах риска "
        f"{sum(1 for item in traded if str(item.get('BOARDID')) in RISK_BOARDS)}; "
        f"наших среди них {len(risky)}"
    )
    done = 0
    for item in risky:
        secid = str(item.get("SECID"))
        try:
            moex.fetch(
                f"securities/{secid}.json", f"security_{secid}", {"iss.meta": "off"}
            )
            done += 1
        except moex.MoexError as failure:
            logger.error("карточка %s: %s", secid, str(failure)[:100])
    print(
        f"карточек выпусков получено {done} из {len(risky)}; "
        f"запросов к источнику {moex.pace.requested}, "
        f"ответов с диска {moex.pace.from_cache}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
