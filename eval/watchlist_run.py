"""Список наблюдения: один HTML-файл со всеми эмитентами и их корзинами.

    uv run python eval/watchlist_run.py    # data/output/watchlist_<дата>_manual_<ЧЧММСС>.html
    uv run python eval/watchlist_run.py --out ПУТЬ

**Это чтение, и ничего кроме.** Из интерфейса нельзя ни исправить корзину,
ни подтвердить комплект: решение о комплекте принимается командой с автором
и уходит в журнал, а страница, позволяющая менять оценку мышью, оставляет
решение без следа. Файл открывается в браузере и никуда не обращается —
ни к сети, ни к базе: локальный контур, данные не покидают машину.

**Величины и основания берёт боевой путь.** Показатели — расчёт по фактам
(`metrics.ifrs_store.compute_from_facts`), корзину и подгруппу — маршрутизация
(`scoring.routing.route`), стоп-факторы — оценка (`scoring.ifrs_store`).
Прогон собирает страницу и считает сводку.

**Технических кодов на странице нет.** Основание называется наименованием
из справочника маршрутизации, стоп-фактор — своим наименованием, величина —
через единую точку округления. Код показателя человеку ничего не говорит,
а предмет и величина говорят.
"""

import argparse
import html
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import foreign_units  # noqa: E402
from finlib.report.market_chart import charts  # noqa: E402
from finlib.report.policy import load_policy, months_between  # noqa: E402
from finlib.report.watchlist import render as render_interface  # noqa: E402
from finlib.report.watchlist_data import csv_rows, payload  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_catalogue import catalogue_for  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.market import load_market as _market_rules  # noqa: E402
from finlib.sources.market import series as _market_series  # noqa: E402
from finlib.sources.notification_journal import MOSCOW, atomic_write  # noqa: E402

logger = logging.getLogger(__name__)

def _coverage(item) -> str:
    """Строка покрытия: что проверено и чего нет.

    **Пустая графа читается как «ничего не проверяли».** У эмитента без
    оснований проверено всё, что маршрут умеет: три величины и события.
    «Долг ✓ (оценка сверху)» отличается от «долг ✓» намеренно — вывод
    по границе доказателен, но это граница, а не величина.

    **Чем зовётся каждая из трёх, объявляет стандарт.** У РСБУ долговой
    нагрузки нет вовсе, и графа, спрашивающая о ней кодом МСФО, отвечала бы
    «нет данных» у каждого эмитента без консолидированной отчётности.
    """
    if item.standard is None:
        return "проверено: отчётности нет, только события и рейтинги"
    rule = catalogue_for(item.standard).rule
    parts = []
    if rule.burden and item.values.get(rule.burden) is not None:
        parts.append("долг ✓")
    elif rule.bound and item.values.get(rule.bound) is not None:
        parts.append("долг ✓ (оценка сверху)")
    else:
        parts.append("долг — нет данных")
    parts.append(
        "капитал ✓" if item.values.get("equity_ratio") is not None
        else "капитал — нет данных"
    )
    parts.append(
        "ликвидность ✓" if item.values.get("cur_liq") is not None
        else "ликвидность — нет данных"
    )
    parts.append("события — нет данных")
    return "проверено: " + ", ".join(parts)


# Четыре исхода правила давности дефолта. Считаются все четыре вместе
# со знаменателем: у правила с четырьмя исходами ноль срабатываний одного
# из них ничего не значит без остальных трёх.
_DEFAULT_OUTCOME_NAMES: dict[str, str] = {
    "emission_default": "дефолт не улажен, до 3 лет: разбор",
    "default_unsettled_stale": "не улажен, старше: вопрос",
    "default_settled_recent": "улажен, до 3 лет: история",
    "default_settled_stale": "улажен, старше: справочно",
}
_DEFAULT_OUTCOMES = frozenset(_DEFAULT_OUTCOME_NAMES)


def _chart(inn: str) -> str:
    """Рамки рыночного ряда эмитента встроенным SVG; пусто — ряда нет.

    Рисует их тот же код, что и в карточке (`report.market_chart`): страница
    и карточка обязаны показывать одно, а второй рисовальщик разошёлся бы
    с первым в первом же масштабе.
    """
    market = _market_series()
    points = market.ordered(inn)
    if not points:
        return ""
    return "".join(charts(points, market, _market_rules()))


