"""Разведка фазы 4: что ISS даёт по акциям и как они связываются с нашим кругом.

    uv run python eval/moex_shares_probe.py > data/output/shares_probe.md

**Разведка, а не слой.** Правил отсюда не делается и код слоя не пишется:
сперва надо знать, что источник отдаёт, на какой глубине и чем связывается
с эмитентами, которых мы уже маршрутизируем.

**Связь найдена у самой биржи, и она по ИНН.** Поиск ISS
(`/iss/securities.json`) отдаёт поле `emitent_inn` — то есть связывать
по наименованию не нужно вовсе. Это важнее, чем кажется: сопоставление
по наименованию у нас уже стоило 68 ложных срабатываний на одной проверке,
а «ПАО "Артген"» у биржи против «АРТГЕН» в реестре — ровно такой случай.

**Что здесь измеряется:** сколько эмитентов нашего круга имеют торгуемые
акции, сколько из них с облигациями в обращении, какова глубина истории
и состав полей, есть ли дивиденды и уровень листинга, какие индексы
доступны и насколько расширится периметр, если добавить эмитентов
только с акциями.
"""

import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.scoring.routing_store import cards  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.moex import fetch, rows  # noqa: E402

logger = logging.getLogger(__name__)

# Уровни котировального списка: первый и второй — это и есть «котировальный
# список», третий — некотировальная часть. Число, а не слово: биржа отдаёт
# `LISTLEVEL` целым.
QUOTED = (1, 2)


def traded_shares() -> list[dict]:
    """Все торгуемые акции биржи с ИНН эмитента, одним перечнем.

    Берётся поиском ISS, а не срезом доски: у среза доски ИНН нет вовсе,
    а связь по наименованию — то самое, чего мы избегаем.
    """
    found: list[dict] = []
    start = 0
    while True:
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
        found += page
        start += 100
        if start > 3000:
            # Предел на случай, если источник перестанет отдавать пустую
            # страницу: разведка не должна ходить в сеть бесконечно.
            logger.warning("перечень акций длиннее трёх тысяч — обрыв")
            break
    return found


def listing_levels() -> dict[str, int]:
    """Уровень котировального списка по бумагам основной доски."""
    answer = fetch(
        "/engines/stock/markets/shares/boards/TQBR/securities.json",
        "shares_tqbr",
        {"iss.meta": "off"},
    )
    return {
        item["SECID"]: item["LISTLEVEL"]
        for item in rows(answer, "securities")
        if item.get("LISTLEVEL") is not None
    }


def history_span(secid: str) -> list[dict]:
    """Глубина истории по каждой доске бумаги — из ответа самой биржи.

    **Глубина не выясняется перебором лет, а объявлена источником.** Блок
    `boards` ответа `/securities/{secid}.json` несёт `history_from`
    и `history_till` у каждой доски: это и есть ответ, а перебор лет
    отвечал бы про наш запрос, а не про источник. У Сбербанка так видно,
    что TQBR идёт с 2013 года, а до него бумага торговалась на EQBR
    с 2011-го — и ряд, собранный по одной доске, начался бы позже правды.
    """
    answer = fetch(f"/securities/{secid}.json", f"security_{secid}", {"iss.meta": "off"})
    return [
        item
        for item in rows(answer, "boards")
        if item.get("market") == "shares" and item.get("history_from")
    ]


def history_fields(secid: str) -> tuple[list[str], int]:
    """Поля дневной истории и число дней в текущем году (страница ISS)."""
    # **Имя кэша называет запрос целиком, включая отбор.** Одно имя на два
    # разных запроса — и второй читает ответ первого: так в этой же разведке
    # перечень индексов пришёл пустым, потому что лежал под именем прежней
    # пробы. Правило у модуля источника объявлено, и нарушил его я.
    year = date.today().year
    answer = fetch(
        f"/history/engines/stock/markets/shares/securities/{secid}.json",
        f"shares_history_{secid}_from_{year}",
        {"from": f"{year}-01-01", "iss.meta": "off"},
    )
    seen = rows(answer, "history")
    return (list(seen[0]) if seen else []), len(seen)


def dividends(secid: str) -> str:
    """Что источник отвечает про дивиденды; пусто — отдаёт их.

    **Два пути проверены, и оба закрыты.** Путь `/securities/{secid}/
    dividends.json` молча отдаёт страницу самой бумаги — то есть
    несуществующий подпуть источник не отвергает, а подменяет ответом
    родителя; это тот же класс, что «отбор по дате применяется не везде».
    Объявленный в справочнике `/iss/cci/corp-actions/dividends` отвечает
    HTML со словами «Информация доступна только подписчикам».
    """
    answer = fetch(
        f"/securities/{secid}/dividends.json", f"dividends_{secid}", {"iss.meta": "off"}
    )
    if rows(answer, "dividends"):
        return ""
    return "подпуть подменяется страницей бумаги; сам метод — по подписке"


def indices() -> list[dict]:
    """Перечень индексов биржи: бумаги рынка `index`, а не аналитика по ним."""
    answer = fetch(
        "/engines/stock/markets/index/securities.json",
        "indices_market_securities",
        {"iss.meta": "off"},
    )
    return rows(answer, "securities")


