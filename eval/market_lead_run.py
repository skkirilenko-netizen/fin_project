"""Упреждение трёх слоёв: рынок против отчётности и рейтингов. **Только замер.**

    uv run python eval/market_lead_run.py > data/output/market_lead.md

**Вопрос, ради которого рыночный слой и заводится.** Годовая отчётность
о событиях между отчётными датами не говорит ничего: у Кириллицы величины
за 2025 год здоровые, а 07.09.2026 не исполнено погашение. Замер отвечает,
у скольких эмитентов с событием рынок сказал раньше отчётности и рейтинга
и на сколько дней.

**Считает не он.** Ряд спредов, ориентир дня и признаки живут в боевом пути
(`finlib.sources.market`, `finlib.scoring.market`); здесь только наблюдение
за ними. Прежде счёт стоял тут — это был объявленный долг, и он закрыт
вместе с утверждением методики 24.09.2026.

**Три упреждения меряются одной мерой.** Для каждого эмитента с датированным
неисполненным событием берётся первый день, когда слой о нём высказался,
и считается разница до события. Слои при этом разной природы, и это названо:
отчётность и рейтинги читаются из **записанной истории корзин**, а рынок —
из ряда торгов.

**Окна слоёв разной длины, и общее окно объявляется числом.** Ряд торгов
идёт с 24.09.2024, история корзин — с 24.09.2025: событие весны 2025 года
отчётность упредить не могла вовсе, и считать его в её знаменателе значило бы
мерить нашу доставку.

**Пороги отсюда не берутся.** Лестница печатается распределением: какую долю
рынка отсекает каждая ступень и на какой перцентиль кратности приходится.
События проверяют упреждение, а не назначают порог: подогнанный под полсотни
наблюдений порог меряет набор.
"""

import logging
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.market import (  # noqa: E402
    first_day_when,
    holds_level,
    holds_own_norm,
    holds_price,
    holds_widening,
)
from finlib.sources.cbonds_events import default_records, issues_of  # noqa: E402
from finlib.sources.market import (  # noqa: E402
    Market,
    Point,
    load_market,
    percentile,
    series,
    universe,
)

logger = logging.getLogger(__name__)

# Слои основания: перечень уже объявлен методикой маршрута (`ground_sources`),
# и второго здесь не заводится. Берутся два рода — отчётность и рейтинг.
_REPORTING = ("отчётность",)
_RATING = ("рейтинг",)

_HISTORY = """
SELECT inn, as_of, basket, grounds_all FROM routing_history
WHERE kind = 'backfill' ORDER BY inn, as_of
"""

# Сколько дней назад смотреть на слои у пропущенного эмитента. Девяносто —
# требование владельца 24.09.2026: квартал до события есть срок, на котором
# признак был бы полезен, а не задним числом верен.
_BEFORE = 90

# Опорные перцентили распределения кратности: на них и стоит лестница.
_PLACES = (50, 75, 90, 95, 99)


def points_of(market: Market, inn: str) -> list[Point]:
    """Ряд эмитента по возрастанию дня."""
    return [item for _, item in sorted(market.points(inn).items())]


def events() -> dict[str, date]:
    """ИНН → дата первого неисполненного события дефолта.

    **Круг тот же, что у рыночного ряда** (`sources.market.universe`):
    у эмитента в дефолте выпусков «в обращении» не остаётся, и перечень
    по ним терял бы ровно тех, ради кого слой заведён — у Кириллицы все три
    бумаги погашены либо в дефолте по погашению.
    """
    records = default_records()
    first: dict[str, date] = {}
    for inn in universe():
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            for item in records.get(issue.emission_id, ()):
                # **Событие — объявленный неплатёж, а не конец льготного
                # срока** (решение владельца 25.09.2026): `default_date`
                # у технического дефолта на 14 дней позже неплатежа, и
                # календарь по нему сдвигал событие на две недели вперёд —
                # упреждение каждого слоя выходило длиннее настоящего.
                if item.settled or item.known_on is None:
                    continue
                if inn not in first or item.known_on < first[inn]:
                    first[inn] = item.known_on
    return first


def layers() -> tuple[dict[str, dict[date, set[str]]], dict[str, str], dict]:
    """Сработавшие основания по дням из записанной истории и их слои.

    Третьим возвращается корзина того же дня: перечень оснований отвечает,
    что сработало, а корзина — что из этого вышло, и у пропущенного эмитента
    нужны оба ответа.
    """
    from finlib.scoring.routing import load_routing

    sources = load_routing().ground_sources
    with connection() as conn:
        rows = fetch_all(_HISTORY, {}, conn=conn)
    found: dict[str, dict[date, set[str]]] = defaultdict(dict)
    baskets: dict[str, dict[date, str]] = defaultdict(dict)
    for row in rows:
        found[row["inn"]][row["as_of"]] = set(row["grounds_all"] or ())
        baskets[row["inn"]][row["as_of"]] = row["basket"]
    return found, sources, baskets


def _first_day(
    history: dict[date, set[str]],
    sources: dict[str, str],
    words: tuple[str, ...],
    until: date = date.max,
) -> date | None:
    """Первый день, когда слой высказался; None — не высказывался вовсе."""
    for when in sorted(history):
        if when > until:
            return None
        for ground in history[when]:
            if any(word in sources.get(ground, "") for word in words):
                return when
    return None


def first_new_ground(
    history: dict[date, set[str]],
    pick,  # noqa: ANN001
    until: date = date.max,
) -> date | None:
    """Первый день, когда у слоя **появилось** основание, отбираемое `pick`.

    Отбор доводом, а не словами источника: слой бывает нужно разделить —
    рефинансирование против остальной отчётности, — и второе определение
    «появления» разошлось бы с первым.
    """
    days = sorted(history)
    if not days:
        return None
    standing = {ground for ground in history[days[0]] if pick(ground)}
    for when in days:
        if when > until:
            return None
        for ground in history[when]:
            if ground not in standing and pick(ground):
                return when
    return None


