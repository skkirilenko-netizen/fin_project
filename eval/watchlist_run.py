"""Список наблюдения: один HTML-файл со всеми эмитентами и их корзинами.

    uv run python eval/watchlist_run.py            # data/output/watchlist_<дата>.html
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

import html
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import foreign_units  # noqa: E402
from finlib.report.market_chart import charts  # noqa: E402
from finlib.report.policy import load_policy, months_between  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_catalogue import catalogue_for  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.market import load_market as _market_rules  # noqa: E402
from finlib.sources.market import series as _market_series  # noqa: E402

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
    for item in with_bonds:
        for name in item["subgroups"][:1]:
            summary[f"— {name}"] += 1
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


def render(rows: list[dict], summary: dict[str, int], routing, today: date) -> str:
    """Собирает страницу: сводка, фильтры, таблица. Только чтение."""
    baskets = [(item.code, item.name) for item in routing.ordered()]
    subgroups: list[str] = []
    for basket in routing.ordered():
        subgroups.extend(item.name for item in basket.groups)
    counts = "".join(
        f'<div class="card"><div class="num">{count}</div>'
        f'<div class="cap">{html.escape(name)}</div></div>'
        for name, count in summary.items()
    )
    options = "".join(
        f'<option value="{html.escape(code)}">{html.escape(name)}</option>'
        for code, name in baskets
    )
    group_options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
        for name in subgroups
    )
    body = "".join(_row_html(item) for item in rows)
    status = (
        f"структура правил {routing.status}"
        + (f" ({routing.approved_by})" if routing.approved_by else "")
        + f", пороги {routing.thresholds}"
    )
    return _PAGE.format(
        today=f"{today:%d.%m.%Y}",
        total=len(rows),
        status=html.escape(status),
        version=html.escape(routing.version),
        cards=counts,
        options=options,
        groups=group_options,
        rows=body,
        coverage=html.escape(_coverage_line(summary)),
        unchecked=_limitations(routing),
    )


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
        f"у {summary['маршрут по событиям и рейтингам']}."
    )


# Карточки эмитентов лежат рядом со списком, и ссылка на них ставится
# **только у собранных**: мёртвая ссылка обещает страницу, которой нет,
# а её отсутствие само говорит, что карточка не собрана.
CARDS = Path("data/output/cards")


def _card_link(inn: str) -> str:
    """ИНН строки со ссылкой на карточку, если она собрана."""
    safe = html.escape(inn)
    if not (CARDS / f"{inn}.md").exists():
        return safe
    return f'<a class="card" href="cards/{safe}.md" title="карточка эмитента">{safe}</a>'


def _row_html(item: dict) -> str:
    """Одна строка таблицы: главное основание, остальные свёрнуто.

    **Одно главное основание в строке.** Перечень из четырёх формулировок
    читается как список дел, а не как ответ на вопрос «что с эмитентом»:
    главное стоит открыто, остальные — строкой «ещё N» под ним.
    """
    said = [text for entry in item["grounds"] for text in entry["details"]]
    main = said[0] if said else ""
    # Справочное стоит после оснований корзины: оно ничего не решает,
    # но и потеряться не должно.
    rest = said[1:] + list(item["notes"])
    grounds = (
        f'<div class="gn">{html.escape(main)}</div>'
        + (
            '<details class="more"><summary>ещё '
            f'{len(rest)}</summary>'
            + "".join(f'<span class="gd">{html.escape(text)}</span>' for text in rest)
            + "</details>"
            if rest
            else ""
        )
        if main
        # **Строка покрытия у «Без внимания».** Пустая графа читается
        # как «ничего не проверяли», тогда как проверено всё, что маршрут
        # умеет: величины и события.
        else ""
    )
    # **График свёрнут, а не вынесен в отдельную графу.** Строка списка
    # отвечает «что с эмитентом», а ряд — «как он к этому пришёл»: открывают
    # его тогда, когда первое уже прочитано.
    if item["chart"]:
        grounds += (
            '<details class="more"><summary>рынок: спред и цена</summary>'
            f'<div class="chart">{item["chart"]}</div></details>'
        )
    if not main:
        # **Строка покрытия у «Без внимания».** Пустая графа читается
        # как «ничего не проверяли», тогда как проверено всё, что маршрут
        # умеет: величины и события.
        grounds = (
            f'<div class="cover">{html.escape(item["coverage"])}</div>'
            + "".join(
                f'<span class="gd">{html.escape(text)}</span>' for text in rest
            )
            + grounds
        )
    values = "".join(
        f'<div class="v"><span class="vn">{html.escape(name)}</span>'
        f'<span class="vv">{html.escape(shown)}</span></div>'
        for name, shown in item["values"]
    )
    subgroup = item["subgroups"][0] if item["subgroups"] else ""
    others = ", ".join(item["subgroups"][1:])
    action = item["actions"][0] if item["actions"] else ""
    # Давность считается от отчётной даты, а её может не быть вовсе:
    # «0 мес.» у эмитента без отчётности читалось бы как свежая.
    when = item["report_date"] + (
        f' · {item["months"]} мес.' if item["months"] is not None else ""
    )
    fresh = (
        f'<span class="stale">{when}</span>'
        if item["stale"] or item["overdue"]
        else when
    )
    assessed = (
        f'<span class="cls">класс {html.escape(item["assessed"])}</span>'
        if item["assessed"]
        else ""
    )
    # **Строка без выпусков в обращении помечена и отделена.** В сводные доли
    # она не идёт: маршрут спрашивает, нужен ли человек, а нужен он там,
    # где есть долг. Из списка она не исчезает — отчётность у нас есть,
    # и молчание о ней читалось бы как «такого эмитента нет».
    idle = (
        ""
        if item["bonds"]
        else '<span class="idle">без выпусков в обращении</span>'
    )
    return (
        f'<tr data-basket="{html.escape(item["basket"])}" '
        f'data-group="{html.escape(subgroup)}" '
        f'data-bonds="{"1" if item["bonds"] else "0"}" '
        f'data-name="{html.escape(item["name"].lower())}">'
        f'<td class="nm">{html.escape(item["name"])} {assessed} {idle}</td>'
        f'<td class="inn">{_card_link(item["inn"])}</td>'
        f'<td class="bk b-{html.escape(item["basket"])}">'
        f'{html.escape(item["basket_name"])}</td>'
        f'<td class="sg">{html.escape(subgroup)}'
        + (f'<span class="act">{html.escape(action)}</span>' if action else "")
        + (f'<span class="oth">ещё: {html.escape(others)}</span>' if others else "")
        + f'</td><td class="gs">{grounds}</td><td class="vs">{values}</td>'
        # **Источник, стандарт и контур — одна графа.** Единица у коэффициентов
        # не значит ничего, а вот чья это отчётность и какого она контура —
        # значит: отдельная отчётность управляющей компании и консолидированная
        # группы описывают разные предметы.
        f'<td class="src">{html.escape(item["origin"])}</td>'
        f'<td class="fr">{fresh}</td></tr>'
    )


_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Список наблюдения</title>
<style>
  :root {{
    --bg: #fbfbf9; --fg: #1c1b19; --mut: #6b6862; --line: #e2ded6;
    --review: #b3261e; --attention: #8a6100; --clear: #1f6b3a; --card: #fff;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #17181a; --fg: #ececec; --mut: #9c9a95; --line: #2e3033;
      --review: #ff8a80; --attention: #ffca6a; --clear: #7ad39a; --card: #1e2022;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--fg);
    font: 15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  .wrap {{ max-width: 1240px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--mut); font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 14px; min-width: 132px;
  }}
  .num {{ font-size: 22px; font-weight: 600; }}
  .cap {{ color: var(--mut); font-size: 12px; }}
  .bar {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
  select, input {{
    font: inherit; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--card); color: var(--fg);
  }}
  input {{ min-width: 220px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{
    text-align: left; vertical-align: top; padding: 9px 10px;
    border-bottom: 1px solid var(--line); font-size: 13px;
  }}
  th {{
    position: sticky; top: 0; background: var(--bg); font-size: 12px;
    text-transform: uppercase; letter-spacing: .04em; color: var(--mut);
  }}
  .nm {{ font-weight: 600; min-width: 200px; }}
  .inn {{ font-variant-numeric: tabular-nums; color: var(--mut); }}
  .card {{ color: var(--mut); text-decoration: underline dotted; }}
  .card:hover {{ color: var(--fg); }}
  .bk {{ font-weight: 600; white-space: nowrap; }}
  .b-review {{ color: var(--review); }}
  .b-attention {{ color: var(--attention); }}
  .b-clear {{ color: var(--clear); }}
  .sg {{ min-width: 150px; }}
  .act, .oth {{ display: block; color: var(--mut); font-size: 12px; }}
  .g {{ margin-bottom: 6px; }}
  .gn {{ display: block; }}
  .gd {{ display: block; color: var(--mut); font-size: 12px; }}
  .chart {{ margin-top: 6px; overflow-x: auto; }}
  .chart svg {{ display: block; margin-bottom: 4px; }}
  .v {{ display: flex; justify-content: space-between; gap: 10px; }}
  .vn {{ color: var(--mut); }}
  .vv {{ font-variant-numeric: tabular-nums; }}
  .vs {{ min-width: 220px; }}
  .fr {{ white-space: nowrap; }}
  .stale {{ color: var(--review); }}
  .cls, .idle {{
    font-size: 11px; color: var(--mut); border: 1px solid var(--line);
    border-radius: 6px; padding: 1px 5px; white-space: nowrap;
  }}
  .foot {{ color: var(--mut); font-size: 12px; margin-top: 18px; }}
  @media (max-width: 720px) {{
    .vs, .gs {{ min-width: 0; }}
    th, td {{ padding: 8px 6px; font-size: 12px; }}
  }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Список наблюдения</h1>
  <div class="sub">
    {total} эмитентов, собрано {today}. Правила маршрутизации {version}:
    {status}. Страница только читает: корзина и основания получены расчётом,
    исправить их отсюда нельзя.
  </div>
  <div class="cards">{cards}</div>
  <div class="bar">
    <select id="basket"><option value="">все корзины</option>{options}</select>
    <select id="group"><option value="">все подгруппы</option>{groups}</select>
    <select id="bonds">
      <option value="1">с выпусками в обращении</option>
      <option value="">и с выпусками, и без</option>
      <option value="0">только без выпусков в обращении</option>
    </select>
    <input id="search" type="search" placeholder="поиск по наименованию"
           autocomplete="off">
    <span id="shown" class="cap"></span>
  </div>
  <table>
    <thead><tr>
      <th>Эмитент</th><th>ИНН</th><th>Корзина</th><th>Подгруппа</th>
      <th>Основания</th><th>Величины маршрута</th><th>Источник</th>
      <th>Отчётность</th>
    </tr></thead>
    <tbody id="body">{rows}</tbody>
  </table>
  <div class="foot">
    Корзина по умолчанию упорядочена: разбор, внимание, без внимания.
    Величины печатаются той же разрядностью, что в заключении.
    <br>{coverage}
    <br>{unchecked}
  </div>
</div>
<script>
  const rows = Array.from(document.querySelectorAll('#body tr'));
  const basket = document.getElementById('basket');
  const group = document.getElementById('group');
  const search = document.getElementById('search');
  const shown = document.getElementById('shown');
  // **Отбор по долгу стоит первым и по умолчанию показывает эмитентов
  // с выпусками в обращении.** Сводные доли считаются по ним же: строка
  // без долга из списка не исчезает, но маршрут спрашивает, нужен ли человек,
  // а нужен он там, где есть долг.
  const bonds = document.getElementById('bonds');
  function apply() {{
    const b = basket.value, g = group.value, d = bonds.value;
    const q = search.value.trim().toLowerCase();
    let visible = 0;
    for (const row of rows) {{
      const ok = (!b || row.dataset.basket === b)
        && (!g || row.dataset.group === g)
        && (!d || row.dataset.bonds === d)
        && (!q || row.dataset.name.includes(q));
      row.hidden = !ok;
      if (ok) visible++;
    }}
    shown.textContent = 'показано ' + visible + ' из ' + rows.length;
  }}
  basket.addEventListener('change', apply);
  group.addEventListener('change', apply);
  bonds.addEventListener('change', apply);
  search.addEventListener('input', apply);
  apply();
</script>
</body>
</html>
"""


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


def main() -> int:
    """Собирает файл списка наблюдения; 1 — если эмитентов не нашлось."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    out = Path(f"data/output/watchlist_{today:%Y-%m-%d}.html")
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    with connection() as conn:
        rows, summary = rows_of(conn, today)
    if not rows:
        print(
            "эмитентов с комплектом вне карантина нет: страница не собрана. "
            "Это не пустой список, а отсутствие данных."
        )
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, summary, load_routing(), today), encoding="utf-8")
    _journal(out.with_name(f"watchlist_exclusions_{today:%Y-%m-%d}.md"), today)
    print(f"{out}: эмитентов {len(rows)}")
    for name, count in summary.items():
        print(f"  {name}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
