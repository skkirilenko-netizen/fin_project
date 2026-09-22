"""Входы маршрутизации из базы: одно место на список и на замер.

**Прежде их было два.** Замер распределения и экран наблюдения собирали
величины сами, одними и теми же запросами, — и это тот самый второй путь
к одному ответу: расхождение между «в списке» и «в отчёте» увидеть было бы
нечем. Здесь собрано всё, что маршрут получает из базы, а решение принимает
`scoring.routing.route`.

**Ноль агрегатора величиной не считается и здесь.** Правило объявлено у вида
отчёта (`cbonds_mapping.yaml`, `zero_reading`) и применяется всюду, где ноль
участвует в суждении: у ЯКОВЛЕВА операционная прибыль пришла нулём, и маршрут
читал это как убыток — то есть как утверждение об эмитенте, сделанное
по величине, которой источник не раскрыл.

**Поглощённый эмитент в список не попадает** (`routing.universe`): его
отчётность — история, а поглощение объявлено карточкой источника.
"""

import json
import logging
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from finlib.db import PgConnection, fetch_all
from finlib.metrics.ifrs import MetricValue
from finlib.normalize.lines import load_lines
from finlib.scoring.routing import (
    ManualFloor,
    Refinance,
    RoutingPolicy,
    Verdict,
    led_by_guarantor,
    load_routing,
    route,
)
from finlib.sources.cbonds_events import (
    DEFAULT_STATUSES,
    Guarantee,
    Issue,
    IssuerEvents,
    credit_scales,
    default_records,
    events_of,
    guarantees_of,
    in_unit,
    issues_of,
    latest_snapshot,
    point_order,
)
from finlib.sources.cbonds_flows import refinancing
from finlib.sources.moex_risk import risk_sectors
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Карточки эмитентов источника: признак поглощения и отрасль. Файл собирает
# `eval/cbonds_emitents.py` и складывает на диск; в сеть отсюда не ходим.
CARDS = Path("data/raw/cbonds/emitents.json")

_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date, max(o.name) AS name
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
GROUP BY f.inn
"""

# **Выборка называет стандарт.** Те же коды проверок нуля пишет доставка РСБУ,
# и без стандарта запись о комплекте РСБУ отправляла бы эмитента в разбор
# по его комплекту МСФО.
_ZERO_FAILED = """
SELECT DISTINCT d.inn, s.report_year
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.status = 'fail' AND s.standard = 'ifrs' AND d.check_code IN (
    'cbonds_identity_mismatch', 'cbonds_sections_mismatch', 'cbonds_zero_total'
)
"""

# Денежные средства комплекта: знаменатель рефинансирования. Выборка называет
# стандарт и предпочтение источника, как всякая выборка по ИНН.
_CASH = """
SELECT f.value FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'ifrs' AND f.report_date = %(d)s
  AND f.line_code = 'ifrs.cash' AND s.is_actual AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""

# Величина вместе со способом получения: ноль от агрегатора означает
# и нераскрытие, и судить по нему нельзя.
_OPERATING_PROFIT = """
SELECT f.value, s.source FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'ifrs' AND f.report_date = %(d)s
  AND f.line_code = 'ifrs.operating_profit' AND s.is_actual
  AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""

_SOURCES = """
SELECT DISTINCT source, unit_code, reporting_type FROM src_file
WHERE inn = %(inn)s AND standard = 'ifrs' AND is_actual
  AND status <> 'quarantine' AND report_year = %(year)s
"""

_ASSESSED = """
SELECT a.inn, a.class_code, a.report_date
FROM assessment a
WHERE a.standard = 'ifrs' AND a.class_code IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM src_file s
      WHERE s.inn = a.inn AND s.standard = 'ifrs' AND s.source <> 'cbonds'
        AND s.report_year = EXTRACT(YEAR FROM a.report_date)::int
  )