def main() -> int:
    """Печатает разведку: связь, глубину, дивиденды, индексы, периметр."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    known = cards()
    bonds = set(bond_issuers())
    shares = traded_shares()
    levels = listing_levels()

    print("# Разведка фазы 4: акции\n")
    print(
        "Правил отсюда не делается и кода слоя не пишется: разведка отвечает, "
        "что источник отдаёт и чем связывается с нашим кругом.\n"
    )

    print("## Связь эмитентов\n")
    print(
        "**Связь по ИНН, и её даёт сама биржа.** Поиск ISS отдаёт поле "
        "`emitent_inn` у каждой бумаги — сопоставлять по наименованию "
        "не нужно вовсе. Это надёжнее на порядок: «ПАО \"Артген\"» у биржи "
        "против «АРТГЕН» в реестре — тот самый случай, на котором проверка "
        "искажения наименования дала 68 срабатываний вместо двух.\n"
    )
    by_group = Counter(item["group"] for item in shares)
    print("| Род бумаги | Бумаг в обращении |")
    print("|---|---|")
    for group, count in by_group.most_common():
        print(f"| {group} | {count} |")

    ours = {
        item["emitent_inn"]
        for item in shares
        if item["group"] == "stock_shares" and item.get("emitent_inn")
    }
    inside = ours & set(known)
    with_bonds = inside & bonds
    print(
        f"\nЭмитентов с торгуемыми акциями — **{len(ours)}**. Из них в нашем "
        f"круге (977 карточек) — **{len(inside)}**, и у **{len(with_bonds)}** "
        f"из них есть выпуски облигаций в обращении; у "
        f"{len(inside) - len(with_bonds)} акции есть, а облигаций в обращении "
        "нет.\n"
    )
    outside = ours - set(known)
    quoted_outside = {
        item["emitent_inn"]
        for item in shares
        if item["emitent_inn"] in outside
        and levels.get(item["secid"]) in QUOTED
    }
    print(
        f"**Насколько расширится периметр.** Эмитентов с акциями вне нашего "
        f"круга — **{len(outside)}**; из них в котировальном списке первого "
        f"и второго уровней — **{len(quoted_outside)}**. Это и есть цена "
        "решения «добавить эмитентов только с акциями».\n"
    )
    seen = Counter(
        levels.get(item["secid"])
        for item in shares
        if item["group"] == "stock_shares"
    )
    print("| Уровень листинга | Бумаг |")
    print("|---|---|")
    for level, count in sorted(seen.items(), key=lambda item: (item[0] is None, item[0])):
        print(f"| {level if level is not None else 'не на основной доске'} | {count} |")

    print("\n## История торгов\n")
    fields, this_year = history_fields("SBER")
    print(f"Поля дневной истории: {', '.join(fields)}.\n")
    print(
        "**Глубина объявлена самим источником**, а не выясняется перебором "
        "лет: у каждой доски бумаги стоят `history_from` и `history_till`. "
        "Ряд, собранный по одной доске, начался бы позже правды — режимы "
        "торгов менялись.\n"
    )
    print("| Бумага | Доска | История с | по |")
    print("|---|---|---|---|")
    for secid in ("SBER", "GAZP", "AFLT"):
        for board in history_span(secid):
            print(
                f"| {secid} | {board['boardid']} | {board['history_from']} "
                f"| {board['history_till']} |"
            )
    print(
        f"\nЗа текущий год страница ISS отдала {this_year} дней — это её "
        "предел на запрос, а не глубина: перечень берётся страницами.\n"
    )

    print("## Дивиденды\n")
    refused = dividends("SBER")
    if refused:
        print(
            f"**Публичный ISS дивидендов не отдаёт**: {refused}. Cbonds "
            "их тоже не отдаёт — `get_stocks_dividends_v2` и "
            "`get_stocks_full` отвечают «invalid resource name», то есть "
            "метод есть, доступа к нему нет.\n"
        )
        print(
            "Следствие для слоя: **доходность с учётом дивидендов пока "
            "не считается ничем из имеющегося**. Это не мелочь — без неё "
            "дивидендный разрыв читается как падение цены, и у эмитента "
            "с дивидендом в десять процентов признак сработает ровно "
            "в день отсечки.\n"
        )
    else:
        print("метод дивидендов отвечает — проверить состав полей.\n")

    print("## Индексы\n")
    names = indices()
    # **Перечень рынка индексов — не перечень индексов акций.** Из 868 бумаг
    # большинство — iNAV биржевых фондов и товарные индексы НТБ; отбирать
    # надо объявленные, а не первые попавшиеся.
    wanted = (
        "IMOEX", "MCFTR", "RTSI", "MOEXBC", "MOEXBMI",
        "MOEXOG", "MOEXEU", "MOEXTL", "MOEXMM", "MOEXFN",
        "MOEXCN", "MOEXTN", "MOEXCH", "MOEXIT", "MOEXRE",
    )
    seen_codes = {str(item.get("SECID")) for item in names}
    print(
        f"Бумаг на рынке индексов — **{len(names)}**, и большинство из них "
        "к акциям отношения не имеет: iNAV биржевых фондов и товарные индексы "
        "НТБ. Ниже — широкие, полной доходности и отраслевые; глубина "
        "объявлена источником у каждого.\n"
    )
    print("| Индекс | Наименование | История с | по |")
    print("|---|---|---|---|")
    for code in wanted:
        if code not in seen_codes:
            print(f"| {code} | **нет в перечне** | — | — |")
            continue
        answer = fetch(f"/securities/{code}.json", f"index_{code}", {"iss.meta": "off"})
        spans = [
            item
            for item in rows(answer, "boards")
            if item.get("history_from")
        ]
        title = next(
            (
                str(item.get("SHORTNAME"))
                for item in names
                if str(item.get("SECID")) == code
            ),
            code,
        )
        if spans:
            first = min(str(item["history_from"]) for item in spans)
            last = max(str(item["history_till"]) for item in spans)
            print(f"| {code} | {title} | {first} | {last} |")
        else:
            print(f"| {code} | {title} | глубина не объявлена | — |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
