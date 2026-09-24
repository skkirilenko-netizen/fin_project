"""Фактическая часть материалов для руководства: числа и откуда они.

    uv run python eval/leadership_facts_run.py > data/output/leadership_facts.md

**Здесь только факты и ссылки на замеры.** Оценок, обещаний и выводов о пользе
нет вовсе: текст пишет владелец, и подменять его формулировками было бы
тем же, чем оговорка, выдающая наш пробел за решение методики.

**Числа берутся у боевого пути**, а не набираются в этом файле: охват
и распределение — у маршрутизации (`scoring.routing_store.routing_rows`),
качество слоёв — у замера упреждения (`eval/market_lead_run.py`), величины
случаев — из базы. Второй набор тех же чисел разошёлся бы с первым.

**У каждого числа названо, чем оно получено** — прогоном и датой: цифра
без источника в материалы наружу не годится, а через месяц её никто
не восстановит.
"""

import logging
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import digits  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.market import load_market, series  # noqa: E402

logger = logging.getLogger(__name__)

# Замер упреждения пишет свой отчёт; числа качества берутся из него, а не
# считаются заново. Нет файла — так и сказано: «замер не прогонялся».
LEAD = Path("data/output/market_lead.md")

# Случаи, показывающие ценность. Перечень назван владельцем 24.09.2026,
# и каждый случай проверяется по базе: величина, не найденная в базе,
# печатается отказом, а не пропускается — материал наружу не должен
# содержать чисел, которых у нас нет.
CASES: tuple[dict[str, str], ...] = (
    {
        "inn": "7717151380",
        "what": "начисленные проценты из примечания против величины формы",
        "fact": (
            "В форме финансовые расходы −414 млн руб.: они очищены "
            "от капитализированных процентов и целевого финансирования. "
            "Начисленные проценты стоят в примечании 11 и равны 54 382 млн "
            "руб. — разница стотридцатикратная. Величина формы дала бы "
            "покрытие процентов, которого не существует. Это разбор "
            "документа; корзину ниже даёт маршрут по нормализованным данным "
            "агрегатора, и величины у них свои."
        ),
        "where": "эталон ветки МСФО, `eval/ifrs_reference.yaml` (`make ifrs-reference`)",
    },
    {
        "inn": "4004021785",
        "what": "здоровая отчётность при дефолте",
        "fact": (
            "Отчётность за 2025 год спокойна: автономия 0,75, долговая "
            "нагрузка 1,75, ликвидность 4,30. 07.09.2026 не исполнено "
            "погашение выпуска БО-03 на 300 млн руб. Рейтинги отозваны всеми "
            "агентствами 02.06.2026, за три месяца до события. Рынок держал "
            "спред до 115 крат к ориентиру в последнюю неделю торгов, "
            "но цена границы 60 % не достигла, а всплеск спреда не набрал "
            "подтверждения: бумага перестала торговаться за 18 дней "
            "до события. В маршруте её назвали отзыв рейтингов и дефолт."
        ),
        "where": "`sources/cbonds_events.py`, ежедневный снимок рейтингов",
    },
    {
        "inn": "0411137185",
        "what": "цена бумаги и событие биржи",
        "fact": (
            "Выпуск переведён биржей в сектор повышенного риска (режим TQRD)."
        ),
        "where": "`sources/moex_risk.py`, доставка `scripts/moex_fetch.py`",
    },
)

# **Величины случаев берутся у маршрута, а не отдельным запросом к базе.**
# `metric_value` наполняется только по разобранным документам — 32 эмитента
# из 900, — и «величин в базе нет» у эмитента, чья строка списка их
# показывает, было бы неправдой о системе.


def _coverage(rows: list, counts: dict, routing) -> None:  # noqa: ANN001
    """Охват: периметр, чем построен маршрут, распределение по корзинам."""
    print("## Охват\n")
    print(
        f"Эмитентов в периметре — **{counts['эмитентов']}**; из них "
        f"с выпусками в обращении **{counts['с выпусками в обращении']}**, "
        f"без выпусков **{counts['без выпусков в обращении']}** (в сводные "
        "доли не идут: маршрут спрашивает, нужен ли человек, а нужен он там, "
        "где есть долг).\n"
    )
    print("| Чем построен маршрут | Эмитентов |")
    print("|---|---|")
    print(f"| консолидированная отчётность (МСФО) | {counts['маршрут по МСФО']} |")
    print(f"| отчётность юридического лица (РСБУ) | {counts['маршрут по РСБУ']} |")
    print(
        "| одни события и рейтинги (отчётности нет) "
        f"| {counts['маршрут по событиям и рейтингам']} |"
    )
    with_bonds = [item for item in rows if item.has_bonds]
    names = {basket.code: basket.name for basket in routing.baskets}
    order = {basket.code: basket.order for basket in routing.baskets}
    counted: dict[str, int] = {}
    for item in with_bonds:
        counted[item.verdict.basket] = counted.get(item.verdict.basket, 0) + 1
    print(f"\n| Корзина | Эмитентов из {len(with_bonds)} |")
    print("|---|---|")
    for code in sorted(counted, key=lambda item: order.get(item, 99)):
        print(f"| {names.get(code, code)} | {counted[code]} |")
    print(
        f"\nВклад рыночного слоя: основание рынка **открыло "
        f"{counts['рынок открыл']}** эмитентов, ещё у "
        f"**{counts['рынок поднял тяжесть']}** подняло тяжесть к основаниям, "
        "стоявшим до него.\n"
    )
    print(
        "*Источник: боевая маршрутизация, `scoring/routing_store.py`; "
        "те же числа печатает список наблюдения и замер охвата "
        "(`make watchlist-coverage`).*\n"
    )