def rows_of(conn, today: date) -> tuple[list[dict], dict[str, int]]:
    """Строки списка наблюдения и сводка по корзинам и подгруппам.

    Входы и вердикт берёт `scoring.routing_store.routing_rows` — одно место
    на список и на замер распределения: прежде оба собирали величины сами,
    и расхождение «в списке иначе, чем в отчёте» увидеть было бы нечем.
    """
    routing = load_routing()
    report_policy = load_policy()
    found, counts = routing_rows(conn, today)

    rows: list[dict] = []
    for item in found:
        verdict = item.verdict
        basket = routing.basket(verdict.basket)
        ground_names = {entry.code: entry.name for entry in basket.grounds}
        # Отчётности может не быть вовсе: тогда давности не существует,
        # и ноль месяцев здесь означал бы свежую отчётность.
        months = (
            months_between(item.report_date, today)
            if item.report_date is not None
            else None
        )
        # Единица берётся у строки, а не набирается здесь: она одна на весь
        # выход, и второй её набор однажды разошёлся с первым.
        unit = item.unit
        # **Давность видна всегда.** Более сильное основание её не гасит:
        # признак берётся у вердикта, а не у корзины, и печатается в своей
        # графе даже тогда, когда корзину назвало другое основание.
        overdue = any(
            entry.ground in ("disclosure_overdue", "reporting_two_cycles_old")
            for entry in verdict.findings
        )
        rows.append(
            {
                "name": item.name,
                "inn": item.inn,
                "basket": verdict.basket,
                "basket_name": verdict.basket_name,
                "order": basket.order,
                # **Порядок подгруппы — порядок тяжести внутри корзины.**
                # «Внимание» — 461 эмитент из 701, и корзина такого размера
                # выбирать не помогает, если внутри неё нет порядка: события
                # и рейтинги выше величин, величины выше нехватки данных
                # и просрочки раскрытия. Берётся он у справочника, а не
                # назначается страницей.
                "subgroup_order": next(
                    (
                        entry.order
                        for entry in basket.groups
                        if entry.code == verdict.subgroup
                    ),
                    len(basket.groups) + 1,
                ),
                "subgroups": list(verdict.subgroup_names),
                "actions": list(verdict.actions),
                # Основание — наименование справочника, а под ним предмет
                # с величиной: без них корзина остаётся словом без опоры.
                "grounds": [
                    {
                        "name": ground_names.get(ground, ground),
                        "details": [
                            entry.text
                            for entry in verdict.findings
                            if entry.ground == ground
                        ],
                    }
                    for ground in verdict.grounds
                ],
                # **Справочное обстоятельство корзины не называет, но строка
                # о нём молчать не вправе.** Урегулированный дефолт
                # десятилетней давности человек найдёт в карточке сам,
                # и молчание маршрута прочтёт как недосмотр.
                "notes": [entry.text for entry in verdict.notes],
                # **График рисуется там, где рынок высказался**, а не у всех
                # подряд: он объясняет основание, и у эмитента без рыночного
                # основания объяснять нечего. Цена полноты измерена — две
                # рамки весят около четырёх килобайт, и на все 900 строк это
                # четыре мегабайта против страницы в полтора.
                "chart": _chart(item.inn)
                if any(
                    entry.ground.startswith("market_")
                    for entry in verdict.findings
                )
                else "",
                # **Чистый долг и EBITDA называются порознь.** Отрицательное
                # отношение означает либо чистую денежную позицию, либо убыток,
                # и по одному отношению их не различить.
                # Единица — комплекта, а не стандарта: консолидированная
                # отчётность составляется в миллионах, и «тыс. руб.» у неё —
                # ошибка в тысячу раз, которую не ловит ни один контроль.
                # Набраны они один раз — в маршруте, который знает справочник
                # своего стандарта: `cur_liq` МСФО и `cur_liq` РСБУ зовутся
                # по-разному, и второй набор печатал бы чужое наименование.
                "values": [[name, shown] for _, name, shown in item.shown_values],
                "sources": list(item.sources),
                # **Источник, стандарт и контур.** Единица у коэффициентов
                # не информативна, а контур — да: отдельная отчётность
                # управляющей компании и консолидированная группы описывают
                # разные предметы, и теперь в списке стоят обе. Графа берёт
                # контур у строки: прежде она была написана здесь словами
                # и говорила «МСФО · консолидированная» у каждой.
                "origin": " · ".join(
                    part for part in (", ".join(item.sources), item.basis) if part
                ),
                "unit": unit,
                # Строка покрытия для «Без внимания»: перечислено то, что
                # проверено, и названо то, чего у нас нет.
                "coverage": _coverage(item),
                "bonds": item.has_bonds,
                "report_date": (
                    f"{item.report_date:%d.%m.%Y}"
                    if item.report_date is not None
                    else "отчётности нет"
                ),
                "months": months,
                "stale": months is not None and report_policy.freshness.stale(months),
                "overdue": overdue,
                "assessed": item.assessed_class,
            }
        )
    rows.sort(
        key=lambda item: (item["order"], item["subgroup_order"], item["name"].lower())
    )
    # **Единица сверяется у каждой строки, а не у документа одного.** Проверка
    # стояла только в заключении, и список печатал мимо неё: 39 строк подписали
    # миллионы тысячами. Сверяет её та же функция, что документ, и число
    # сверенных строк печатается — ноль расхождений при неизвестном числе
    # сверок не означает ничего.
    checked = 0
    for item in found:
        checked += 1
        # **У каждой величины сверяется её единица.** Основание, перенесённое
        # от поручителя, названо в его единице — и сверяется с ней: величина
        # чужая, и единица у неё чужая. Свалив их в одну строку, проверка
        # объявила бы расхождением верную печать.
        wrong = foreign_units(
            " ".join([item.unit, *(shown for _, _, shown in item.shown_values)]),
            item.unit,
        ) + [
            name
            for unit, text in item.verdict.by_unit(item.unit)
            for name in foreign_units(text, unit)
        ]
        if wrong:
            raise ValueError(
                f"{item.name} ({item.inn}): напечатана единица "
                f"«{', '.join(wrong)}», а комплект составлен в «{item.unit}»"
            )
    logger.info("единица сверена у строк: %d, расхождений 0", checked)
    # **Сводные доли считаются по эмитентам с долгом в обращении.** Маршрут
    # спрашивает, нужен ли человек, а нужен он там, где есть долг: эмитент,
    # долг которого погашен, из списка не исчезает, но доли корзин мерили бы
    # по нему состав списка, а не охват рынка. Число таких строк стоит рядом
    # отдельной графой — молчание о них читалось бы как «их нет».
    with_bonds = [item for item in rows if item["bonds"]]
    summary = Counter(item["basket_name"] for item in with_bonds)
    # **Подгруппа называется вместе со своей корзиной.** Одно наименование
    # стоит теперь в двух корзинах — «рынок» и в «Разборе», и во «Внимании», —
    # и сложенные в одну строку они дали бы 130 там, где в разборе 63:
    # число верное, а предмет другой.
    for item in with_bonds:
        for name in item["subgroups"][:1]:
            summary[f"— {item['basket_name']}: {name}"] += 1
    # **Число вышедших из списка печатается всегда**, и рядом — число тех,
    # у кого статус не подтверждён: список, уменьшившийся без записи, врёт
    # о себе сам.
    summary["вышло из списка"] = counts["вышло из списка"]
    summary["статус не подтверждён"] = counts["статус не подтверждён"]
    # Знаменатель правила поручителя: «ноль корзин, взятых у поручителя»
    # без числа самих финансирующих структур неотличим от невыполненного.
    summary["финансирующих структур"] = counts["финансирующих структур"]
    summary["— корзина взята у поручителя"] = counts[
        "из них корзина взята у поручителя"
    ]
    summary["пар с поручителем в списке"] = counts["пар с поручителем в списке"]
    summary["— поднято по поручителю"] = counts["поднято по поручителю"]
    # **Группа корзины не называет, и число названных справочно печатается.**
    # Правило, погашенное молча, неотличимо от невыполненного: прежде здесь
    # стояло «поднято по группе» и поднимало 31 эмитента.
    summary["названо справочно по группе"] = counts["названо справочно по группе"]
    # **Вклад рыночного слоя порознь**: у скольких он единственное
    # обстоятельство и у скольких добавился к уже стоявшим основаниям.
    summary["рынок открыл"] = counts["рынок открыл"]
    summary["рынок поднял тяжесть"] = counts["рынок поднял тяжесть"]
    # **Журнал решений человека печатает оба числа.** Ноль сработавших
    # при неизвестном числе истёкших неотличим от журнала, который не ведут.
    summary["решений человека действует"] = counts["решений человека действует"]
    summary["— истекло"] = counts["решений человека истекло"]
    # Верхний десяток по объёму долга и то, у скольких из них покрытие
    # неполное: «ноль затронутых» без числа самих системно значимых
    # неотличим от невыполненного правила.
    # **У обеих мер рефинансирования печатается знаменатель.** «Сработало
    # у 92» без числа выпусков, по которым график вообще есть, не говорит,
    # мерили мы рынок или ту его часть, до которой дошла доставка.
    issues = sum(
        item.refinance.issues for item in found if item.refinance is not None
    )
    summary["выпусков в обращении у списка"] = issues
    summary["— без графика платежей"] = sum(
        item.refinance.without_schedule
        for item in found
        if item.refinance is not None
    )
    summary["— без ответа об офертах"] = sum(
        item.refinance.without_offers for item in found if item.refinance is not None
    )
    # **Обе меры печатаются порознь.** Первая считает то, что эмитент обязан
    # заплатить, вторая — то, что он заплатит, если владельцы предъявят
    # оферты. Одно число на две меры скрыло бы, какая из них сработала.
    for code, name in (
        ("refinancing_gap", "— не хватает на платежи года"),
        ("refinancing_offers", "— не хватает при предъявлении оферт"),
    ):
        summary[name] = sum(
            1
            for item in found
            if any(entry.ground == code for entry in item.verdict.findings)
        )
    summary["системно значимых"] = counts["системно значимых"]
    summary["— с неполным покрытием"] = sum(
        1
        for item in found
        if any(
            entry.ground == "systemic_partial_cover" for entry in item.verdict.findings
        )
    )
    # **Правило давности дефолта называет все четыре исхода и знаменатель.**
    # Ноль давних дефолтов при неизвестном числе эмитентов с признаком
    # неотличим от невыполненного правила, а исходов у правила четыре:
    # разбор, вопрос об урегулировании, кредитная история, справочное.
    marked = 0
    for item in found:
        outcomes = {entry.ground for entry in item.verdict.findings} | {
            entry.ground for entry in item.verdict.notes
        }
        if not outcomes & _DEFAULT_OUTCOMES:
            continue
        marked += 1
    # **Охват — такое же сведение сводки, как корзина.** «616 во внимании»
    # без «из 702 эмитентов с долгом» выглядит полнотой, а чем построен
    # маршрут, меняет смысл корзины: «Без внимания» по одним событиям
    # и «Без внимания» по отчётности означают разное.
    for key in (
        "с выпусками в обращении",
        "без выпусков в обращении",
        "маршрут по МСФО",
        "маршрут по РСБУ",
        "маршрут по событиям и рейтингам",
        "холдингов на одной РСБУ",
    ):
        summary[key] = counts[key]
    # **Тип эмитента объявляется вместе с числом распознанных.** «Структурный
    # эмитент» без знаменателя читался бы как перечень, а это признак данных,
    # и сколько он распознаёт — часть правила.
    for key in sorted(key for key in counts if key.startswith("тип: ")):
        summary[key] = counts[key]
    summary["эмитентов"] = counts["эмитентов"]
    summary["эмитентов с признаком дефолта"] = marked
    for ground, name in _DEFAULT_OUTCOME_NAMES.items():
        summary[f"— {name}"] = sum(
            1
            for item in found
            if any(
                entry.ground == ground
                for entry in tuple(item.verdict.findings) + tuple(item.verdict.notes)
            )
        )
    return rows, dict(summary)


