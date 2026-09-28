"""Промежуточная отчётность: видит ли она раньше годовой. **Только замер.**

    uv run python eval/interim_lead_run.py > data/output/interim_lead.md

**Вопрос фазы 5.** Годовая отчётность о том, что случилось между отчётными
датами, не говорит ничего: у Кириллицы величины за 2025 год здоровые,
а 07.09.2026 не исполнено погашение. Промежуточный комплект стоит между
двумя годовыми — и вопрос ровно в том, сказал бы он раньше и на сколько.

**Три вопроса, и они порознь**: свежесть самого комплекта; рефинансирование
на свежих денежных средствах; признаки изменения — падение денежных средств,
рост краткосрочного долга, снижение скользящего операционного результата.
Уровни сюда не смешиваются: шкалы откалиброваны на годовых величинах,
и промежуточный комплект, поданный в них как есть, мерил бы не то.

**Меры те же, что у рыночного слоя** (решения владельца 24.09.2026): общее
окно наблюдения объявляется числом; «поймал» означает «сработал **до**
события»; появление основания отделяется от его стояния.

**Считает не он.** Ряд комплектов, доли изменения, распределение и отсечки
живут в боевом пути (`finlib.scoring.interim`, `finlib.metrics.interim`),
сравнение рефинансирования — в маршруте (`scoring.routing.short_of_cash`).
Здесь только наблюдение за ними.
"""

import logging
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_lead_run import events  # noqa: E402

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.interim import (  # noqa: E402
    Observation,
    change,
    cutoffs,
    distribution,
    findings,
    issuer_series,
    load_interim,
    pairs,
)
from finlib.scoring.routing import load_routing, short_of_cash  # noqa: E402
from finlib.sources.cbonds_events import in_unit, issues_of  # noqa: E402
from finlib.sources.cbonds_flows import refinancing  # noqa: E402
from finlib.sources.market import load_market, universe  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

# Сколько комплектов какого вида у нас есть: знаменатель всего замера.
_SETS = """
SELECT COALESCE(reporting_kind, 'full') AS kind, standard, count(*) AS sets,
       count(DISTINCT inn) AS issuers,
       min(period_end) AS first_day, max(period_end) AS last_day
FROM src_file WHERE is_actual AND status <> 'quarantine'
GROUP BY 1, 2 ORDER BY 2, 1
"""

# Эмитенты, у которых промежуточные комплекты есть вовсе.
_WITH_INTERIM = """
SELECT DISTINCT inn FROM src_file
WHERE reporting_kind = 'interim' AND is_actual AND status <> 'quarantine'
"""


def _money(value: Decimal | None) -> str:
    """Величина с разделителем разрядов; пусто — величины нет."""
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def _share(value: Decimal) -> str:
    """Доля процентами."""
    return f"{value * 100:.1f} %"


def _visible(moment: date, kind: str, known, standard: Standard) -> date:  # noqa: ANN001
    """День, с которого комплект виден: отчётная дата плюс срок закона.

    Настоящей даты раскрытия у данных агрегатора нет вовсе — он её не
    сообщает ничем, — и берётся срок закона тем же правилом, каким его берёт
    маршрут (`routing.history.known_from`). Правило объявлено предварительным
    там же, и второго его экземпляра здесь не заводится. Срок свой у каждого
    стандарта: 402-ФЗ у РСБУ, 208-ФЗ у МСФО.
    """
    return moment + timedelta(
        days=known.days(standard, interim=kind == "interim")
    )


def _freshest(
    observations: tuple[Observation, ...], edge: date, known, interim: bool  # noqa: ANN001
) -> Observation | None:
    """Свежий комплект, видный к названному дню; `interim` — считать ли промежуточные."""
    seen = [
        item
        for item in observations
        if (interim or not item.interim)
        and _visible(item.moment, item.kind, known, item.standard) <= edge
    ]
    return seen[-1] if seen else None


def _said(days: list[int]) -> str:
    """Медиана и размах ряда дней; пусто — наблюдений нет."""
    if not days:
        return "—"
    return f"{int(statistics.median(days))} (от {min(days)} до {max(days)})"


