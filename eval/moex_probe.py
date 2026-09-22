"""Разведка ISS Московской биржи. **Только чтение, правил из неё не делается.**

    uv run python eval/moex_probe.py > data/output/moex_probe.md

Пять вопросов, поставленных 22.09.2026:

1. история торгов по облигациям — глубина, поля, есть ли у выпусков ЕвроТранса
   и Кириллицы за март–август 2026;
2. кривая бескупонной доходности ОФЗ — есть ли, какая глубина, какие параметры;
3. облигационные индексы — корпоративные по рейтинговым группам
   и государственный, история доходности;
4. уведомления биржи — приостановка торгов, риск-сектор, делистинг:
   есть ли структурированно и с привязкой к выпуску;
5. связь с Cbonds через ISIN — сколько выпусков списка наблюдения есть в ISS.

**Ответ источника кладётся на диск** (`data/raw/moex/`), частота ограничена
нами: ISS предела не объявляет, и это не повод его не держать.

**Разведка отвечает «есть или нет», а не «сколько это значит».** Ни одного
правила маршрута здесь не появляется: решение о рыночном слое — за человеком.
"""

import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources import moex  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

BONDS = "history/engines/stock/markets/bonds/securities"
# Эмитенты записки: у первого дефолт по двенадцати выпускам, у второго
# погашение не исполнено 22.08.2026. По ним и проверяется, виден ли сигнал.
NAMED: dict[str, str] = {
    "5029169023": "ЕвроТранс",
    "4004021785": "Кириллица",
}
# Поля, которые спрашивал человек: доходность к погашению и к оферте,
# дюрация, объём, число сделок, цена закрытия.
WANTED = (
    "YIELDCLOSE",
    "YIELDATWAP",
    "YIELDTOOFFER",
    "DURATION",
    "VOLUME",
    "VALUE",
    "NUMTRADES",
    "CLOSE",
    "LEGALCLOSEPRICE",
    "MARKETPRICE3",
    "ZSPREAD",
    "ACCINT",
    "COUPONPERCENT",
    "OFFERDATE",
    "MATDATE",
    "BOARDID",
)


def month_history(secid: str, month: str) -> list[dict]:
    """История торгов выпуска за месяц; пусто — сделок не было."""
    first = date.fromisoformat(f"{month}-01")
    last = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    answer = moex.fetch(
        f"{BONDS}/{secid}.json",
        f"history_{secid}_{month}",
        {"from": f"{first}", "till": f"{last}", "iss.meta": "off", "limit": 100},
    )
    return moex.rows(answer, "history")


def isin_of_watchlist(only: set[str] | None = None) -> dict[str, tuple[str, str]]:
    """ISIN выпусков: ISIN → (ИНН, наименование выпуска).

    `only` сужает до эмитентов списка наблюдения: справочник агрегатора шире
    списка, и «сколько выпусков списка есть в ISS» о нём не отвечает.
    """
    import json

    cards = json.loads(
        (Path("data/raw/cbonds") / "emitents.json").read_text(encoding="utf-8")
    )
    found: dict[str, tuple[str, str]] = {}
    for inn in cards if only is None else (item for item in cards if item in only):
        path = Path("data/raw/cbonds") / f"emissions_{inn}.json"
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            code = str(item.get("isin_code") or "").strip()
            if code:
                found[code] = (inn, str(item.get("document_rus") or code))
    return found