def render(rows: list[dict], summary: dict, routing, today: date, *,
           output: Path | None = None, report: Path | None = None,
           csv_path: Path | None = None, late_report: Path | None = None,
           cards: Path | None = None, coverage: str | None = None) -> str:
    """Собирает согласованный интерфейс по готовым строкам и сохранённому отчёту."""
    output = output or Path(f"data/output/watchlist_{today:%Y-%m-%d}.html")
    report = report or Path(f"data/output/changes_{today:%Y-%m-%d}.md")
    csv_path = csv_path or Path(f"data/output/watchlist_{today:%Y-%m-%d}.csv")
    data = payload(rows, summary, today, output=output, report=report, csv_path=csv_path,
                   cards=cards or CARDS, limitations=list(routing.limitations),
                   coverage=coverage if coverage is not None else _coverage_line(summary),
                   late_report=late_report)
    return render_interface(data)


# **Список называет то, чего он не проверяет.** Пустое место читается как
# «проверено всё», и по этой же причине рядом с корзинами стоит строка охвата:
# «347 эмитентов» без «из 702 с выпусками в обращении» выглядит полнотой.
# Сами оговорки объявлены методикой (`routing.limitations`): текст, который
# читатель принимает за оговорку методики, правится диффом, а не кодом
# страницы.
def _limitations(routing) -> str:
    """Ограничения списка одной строкой — в том порядке, в каком объявлены."""
    return "".join(
        f"<div>{html.escape(' '.join(item.split()))}</div>"
        for item in routing.limitations
    )