def _load() -> tuple[dict[str, tuple[Observation, ...]], dict[str, date], set[str]]:
    """Ряды комплектов всех эмитентов универсума, события и кто имеет промежуточные."""
    # **Ряд эмитента — одного стандарта, и выбирается он общим правилом
    # предпочтения** (`standards.yaml`, `base_standard`): у группы
    # с консолидированной отчётностью — МСФО, у прочих — РСБУ. Пары
    # комплектов разных стандартов не сравниваются: ряды несопоставимы.
    # До 25.09.2026 ряд брался только по РСБУ — промежуточных МСФО в базе
    # не было, потому что их отбраковала загрузка с ключом по году.
    # Выбор ряда — боевой (`scoring.interim.issuer_series`): его же зовёт
    # карточка, и второй способ выбрать ряд разошёлся бы с первым.
    with connection() as conn:
        rows = fetch_all(_WITH_INTERIM, {}, conn=conn)
        have = {row["inn"] for row in rows}
        known = sorted(set(universe()) | have)
        found: dict[str, tuple[Observation, ...]] = {}
        for inn in known:
            chosen = issuer_series(conn, inn)
            if chosen:
                found[inn] = chosen
                _CHOSEN[inn] = chosen[0].standard
    return found, events(), have


# Стандарт ряда каждого эмитента: печатается счётом, чтобы было видно,
# на чём замер стоит.
_CHOSEN: dict[str, Standard] = {}


def _sets_table() -> None:
    """Что загружено: комплекты по видам и стандартам вместе с глубиной."""
    print("\n## Что загружено\n")
    print(
        "**Цена загрузки была названа до начала и оказалась верной**: все "
        "ответы агрегатора уже лежали на диске, и промежуточные комплекты "
        "получены **нулём сетевых запросов** — из тех же ответов, из которых "
        "прежде брались только годовые. Прогон занял 4 минуты 16 секунд.\n"
    )
    print("| Стандарт | Вид | Комплектов | Эмитентов | С | По |")
    print("|---|---|---|---|---|---|")
    with connection() as conn:
        for row in fetch_all(_SETS, {}, conn=conn):
            print(
                f"| {row['standard']} | {row['kind']} | {row['sets']} | "
                f"{row['issuers']} | {row['first_day']} | {row['last_day']} |"
            )
    # **Прежде здесь стояло «промежуточной консолидированной отчётности нет
    # ни одной — агрегатор её не отдаёт вовсе».** Это было нашей ошибкой:
    # их отбраковала загрузка с ключом по году, и сказанное строкой пережило
    # перегрузку. Теперь о составе говорит только таблица выше.
    print(
        "\nПромежуточная отчётность МСФО агрегатора перегружена 25.09.2026: "
        "прежде её отбраковывала загрузка с ключом комплекта по году."
    )


def _window(
    by_issuer: dict[str, tuple[Observation, ...]], moments: dict[str, date]
) -> tuple[date, date, dict[str, date]]:
    """Общее окно наблюдения и события, попавшие в него.

    **Слои с разными окнами несравнимы**, и окно здесь задаёт не история
    торгов, а глубина промежуточных комплектов: событие, случившееся прежде
    первого промежуточного комплекта, промежуточная отчётность упредить
    не могла вовсе, и держать его в знаменателе значило бы мерить доставку.
    """
    days = [item.moment for obs in by_issuer.values() for item in obs if item.interim]
    first, last = min(days), max(days)
    inside = {
        inn: moment for inn, moment in moments.items() if first <= moment <= last
    }
    print("\n## Общее окно наблюдения\n")
    print(
        f"Промежуточные комплекты идут с **{first:%d.%m.%Y}** по "
        f"**{last:%d.%m.%Y}**. Событий с датой всего **{len(moments)}**, "
        f"в окно попадают **{len(inside)}**; остальные "
        f"**{len(moments) - len(inside)}** случились прежде первого "
        "промежуточного комплекта либо позже последнего, и в знаменателе они "
        "мерили бы глубину доставки, а не признак."
    )
    return first, last, inside