ORDER BY a.inn, a.report_date DESC
"""

# **Журнал ручных решений: берётся последнее действующее на дату.** Записи
# не переписываются, поэтому выборка называет и дату решения, и срок; истёкшие
# отбираются отдельно и считаются — ноль сработавших решений при неизвестном
# числе истёкших не означает ничего.
_DECISIONS = """
SELECT DISTINCT ON (inn, standard)
       inn, standard, basket, author, reason, decided_on, valid_until
FROM routing_decision
WHERE standard = %(standard)s AND valid_until >= %(today)s
ORDER BY inn, standard, decided_on DESC, id DESC
"""

_DECISIONS_EXPIRED = """
SELECT count(DISTINCT inn) AS n FROM routing_decision
WHERE standard = %(standard)s AND valid_until < %(today)s
  AND inn NOT IN (
      SELECT inn FROM routing_decision
      WHERE standard = %(standard)s AND valid_until >= %(today)s
  )
"""

SOURCE_NAMES = {"file": "PDF", "gir_bo": "ГИР БО", "cbonds": "Cbonds"}


def decisions(conn: PgConnection, today: date) -> dict[str, ManualFloor]:
    """Действующие решения человека о корзине: ИНН → решение.

    Решение принимается командой с автором и уходит в журнал; маршрут берёт
    последнее действующее. Истёкшее решение не применяется, но из журнала
    не исчезает — журнал доказательная база.
    """
    found: dict[str, ManualFloor] = {}
    for row in fetch_all(
        _DECISIONS, {"standard": Standard.IFRS.value, "today": today}, conn=conn
    ):
        found[row["inn"]] = ManualFloor(
            basket=row["basket"],
            author=row["author"],
            reason=row["reason"],
            decided_on=row["decided_on"],
            valid_until=row["valid_until"],
        )
    return found


@dataclass(frozen=True, slots=True)
class RoutingRow:
    """Эмитент со своими входами маршрута и вердиктом."""

    inn: str
    name: str
    report_date: date
    verdict: Verdict
    computed: tuple[MetricValue, ...]
    stop_factors: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    unit_code: str | None = None
    # **Наименование денежной единицы комплекта — графа строки, а не дело
    # каждого выхода.** Прежде её набирали порознь список, выгрузка и маршрут,
    # и третий набор разошёлся с первыми двумя: графы печатали «млн руб.»,
    # а основания рядом с ними — «тыс. руб.». Величина одна, и набирается
    # она один раз.
    unit: str = ""
    assessed_class: str = ""
    branch: str = ""
    group: str = ""
    # События эмитента: выпуски и рейтинги. «Данных нет» и «событий нет» —
    # разные вещи, и признак их различает.
    events: IssuerEvents | None = None
    # Поручительства финансирующей структуры: у SPV корзина берётся
    # у того, кто отвечает по долгу, и перечень нужен второму проходу.
    guarantees: tuple[Guarantee, ...] = ()
    # Денежные средства комплекта: знаменатель рефинансирования. Лежат здесь,
    # а не в каждом замере своим запросом: один вопрос — один запрос.
    cash: Decimal | None = None
    # Платежи по облигациям ближайших месяцев против денежных средств —
    # в единице комплекта, готовыми: замер и список печатают одно и то же.
    refinance: Refinance | None = None
    # Величины, названные порознь: отрицательное отношение чистого долга
    # к EBITDA означает либо чистую денежную позицию, либо убыток, и путать
    # их нельзя.
    values: dict[str, Decimal] = field(default_factory=dict)


def cards() -> dict[str, dict]:
    """Карточки эмитентов с диска; пустой словарь — карточек нет."""
    if not CARDS.exists():
        logger.warning("карточек эмитентов на диске нет: %s", CARDS)
        return {}
    return json.loads(CARDS.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class Exclusion:
    """Запись журнала исключений: кто вышел из списка, почему и когда.

    **Список, уменьшившийся без записи, врёт о себе сам.** Поэтому выход
    называет причину, дату и преемника, а дата называется тем, чем является:
    у карточки есть только дата обновления, и днём поглощения она не является.
    """

    inn: str
    name: str
    reason: str
    updated: str
    successor: str


def exclusions(
    known: dict[str, dict], routing: RoutingPolicy
) -> tuple[dict[str, Exclusion], dict[str, str]]:
    """Кто выходит из списка и кто идёт в очередь статуса.

    **Основание выхода — статус, а не поле поглощения.** Поле названо
    у источника «Компания, оставшаяся после слияния/поглощения» и заполнено
    у живых тоже: прочитанное как «поглощён», оно вывело из списка 24 живых
    эмитента. Выходит ликвидированный эмитент, **преемник которого известен**;
    ликвидированный без известного преемника не выходит, а идёт в очередь
    статуса — о нём нечего сказать, и это не то же, что «его нет».
    """
    universe = routing.universe
    by_id = {str(card.get("id")): card for card in known.values()}
    out: dict[str, Exclusion] = {}
    unconfirmed: dict[str, str] = {}
    for inn, card in known.items():
        status = str(card.get("emitent_statuses_id") or "")
        if not universe.known_status(status):
            unconfirmed[inn] = universe.status_of(status)
            continue
        if status not in universe.exclude_statuses:
            continue
        # Ноль и пустота в поле преемника означают одно: преемник не назван.
        target = str(card.get("emitents_id_absorption") or "").strip()
        if target in ("", "0", "None"):
            target = ""
        successor = by_id.get(target)
        if universe.require_successor and successor is None:
            unconfirmed[inn] = (
                f"{universe.status_of(status)}, преемник не назван"
                if not target
                else f"{universe.status_of(status)}, преемника {target} нет в справочнике"
            )
            continue
        out[inn] = Exclusion(
            inn=inn,
            name=str(card.get("name_rus") or inn),
            reason=universe.status_of(status),
            updated=str(card.get("updating_date") or "")[:10],
            successor=(
                f"{successor.get('name_rus')} (ИНН {successor.get('emitent_inn')})"
                if successor is not None
                else "не назван"
            ),
        )
    return out, unconfirmed


def value_of(computed: tuple[MetricValue, ...], code: str) -> Decimal | None:
    """Величина показателя; None — не рассчитан."""
    item = next((entry for entry in computed if entry.code == code), None)
    return item.value if item is not None and item.calculable else None


def routing_rows(
    conn: PgConnection,
    today: date | None = None,
    blind: frozenset[str] = frozenset(),
) -> tuple[list[RoutingRow], dict[str, int]]:
    """Собирает входы и вердикты по всем эмитентам; рядом — счётчики отбора.

    Счётчики возвращаются вместе со строками: «в списке 353» без числа
    исключённых не говорит, полон ли список.

    **`blind` закрывает маршруту часть входов, и это нужно замеру качества.**
    Событийное правило проверяется на тех же событиях, на которых построено,
    и всегда выходит идеальным; чтобы спросить «видели ли эмитента отчётность,
    рейтинги, группы и поручители **до** события», события дефолта от маршрута
    прячутся: `blind={"defaults"}`. Рейтинги при этом остаются — их прячет
    `blind={"ratings"}`. Ключ — довод замера, а не режим работы: боевой вызов
    его не передаёт, и второго пути к вердикту не появляется.
    """
    from finlib.metrics.ifrs_store import compute_from_facts
    from finlib.normalize.ifrs_issuer_type import load_issuer_types
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.scoring.ifrs_store import stop_factors_of

    today = today or date.today()
    policy = load_ifrs_metrics()
    routing = load_routing()
    types = load_issuer_types()
    known = cards()
    skip, unconfirmed = exclusions(known, routing)
    spv = {
        inn for inn, card in known.items() if str(card.get("emitent_spv")) == "1"
    }
    on, snapshot = latest_snapshot()
    # Справочники шкал читаются один раз на прогон, а не на эмитента: файл
    # один и тот же, а эмитентов триста.
    credit = credit_scales()
    order = point_order()
    # Перечень дефолтов — один файл на всю страну, 3 529 событий: читается
    # один раз, а не по эмитенту.
    defaults = default_records()
    units = load_lines().units
    # Сектор повышенного риска биржи: один файл перечня и карточки выпусков.
    # Пустой словарь означает, что доставки не было, — и это не «переводов
    # нет»: `scripts/moex_fetch.py`.
    risky = risk_sectors()
    if not risky:
        logger.warning(
            "перечня сектора риска на диске нет: событие биржи в маршрут "
            "не попадёт, и это отсутствие данных, а не отсутствие переводов"
        )
    if on is None:
        logger.warning(
            "снимка рейтингов на диске нет: событийный слой будет пуст, "
            "и это не «событий нет», а отсутствие данных"
        )
    assessed: dict[str, str] = {}
    for row in fetch_all(_ASSESSED, {}, conn=conn):
        assessed.setdefault(row["inn"], row["class_code"])
    quarantined = {
        (row["inn"], row["report_year"])
        for row in fetch_all(_ZERO_FAILED, {}, conn=conn)
    }

    # **Верхний десяток по объёму долга в обращении.** Доля считается
    # от эмитентов, у которых объём известен и положителен: у эмитента
    # без облигаций величины нет вовсе, и в знаменателе он мерил бы состав
    # списка, а не долг.
    volumes = {
        inn: total
        for inn in known
        if (total := _outstanding(inn)) is not None and total > 0
    }
    systemic = _top_share(volumes, routing.systemic.top_share)
    counts = {
        "эмитентов": 0,
        # **Число вышедших печатается всегда.** Список, уменьшившийся без
        # записи, врёт о себе сам, а «ноль вышедших» — сведение, а не пустота.
        "вышло из списка": 0,
        "статус не подтверждён": 0,
        "карточек": len(known),
        "с раскрытым объёмом долга": len(volumes),
        "системно значимых": len(systemic),
    }
    # **Решения человека и их истёкшие записи считаются порознь.** Ноль
    # сработавших при неизвестном числе истёкших неотличим от журнала,
    # который никто не ведёт.
    decided = decisions(conn, today)
    counts["решений человека действует"] = len(decided)
    counts["решений человека истекло"] = int(
        (
            fetch_all(
                _DECISIONS_EXPIRED,
                {"standard": Standard.IFRS.value, "today": today},
                conn=conn,
            )
            or [{"n": 0}]
        )[0]["n"]
    )
    rows: list[RoutingRow] = []
    # Доводы маршрута по каждому эмитенту: второй проход добавляет к ним
    # обстоятельство другого эмитента, а не набирает перечень заново.
    given: dict[str, dict[str, object]] = {}
    for row in fetch_all(_LATEST, {}, conn=conn):
        inn, moment = row["inn"], row["report_date"]
        if inn in skip:
            counts["вышло из списка"] += 1
            logger.info(
                "%s вышел из списка: %s, преемник %s (карточка обновлена %s)",
                inn,
                skip[inn].reason,
                skip[inn].successor,
                skip[inn].updated,
            )
            continue
        counts["статус не подтверждён"] += int(inn in unconfirmed)
        computed = compute_from_facts(inn, moment, conn, policy)
        stops = stop_factors_of(inn, moment, computed, conn)
        events = events_of(inn, snapshot, credit, order, defaults)
        if blind:
            # **Прячутся признаки дефолта, а не выпуски.** Выпуск нужен
            # и рефинансированию, и объёму долга — это не события, а срочность
            # и размер; убрать их вместе с дефолтом значило бы ослепить
            # маршрут сильнее, чем спрошено.
            events = replace(
                events,
                issues=tuple(_without_default(item) for item in events.issues)
                if "defaults" in blind
                else events.issues,
                records=() if "defaults" in blind else events.records,
                ratings=() if "ratings" in blind else events.ratings,
            )
        # Поручительства читаются у всех, а не только у финансирующих
        # структур: у обычного эмитента поручитель в разборе — такое же
        # обстоятельство, как эмитент своей группы. Корзина берётся вторым
        # проходом, а имя нужно уже здесь: формулировка SPV без него говорила
        # бы о группе там, где речь о том, кто отвечает по долгу.
        secured = guarantees_of(
            inn, frozenset(routing.events.guarantee_statuses)
        )
        delivered = fetch_all(_SOURCES, {"inn": inn, "year": moment.year}, conn=conn)
        unit_code = next(
            (item["unit_code"] for item in delivered if item["unit_code"]), None
        )
        cash = _cash(inn, moment, conn)
        # **Единица комплекта набирается один раз и одна на всю строку.**
        # Основания маршрута, графы списка и графы выгрузки печатают одни
        # и те же величины, и вторая точка набора единицы разошлась бы
        # с первой — ровно это и случилось: графы печатали «млн руб.»,
        # а основания рядом с ними «тыс. руб.».
        unit = units.name_of(unit_code) if unit_code else ""
        # **Срочность долга собирается здесь, а не в маршруте**: маршрут
        # решает по величинам, а величины берутся из одного места. Обе
        # приведены к единице комплекта — иначе ошибка в тысячу раз.
        plan = refinancing(events.issues, routing.refinancing.months, today)
        refinance = Refinance(
            due=in_unit(plan.scheduled, unit_code) if plan.known else None,
            cash=cash,
            unit=unit,
            months=routing.refinancing.months,
        )
        # **Доводы маршрута набираются один раз и переиспользуются вторым
        # проходом.** Прежде второй проход собирал перечень доводов заново
        # руками, и в нём недоставало четырёх: у поднятого эмитента исчезали
        # основания рефинансирования, крупного долга, сектора повышенного
        # риска и неподтверждённого статуса. Это тот же «второй путь к одному
        # ответу», только незаметный — корзина при этом получалась правдоподобной.
        inputs: dict[str, object] = dict(
            unit=unit,
            quarantined=(inn, moment.year) in quarantined,
            stop_factors=stops.triggered,
            financing_structure=inn in spv,
            guarantor=", ".join(sorted({item.name for item in secured})),
            operating_profit=_operating_profit(inn, moment, conn),
            latest_annual=moment,
            assessed_class=assessed.get(inn),
            branch=str((known.get(inn) or {}).get("branch_name_rus") or ""),
            group=str((known.get(inn) or {}).get("group_name_rus") or ""),
            events=events,
            today=today,
            policy=policy,
            refinance=refinance,
            systemic_volume=systemic.get(inn),
            status_unconfirmed=unconfirmed.get(inn, ""),
            manual_floor=decided.get(inn),
            risk_sector=tuple(
                replace(risky[item.isin], name=item.name)
                for item in events.issues
                if item.isin and item.isin in risky
            ),
            routing=routing,
            types=types,
        )
        verdict = route(computed, **inputs)
        given[inn] = inputs
        card = known.get(inn, {})
        counts["эмитентов"] += 1
        rows.append(
            RoutingRow(
                inn=inn,
                name=(row["name"] or inn).strip(),
                report_date=moment,
                verdict=verdict,
                computed=computed,
                stop_factors=stops.triggered,
                sources=tuple(
                    sorted(
                        {
                            SOURCE_NAMES.get(item["source"], item["source"])
                            for item in delivered
                        }
                    )
                ),
                unit_code=unit_code,
                unit=unit,
                assessed_class=assessed.get(inn, ""),
                branch=str(card.get("branch_name_rus") or ""),
                group=str(card.get("group_name_rus") or ""),
                events=events,
                guarantees=secured,
                cash=cash,
                refinance=refinance,
                values={
                    code: value
                    for code in ("net_debt", "ebitda", "net_debt_ebitda",
                                 "net_debt_op_profit", "equity_ratio", "cur_liq")
                    if (value := value_of(computed, code)) is not None
                },
            )
        )

    # **Поручитель — второй проход по той же причине, что и группа.** Корзина
    # финансирующей структуры берётся у того, кто отвечает по её долгу,
    # а она известна лишь после того, как посчитаны все. Счётчик печатает
    # знаменатель: «ноль SPV с поручителем» без числа самих SPV неотличим
    # от невыполненного правила.
    by_inn = {item.inn: item for item in rows}
    counts["финансирующих структур"] = sum(1 for item in rows if item.inn in spv)
    counts["из них корзина взята у поручителя"] = 0
    # Знаменатель правила поручителя: «поднято ноль» без числа пар,
    # у которых поручитель вообще есть в списке, ничего не значит.
    counts["пар с поручителем в списке"] = 0
    counts["поднято по поручителю"] = 0
    secured_rows: list[RoutingRow] = []
    for item in rows:
        backing = [
            by_inn[entry.inn]
            for entry in item.guarantees
            if entry.inn in by_inn and entry.inn != item.inn
        ]
        if not backing:
            secured_rows.append(item)
            continue
        counts["пар с поручителем в списке"] += 1
        # Поручителей бывает несколько — берётся тяжелейший: обязательство
        # каждого действует само по себе, и слабейшее ничего не отменяет.
        heaviest = min(
            backing, key=lambda entry: routing.basket(entry.verdict.basket).order
        )
        if item.inn in spv:
            # Финансирующая структура собой не оценивается: её корзина —
            # корзина того, кто отвечает по её долгу.
            counts["из них корзина взята у поручителя"] += 1
            secured_rows.append(
                replace(
                    item,
                    verdict=led_by_guarantor(
                        item.verdict,
                        heaviest.name,
                        heaviest.verdict,
                        item.group,
                        routing,
                    ),
                )
            )
            continue
        # **У обычного эмитента корзина поручителя не переносится.** Разбор
        # сказан о поручителе, а не о заёмщике; обстоятельство же говорит
        # и о заёмщике, и он не мягче внимания — то же соразмерно, что
        # у группового контура.
        if heaviest.verdict.basket != "review" or item.verdict.basket in (
            "review",
            "status_unknown",
        ):
            secured_rows.append(item)
            continue
        counts["поднято по поручителю"] += 1
        secured_rows.append(
            replace(
                item,
                verdict=route(
                    item.computed,
                    **{
                        **given[item.inn],
                        "guarantor_under_review": heaviest.name,
                    },
                ),
            )
        )
    rows = secured_rows

    # **Групповой контур — второй проход, и иначе он невозможен.** Корзину
    # члена группы решает обстоятельство другого эмитента, а оно известно
    # только после того, как посчитаны все.
    #
    # **Корзину группа больше не называет** (решение 22.09.2026): поле
    # источника отражает бенефициара, а не финансовую связь, и ни одна
    # из семи проверенных пар финансовой связью не оказалась. Обстоятельство
    # при этом остаётся справочным, и второй проход по-прежнему нужен: имя
    # эмитента в разборе известно только после того, как посчитаны все.
    in_review = {
        item.group: item
        for item in rows
        if item.group and item.verdict.basket == "review"
    }
    counts["названо справочно по группе"] = 0
    if in_review:
        lifted: list[RoutingRow] = []
        for item in rows:
            leader = in_review.get(item.group)
            if leader is None or item.inn == leader.inn:
                lifted.append(item)
                continue
            if item.verdict.basket == "status_unknown":
                lifted.append(item)
                continue
            counts["названо справочно по группе"] += 1
            lifted.append(
                replace(
                    item,
                    verdict=route(
                        item.computed,
                        **{
                            **given[item.inn],
                            "group_under_review": (item.group, leader.name),
                        },
                    ),
                )
            )
        rows = lifted
    return rows, counts


def _without_default(issue: Issue) -> Issue:
    """Выпуск без признаков дефолта: для замера маршрута без событий.

    **Дефолт объявлен двумя полями и статусом, и прячутся все три.** Признак
    неурегулированности гасится, а статус «дефолт по погашению» заменяется
    пустым: в нём дефолт назван словом, и оставить его значило бы прятать
    признак, оставив признание. Статус при этом не участвует ни в
    рефинансировании, ни в объёме долга — там берутся только «в обращении»
    и «размещается», а дефолтный выпуск в них не входит.
    """
    return replace(
        issue,
        default=False,
        unsettled=False,
        status="" if issue.status in DEFAULT_STATUSES else issue.status,
    )


def _outstanding(inn: str) -> Decimal | None:
    """Объём долга в обращении по выпускам эмитента; None — объём не раскрыт.

    Складываются выпуски в обращении и размещаемые: у погашенного долга нет.
    `None` означает, что ни у одного выпуска объёма не раскрыто, — и это
    не ноль: эмитент без облигаций и эмитент с нераскрытым объёмом
    в верхнем десятке различаются.
    """
    issues, known = issues_of(inn)
    if not known:
        return None
    parts = [
        item.outstanding
        for item in issues
        if item.status in ("в обращении", "размещается") and item.outstanding is not None
    ]
    return sum(parts, start=Decimal(0)) if parts else None


def _top_share(volumes: dict[str, Decimal], share: Decimal) -> dict[str, Decimal]:
    """Верхняя доля перечня по величине; при пустом перечне — пусто.

    Округление вверх намеренно: у 33 эмитентов десятая часть — четыре,
    а не три, и потерять четвёртого значило бы сдвинуть отсечку тише,
    чем объявлено.
    """
    if not volumes:
        return {}
    ordered = sorted(volumes.items(), key=lambda item: item[1], reverse=True)
    size = -(-int(len(ordered) * share * 1000) // 1000) or 1
    return dict(ordered[:size])


def _cash(inn: str, moment: date, conn: PgConnection) -> Decimal | None:
    """Денежные средства комплекта; None — величина не раскрыта.

    Ноль здесь остаётся нулём: денежные средства бывают нулевыми, а правило
    нераскрытия относится к величинам, которые ломают тождество отчётности
    либо равны нулю у итога при ненулевом составе. Знаменатель из нуля
    отношения не даёт, и решает это тот, кто делит.
    """
    found = fetch_all(_CASH, {"inn": inn, "d": moment}, conn=conn)
    return found[0]["value"] if found else None


def _operating_profit(inn: str, moment: date, conn: PgConnection) -> Decimal | None:
    """Операционная прибыль периода; ноль от агрегатора величиной не считается.

    **Ноль у агрегатора означает и нераскрытие**, и судить по нему нельзя:
    у ЯКОВЛЕВА ноль читался как операционный убыток, то есть как утверждение
    об эмитенте, сделанное по величине, которой источник не раскрыл. Правило
    то же, что у контролей сходимости, и объявлено там же.
    """
    found = fetch_all(_OPERATING_PROFIT, {"inn": inn, "d": moment}, conn=conn)
    if not found:
        return None
    value, source = Decimal(found[0]["value"]), found[0]["source"]
    if value == 0 and _zero_is_unknown(source):
        logger.info(
            "%s за %s: операционная прибыль доставлена нулём (%s) — "
            "величиной не считается",
            inn,
            moment,
            source,
        )
        return None
    return value


def _zero_is_unknown(source: str) -> bool:
    """Означает ли ноль этого способа получения «неизвестно»."""
    if source != "cbonds":
        return False
    from finlib.normalize.cbonds_mapping import load_cbonds_mapping

    mapping = load_cbonds_mapping()
    return any(
        report.zero_reading is not None and report.zero_reading.as_not_disclosed
        for report in mapping.reports.values()
    )