def _coverage_line(summary: dict[str, int]) -> str:
    """Строка охвата: кого список видит, чем построен маршрут и кого нет.

    **Числа берутся у маршрута, а не набираются здесь.** Те же счётчики
    печатает замер охвата (`make watchlist-coverage`), и второй их набор
    разошёлся бы с первым — увидеть это было бы нечем.
    """
    return (
        f"Охват: эмитентов с выпусками в обращении у источника "
        f"{summary['с выпусками в обращении']}, строк в списке "
        f"{summary['эмитентов']}, из них без выпусков в обращении "
        f"{summary['без выпусков в обращении']} — в доли охвата они не идут. "
        f"Маршрут построен по консолидированной отчётности у "
        f"{summary['маршрут по МСФО']}, по отчётности юридического лица "
        f"у {summary['маршрут по РСБУ']}, по одним событиям и рейтингам "
        f"у {summary['маршрут по событиям и рейтингам']}. "
        # **Рынок открывает эмитента либо поднимает тяжесть уже открытому.**
        # Одно число на оба случая читалось бы как «рынок нашёл столько-то»,
        # тогда как большинству он добавился к основаниям, уже стоявшим.
        f"Рыночное основание открыло {summary['рынок открыл']} эмитентов, "
        f"ещё у {summary['рынок поднял тяжесть']} подняло тяжесть к уже "
        "стоявшим основаниям."
    )


