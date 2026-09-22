"""Доставка рыночных срезов ISS: торги дня и кривая ОФЗ того же дня.

    uv run python scripts/moex_market_fetch.py [--weeks 28] [--step 7]

**Срез дня и кривая того же дня берутся парой.** Спред считается как
доходность выпуска минус кривая в точке его дюрации, и кривая другого дня
дала бы спред, которого не было: у ОФЗ за полгода уровень сместился
на проценты.

**Срез отдаётся страницами по сотне, кривая — внутридневными записями.**
Первое собирается целиком (`moex.paged`), у второго берётся последняя запись
дня: кривая внутри дня меняется, и «кривая на дату» — это её закрытие.

Сетка недельная намеренно. Ежедневная — это 35 запросов на дату против 28
дат, то есть в семь раз больше обращений к публичному источнику без ключа;
для первого замера порогов недельной сетки достаточно, а день события
берётся отдельно и по выпуску.
"""

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import moex  # noqa: E402

logger = logging.getLogger(__name__)

XSEC = "history/engines/stock/markets/bonds/securities.json"
ZCYC = "history/engines/stock/zcyc.json"
CURVES = moex.CACHE / "zcyc_by_day.json"


def curve_of(day: date) -> dict | None:
    """Параметры кривой на закрытие дня; None — торгов в этот день не было."""
    answer = moex.fetch(ZCYC, f"zcyc_{day}", {"date": f"{day}", "iss.meta": "off"})
    got = moex.rows(answer, "params")
    if not got:
        return None
    # Внутри дня кривая меняется; «кривая на дату» — её последняя запись.
    return max(got, key=lambda item: str(item.get("tradetime") or ""))


def main() -> int:
    """Забирает срезы и кривые; 1 — если не удалось ни одного дня."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    weeks = int(sys.argv[sys.argv.index("--weeks") + 1]) if "--weeks" in sys.argv else 28
    step = int(sys.argv[sys.argv.index("--step") + 1]) if "--step" in sys.argv else 7
    today = date.today()
    days = [today - timedelta(days=step * item) for item in range(weeks)]

    curves: dict[str, dict] = {}
    if CURVES.exists():
        curves = json.loads(CURVES.read_text(encoding="utf-8"))
    done = empty = 0
    for day in days:
        key = f"{day}"
        try:
            rows = moex.paged(XSEC, f"xsec_{day}", "history", {"date": key, "iss.meta": "off"})
        except moex.MoexError as failure:
            logger.error("срез %s: %s", day, str(failure)[:100])
            continue
        if not rows:
            empty += 1
            continue
        if key not in curves:
            found = curve_of(day)
            if found is None:
                logger.warning("кривой на %s нет: спред этого дня не посчитать", day)
            else:
                curves[key] = found
        done += 1
        logger.info("%s: строк среза %d", day, len(rows))
        # Кривые пишутся после каждого дня, а не в конце: прогон идёт минуты,
        # и оборванный на середине не должен оставлять диск без того,
        # что уже получено.
        CURVES.write_text(
            json.dumps(curves, ensure_ascii=False, default=str), encoding="utf-8"
        )

    CURVES.write_text(
        json.dumps(curves, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(
        f"дней со срезом {done}, без торгов {empty}, кривых на диске "
        f"{len(curves)}; запросов к источнику {moex.pace.requested}, "
        f"ответов с диска {moex.pace.from_cache}"
    )
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