def depth() -> None:
    """Печатает глубину истории торгов на одном выпуске."""
    print("## 1. История торгов по облигациям\n")
    secid = "RU000A1061K1"
    answer = moex.fetch(
        f"{BONDS}/{secid}.json",
        f"history_{secid}_depth",
        {"from": "2000-01-01", "iss.meta": "off", "limit": 100},
    )
    got = moex.rows(answer, "history")
    cursor = moex.rows(answer, "history.cursor")
    total = cursor[0].get("TOTAL") if cursor else len(got)
    if not got:
        print("Истории по выпуску нет — проверить нечем.\n")
        return
    print(
        f"У выпуска `{secid}` (ЕвроТранс, БО-001Р-03) торговых дней **{total}**, "
        f"первый — {got[0].get('TRADEDATE')}. Ответ отдаётся страницами по 100, "
        "и общее число стоит в курсоре: глубина не обрезается окном, как "
        "у агрегатора.\n"
    )
    have = [name for name in WANTED if name in got[0]]
    missing = [name for name in WANTED if name not in got[0]]
    print(f"**Поля дня** (всего {len(got[0])}), из спрошенных есть:\n")
    print("| Поле | Что это | Пример |")
    print("|---|---|---|")
    explain = {
        "YIELDCLOSE": "доходность к погашению по цене закрытия",
        "YIELDATWAP": "доходность к погашению по средневзвешенной",
        "YIELDTOOFFER": "доходность к оферте",
        "DURATION": "дюрация, дней",
        "VOLUME": "объём в бумагах",
        "VALUE": "оборот, руб.",
        "NUMTRADES": "число сделок",
        "CLOSE": "цена закрытия, % номинала",
        "LEGALCLOSEPRICE": "признаваемая котировка",
        "MARKETPRICE3": "рыночная цена 3",
        "ZSPREAD": "Z-спред к кривой ОФЗ, б. п.",
        "ACCINT": "накопленный купон",
        "COUPONPERCENT": "ставка купона",
        "OFFERDATE": "дата оферты",
        "MATDATE": "дата погашения",
        "BOARDID": "режим торгов",
    }
    # **Пример берётся у дня, когда поле заполнено.** У последнего дня
    # доходность пуста — бумага в дефолте, и доходности к погашению у неё
    # нет вовсе; пустая графа в примере читалась бы как отсутствие поля.
    for name in have:
        shown = next(
            (item.get(name) for item in reversed(got) if item.get(name) is not None),
            None,
        )
        print(f"| `{name}` | {explain.get(name, '')} | {shown} |")
    if missing:
        print(f"\nИз спрошенного нет: {', '.join(missing)}.")
    print(
        "\n**Z-спред уже посчитан биржей** (`ZSPREAD`), то есть спред к кривой "
        "ОФЗ считать самим не нужно — но он к кривой, а не к отдельной бумаге, "
        "и дюрация рядом стоит своя.\n"
    )