# Карточки эмитентов лежат рядом со списком, и ссылка на них ставится
# **только у собранных**: мёртвая ссылка обещает страницу, которой нет,
# а её отсутствие само говорит, что карточка не собрана.
CARDS = Path("data/output/cards")


def _journal(where: Path, today: date) -> None:
    """Пишет журнал исключений: кто вышел из списка, почему и когда.

    **Ни один эмитент не покидает список молча.** Журнал ведётся тем же
    правилом, которым маршрут исключает, — `routing_store.exclusions`, —
    и второго перечня не появляется: разойтись с ним было бы нечем замечено.
    """
    from finlib.scoring.routing_store import cards, exclusions

    known = cards()
    out, unconfirmed = exclusions(known, load_routing())
    lines = [
        f"# Журнал исключений списка наблюдения на {today:%d.%m.%Y}\n",
        "**Ни один эмитент не покидает список молча.** Выход объявляется "
        "причиной, датой и преемником; неподтверждённое — не выход, а очередь "
        "«установить статус эмитента».\n",
        "**Поле поглощения выходом не является**: оно названо у источника "
        "«Компания, оставшаяся после слияния/поглощения» и заполнено у живых "
        "тоже — у Ростелекома, МегаФона, Норникеля. Прочитанное как "
        "«поглощён», оно вывело из списка 24 живых эмитента 22.09.2026. "
        "Решает статус карточки, а поле лишь называет преемника.\n",
        f"Карточек справочника — {len(known)}. Вышли из списка — "
        f"**{len(out)}**, статус не подтверждён у **{len(unconfirmed)}**.\n",
        "## Вышли из списка\n",
        "| Эмитент | ИНН | Причина | Преемник | Карточка обновлена |",
        "|---|---|---|---|---|",
    ]
    for item in sorted(out.values(), key=lambda entry: entry.name):
        lines.append(
            f"| {item.name} | {item.inn} | {item.reason} | {item.successor} "
            f"| {item.updated or 'не указана'} |"
        )
    lines.append("\n## Статус не подтверждён: остались в очереди статуса\n")
    lines.append("| Эмитент | ИНН | Что говорит карточка |")
    lines.append("|---|---|---|")
    for inn, reason in sorted(
        unconfirmed.items(), key=lambda pair: str(known[pair[0]].get("name_rus"))
    ):
        lines.append(f"| {known[inn].get('name_rus')} | {inn} | {reason} |")
    where.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _now() -> datetime:
    """Текущее московское время ручного запуска."""
    return datetime.now(MOSCOW)