def _freshness(
    by_issuer: dict[str, tuple[Observation, ...]], inside: dict[str, date]
) -> int | None:
    """Вопрос 1: на сколько раньше виден промежуточный комплект; медиана выигрыша."""
    known = load_routing().history.known_from
    print("\n## Вопрос 1. Свежесть: насколько ближе к событию стоит комплект\n")
    print(
        "У каждого эмитента с событием берутся два свежих комплекта, видных "
        "**до** события: свежий годовой и свежий любой. Разница между их "
        "отчётными датами и есть то, на сколько ближе к событию стоит "
        "промежуточная отчётность. День видимости считается сроком закона — "
        "настоящей даты раскрытия агрегатор не сообщает ничем.\n"
    )
    gained: list[int] = []
    stale_annual: list[int] = []
    stale_any: list[int] = []
    none_at_all = 0
    rows: list[tuple[str, str, str, str, int]] = []
    for inn, moment in sorted(inside.items(), key=lambda item: item[1]):
        obs = by_issuer.get(inn)
        if not obs:
            continue
        annual = _freshest(obs, moment, known, interim=False)
        anyone = _freshest(obs, moment, known, interim=True)
        if anyone is None:
            none_at_all += 1
            continue
        stale_any.append((moment - anyone.moment).days)
        if annual is None:
            rows.append((inn, "—", f"{anyone.moment:%d.%m.%Y}", anyone.kind, -1))
            continue
        stale_annual.append((moment - annual.moment).days)
        gain = (anyone.moment - annual.moment).days
        gained.append(gain)
        rows.append(
            (
                inn,
                f"{annual.moment:%d.%m.%Y}",
                f"{anyone.moment:%d.%m.%Y}",
                anyone.kind,
                gain,
            )
        )
    print(
        f"Эмитентов с событием в окне **{len(inside)}**, комплекты есть "
        f"у **{len(rows)}**, комплекта до события нет вовсе у **{none_at_all}**.\n"
    )
    print("| Мера | Дней |")
    print("|---|---|")
    print(f"| давность свежего годового комплекта к событию | {_said(stale_annual)} |")
    print(f"| давность свежего любого комплекта к событию | {_said(stale_any)} |")
    print(f"| выигрыш промежуточного комплекта | {_said(gained)} |")
    moved = [item for item in gained if item > 0]
    print(
        f"\nВыигрыш есть у **{len(moved)}** эмитентов из {len(gained)}; "
        f"у остальных свежий комплект и так годовой — промежуточного за это "
        "время либо нет, либо он ещё не был бы виден.\n"
    )
    print("| ИНН | Свежий годовой | Свежий любой | Вид | Выигрыш, дней |")
    print("|---|---|---|---|---|")
    for inn, annual, anyone, kind, gain in rows[:25]:
        said = "—" if gain < 0 else str(gain)
        print(f"| {inn} | {annual} | {anyone} | {kind} | {said} |")
    if len(rows) > 25:
        print(f"\nПоказаны 25 строк из {len(rows)}.")
    return int(statistics.median(gained)) if gained else None


