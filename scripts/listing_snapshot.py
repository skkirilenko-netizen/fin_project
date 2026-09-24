"""Ежедневный снимок уровня листинга: истории смен у источника нет вовсе.

    uv run python scripts/listing_snapshot.py            # снимок на сегодня
    uv run python scripts/listing_snapshot.py --refresh  # переписать дневной

**ISS отдаёт только текущий уровень.** У бумаги в срезе доски стоит
`LISTLEVEL` — первый, второй или третий, — и метода, который показал бы,
когда он менялся, у источника нет: в справочнике ISS есть история торгов
по доскам, но не история котировального списка. Значит, история смен берётся
из последовательности снимков либо не берётся никогда — тот же случай, что
у рейтингов.

**Смена уровня и приостановка торгов — событие того же рода, что перевод
в сектор повышенного риска**: решение биржи с датой, а не наше суждение
о величинах. Правил из снимков пока не делается и в маршрут они не входят —
сперва надо, чтобы накопилось, на чём мерить (решение владельца 24.09.2026,
фаза 4 дорожной карты закрыта именно поэтому).

**Снимок дня не перезаписывается.** Файл — доказательная база: два прогона
в один день обязаны дать один файл, иначе «история» окажется историей наших
прогонов. Повторный прогон того же дня ничего не делает и говорит об этом.

Запросов: **один на весь снимок** — срез доски отдаётся целиком.
"""

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources.moex import fetch, rows  # noqa: E402

logger = logging.getLogger(__name__)

SNAPSHOTS = Path("data/raw/moex/listing")
# Основная доска акций: уровень котировального списка объявлен только у неё.
# Бумага вне её в снимок не попадает, и это не пробел, а свойство предмета:
# уровня листинга у неё нет.
BOARD = "/engines/stock/markets/shares/boards/TQBR/securities.json"


def snapshot(today: date, refresh: bool = False) -> Path | None:
    """Складывает снимок дня на диск; None — снимок дня уже есть."""
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    where = SNAPSHOTS / f"{today:%Y-%m-%d}.json"
    if where.exists() and not refresh:
        logger.info("снимок %s уже есть — повторный прогон ничего не меняет", today)
        return None
    # Имя кэша называет день: ответ источника за вчера и за сегодня — разные
    # ответы, и одно имя на оба означало бы чтение вчерашнего.
    answer = fetch(BOARD, f"listing_{today:%Y-%m-%d}", {"iss.meta": "off"})
    seen = rows(answer, "securities")
    # **Запись хранится целиком.** Снимок — единственное, чего не переспросишь
    # задним числом, и решать сегодня, какие поля понадобятся через полгода,
    # значит решать за того, кто будет смотреть.
    where.write_text(
        json.dumps(
            {"as_of": f"{today}", "board": "TQBR", "securities": seen},
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    levels: dict[str, int] = {}
    for item in seen:
        level = item.get("LISTLEVEL")
        if level is not None:
            levels[str(level)] = levels.get(str(level), 0) + 1
    logger.info(
        "снимок %s: бумаг %d, по уровням %s",
        today,
        len(seen),
        ", ".join(f"{name}: {count}" for name, count in sorted(levels.items())),
    )
    return where


ISSUERS = Path("data/raw/moex/share_issuers.json")
# Связь бумаги с эмитентом почти не меняется: новые выпуски акций — событие
# редкое. Перечень обновляется раз в неделю, а не ежедневно: восемь запросов
# в день на то, что не движется, — плата ни за что.
ISSUERS_EVERY_DAYS = 7


def issuers(today: date) -> dict[str, str]:
    """SECID → ИНН эмитента: связь даёт сама биржа полем `emitent_inn`.

    **Срез доски ИНН не содержит**, и связывать по наименованию нельзя —
    «ПАО "Артген"» у биржи против «АРТГЕН» в реестре. Поиск ISS отдаёт ИНН
    прямо, и это единственная надёжная связь.
    """
    if ISSUERS.exists():
        age = today.toordinal() - date.fromtimestamp(
            ISSUERS.stat().st_mtime
        ).toordinal()
        if age < ISSUERS_EVERY_DAYS:
            return json.loads(ISSUERS.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    start = 0
    while start <= 3000:
        answer = fetch(
            "/securities.json",
            f"shares_page_{start}",
            {
                "engine": "stock",
                "market": "shares",
                "is_trading": "1",
                "limit": 100,
                "start": start,
                "iss.meta": "off",
            },
        )
        page = rows(answer, "securities")
        if not page:
            break
        for item in page:
            if item.get("group") == "stock_shares" and item.get("emitent_inn"):
                found[str(item["secid"])] = str(item["emitent_inn"])
        start += 100
    ISSUERS.parent.mkdir(parents=True, exist_ok=True)
    ISSUERS.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    logger.info("связь бумаг с эмитентами обновлена: %d бумаг", len(found))
    return found


def main(argv: list[str] | None = None) -> int:
    """Делает снимок дня; 1 — источник не ответил."""
    parser = argparse.ArgumentParser(description="Снимок уровня листинга акций")
    parser.add_argument("--refresh", action="store_true", help="переписать снимок дня")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        where = snapshot(date.today(), refresh=args.refresh)
        issuers(date.today())
    except Exception as failure:  # noqa: BLE001 — причина называется словами
        logger.error("снимок не сделан: %s", failure)
        return 1
    print(where or "снимок этого дня уже лежит на диске")
    return 0


if __name__ == "__main__":
    sys.exit(main())