def _first_new_day(
    history: dict[date, set[str]],
    sources: dict[str, str],
    words: tuple[str, ...],
    until: date = date.max,
) -> date | None:
    """Первый день, когда у слоя **появилось** основание, которого не было.

    **Упреждение, упирающееся в начало истории, упреждением не является**
    (решение владельца 24.09.2026). Основание, стоявшее уже в первый
    наблюдавшийся день, ничего не предсказало: оно описывает положение,
    а не событие, и сколько оно стояло до начала наблюдения — неизвестно.
    Здесь такие основания исключаются, и остаётся только появление нового.
    """
    return first_new_ground(
        history,
        lambda ground: any(
            word in sources.get(ground, "") for word in words
        ),
        until,
    )


def _appeared(points: list[Point], holds, until: date, of: int, out_of: int):  # noqa: ANN001, ANN201
    """День срабатывания признака, если он **не** держался с первого дня ряда.

    То же правило, что у слоёв истории, и применяется оно к рынку наравне:
    признак, стоявший с первого наблюдавшегося дня, о событии не предупредил.
    У Кириллицы ступень p95 стояла с четвёртого дня ряда и до самого
    события — это описание положения, а не упреждение.
    """
    first = first_day_when(points, holds, until, of, out_of)
    if first is None or not points:
        return None
    # Признак держался уже в самом начале ряда: подтверждение набирается
    # за первые `out_of` наблюдений, и раньше него сработать оно не может.
    edge = points[min(out_of, len(points)) - 1].day
    return None if first <= edge else first


def _said(days: list[int]) -> str:
    """Упреждение словами: медиана, край и знаменатель."""
    if not days:
        return "наблюдений нет"
    return (
        f"медиана **{statistics.median(days):.0f}** дн., "
        f"от {min(days)} до {max(days)}, наблюдений {len(days)}"
    )


def _row(name: str, first, over: set[str], inside: dict, base: float,  # noqa: ANN001
         short: bool = False) -> None:
    """Строка сравнения признаков: пять чисел и упреждение.

    `short` убирает графу ложных срабатываний — она выводится из первых двух,
    и в таблице появления места ей нет.
    """
    fired = {inn for inn in over if first(inn, date.max) is not None}
    # **Высказаться после события — не поймать его.** Прежде «с событием»
    # считало всякого, у кого признак сработал когда угодно, в том числе
    # на следующий день после неисполненного платежа: у цены это давало
    # выявляемость 96,9 %, тогда как до события она срабатывала реже.
    # Признак засчитывается пойманным, только если он сработал **до** события.
    days = {
        inn: day
        for inn in fired & set(inside)
        if (day := first(inn, inside[inn])) is not None
    }
    hit = set(days)
    leads = [(inside[inn] - day).days for inn, day in days.items()]
    precision = len(hit) / len(fired) if fired else 0
    mine = set(inside) & over
    recall = len(hit) / len(mine) if mine else 0
    lift = precision / base if base else 0
    false = "" if short else f"| {len(fired) - len(hit)} "
    print(
        f"| {name} | {len(fired)} | {len(hit)} {false}"
        f"| {precision:.1%} | {recall:.1%} | {lift:.1f}× | {_said(leads)} |"
    )


def _multiples(market: Market) -> dict[date, list[Decimal]]:
    """Кратности спреда к ориентиру по дням: одно место на оба распределения.

    День без спреда в распределение не идёт: у флоатера доходности к сроку
    нет, а цена есть, и считать такую точку кратностью не по чему.
    """
    by_day: dict[date, list[Decimal]] = defaultdict(list)
    for own in market.issuers.values():
        for day, item in own.items():
            level = market.benchmark.get(day)
            if level and level > 0 and item.spread is not None:
                by_day[day].append(item.spread / level)
    return by_day


def _quantiles(market: Market) -> list[tuple[int, Decimal]]:
    """Кратность на опорных перцентилях распределения по дням.

    Считается по дням и сводится медианой: распределение кратности двигается
    вместе с рынком, и один перцентиль по всей истории смешал бы спокойный
    год с кризисным месяцем.
    """
    by_day = _multiples(market)
    found: list[tuple[int, Decimal]] = []
    for place in _PLACES:
        values = [
            percentile(sorted(items), place)
            for items in by_day.values()
            if len(items) >= 20
        ]
        if values:
            found.append((place, statistics.median(values)))
    return found


def _ladder(policy, market: Market) -> list[tuple]:  # noqa: ANN001
    """Доля рынка и перцентиль кратности у каждой ступени лестницы."""
    by_day = _multiples(market)
    found: list[tuple] = []
    for step in policy.ladder.steps:
        shares: list[float] = []
        places: list[float] = []
        for values in by_day.values():
            if len(values) < 20:
                continue
            above = sum(1 for item in values if item >= step.multiple)
            shares.append(above / len(values))
            places.append(100 * (1 - above / len(values)))
        found.append(
            (
                step.code,
                step.multiple,
                statistics.median(shares) if shares else 0.0,
                statistics.median(places) if places else 0.0,
                "маршрут" if step.in_route else "—",
            )
        )
    return found


def _signals(market: Market, floor: Decimal | None) -> list[tuple]:
    """Перечень рыночных признаков: имя и признак дня.

    **Ступени лестницы стоят на перцентилях распределения**, а не на круглых
    числах: замер 24.09.2026 показал, что 1,5× отсекает три четверти рынка,
    то есть мерит фон. Ниже p75 в маршрут не идёт ничего (решение владельца).
    """
    steps = dict(_quantiles(market))
    found: list[tuple] = []
    for place in (75, 90, 95, 99):
        multiple = steps.get(place)
        if multiple is not None:
            found.append(
                (
                    f"уровень: кратность ≥ {multiple:.2f}× (p{place})",
                    holds_level(market, multiple, floor),
                )
            )
    # Расширение: то же движение, померенное наблюдениями и календарём.
    for back, calendar, name in (
        (4, False, "4 наблюдения"),
        (14, True, "две недели"),
        (30, True, "месяц"),
    ):
        found.append(
            (
                f"расширение: спред +60 % за {name}",
                holds_widening(Decimal("0.6"), back, calendar),
            )
        )
    for multiple in ("1.5", "2.0", "3.0"):
        found.append(
            (
                f"своя норма: спред ≥ {multiple}× медианы за 90 дней",
                holds_own_norm(Decimal(multiple), 90, 20),
            )
        )
    for below in ("75", "60", "40"):
        found.append(
            (f"цена ниже {below} % номинала", holds_price(Decimal(below)))
        )
    return found


