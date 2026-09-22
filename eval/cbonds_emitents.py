"""Карточки эмитентов Cbonds: признак SPV, отрасль, категория МСП.

**Признак берётся из данных, а не из наименования.** «…Финанс» в названии —
примета, по которой в один вид попали бы Русбонд-Удобрения и Коршуновский ГОК;
`emitent_spv` справочника эмитентов отвечает прямо.

    uv run python eval/cbonds_emitents.py            # добор недостающих
    uv run python eval/cbonds_emitents.py --limit 50 # частями

Один запрос на ИНН: отбор `in` источник не поддерживает — проверено, по трём
ИНН возвращает ноль записей. Ответы складываются в один файл на диске,
и повторный запуск спрашивает только то, чего в нём нет.

**Перечень задаётся долгом, а не отчётностью** (фаза 1 дорожной карты). Прежде
он брался из доставок МСФО, и карточки не было у эмитента, раскрывающего
только РСБУ, — то есть у 496 из 702 эмитентов с выпусками в обращении. Без
карточки нет ни статуса, ни отрасли, ни признака финансирующей структуры,
а значит, нет ни выхода из списка, ни отраслевого гашения: эмитент не просто
отсутствовал в списке — отсутствие его не было видно даже как отсутствие.
Универсум МСФО остаётся в перечне: эмитент без облигаций, у которого
отчётность есть, из справочника не выбрасывается.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.config import settings  # noqa: E402
from finlib.sources import cbonds  # noqa: E402

logger = logging.getLogger(__name__)

CARDS = Path("data/raw/cbonds/emitents.json")
# Предел ночи: источник объявляет 10 000 запросов в сутки, и прогон обязан
# останавливаться раньше, а не выяснять предел отказом.
BUDGET = 1000


def known() -> dict[str, dict]:
    """Уже собранные карточки."""
    if CARDS.exists():
        return json.loads(CARDS.read_text(encoding="utf-8"))
    return {}


def main(argv: list[str] | None = None) -> int:
    """Доносит карточки эмитентов до полного набора."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=BUDGET, help="сколько добрать")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR, format="%(message)s")

    bonds = cbonds.bond_issuers()
    rows = cbonds.msfo_universe()
    reporting = {(row.get("emitent_inn") or "").strip() for row in rows} - {""}
    issuers = sorted(set(bonds) | reporting)
    cards = known()
    missing = [inn for inn in issuers if inn not in cards]
    print(
        f"эмитентов {len(issuers)}: с выпусками в обращении {len(bonds)}, "
        f"с отчётностью МСФО у источника {len(reporting)}, "
        f"и то и другое {len(set(bonds) & reporting)}"
    )
    print(f"карточек есть {len(cards)}, добрать {len(missing)}")
    if not settings.cbonds_ready and missing:
        print("обратиться к источнику нечем: CBONDS_LOGIN и CBONDS_PASSWORD не заданы")
        return 1

    asked = 0
    for inn in missing[: args.limit]:
        body = {
            "auth": {
                "login": settings.cbonds_login,
                "password": settings.cbonds_password,
            },
            "filters": [{"field": "emitent_inn", "operator": "eq", "value": inn}],
            "quantity": {"limit": 5, "offset": 0},
        }
        response = httpx.post(
            f"{settings.cbonds_base_url}/get_emitents/", json=body, timeout=30.0
        )
        time.sleep(0.6)
        asked += 1
        if response.status_code != 200:
            cards[inn] = {"error": response.text[:120]}
            continue
        found = response.json().get("items") or []
        cards[inn] = found[0] if found else {"error": "не найден"}
        if asked % 50 == 0:
            CARDS.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
            print(f"  ...{asked} из {len(missing[: args.limit])}", flush=True)
    CARDS.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    spv = sum(1 for card in cards.values() if str(card.get("emitent_spv")) == "1")
    errors = sum(1 for card in cards.values() if "error" in card)
    print(
        f"запросов сделано {asked}; карточек {len(cards)}, из них с признаком SPV "
        f"{spv}, не найдено {errors}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
