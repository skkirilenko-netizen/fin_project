"""Доставка рыночных срезов ISS: торги дня и кривая ОФЗ того же дня.

    uv run python scripts/moex_market_fetch.py [--depth-days 196] [--step 7]

**Срез дня и кривая того же дня берутся парой.** Спред считается как
доходность выпуска минус кривая в точке его дюрации, и кривая другого дня
дала бы спред, которого не было: у ОФЗ за полгода уровень сместился
на проценты.

**Срез отдаётся страницами по сотне, кривая — внутридневными записями.**
Первое собирается целиком (`moex.paged`), у второго берётся последняя запись
дня: кривая внутри дня меняется, и «кривая на дату» — это её закрытие.

**Глубина и шаг — доводы прогона, а не свойство доставки.** Недельная сетка
за полгода отвечала на вопрос первого замера — есть ли у биржи история
вообще; дневная за два года есть доставка данных фазы 3, где ориентир
считается перцентилем по дню, и неделя такой ряд не даёт. Оба довода стоят
в отчёте прогона: «дней со срезом 500» без объявленной сетки не говорит,
мерили мы два года или полгода.

**Срез берётся по рынку целиком, а не по нашим выпускам.** Так дешевле —
38 страниц на день против шести на выпуск при пяти тысячах выпусков, — но
дело не в цене: ориентир фазы 3 есть перцентиль ликвидного ядра рынка,
и, собрав только своих, мы посчитали бы перцентиль по перечню, который сами
и задали. Это тот же дефект универсума, что был у списка наблюдения.

**Отказ источника прекращает доставку, а не пропускается.** Публичный
источник без ключа отвечает отказом тогда, когда мы ему надоели, и прогон,
идущий дальше сквозь отказы, дотягивает до конца сетки с дырами вместо дней.
День записывается целиком, поэтому оборванный прогон продолжается следующим
запуском с диска.
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
# **Кривая берётся методом закрытия дня, а не историей внутри дня** (решение
# человека 24.09.2026). `history/engines/stock/zcyc.json?date=` отдаёт около
# девятнадцати тысяч внутридневных записей, из которых нужна одна: 1 427 МБ
# кэша против 1 660 МБ всех срезов торгов разом. Здешний метод отдаёт одну
# строку параметров на закрытие и вдобавок `yearyields` — саму кривую
# в одиннадцати опорных точках, которыми фаза 3 обязана сверить формулу.
ZCYC = "engines/stock/zcyc.json"
CURVES = moex.CACHE / "zcyc_by_day.json"


def curve_of(day: date) -> dict | None:
    """Кривая на закрытие дня: параметры и опубликованные точки.

    `None` — торгов в этот день не было. Точки хранятся рядом с параметрами
    намеренно: по параметрам кривая считается, а точками счёт проверяется,
    и второй запрос за ними разошёлся бы с первым по дню.
    """
    answer = moex.fetch(ZCYC, f"zcyc_day_{day}", {"date": f"{day}", "iss.meta": "off"})
    got = moex.rows(answer, "params")
    if not got:
        return None
    found = dict(got[0])
    found["yearyields"] = [
        {"period": item.get("period"), "value": item.get("value")}
        for item in moex.rows(answer, "yearyields")
    ]
    return found


def _arg(name: str, fallback: int) -> int:
    """Целый довод командной строки; не названный берётся из умолчания."""
    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else fallback


def _applied(rows: list[dict], day: date) -> None:
    """Сверяет, что источник применил отбор по дате.

    **Неподдерживаемое поле отбора ISS пропускает молча** — то же, что у Cbonds
    и с той же ценой: у истории кривой `from`/`till` не применяются вовсе,
    и ответ приходит за сегодня. Строка чужого дня, попавшая в срез, выглядит
    как настоящая, и заметить её нечем, поэтому сверяется каждый день.
    """
    wrong = {str(item.get("TRADEDATE")) for item in rows} - {f"{day}"}
    if wrong:
        raise moex.MoexError(
            f"срез {day}: источник вернул дни {sorted(wrong)[:3]} — "
            "отбор по дате не применён"
        )


def _forget_empty(day: date) -> None:
    """Пустой срез рабочего дня, лежащий на диске с прежних прогонов, — забыть.

    Иначе он брался бы с диска вечно: так 22–25.09.2026 выпали из ряда.
    """
    kept = moex.CACHE / f"xsec_{day}.json"
    if not kept.exists():
        return
    if json.loads(kept.read_text(encoding="utf-8")).get("history"):
        return
    for stale in (
        *moex.CACHE.glob(f"xsec_{day}*.json"),
        *moex.CACHE.glob(f"zcyc_day_{day}.json"),
    ):
        stale.unlink()


def main() -> int:
    """Забирает срезы и кривые; 1 — если источник отказал либо дней нет."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    step = _arg("--step", 7)
    depth = _arg("--depth-days", 196)
    today = date.today()
    days = [today - timedelta(days=shift) for shift in range(0, depth + 1, step)]

    curves: dict[str, dict] = {}
    if CURVES.exists():
        curves = json.loads(CURVES.read_text(encoding="utf-8"))
    done = empty = 0
    unpublished: list[str] = []
    stopped = ""
    for day in days:
        key = f"{day}"
        if day.weekday() < 5:
            _forget_empty(day)
        try:
            rows = moex.paged(
                XSEC, f"xsec_{day}", "history", {"date": key, "iss.meta": "off"}
            )
            _applied(rows, day)
            # **Запись без опубликованных точек считается недобранной.**
            # Прежний метод отдавал только параметры, и отличить «кривой нет»
            # от «кривая взята дорогим методом» можно единственным способом —
            # по составу самой записи.
            got = curves.get(key)
            found = got if got and "yearyields" in got else curve_of(day)
        except moex.MoexError as failure:
            # **Ранняя остановка, а не пропуск дня.** Прогон, идущий дальше
            # сквозь отказы, кончается сеткой с дырами, и по числу дней этого
            # не видно: «дней со срезом 400» одинаково выглядит у полной сетки
            # в 400 дней и у дырявой в 500.
            stopped = f"{day}: {failure}"
            logger.error("доставка остановлена — %s", stopped)
            break
        if not rows and day.weekday() < 5:
            # **Пустой ответ за рабочий день — отказ, а не данные** (решение
            # владельца 25.09.2026). Итоги дня биржа публикует после торгов,
            # и срез, спрошенный раньше, приходит пустым: 22–25.09.2026 такие
            # ответы легли на диск и брались оттуда как «торгов не было».
            # Пустой ответ не хранится, и следующий прогон спросит снова.
            # Праздник в рабочий день выглядит так же и будет спрошен снова
            # — это дешевле, чем принять недоставку за отсутствие торгов.
            for stale in (
                *moex.CACHE.glob(f"xsec_{day}*.json"),
                *moex.CACHE.glob(f"zcyc_day_{day}.json"),
            ):
                stale.unlink()
            curves.pop(key, None)
            unpublished.append(key)
            continue
        if not rows:
            empty += 1
            continue
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
        f"сетка: глубина {depth} дней, шаг {step}, точек {len(days)}. "
        f"Дней со срезом {done}, без торгов {empty}, кривых на диске "
        f"{len(curves)}; запросов к источнику {moex.pace.requested}, "
        f"ответов с диска {moex.pace.from_cache}"
    )
    if unpublished:
        print(
            f"рабочих дней без итогов торгов {len(unpublished)}: "
            f"{', '.join(unpublished)} — не данные, а недоставка; спросим снова"
        )
    if stopped:
        print(f"**Доставка остановлена отказом источника** — {stopped}")
        return 1
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