def _at(points: list[Point], market: Market, edge: date) -> str:
    """Что говорил рынок в названный день: спред, кратность, цена.

    Берётся последнее наблюдение **не позже** дня: у неликвидной бумаги торгов
    в сам день может не быть вовсе, и «рынок молчал» тогда означало бы
    отсутствие сделки, а не отсутствие сигнала. Давность наблюдения печатается
    рядом — без неё свежая цена неотличима от полугодовой.
    """
    seen = [item for item in points if item.day <= edge]
    if not seen:
        return "наблюдений до этого дня нет"
    item = seen[-1]
    level = market.benchmark.get(item.day)
    said = (
        f"спред {item.spread:.0f} б. п."
        if item.spread is not None
        else "спред не считается (доходность к сроку не определена)"
    )
    if level and level > 0 and item.spread is not None:
        said += f" при ориентире {level:.0f} ({item.spread / level:.1f}×)"
    if item.price is not None:
        said += f", цена {item.price:.1f} %"
    behind = (edge - item.day).days
    return said + (f" (наблюдение {behind} дн. назад)" if behind else "")


def _said_layer(
    history: dict[date, set[str]],
    baskets: dict[date, str],
    sources: dict[str, str],
    words: tuple[str, ...],
    names: dict[str, str],
    edge: date,
) -> str:
    """Что говорил слой истории в названный день: корзина и его основания."""
    days = [day for day in sorted(history) if day <= edge]
    if not days:
        return "истории до этого дня нет"
    day = days[-1]
    mine = sorted(
        names.get(ground, ground)
        for ground in history[day]
        if any(word in sources.get(ground, "") for word in words)
    )
    return (
        f"корзина «{baskets.get(day, '')}», "
        + ("основания слоя: " + "; ".join(mine) if mine else "оснований слоя нет")
    )


def _events_of(inn: str, records: dict, moment: date) -> list[str]:
    """Неисполненные события эмитента на дату: выпуск, вид, сумма."""
    issues, known = issues_of(inn)
    if not known:
        return ["перечня выпусков на диске нет"]
    said: list[str] = []
    for issue in issues:
        for item in records.get(issue.emission_id, ()):
            if item.settled or item.moment is None or item.moment != moment:
                continue
            amount = (
                f", сумма {item.amount:,.0f}".replace(",", " ") if item.amount else ""
            )
            said.append(
                f"{issue.name} ({issue.reg_number or 'рег. номера нет'}): "
                f"{item.kind}, {item.moment:%d.%m.%Y}{amount}"
            )
    return said or ["события на эту дату в перечне не нашлось"]


def _lead(day: date | None, moment: date) -> str:
    """Упреждение днями; пусто — слой не высказался."""
    if day is None or day > moment:
        return "—"
    return f"{(moment - day).days}"


def _told(history: dict, sources: dict, words: tuple[str, ...],
          moment: date, started: date) -> str:
    """Упреждение слоя истории с оговоркой о длине самой истории.

    **Три исхода, и их нельзя сводить к двум.** Слой высказался; слой молчал;
    слоя на ту дату не наблюдалось вовсе. Третье выглядит как второе и им
    не является: молчание — свойство слоя, отсутствие истории — наше.
    Высказывание первым же днём истории тоже отмечается: прежде того дня
    мы не смотрели, и упреждение здесь не меньше названного.
    """
    if moment < started:
        return "нет истории"
    day = _first_day(history, sources, words, moment)
    if day is None or day > moment:
        return "—"
    edge = "≥" if day <= min(history, default=day) else ""
    return f"{edge}{(moment - day).days}"


def _appearance(policy, market: Market, history: dict, sources: dict,  # noqa: ANN001
                inside: dict, common: dict, started: date, base: float) -> None:
    """Упреждение появившихся оснований против упреждения стоявших.

    **Основание, стоявшее с первого наблюдавшегося дня, не упредило ничего**
    (решение владельца 24.09.2026). «Медиана 240 дней» у слоя отчётности
    означала «стояло уже тогда, когда мы начали смотреть», и сравнивать её
    с рыночной было нельзя. Здесь то же измерение сделано по появлению:
    первый день, когда у слоя возникло основание, которого прежде не было.

    Правило применяется и к рынку — иначе оно мерило бы слои разными мерками.
    """
    print("\n### Упреждение появления, а не стояния\n")
    print(
        "Из упреждения исключены основания, стоявшие уже в первый "
        "наблюдавшийся день: они описывают положение, а не предсказывают "
        "событие, и сколько они стояли до начала наблюдения — неизвестно. "
        "Разница между этой таблицей и предыдущими и есть цена различения.\n"
    )
    print(
        "| Признак | Появился | С событием | Точность | Выявляемость "
        "| Прирост | Упреждение |"
    )
    print("|---|---|---|---|---|---|---|")
    known = set(market.issuers)
    for step in sorted(policy.route_steps, key=lambda item: item.percentile):
        # Подтверждение берётся у самой ступени: у p99 оно своё — «5 из 10».
        rule = step.confirmation or policy.confirmation.default
        _row(
            f"уровень p{step.percentile} ({step.multiple:.2f}×, "
            f"подтверждение {rule.of} из {rule.out_of})",
            lambda inn, until, s=step, r=rule: _appeared(
                points_of(market, inn),
                holds_level(market, s.multiple, policy.floor),
                until,
                r.of,
                r.out_of,
            ),
            known,
            inside,
            base,
            short=True,
        )
    below = policy.distress_zone.price_below_percent
    _row(
        f"цена ниже {below:.0f} % номинала",
        lambda inn, until: _appeared(
            points_of(market, inn), holds_price(below), until, 1, 1
        ),
        known,
        inside,
        base,
        short=True,
    )
    share = len(set(common) & set(history)) / len(history) if history else 0
    for name, words in (
        (f"отчётность (с {started:%d.%m.%Y})", _REPORTING),
        (f"рейтинги (с {started:%d.%m.%Y})", _RATING),
    ):
        _row(
            name,
            lambda inn, until, w=words: _first_new_day(
                history.get(inn, {}), sources, w, until
            ),
            set(history),
            common,
            share,
            short=True,
        )