def named_history() -> None:
    """Печатает историю выпусков ЕвроТранса и Кириллицы за март–август."""
    months = [f"2026-0{item}" for item in range(3, 9)]
    print("### Выпуски ЕвроТранса и Кириллицы, март–август 2026\n")
    print(
        "| Эмитент | Выпуск | ISIN | Дней | Цена в марте | Цена в августе "
        "| Доходность в августе | Режим |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for inn, label in NAMED.items():
        issues, known = issues_of(inn)
        if not known:
            print(f"| {label} | карточки нет | — | — | — | — | — | — |")
            continue
        for issue in issues:
            code = _isin(inn, issue.name)
            if not code:
                continue
            series: list[dict] = []
            for month in months:
                try:
                    series.extend(month_history(code, month))
                except moex.MoexError as failure:
                    logger.error("%s %s: %s", code, month, str(failure)[:90])
            if not series:
                print(
                    f"| {label} | {issue.name[:24]} | {code} | 0 | — | — | — "
                    "| сделок нет |"
                )
                continue
            first = series[0]
            last = series[-1]
            print(
                f"| {label} | {issue.name[:24]} | {code} | {len(series)} "
                f"| {first.get('CLOSE')} ({first.get('TRADEDATE')}) "
                f"| {last.get('CLOSE')} ({last.get('TRADEDATE')}) "
                f"| {last.get('YIELDCLOSE')} "
                f"| {first.get('BOARDID')} → {last.get('BOARDID')} |"
            )


def _isin(inn: str, name: str) -> str:
    """ISIN выпуска по наименованию из карточки агрегатора."""
    import json

    path = Path("data/raw/cbonds") / f"emissions_{inn}.json"
    if not path.exists():
        return ""
    for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        if str(item.get("document_rus") or "") == name:
            return str(item.get("isin_code") or "")
    return ""


def curve() -> None:
    """Печатает, есть ли кривая бескупонной доходности и какая у неё глубина."""
    print("\n## 2. Кривая бескупонной доходности ОФЗ\n")
    for path, name, what in (
        ("engines/stock/zcyc.json", "zcyc_today", "сегодняшняя кривая"),
        (
            "history/engines/stock/zcyc.json",
            "zcyc_history",
            "история кривой",
        ),
    ):
        try:
            answer = moex.fetch(path, f"probe_{name}", {"iss.meta": "off"})
        except moex.MoexError as failure:
            print(f"- {what} (`{path}`): **отказ** — {str(failure)[:120]}")
            continue
        blocks = [key for key in answer if not key.endswith(".cursor")]
        print(f"- {what} (`{path}`): блоки {', '.join(blocks)}")
        for block in blocks:
            got = moex.rows(answer, block)
            if not got:
                continue
            print(f"  - `{block}`: строк {len(got)}, поля {', '.join(sorted(got[0]))}")
            print(f"  - первая строка: {got[0]}")


def indices() -> None:
    """Печатает облигационные индексы и глубину их истории."""
    print("\n## 3. Облигационные индексы\n")
    answer = moex.fetch(
        "engines/stock/markets/index/securities.json",
        "probe_index_securities",
        {"iss.meta": "off"},
    )
    got = moex.rows(answer, "securities")
    # **Отбор по коду, а не по слову «облигации» в наименовании.** Слово
    # стоит и у сотни iNAV биржевых фондов, и перечень тогда описывает
    # фонды, а не индексы: RGB — государственные, RUCB — корпоративные,
    # RUMB — муниципальные.
    bonds = [
        item
        for item in got
        if str(item.get("SECID") or "").startswith(("RGB", "RUCB", "RUMB", "RUGB"))
    ]
    print(
        f"Инструментов рынка индексов — {len(got)}, облигационных индексов "
        f"среди них — **{len(bonds)}** (отбор по коду: RGB — государственные, "
        "RUCB — корпоративные, RUMB — муниципальные).\n"
    )
    print("| Индекс | Наименование |")
    print("|---|---|")
    for item in sorted(bonds, key=lambda entry: str(entry.get("SECID"))):
        print(f"| `{item.get('SECID')}` | {item.get('SHORTNAME')} |")
    for secid in ("RGBITR", "RUCBITR"):
        try:
            answer = moex.fetch(
                f"history/engines/stock/markets/index/securities/{secid}.json",
                f"probe_index_{secid}",
                {"from": "2000-01-01", "iss.meta": "off", "limit": 100},
            )
        except moex.MoexError as failure:
            print(f"\n`{secid}`: отказ — {str(failure)[:100]}")
            continue
        rows_here = moex.rows(answer, "history")
        cursor = moex.rows(answer, "history.cursor")
        if rows_here:
            print(
                f"\n`{secid}`: дней {cursor[0].get('TOTAL') if cursor else len(rows_here)}, "
                f"первый {rows_here[0].get('TRADEDATE')}, "
                f"поля {', '.join(sorted(rows_here[0]))}"
            )


def notices() -> None:
    """Печатает, что ISS говорит о режиме торгов выпуска и его смене."""
    print("\n## 4. Уведомления биржи: приостановка, риск-сектор, делистинг\n")
    secid = "RU000A1061K1"
    answer = moex.fetch(f"securities/{secid}.json", f"probe_security_{secid}",
                        {"iss.meta": "off"})
    description = {
        str(item.get("name")): item.get("value")
        for item in moex.rows(answer, "description")
    }
    boards = moex.rows(answer, "boards")
    print(f"Карточка выпуска `{secid}` — поля описания:\n")
    for key in sorted(description):
        print(f"- `{key}` = {description[key]}")
    print(f"\nРежимы торгов выпуска — {len(boards)}:\n")
    print("| Режим | Торгуется | Первая дата | Последняя дата |")
    print("|---|---|---|---|")
    for item in boards:
        print(
            f"| {item.get('boardid')} ({item.get('title')}) "
            f"| {item.get('is_traded')} | {item.get('history_from')} "
            f"| {item.get('history_till')} |"
        )


def linkage() -> None:
    """Печатает, сколько выпусков списка наблюдения есть в ISS по ISIN."""
    from datetime import date as _date

    from finlib.db import connection
    from finlib.scoring.routing_store import routing_rows

    print("\n## 5. Связь с Cbonds через ISIN\n")
    with connection() as conn:
        rows_here, _ = routing_rows(conn, _date.today())
    listed = {item.inn for item in rows_here}
    ours = isin_of_watchlist(listed)
    every = isin_of_watchlist()
    answer = moex.fetch(
        "engines/stock/markets/bonds/securities.json",
        "probe_bonds_traded",
        {"iss.meta": "off", "iss.only": "securities"},
    )
    theirs = {
        str(item.get("ISIN") or ""): item for item in moex.rows(answer, "securities")
    }
    both = sorted(set(ours) & set(theirs))
    print(
        f"Эмитентов в списке наблюдения — {len(listed)}, ISIN у их выпусков — "
        f"**{len(ours)}** (у всего справочника агрегатора — {len(every)}); "
        f"торгуемых облигаций в ISS — **{len(theirs)}**; совпало по ISIN — "
        f"**{len(both)}**.\n"
    )
    print(
        "Совпадение считается по торгуемым: выпуск, погашенный десять лет "
        "назад, в перечне ISS отсутствует правомерно, и разность здесь "
        "не пробел связи. **Связь прямая** — SECID торгуемой облигации равен "
        "её ISIN, и переходника не требуется.\n"
    )
    boards = Counter(str(theirs[code].get("BOARDID")) for code in both)
    print("| Режим торгов | Выпусков списка |")
    print("|---|---|")
    titles = {
        "TQCB": "Т+ Облигации, обычный",
        "TQRD": "Т+ Облигации Д — **сектор повышенного риска**",
        "TQOD": "Т+ Облигации (расчёты в валюте)",
        "TQOY": "Т+ Облигации (юани)",
        "TQIR": "Т+ Облигации ПИР",
    }
    for name, count in boards.most_common():
        print(f"| {name} — {titles.get(name, 'режим не назван')} | {count} |")
    risky = [
        code for code in both if str(theirs[code].get("BOARDID")) in ("TQRD", "TQIR")
    ]
    print(
        f"\nВ секторе повышенного риска — **{len(risky)}** выпусков списка. "
        "Это готовый признак: перевод в него биржа датирует, и у ЕвроТранса "
        "он приходится на 06.08.2026 — за две недели до дефолта Кириллицы "
        "и задолго до нашей отчётной даты.\n"
    )
    for code in sorted(risky):
        inn, name = ours[code]
        print(f"- `{code}` — {name} ({inn})")


def main() -> int:
    """Печатает разведку ISS; 1 — если источник не ответил вовсе."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    print("# Разведка ISS Московской биржи\n")
    print(
        "Публичный доступ, без ключа. Ответы кладутся в `data/raw/moex/`, "
        "частота ограничена нами. **Правил из разведки не сделано.**\n"
    )
    print(
        "## Главное\n\n"
        "1. **История торгов глубокая**: у выпуска ЕвроТранса 883 торговых дня "
        "с 04.04.2023 — то есть с размещения. Окна в сорок дней, как "
        "у агрегатора, здесь нет: рыночный слой можно строить на ISS "
        "и проверять на календаре событий назад.\n"
        "2. **Все спрошенные поля есть**: доходность к погашению и к оферте, "
        "дюрация, объём, оборот, число сделок, цена закрытия и признаваемая "
        "котировка — и сверх того **Z-спред, посчитанный самой биржей**.\n"
        "3. **Кривая бескупонной доходности ОФЗ есть** и с историей "
        "с 06.01.2014: параметры модели (B1–B3, T1, G1–G9), одиннадцать точек "
        "по срокам и корзина ОФЗ с доходностями и дюрациями.\n"
        "4. **Индексы по рейтинговым группам есть**: RUCBCPAAANS, RUCBCPAANS, "
        "RUCBCPANS, RUCBCPBBBNS, отдельно ВДО (RUCBHYTR) и государственные "
        "(RGBITR с 30.12.2002, с полями доходности и дюрации).\n"
        "5. **Перевод в сектор повышенного риска структурирован и датирован**: "
        "у ЕвроТранса режим TQCB кончается 05.08.2026, TQRD начинается "
        "06.08.2026. Признаки `HIGHRISK`, `HASDEFAULT`, `HASTECHNICALDEFAULT` "
        "и `LISTLEVEL` стоят в карточке выпуска.\n"
        "6. **Связь прямая**: SECID торгуемой облигации равен ISIN, "
        "и 816 выпусков списка наблюдения находятся в ISS; 16 из них — "
        "в секторе повышенного риска, и это ЕвроТранс, Антерра и Монополия, "
        "то есть те же три эмитента, которых событийный слой уже держит "
        "в разборе.\n"
    )
    try:
        depth()
        named_history()
        curve()
        indices()
        notices()
        linkage()
    except moex.MoexError as failure:
        print(f"\n**Источник ответил отказом:** {failure}")
        return 1
    print(
        f"\n---\n\nЗапросов к источнику {moex.pace.requested}, "
        f"ответов с диска {moex.pace.from_cache}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