def _refinancing(
    by_issuer: dict[str, tuple[Observation, ...]], inside: dict[str, date]
) -> int:
    """Вопрос 2: рефинансирование на промежуточных денежных средствах; скольким открыл."""
    routing = load_routing()
    known = routing.history.known_from
    policy = load_interim()
    cash_line = "cash"
    print("\n## Вопрос 2. Рефинансирование по промежуточным денежным средствам\n")
    print(
        "Мера та же, что в маршруте: платежи ближайших двенадцати месяцев "
        "против денежных средств, сравнение — та же функция "
        "(`scoring.routing.short_of_cash`), отсечка — та же. Меняется один "
        "довод: денежные средства берутся с балансовой даты свежего комплекта "
        "любого вида, а не только годового. Баланс промежуточного комплекта "
        "берётся на дату как есть — приводить его к году не к чему.\n"
    )
    counts = {
        "эмитентов с событием в окне": len(inside),
        "график платежей есть": 0,
        "денежные средства раскрыты в обоих комплектах": 0,
        "не хватает по годовому": 0,
        "не хватает по промежуточному": 0,
        "промежуточный открыл нехватку": 0,
        "промежуточный нехватку снял": 0,
    }
    opened: list[tuple[str, str, str, str]] = []
    for inn, moment in sorted(inside.items()):
        obs = by_issuer.get(inn)
        if not obs:
            continue
        issues, has = issues_of(inn)
        if not has:
            continue
        plan = refinancing(tuple(issues), routing.refinancing.days, moment)
        if not plan.known or not plan.scheduled:
            continue
        counts["график платежей есть"] += 1
        annual = _freshest(obs, moment, known, interim=False)
        anyone = _freshest(obs, moment, known, interim=True)
        if annual is None or anyone is None:
            continue
        was, now = annual.values[cash_line], anyone.values[cash_line]
        if was is None or now is None:
            continue
        counts["денежные средства раскрыты в обоих комплектах"] += 1
        # **Величины приводятся к одной единице.** Объём выпуска источник
        # отдаёт в рублях, отчётность бывает в тысячах и в миллионах, и
        # приведение делает та же функция, что в маршруте: ошибка здесь —
        # в тысячу раз, и ни один контроль сходимости её не ловит.
        due = in_unit(plan.scheduled, anyone.unit_code)
        if due is None:
            continue
        before = short_of_cash(routing, was, due)
        after = short_of_cash(routing, now, due)
        counts["не хватает по годовому"] += int(before)
        counts["не хватает по промежуточному"] += int(after)
        if after and not before:
            counts["промежуточный открыл нехватку"] += 1
            opened.append(
                (
                    inn,
                    f"{annual.moment:%d.%m.%Y}",
                    f"{anyone.moment:%d.%m.%Y}",
                    f"{_money(was)} → {_money(now)} при платежах {_money(due)}",
                )
            )
        if before and not after:
            counts["промежуточный нехватку снял"] += 1
    print("| Мера | Эмитентов |")
    print("|---|---|")
    for name, value in counts.items():
        print(f"| {name} | {value} |")
    if opened:
        print("\n**Кого открыла свежая величина:**\n")
        print("| ИНН | Годовой | Промежуточный | Денежные средства |")
        print("|---|---|---|---|")
        for row in opened[:15]:
            print("| " + " | ".join(row) + " |")
    else:
        print(
            "\n**Ни у одного эмитента с событием свежая величина нехватки "
            "не открыла.** Ноль здесь считается вместе со знаменателем: "
            f"проверено {counts['денежные средства раскрыты в обоих комплектах']} "
            "эмитентов, у которых денежные средства раскрыты в обоих "
            "комплектах и есть график платежей."
        )
    print(f"\nПризнаков изменения в справочнике: {len(policy.features)}.")
    return counts["промежуточный открыл нехватку"]


@dataclass(frozen=True, slots=True)
class Measured:
    """Признак изменения, как его померил прогон: держался, поймал, прирост."""

    name: str
    standing: int
    caught: int
    lift: float
    lead: int | None


def _fired_on(
    policy, feature, obs: tuple[Observation, ...], edge: Decimal  # noqa: ANN001
) -> list[Observation]:
    """Комплекты, на которых признак сработал (пара «прежний — этот»)."""
    found: list[Observation] = []
    for was, now in pairs(obs):
        moved = change(policy, feature, was, now)
        if moved is not None and moved >= edge:
            found.append(now)
    return found