def _halted(market: Market, inside: dict) -> None:
    """Прекращение торгов перед событием: у скольких и за сколько дней.

    **Граница метода, найденная на Кириллице.** Бумага перестала торговаться
    за восемнадцать дней до неисполненного погашения, и рыночный слой лишился
    предмета ровно тогда, когда был нужнее всего. Вопрос владельца: частый ли
    это случай — и если частый, то само прекращение торгов есть признак.
    """
    print("\n## Прекращение торгов перед событием\n")
    # **Событие позже последнего доставленного дня в меру не идёт.** Разрыв
    # у него равен возрасту нашей доставки, а не молчанию бумаги: у события
    # 28.09.2026 при ряде до 21.09.2026 выходит «семь дней без торгов», хотя
    # торги, возможно, шли. Это та же ошибка, что «ноль упреждения» у события
    # раньше начала ряда, только с другого конца.
    last_day = max(market.benchmark, default=date.min)
    late = {inn for inn, moment in inside.items() if moment > last_day}
    rows: list[tuple[str, date, date, int]] = []
    for inn, moment in sorted(inside.items(), key=lambda item: item[1]):
        if inn in late:
            continue
        traded = [
            item.day
            for item in market.ordered(inn)
            if item.price is not None and item.day <= moment
        ]
        if not traded:
            continue
        rows.append((inn, moment, traded[-1], (moment - traded[-1]).days))
    if not rows:
        print("ни одного эмитента с торгами до события — мерить нечего.\n")
        return
    gaps = sorted(gap for _, _, _, gap in rows)
    print(
        f"Из меры исключены {len(late)} эмитентов, чьё событие позже "
        f"последнего доставленного дня ({last_day:%d.%m.%Y}): разрыв у них "
        "равен возрасту доставки, а не молчанию бумаги.\n"
    )
    print(
        f"Эмитентов с событием и торгами до него **{len(rows)}**. Разрыв "
        "между последним днём торгов и событием: медиана "
        f"**{statistics.median(gaps):.0f}** дн., от {min(gaps)} до {max(gaps)}. "
        f"Торги прекратились за неделю и более до события у "
        f"**{sum(1 for gap in gaps if gap >= 7)}**, за месяц и более — "
        f"у **{sum(1 for gap in gaps if gap >= 30)}**.\n"
    )
    print(
        "**Разрыв — не молчание рынка, а отсутствие предмета.** Там, где торги "
        "прекратились, рыночный признак не может ни сработать, ни промолчать: "
        "наблюдения нет вовсе. Ноль здесь означает, что бумага торговалась "
        "в самый день события.\n"
    )
    print("| ИНН | Событие | Последний день торгов | Разрыв, дн. |")
    print("|---|---|---|---|")
    for inn, moment, last, gap in sorted(rows, key=lambda item: -item[3])[:15]:
        print(f"| {inn} | {moment:%d.%m.%Y} | {last:%d.%m.%Y} | {gap} |")


# --- новая мера: поточечно (решение владельца 29.09.2026) ----------------------
#
# **Мера «сработал хоть раз» мерила длину ряда**, и с 29.09.2026 рыночное
# основание стоит, пока подтверждено недавно (`market.yaml`, `lifetime`).
# Прирост по эмитентам — «кто хоть раз сработал и у кого было событие» —
# у стоящего по сроку основания отвечает не на тот вопрос. Поэтому рядом
# с прежней мерой печатается поточечная: на срезах — первых торговых днях
# месяцев — кто стоит, и у кого из них событие в следующие `HORIZON` дней.
# Требование владельца: обе меры рядом, одна другую не заменяет.
HORIZON = 90