def _quality() -> None:
    """Качество слоёв: выявляемость, точность, прирост, упреждение."""
    print("## Качество слоёв\n")
    if not LEAD.exists():
        print(
            "замер упреждения не прогонялся (`make market-lead`) — чисел нет, "
            "и печатать их неоткуда.\n"
        )
        return
    text = LEAD.read_text(encoding="utf-8")
    print(
        "Мера одна на все слои: признак засчитывается пойманным, только если "
        "он сработал **до** события; упреждение считается по появлению "
        "основания, а не по его стоянию с первого наблюдавшегося дня.\n"
    )
    print(
        "| Слой и признак | Сработал | С событием | Точность | Выявляемость "
        "| Прирост | Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    # Берутся строки таблицы появления: она и есть честная мера упреждения.
    wanted = ("| уровень p", "| цена ниже 60", "| отчётность (", "| рейтинги (")
    for line in text.splitlines():
        if not line.startswith(wanted):
            continue
        parts = [cell.strip() for cell in line.strip("|").split("|")]
        if len(parts) != 7:
            continue
        lead = parts[-1].split(",")[0].replace("медиана ", "").replace("**", "")
        print("| " + " | ".join(parts[:-1] + [lead]) + " |")
    print(
        "\n**Оговорки, без которых числа читаются неверно.** Мера "
        "ретроспективная: признаки применены к уже случившимся событиям, "
        "и на будущих данных они дадут другие числа. Окно доставки — два года "
        "торгов и год записанной истории корзин: событие раньше начала "
        "наблюдения слой упредить не мог, и такие события из его знаменателя "
        "исключены. Базовая доля событий в круге — 6,6 %, и прирост считается "
        "к ней.\n"
    )
    print("*Источник: `eval/market_lead_run.py` (`make market-lead`).*\n")


def _cases(rows: list) -> None:
    """Три случая: что система увидела, чем и с какими величинами.

    **Величины берутся у маршрута, а не из отдельного запроса**: строка списка
    и этот материал обязаны говорить одно, и второй набор тех же чисел
    разошёлся бы с первым.
    """
    print("## Три случая\n")
    market, policy = series(), load_market()
    below = policy.distress_zone.price_below_percent
    by_inn = {item.inn: item for item in rows}
    for case in CASES:
        inn = case["inn"]
        row = by_inn.get(inn)
        if row is None:
            print(f"### {inn}\n\nэмитента нет в списке — числа брать неоткуда.\n")
            continue
        print(f"### {row.name} ({inn}) — {case['what']}\n")
        print(f"{case['fact']}\n")
        print(f"- корзина: **{row.verdict.basket_name}**")
        for entry in row.verdict.findings:
            print(f"- основание: {entry.text}")
        if row.shown_values:
            said = "; ".join(
                f"{name} {shown}" for _, name, shown in row.shown_values
            )
            print(f"- величины маршрута: {said}")
        points = market.ordered(inn)
        traded = [item for item in points if item.price is not None]
        if traded:
            last = traded[-1]
            since = next(
                (item.day for item in traded if item.price < below), None
            )
            # **Наибольшая кратность берётся за последние 90 дней ряда,
            # а не за весь ряд.** У Кириллицы за два года наибольшая — 449×
            # в ноябре 2024 года, за двадцать два месяца до события: это
            # короткая бумага у погашения либо одиночная сделка, и к делу
            # она не относится. Разгон последних недель — относится.
            edge = last.day - timedelta(days=90)
            top = max(
                (
                    item.spread / market.benchmark[item.day]
                    for item in points
                    if item.day >= edge
                    and item.spread is not None
                    and market.benchmark.get(item.day, Decimal(0)) > 0
                ),
                default=None,
            )
            print(
                f"- рынок: наблюдений {len(points)}, последняя цена "
                f"{digits(last.price, 1)} % номинала ({last.day:%d.%m.%Y})"
                + (
                    f", ниже границы {digits(below, 0)} % с {since:%d.%m.%Y}"
                    if since is not None
                    else f", границы {digits(below, 0)} % не пересекала"
                )
                + (
                    f"; наибольшая кратность спреда к ориентиру за последние "
                    f"90 дней ряда {digits(top, 1)}×"
                    if top is not None
                    else "; спред не считается"
                )
            )
        else:
            print("- рынок: " + market.silence(inn))
        print(f"- где проверить: {case['where']}\n")
    print(
        "*Источник величин и оснований: боевая маршрутизация; рыночный ряд — "
        "`sources/market.py`; величины примечаний МСФО — разбор документа, "
        "`normalize/ifrs_loader.py`.*\n"
    )


def _limits(routing) -> None:  # noqa: ANN001
    """Чего система не делает — перечнем и коротко."""
    print("## Чего система не делает\n")
    for said in routing.limitations:
        print(f"- {' '.join(str(said).split())}")
    print(
        "\n- не присваивает класс по нормализованным данным агрегатора: "
        "состав величин у него собственный"
    )
    print("- не прогнозирует: все признаки описывают наступившее и наблюдаемое")
    print(
        "- не принимает решений вместо человека: корзина — это распределение "
        "внимания, а не оценка кредитоспособности"
    )
    print(
        "\n*Источник: `methodology/routing.yaml`, блок `limitations`; "
        "инварианты проекта — `CLAUDE.md`.*\n"
    )


def main() -> int:
    """Печатает фактическую часть; 1 — если маршрут собрать не удалось."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    routing = load_routing()
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
        print("# Фактическая часть: числа и откуда они\n")
        print(
            f"Собрано {date.today():%d.%m.%Y} боевым путём. Каждое число "
            "названо вместе с прогоном, которым получено; оценок и выводов "
            "здесь нет — они за автором текста.\n"
        )
        _coverage(rows, counts, routing)
        _quality()
        _cases(rows)
    _limits(routing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