def _features(
    by_issuer: dict[str, tuple[Observation, ...]],
    inside: dict[str, date],
    have: set[str],
) -> tuple["Measured", ...]:
    """Вопрос 3: признаки изменения порознь от уровней.

    Возвращает измеренное по каждому признаку — вывод берёт числа отсюда,
    а не вписывает строкой.
    """
    policy = load_interim()
    spread, denominators = distribution(policy, by_issuer)
    edges = cutoffs(policy, spread)
    print("\n## Вопрос 3. Признаки изменения\n")
    print(
        "**Уровни сюда не смешиваются.** Признак говорит о перемене между "
        "двумя комплектами, а не о том, каково положение: отсечка берётся "
        "перцентилем распределения самих долей. Знаменатель у каждого "
        "признака свой — пробелы у денежных средств и у операционного "
        "результата разные, и одно общее число делило бы величины разного "
        "рода.\n"
    )
    print(
        "| Признак | Перцентиль | Отсечка | Пар | Измерено | Мерить нечем | Медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    for feature in policy.features:
        values = spread.get(feature.code, [])
        seen = denominators[feature.code]
        edge = edges.get(feature.code)
        median = f"{statistics.median(values) * 100:.1f} %" if values else "—"
        said = _share(edge) if edge is not None else "—"
        print(
            f"| {feature.name} | p{feature.percentile} | {said} | {seen['пар']} | "
            f"{seen['измерено']} | {seen['мерить нечем']} | {median} |"
        )

    known = load_routing().history.known_from
    base = len(inside) / max(len(by_issuer), 1)
    print("\n### Две меры, и вторая мерит длину ряда\n")
    print(
        "**«Сработал хотя бы раз» здесь не мера признака.** У эмитента "
        "за пять лет два десятка комплектов, и признак, отсекающий двадцатую "
        "часть наблюдений, срабатывает почти у каждого: упреждение выходит "
        "в тысячу с лишним дней — то есть равно длине ряда, а не сроку "
        "предупреждения. Правило это уже было сказано о рыночном слое, "
        "и здесь оно повторяется тем же числом.\n"
    )
    print(
        "**Мера признака — «держался на свежем комплекте к событию»**: берётся "
        "последний комплект, видный до события, и спрашивается, сработал ли "
        "признак на нём. Упреждение тогда есть срок от видимости этого "
        "комплекта до события, и оно по устройству не больше цикла "
        "раскрытия.\n"
    )
    print(
        "| Признак | Сработал (хотя бы раз) | Держался на свежем | "
        "С событием, свежий | Прирост | Упреждение, дней | «Хотя бы раз», дней |"
    )
    print("|---|---|---|---|---|---|---|")
    measured: list[Measured] = []
    for feature in policy.features:
        edge = edges.get(feature.code)
        if edge is None:
            continue
        ever: set[str] = set()
        standing: set[str] = set()
        caught: dict[str, int] = {}
        ever_lead: list[int] = []
        for inn, obs in by_issuer.items():
            fired = _fired_on(policy, feature, obs, edge)
            if not fired:
                continue
            ever.add(inn)
            moment = inside.get(inn)
            edge_day = moment or date.max
            visible = [
                item
                for item in obs
                if _visible(item.moment, item.kind, known, item.standard) <= edge_day
            ]
            last = visible[-1] if visible else None
            if last is not None and last in fired:
                standing.add(inn)
            if moment is None:
                continue
            early = [
                item
                for item in fired
                if _visible(item.moment, item.kind, known, item.standard) <= moment
            ]
            if early:
                ever_lead.append(
                    (
                        moment
                        - _visible(
                            early[0].moment, early[0].kind, known, early[0].standard
                        )
                    ).days
                )
            if last is not None and last in fired:
                caught[inn] = (
                    moment - _visible(last.moment, last.kind, known, last.standard)
                ).days
        share = len(caught) / len(standing) if standing else 0.0
        lift = share / base if base else 0.0
        measured.append(
            Measured(
                name=feature.name,
                standing=len(standing),
                caught=len(caught),
                lift=lift,
                lead=int(statistics.median(caught.values())) if caught else None,
            )
        )
        print(
            f"| {feature.name} | {len(ever)} | {len(standing)} | {len(caught)} | "
            f"{lift:.2f}× | {_said(sorted(caught.values()))} | {_said(sorted(ever_lead))} |"
        )
    print(
        f"\nЗнаменатель прироста: событий в окне **{len(inside)}** на "
        f"**{len(by_issuer)}** эмитентов с комплектами, то есть базовая доля "
        f"**{base * 100:.1f} %**. Эмитентов, у которых промежуточные "
        f"комплекты есть вовсе, **{len(have)}**."
    )
    return tuple(measured)


def _together(
    by_issuer: dict[str, tuple[Observation, ...]], inside: dict[str, date]
) -> tuple[int, int]:
    """Все признаки разом: сколько эмитентов с событием увидены хоть одним и из скольких."""
    policy = load_interim()
    spread, _ = distribution(policy, by_issuer)
    edges = cutoffs(policy, spread)
    known = load_routing().history.known_from
    seen: dict[str, tuple[int, str, str]] = {}
    silent: list[str] = []
    for inn, moment in inside.items():
        obs = by_issuer.get(inn)
        if not obs:
            continue
        visible = [
            item for item in obs if _visible(item.moment, item.kind, known, item.standard) <= moment
        ]
        if not visible:
            continue
        last = visible[-1]
        # Признаки спрашиваются на **свежем** комплекте: вопрос замера —
        # что стояло к моменту события, а не что когда-либо срабатывало.
        said = findings(policy, obs, edges, last.moment)
        active = [item for item in said if item.since == last.moment]
        if not active:
            silent.append(inn)
            continue
        lead = (moment - _visible(last.moment, last.kind, known, last.standard)).days
        seen[inn] = (
            lead,
            f"{last.moment:%d.%m.%Y}",
            ", ".join(item.name for item in active),
        )
    print("\n### Все три признака вместе\n")
    print(
        f"Хотя бы один признак держался на свежем комплекте к событию "
        f"у **{len(seen)}** эмитентов из **{len(inside)}** с событием в окне; "
        f"молчали все три у **{len(silent)}**. Упреждение — "
        f"**{_said(sorted(item[0] for item in seen.values()))}** дней, и оно "
        "по устройству не больше цикла раскрытия: признак говорит о комплекте, "
        "а комплект сдаётся раз в квартал.\n"
    )
    if seen:
        print("| ИНН | Комплект | Упреждение, дней | Признаки |")
        print("|---|---|---|---|")
        for inn, (lead, day, names) in sorted(
            seen.items(), key=lambda item: -item[1][0]
        )[:20]:
            print(f"| {inn} | {day} | {lead} | {names} |")
    return len(seen), len(inside)


def _market_said() -> str:
    """Числа рыночного слоя для сравнения — из методики, а не из текста замера.

    **Число, вписанное в строку, устаревает молча.** Прежде здесь стояло
    «4,9× при упреждении 79 дней» — ступень p99 с подтверждением «7 из 10»;
    24.09.2026 подтверждение смягчено до «5 из 10», и сравнение печатало
    величины правила, которого в маршруте больше нет.
    """
    policy = load_market()
    step = next(
        item for item in policy.route_steps if item.basket == "review"
    )
    rule = step.confirmation or policy.confirmation.default
    at = policy.ladder.confirmation_measured[f"at_{rule.of}_of_{rule.out_of}"]
    zone = policy.distress_zone
    price = zone.measured[f"at_{zone.price_below_percent:.0f}"]

    def times(value: Decimal) -> str:
        return f"{value:.1f}".replace(".", ",")

    return (
        f"у ступени p{step.percentile} с подтверждением «{rule.of} из "
        f"{rule.out_of}» прирост {times(at['lift'])}× при упреждении "
        f"{at['lead_days']:.0f} дней, у цены ниже "
        f"{zone.price_below_percent:.0f} % номинала {times(price['lift'])}× "
        f"при {price['lead_days']:.0f} днях"
    )


def _verdict(
    measured: tuple[Measured, ...],
    gain: int | None,
    opened: int,
    held: tuple[int, int],
) -> None:
    """Чем измеренное кончается: что признавать, чего не признавать.

    Все числа приходят из того же прогона: вписанные строкой, они пережили бы
    правку данных неизменными — так «у семи из тридцати пяти» осталось бы
    в выводе, когда после перегрузки промежуточных МСФО их стало шесть.
    """
    working = [item for item in measured if item.caught]
    strongest = max(working, key=lambda item: item.lift) if working else None
    strongest_said = (
        f"у самого сильного признака изменения прирост "
        f"{strongest.lift:.2f}× при упреждении {strongest.lead} дней".replace(".", ",")
        if strongest is not None
        else "ни один признак изменения события на свежем комплекте не поймал"
    )
    # Признак, державшийся у кого-то и не поймавший ни одного события, —
    # тот, что не различает вовсе; называется числом тех, у кого держался.
    silent = [item for item in measured if item.standing and not item.caught]
    silent_said = "".join(
        f", а «{item.name.lower()}» — ни у одного из {item.standing}, "
        "у кого он держался вообще"
        for item in silent
    )
    print("\n## Что из этого следует\n")
    print(
        "**Свежесть промежуточная отчётность даёт, признаки изменения — почти "
        "нет, и это два разных ответа.** Комплект к моменту события "
        f"оказывается ближе на {gain if gain is not None else '—'} дней "
        f"по медиане, и у {opened} эмитентов свежая "
        "величина денежных средств открывает нехватку, которой годовая "
        "не показывала. Признаки же изменения на свежем комплекте держались "
        f"у {held[0]} эмитентов с событием из {held[1]}{silent_said}.\n"
    )
    print(
        "**Сравнивать это следует с рыночным слоем, и сравнение не в пользу "
        f"отчётности**: {_market_said()}; {strongest_said}. Вывод тот "
        "же, что и прежде: **упреждение даёт рынок**, а отчётность отвечает "
        "на вопрос «каково положение» — только теперь отвечает свежее.\n"
    )
    print(
        "**Упреждение признака изменения ограничено устройством, а не "
        "калибровкой.** Признак говорит о комплекте, комплект сдаётся раз "
        "в квартал и виден со срока закона — значит, упреждение не может "
        "превысить цикла раскрытия, и порог тут ничего не изменит. Отсюда "
        "и сравнение с рынком честно только при этой оговорке: рынок говорит "
        "ежедневно.\n"
    )
    print(
        "**Чего замер не говорит.** EBITDA признаком не мерится: признак "
        "операционного результата смотрит операционную прибыль, и у МСФО "
        "тоже. Дата раскрытия "
        "у агрегатора отсутствует вовсе, поэтому видимость комплекта "
        "смоделирована сроком закона — а срок этот в хвосте распределения "
        "ошибается на сотни дней в одну сторону: раскрывают позже, а не "
        "раньше. Значит, измеренное упреждение — верхняя оценка."
    )


def main() -> int:
    """Собирает отчёт замера промежуточной отчётности."""
    logging.basicConfig(level=logging.WARNING)
    print("# Промежуточная отчётность: видит ли она раньше годовой")
    print(
        "\nЗамер фазы 5, 24.09.2026. Ни один признак в маршрут пока не идёт "
        "(`interim.yaml`, `status.in_route: false`): действующими они "
        "объявляются решением человека и по этим числам."
    )
    _sets_table()
    by_issuer, moments, have = _load()
    counted = Counter(standard.value for standard in _CHOSEN.values())
    print(
        f"\nРяд эмитента берётся одного стандарта, правилом предпочтения: "
        f"по МСФО — {counted.get('ifrs', 0)}, по РСБУ — {counted.get('rsbu', 0)}. "
        "Отсечки признаков считаются по долям изменения обоих стандартов вместе: "
        "доля безразмерна, но состав величин у стандартов свой.\n"
    )
    _first, _last, inside = _window(by_issuer, moments)
    gain = _freshness(by_issuer, inside)
    opened = _refinancing(by_issuer, inside)
    measured = _features(by_issuer, inside, have)
    held = _together(by_issuer, inside)
    _verdict(measured, gain, opened, held)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