def cutoffs(days: list[date], start: date, last_event_day: date) -> list[date]:
    """Первые торговые дни месяцев, у которых весь горизонт уже наблюдён."""
    found: list[date] = []
    month = date(start.year, start.month, 1)
    while month + timedelta(days=HORIZON) <= last_event_day:
        later = [day for day in days if day >= month]
        if later and later[0] >= start:
            found.append(later[0])
        month = date(month.year + month.month // 12, month.month % 12 + 1, 1)
    return sorted(set(found))


@dataclass(frozen=True, slots=True)
class Pointwise:
    """Поточечная мера слоя: срезы, стоящие, попадания и знаменатели."""

    name: str
    cuts: int
    standing: int
    hits: int
    events: int
    observed: int

    @property
    def precision(self) -> float:
        """Доля стоящих, у кого событие в горизонте."""
        return self.hits / self.standing if self.standing else 0.0

    @property
    def base(self) -> float:
        """Та же доля у всех наблюдавшихся на срезах."""
        return self.events / self.observed if self.observed else 0.0

    @property
    def lift(self) -> float:
        """Прирост: точность к базовой доле."""
        return self.precision / self.base if self.base else 0.0

    def row(self) -> str:
        """Строка таблицы поточечной меры."""
        return (
            f"| {self.name} | {self.cuts} | {self.standing / max(self.cuts, 1):.1f} "
            f"| {self.hits} | {self.precision:.1%} | {self.base:.1%} "
            f"| {self.lift:.1f}× |"
        )


POINTWISE_HEAD = (
    "| Слой / основание | Срезов | Стоит в среднем | Попаданий | Точность "
    "| Базовая доля | Прирост |\n|---|---|---|---|---|---|---|"
)


def pointwise(
    name: str,
    stands,  # noqa: ANN001 — (inn, день) → стоит ли основание
    observed,  # noqa: ANN001 — (inn, день) → наблюдался ли эмитент к этому дню
    circle: set[str],
    when: dict[str, date],
    cuts: list[date],
) -> Pointwise:
    """Поточечная мера: сводка по срезам, событие — в горизонте после среза.

    Эмитент с событием до среза из среза исключается: о случившемся не
    предупреждают. Знаменатель базовой доли — наблюдавшиеся к срезу.
    """
    standing = hits = events_total = seen = 0
    for cut in cuts:
        edge = cut + timedelta(days=HORIZON)
        for inn in circle:
            moment = when.get(inn)
            if moment is not None and moment <= cut:
                continue
            if not observed(inn, cut):
                continue
            seen += 1
            ahead = moment is not None and cut < moment <= edge
            events_total += int(ahead)
            if stands(inn, cut):
                standing += 1
                hits += int(ahead)
    return Pointwise(name, len(cuts), standing, hits, events_total, seen)


def systemic_issuers() -> set[str]:
    """Системно значимые — тем же правилом, что в маршруте: у них подтверждение длиннее."""
    from finlib.scoring.routing import load_routing
    from finlib.scoring.routing_store import _outstanding, _top_share

    volumes = {
        inn: total
        for inn in universe()
        if (total := _outstanding(inn)) is not None and total > 0
    }
    return set(_top_share(volumes, load_routing().systemic.top_share))


def _market_pointwise(policy, market: Market, when: dict[str, date]) -> None:  # noqa: ANN001
    """Новая мера рыночных оснований: боевой расчёт на срезах и накануне события.

    **Замер не считает сам**: стоит ли основание на дату, отвечает
    `scoring.market.findings` — с полом ориентира, сроком жизни и системным
    подтверждением, как в маршруте.
    """
    from finlib.scoring.market import findings

    systemic = systemic_issuers()
    days = market.calendar()
    circle = set(market.issuers)
    cache: dict[tuple[str, date], tuple] = {}

    def said(inn: str, day: date) -> tuple:
        key = (inn, day)
        if key not in cache:
            cache[key] = findings(policy, market, inn, day, systemic=inn in systemic)
        return cache[key]

    def observed(inn: str, day: date) -> bool:
        own = market.ordered(inn)
        return bool(own) and own[0].day <= day

    grounds = {step.ground: f"p{step.percentile}" for step in policy.route_steps}
    grounds[policy.distress_zone.ground] = (
        f"цена ниже {policy.distress_zone.price_below_percent:.0f} %"
    )
    review = {
        step.ground for step in policy.route_steps if step.basket == "review"
    } | {policy.distress_zone.ground}
    last_event = max(days, default=date.min)
    cuts = cutoffs(days, days[0] + timedelta(days=HORIZON), last_event) if days else []
    print("\n## Новая мера: поточечно\n")
    print(
        f"Срезы — первые торговые дни месяцев ({len(cuts)}: "
        f"{cuts[0]:%m.%Y} — {cuts[-1]:%m.%Y}); событие засчитывается, если "
        f"случилось в следующие {HORIZON} дней после среза. Стоит ли основание, "
        "отвечает боевой расчёт — с полом ориентира "
        f"{policy.floor} б. п. и сроком жизни {policy.lifetime.trading_days} "
        "торговых дней. Прежняя мера — таблицы выше: прирост по эмитентам, "
        "сработавшим хоть раз.\n"
        if cuts
        else "Срезов нет: ряд короче горизонта.\n"
    )
    if not cuts:
        return
    print(POINTWISE_HEAD)
    for ground, label in grounds.items():
        print(
            pointwise(
                label,
                lambda inn, day, g=ground: any(
                    item.ground == g for item in said(inn, day)
                ),
                observed,
                circle,
                when,
                cuts,
            ).row()
        )
    print(
        pointwise(
            "рынок в «Разбор» (p99 либо цена)",
            lambda inn, day: any(item.ground in review for item in said(inn, day)),
            observed,
            circle,
            when,
            cuts,
        ).row()
    )

    # **Накануне события**: стоит ли основание в последний торговый день перед
    # ним, и с какого дня стоит непрерывно. Это вопрос «был ли эмитент
    # в корзине, когда событие пришло», а не «срабатывал ли когда-нибудь».
    print("\n### Накануне события\n")
    inside = {
        inn: moment
        for inn, moment in when.items()
        if inn in circle and days[0] < moment <= last_event
    }
    print("| Основание | Стоит накануне | Из событий | Упреждение от начала стояния, медиана |")
    print("|---|---|---|---|")
    for ground, label in list(grounds.items()) + [("review", "рынок в «Разбор»")]:
        leads: list[int] = []
        for inn, moment in inside.items():
            before = [day for day in days if day < moment]
            if not before:
                continue
            mine = [
                item
                for item in said(inn, before[-1])
                if (item.ground in review if ground == "review" else item.ground == ground)
            ]
            if mine:
                leads.append((moment - min(item.since for item in mine)).days)
        lead = f"{statistics.median(leads):.0f}" if leads else "—"
        print(f"| {label} | {len(leads)} | {len(inside)} | {lead} |")

    # **ВДО называется у держателей p99** (`market.yaml`, `spread.high_yield`):
    # рабочее определение владельца 29.09.2026, и это его первый читатель.
    edge = Decimal(str(policy.spread["high_yield"]["from_bp"]))
    top = next(step.ground for step in policy.route_steps if step.basket == "review")
    holders = [inn for inn in circle if any(item.ground == top for item in said(inn, last_event))]
    high = [
        inn
        for inn in holders
        if (own := market.ordered(inn)) and own[-1].spread is not None and own[-1].spread >= edge
    ]
    print(
        f"\nДержателей p99 на {last_event:%d.%m.%Y}: **{len(holders)}**, из них ВДО "
        f"(G-спред последнего дня от {edge:.0f} б. п.) — **{len(high)}**.\n"
    )


def _overlap(policy, market: Market, history: dict, sources: dict,  # noqa: ANN001
             inside: dict, common: dict, started: date) -> None:
    """Кто ловит событие: только отчётность, только рейтинги, только рынок.

    **Это и есть ответ на вопрос, нужна ли матрица слоёв.** Если каждый
    эмитент с событием ловится всеми тремя, матрица не добавляет ничего;
    если у каждого свой слой — она и есть ответ.
    """
    print(
        f"Событий в общем окне слоёв **{len(common)}** из {len(inside)}: "
        f"история корзин начинается {started:%d.%m.%Y}, и "
        f"{len(inside) - len(common)} событий раньше этого дня отчётность "
        "с рейтингами упредить не могли по нашей доставке, а не по своему "
        "молчанию.\n"
    )
    top = next(step for step in policy.route_steps if step.basket == "review")
    rule = top.confirmation or policy.confirmation.default
    extreme = top.multiple
    below = policy.distress_zone.price_below_percent

    def by_price(points: list[Point], moment: date) -> bool:
        return bool(points) and first_day_when(
            points, holds_price(below), moment
        ) is not None

    def by_route(points: list[Point], moment: date) -> bool:
        if by_price(points, moment):
            return True
        return bool(points) and first_day_when(
            points, holds_level(market, extreme, policy.floor), moment, rule.of,
            rule.out_of,
        ) is not None

    for title, market_said in (
        (f"Рынок — цена ниже {below:.0f} % номинала", by_price),
        (
            f"Рынок — основания маршрута: цена ниже {below:.0f} % либо "
            f"кратность ≥ {extreme:.2f}× с подтверждением {rule.of} "
            f"из {rule.out_of}",
            by_route,
        ),
    ):
        counted: Counter = Counter()
        for inn, moment in common.items():
            said = tuple(
                name
                for name, yes in (
                    ("рынок", market_said(points_of(market, inn), moment)),
                    (
                        "отчётность",
                        _first_day(history.get(inn, {}), sources, _REPORTING, moment)
                        is not None,
                    ),
                    (
                        "рейтинги",
                        _first_day(history.get(inn, {}), sources, _RATING, moment)
                        is not None,
                    ),
                )
                if yes
            )
            counted[said or ("никто",)] += 1
        print(f"\n**{title}**\n")
        print("| Кто сказал до события | Эмитентов |")
        print("|---|---|")
        for names, count in sorted(counted.items(), key=lambda item: -item[1]):
            print(f"| {', '.join(names)} | {count} |")
        alone = {
            name: counted[(name,)] for name in ("рынок", "отчётность", "рейтинги")
        }
        print(
            f"\nТолько рынок — {alone['рынок']}, только отчётность — "
            f"{alone['отчётность']}, только рейтинги — {alone['рейтинги']}, "
            f"никто — {counted[('никто',)]} из {len(common)}."
        )

    # **Упреждение слоя отчётности почти всё упирается в начало истории**,
    # и без этого числа медиана в 240 дней читается как упреждение, тогда как
    # означает «основание стояло уже в первый наблюдавшийся день». Слой этот —
    # не сигнал с датой, а состояние, и сравнивать его упреждение с рыночным
    # нельзя, не назвав, у скольких оно упёрлось в край.
    for name, words in (("отчётности", _REPORTING), ("рейтингов", _RATING)):
        spoke = 0
        edged = 0
        for inn, moment in common.items():
            own = history.get(inn, {})
            day = _first_day(own, sources, words, moment)
            if day is None:
                continue
            spoke += 1
            edged += int(day <= min(own, default=day))
        print(
            f"\nУ слоя {name} упреждение упирается в начало истории "
            f"**{edged} раз из {spoke}**: основание стояло уже в первый "
            "наблюдавшийся день, и сколько оно стояло до него — неизвестно."
        )


def _blind(policy, market: Market, history: dict, baskets: dict,  # noqa: ANN001
           sources: dict, inside: dict, title: str, note: str = "") -> None:
    """Кого не увидел ни один слой: поимённо, с состоянием слоёв за квартал.

    **Это тот же вопрос, что был с Кириллицей, и он важнее ступеней**
    (требование владельца 24.09.2026). Доля пойманных отвечает, чего слои
    стоят вместе; пропущенный эмитент отвечает, чего не хватает — и ответ
    этот виден только поимённо.
    """
    from finlib.scoring.routing import load_routing
    from finlib.scoring.routing_store import cards

    names = {
        ground.code: ground.name
        for basket in load_routing().baskets
        for ground in basket.grounds
    }
    known = cards()
    records = default_records()
    below = policy.distress_zone.price_below_percent
    missed: list[tuple[str, date]] = []
    for inn, moment in sorted(inside.items(), key=lambda item: item[1]):
        points = points_of(market, inn)
        if points and first_day_when(points, holds_price(below), moment) is not None:
            continue
        if _first_day(history.get(inn, {}), sources, _REPORTING, moment):
            continue
        if _first_day(history.get(inn, {}), sources, _RATING, moment):
            continue
        missed.append((inn, moment))
    print(f"\n## {title}: {len(missed)} из {len(inside)}\n")
    print(
        "Доля пойманных отвечает, чего слои стоят вместе; пропущенный эмитент "
        "отвечает, чего не хватает, — и ответ этот виден только поимённо. "
        f"Состояние слоёв взято за {_BEFORE} дней до события: квартал — срок, "
        "на котором признак был бы полезен, а не задним числом верен.\n"
    )
    print(
        "**Молчание слоя и отсутствие данных у слоя — разные вещи**, и у "
        "каждого пропущенного сказано, которое из двух. Рыночного ряда может "
        "не быть вовсе, а история корзин короче рыночной на год: слой, "
        "у которого на ту дату нет истории, не молчал — молчим мы.\n"
    )
    if note:
        print(note + "\n")
    for inn, moment in missed:
        card = known.get(inn, {})
        name = str(card.get("name_rus") or "").strip() or inn
        edge = moment - timedelta(days=_BEFORE)
        points = points_of(market, inn)
        print(f"### {name} ({inn})\n")
        for issue in _events_of(inn, records, moment):
            print(f"- {issue}")
        print(f"\nЗа {_BEFORE} дней до события, {edge:%d.%m.%Y}:\n")
        print(
            "- рынок: "
            + (_at(points, market, edge) if points else market.silence(inn))
        )
        print(
            "- отчётность: "
            + _said_layer(
                history.get(inn, {}), baskets.get(inn, {}), sources,
                _REPORTING, names, edge,
            )
        )
        print(
            "- рейтинги: "
            + _said_layer(
                history.get(inn, {}), baskets.get(inn, {}), sources,
                _RATING, names, edge,
            )
        )
        print(
            "- в день события рынок: "
            + (_at(points, market, moment) if points else market.silence(inn))
            + "\n"
        )


def main() -> int:
    """Печатает упреждение трёх слоёв и распределение кратности."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    policy = load_market()
    market = series()
    when = events()
    history, sources, baskets = layers()

    print("# Упреждение слоёв: рынок против отчётности и рейтингов\n")
    print(
        f"Дней с ориентиром **{len(market.benchmark)}**, эмитентов с рыночным "
        f"рядом **{len(market.issuers)}**, эмитентов с неисполненным "
        f"датированным событием **{len(when)}**. Строк среза прочитано "
        f"{market.counted['строк']}.\n"
    )
    print(
        "**Правила рыночного слоя считает боевой путь** "
        "(`finlib.sources.market`, `finlib.scoring.market`); замер зовёт его "
        "и сравнивает, а сам не считает ничего.\n"
    )

    print("## Что отброшено правилом сравнимости\n")
    print("| Причина | Строк |")
    print("|---|---|")
    for name, count in market.counted.items():
        print(f"| {name} | {count} |")
    print(
        "\nОтброшенное названо числом: правило, отсекающее половину рынка "
        "молча, неотличимо от ошибки чтения.\n"
    )

    print("## Кого рынок не покрывает и почему\n")
    print(
        "**«Рынок молчал» — три разных ответа, и считаются они порознь.** "
        "Выпусков эмитента в истории биржи нет вовсе — это про доставку; "
        "есть, но не торговались ни дня — про ликвидность бумаги; торговались, "
        "а спреда нет — про метод: у флоатера доходность к сроку не определена, "
        "и цена при этом известна.\n"
    )
    traded = {inn for inn, item in market.census.items() if item["with_price"]}
    with_spread = {inn for inn, item in market.census.items() if item["with_spread"]}
    print("| Круг | Эмитентов |")
    print("|---|---|")
    print(f"| в универсуме долга | {market.universe} |")
    print(f"| хотя бы один выпуск с ISIN | {market.with_isin} |")
    print(f"| строки среза есть | {len(market.census)} |")
    print(f"| торговались хоть день (есть цена) | {len(traded)} |")
    print(f"| спред считается хоть день | {len(with_spread)} |")
    print(
        f"\nЦеновой признак работает у {len(traded)} эмитентов, спредовый — "
        f"у {len(with_spread)}: **у {len(traded) - len(with_spread)} из них "
        "цена есть, а спреда нет**, и брать цену из ряда, отсеянного правилом "
        "сравнимости доходности, было бы потерей на ровном месте — правило "
        "это о доходности, а цена в нём не участвует.\n"
    )

    print("## Лестница кратности: что отсекает каждая ступень\n")
    print(
        "Доля рынка и перцентиль считаются по дням с ориентиром: у каждой даты "
        "своя доля, печатается медиана по дням. **Это и есть то обоснование, "
        "которого лестница ждёт**; события в него не входят.\n"
    )
    print("| Ступень | Кратность | Доля рынка | Перцентиль кратности | В маршруте |")
    print("|---|---|---|---|---|")
    for code, multiple, share, place, routed in _ladder(policy, market):
        print(f"| {code} | {multiple:.2f}× | {share:.2%} | {place:.1f} | {routed} |")
    print("\n| Перцентиль кратности | Кратность |")
    print("|---|---|")
    for place, value in _quantiles(market):
        print(f"| {place} | {value:.2f}× |")

    # **Событие раньше первого дня доставки рынок упредить не мог.** У ДВМП
    # дефолт датирован 2018 годом: истории торгов до 24.09.2024 у нас нет
    # вовсе, и ноль упреждения там означал бы «рынок молчал», тогда как
    # молчим мы. Оговорка стоит в каждом замере рынка (требование владельца).
    opened = min(market.benchmark, default=date.max)
    inside = {inn: moment for inn, moment in when.items() if moment >= opened}
    started = min((min(days) for days in history.values() if days), default=date.max)
    common = {inn: moment for inn, moment in inside.items() if moment >= started}

    print(
        "\n## Признаки: точность, выявляемость, прирост, упреждение "
        "(прежняя мера — по эмитентам, сработавшим хоть раз)\n"
    )
    print(
        f"**В окне доставки — {len(inside)} событий из {len(when)}.** Событие "
        f"раньше {opened:%d.%m.%Y} рынок упредить не мог: истории торгов "
        "до этого дня у нас нет вовсе, и ноль упреждения там означал бы "
        "«рынок молчал», тогда как молчим мы.\n"
    )
    known = set(market.issuers)
    base = len(set(inside) & known) / len(known) if known else 0
    print(
        f"Круг рыночных признаков — {len(known)} эмитентов с рядом, из них "
        f"с событием в окне {len(set(inside) & known)}: базовая доля "
        f"**{base:.1%}**. Прирост — точность признака к этой доле.\n"
    )
    rule = policy.confirmation.default
    for title, of, out_of in (
        ("### Без подтверждения: сработал хотя бы раз", 1, 1),
        (f"### С подтверждением {rule.of} из {rule.out_of}", rule.of, rule.out_of),
    ):
        print(f"\n{title}\n")
        print(
            "| Признак | Сработал | С событием | Ложных | Точность | "
            "Выявляемость | Прирост | Упреждение |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for name, holds in _signals(market, policy.floor):
            _row(
                name,
                lambda inn, until, h=holds, a=of, b=out_of: first_day_when(
                    points_of(market, inn), h, until, a, b
                ),
                known,
                inside,
                base,
            )
        # Слои отчётности и рейтингов мерятся тем же способом и на своём
        # окне: история корзин на год короче ряда торгов, и оставив её
        # события в знаменателе, мы записали бы свою короткую историю
        # в недостаток слоя. Подтверждение к ним не применяется: основание
        # маршрута — не дневная величина, и «7 из 10» у него не определено.
        if of == 1:
            share = (
                len(set(common) & set(history)) / len(history) if history else 0
            )
            for name, words in (
                (f"отчётность: любое основание (с {started:%d.%m.%Y})", _REPORTING),
                (f"рейтинги: любое основание (с {started:%d.%m.%Y})", _RATING),
            ):
                _row(
                    name,
                    lambda inn, until, w=words: _first_day(
                        history.get(inn, {}), sources, w, until
                    ),
                    set(history),
                    common,
                    share,
                )

    # **Цена подтверждения меряется на самой ступени, а не вообще.**
    # У Кириллицы кратность дошла до 115× за неделю до неисполненного
    # погашения, а «7 из 10» этого всплеска не застало: бумага перестала
    # торговаться раньше, чем набралось семь наблюдений. Подтверждение снимает
    # фон и снимает же короткий всплеск — а короткий всплеск и есть событие.
    extreme = next(
        step.multiple for step in policy.route_steps if step.basket == "review"
    )
    print(f"\n### Цена подтверждения на ступени p99 ({extreme:.2f}×)\n")
    print(
        "Подтверждение откладывает вывод ровно на то, чем он подтверждается, "
        "и здесь видно, сколько это стоит. Строка «1 из 1» — без "
        "подтверждения.\n"
    )
    print(
        "| Подтверждение | Сработал | С событием | Ложных | Точность "
        "| Выявляемость | Прирост | Упреждение |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for of, out_of in ((1, 1), (3, 5), (5, 10), (7, 10), (14, 20)):
        _row(
            f"{of} из {out_of}",
            lambda inn, until, a=of, b=out_of: first_day_when(
                points_of(market, inn), holds_level(market, extreme, policy.floor),
                until, a, b,
            ),
            known,
            inside,
            base,
        )

    _appearance(policy, market, history, sources, inside, common, started, base)
    _market_pointwise(policy, market, when)
    _halted(market, inside)

    print("\n## Пересечение слоёв на событиях в окне\n")
    print(
        "Вопрос матрицы слоёв: что она добавляет. Рынок берётся двумя "
        "составами порознь — одной ценой и всеми основаниями, с которыми он "
        "вошёл в маршрут: первое отвечает, что даёт признак, второе — что "
        "даёт слой. Расширение и своя норма в пересечение не идут вовсе: "
        "в маршруте их нет.\n"
    )
    _overlap(policy, market, history, sources, inside, common, started)
    _blind(
        policy, market, history, baskets, sources, common,
        "Кого не увидел никто в общем окне слоёв",
    )
    # **Событие вне общего окна тоже надо назвать, и назвать честно.** Там
    # высказаться мог один рынок, и вопрос к нему один: сказал ли. Считать
    # такого эмитента «пропущенным всеми» нельзя — двое из трёх слоёв
    # на ту дату не наблюдались вовсе.
    _blind(
        policy, market, history, baskets, sources,
        {inn: moment for inn, moment in inside.items() if inn not in common},
        "События раньше истории корзин: кого не увидел рынок",
        "**Отчётность и рейтинги здесь высказаться не могли**: истории корзин "
        "на эти даты нет вовсе. Спрос тут с одного рынка, и пропущенным "
        "эмитент назван только в этом смысле.",
    )

    print("\n## Построчно: кто что сказал и когда\n")
    print(
        "Пусто — слой не высказался до события вовсе; «нет истории» — слой "
        "на ту дату не наблюдался, и это про нас, а не про него. Даты слоёв "
        "отчётности и рейтингов — из записанной истории корзин, рыночные — "
        "из срезов. Знак «≥» означает, что слой высказался первым же днём "
        "своей истории: упреждение не меньше названного, а насколько — "
        "неизвестно.\n"
    )
    print(
        "| ИНН | Событие | Расширение | Своя норма | Цена < 60 % "
        "| Отчётность | Рейтинги |"
    )
    print("|---|---|---|---|---|---|---|")
    below = policy.distress_zone.price_below_percent
    for inn, moment in sorted(inside.items(), key=lambda item: item[1]):
        points = points_of(market, inn)
        if not points:
            continue
        widened = first_day_when(
            points, holds_widening(Decimal("0.6"), 4, False), moment
        )
        own_norm = first_day_when(points, holds_own_norm(Decimal(2), 90, 20), moment)
        print(
            f"| {inn} | {moment:%d.%m.%Y} "
            f"| {_lead(widened, moment)} "
            f"| {_lead(own_norm, moment)} "
            f"| {_lead(first_day_when(points, holds_price(below), moment), moment)} "
            f"| {_told(history.get(inn, {}), sources, _REPORTING, moment, started)} "
            f"| {_told(history.get(inn, {}), sources, _RATING, moment, started)} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