# **Ручной запуск не занимает имя планового.** Плановый прогон передаёт
# `--out` с `watchlist_<дата>.html` и отказывается публиковать, если файл уже
# лежит: страница, собранная руками до 10:00, оставила бы день без планового
# списка. Без `--out` имя помечено как ручное и несёт время до секунды —
# два ручных запуска тоже не сталкиваются.
def manual_stamp(today: date, now: datetime) -> str:
    """Метка имён ручного запуска: дата отчёта, пометка и время запуска."""
    return f"{today:%Y-%m-%d}_manual_{now:%H%M%S}"


def main() -> int:
    """Сохраняет новую страницу; отдельная проба CSV не обращается к БД и сети."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--report", type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--late-report", type=Path)
    parser.add_argument("--from-csv", type=Path)
    parser.add_argument("--bonds-snapshot", type=Path)
    parser.add_argument("--cards", type=Path)
    args = parser.parse_args()
    today = args.as_of
    stamp = f"{today:%Y-%m-%d}" if args.out else manual_stamp(today, _now())
    out = args.out or Path(f"data/output/watchlist_{stamp}.html")
    if out.exists():
        raise ValueError(f"сохранённый HTML не переписывается: {out}; выберите другой --out")
    coverage = None
    if args.from_csv:
        bonds = None
        if args.bonds_snapshot:
            raw = json.loads(args.bonds_snapshot.read_text(encoding="utf-8"))
            if raw.get("total") != len(raw["items"]):
                raise ValueError("снимок выпусков неполный")
            bonds = {str(item.get("emitent_inn") or "").strip() for item in raw["items"]}
        rows, summary = csv_rows(args.from_csv, bonds=bonds)
        coverage = ("Отдельная проба по сохранённому CSV. Оценки и величины не пересчитаны. "
                    "Дополнительные счётчики маршрута, давность и графики в CSV не сохранены; "
                    "их неизвестность не означает ноль. Статус выпусков — по переданному файлу "
                    "либо неизвестен. Все исходные графы раскрываются в панели эмитента.")
    else:
        with connection() as conn:
            rows, summary = rows_of(conn, today)
    if not rows:
        print("строк нет: страница не собрана; это отсутствие данных, а не пустой список.")
        return 1
    text = render(rows, summary, load_routing(), today, output=out, report=args.report,
                  csv_path=args.from_csv or args.csv, late_report=args.late_report,
                  cards=args.cards,
                  coverage=coverage)
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, text, exclusive=True)
    if not args.from_csv:
        _journal(out.with_name(f"watchlist_exclusions_{stamp}.md"), today)
    print(f"{out}: эмитентов {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
