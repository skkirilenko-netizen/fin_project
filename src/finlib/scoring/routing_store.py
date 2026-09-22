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
from finlib.scoring.routing import (
    RoutingPolicy,
    Verdict,
    led_by_guarantor,
    load_routing,
    route,
)
from finlib.sources.cbonds_events import (
    Guarantee,
    IssuerEvents,
    credit_scales,
    default_records,
    events_of,
    guarantees_of,
    latest_snapshot,
    point_order,
)

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

SOURCE_NAMES = {"file": "PDF", "gir_bo": "ГИР БО", "cbonds": "Cbonds"}


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


def absorbed(known: dict[str, dict], routing: RoutingPolicy) -> dict[str, str]:
    """ИНН поглощённых эмитентов и идентификатор преемника у каждого."""
    if not routing.universe.exclude_absorbed:
        return {}
    return {
        inn: str(card.get("emitents_id_absorption"))
        for inn, card in known.items()
        if str(card.get("emitents_id_absorption") or "0") != "0"
    }


def value_of(computed: tuple[MetricValue, ...], code: str) -> Decimal | None:
    """Величина показателя; None — не рассчитан."""
    item = next((entry for entry in computed if entry.code == code), None)
    return item.value if item is not None and item.calculable else None


def routing_rows(
    conn: PgConnection, today: date | None = None
) -> tuple[list[RoutingRow], dict[str, int]]:
    """Собирает входы и вердикты по всем эмитентам; рядом — счётчики отбора.

    Счётчики возвращаются вместе со строками: «в списке 353» без числа
    исключённых не говорит, полон ли список.
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
    skip = absorbed(known, routing)
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

    counts = {"эмитентов": 0, "исключено поглощённых": 0, "карточек": len(known)}
    rows: list[RoutingRow] = []
    for row in fetch_all(_LATEST, {}, conn=conn):
        inn, moment = row["inn"], row["report_date"]
        if inn in skip:
            counts["исключено поглощённых"] += 1
            logger.info(
                "%s исключён: карточка объявляет поглощение (преемник %s)",
                inn,
                skip[inn],
            )
            continue
        computed = compute_from_facts(inn, moment, conn, policy)
        stops = stop_factors_of(inn, moment, computed, conn)
        events = events_of(inn, snapshot, credit, order, defaults)
        # Поручитель нужен уже здесь: формулировка финансирующей структуры
        # без него говорила бы о группе там, где речь о том, кто отвечает
        # по долгу. Корзина же его берётся вторым проходом.
        secured = (
            guarantees_of(inn, frozenset(routing.events.guarantee_statuses))
            if inn in spv
            else ()
        )
        verdict = route(
            computed,
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
            routing=routing,
            types=types,
        )
        delivered = fetch_all(_SOURCES, {"inn": inn, "year": moment.year}, conn=conn)
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
                unit_code=next(
                    (item["unit_code"] for item in delivered if item["unit_code"]), None
                ),
                assessed_class=assessed.get(inn, ""),
                branch=str(card.get("branch_name_rus") or ""),
                group=str(card.get("group_name_rus") or ""),
                events=events,
                guarantees=secured,
                cash=_cash(inn, moment, conn),
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
        # Поручителей бывает несколько — берётся тяжелейший: обязательство
        # каждого действует само по себе, и слабейшее ничего не отменяет.
        heaviest = min(
            backing, key=lambda entry: routing.basket(entry.verdict.basket).order
        )
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
    rows = secured_rows

    # **Групповой контур — второй проход, и иначе он невозможен.** Корзину
    # члена группы решает обстоятельство другого эмитента, а оно известно
    # только после того, как посчитаны все. Выравнивания при этом нет:
    # разбор не переносится, член группы поднимается до внимания.
    in_review = {
        item.group: item
        for item in rows
        if item.group and item.verdict.basket == "review"
    }
    counts["поднято по группе"] = 0
    if in_review:
        lifted: list[RoutingRow] = []
        for item in rows:
            leader = in_review.get(item.group)
            if leader is None or item.inn == leader.inn:
                lifted.append(item)
                continue
            if item.verdict.basket in ("review", "status_unknown"):
                lifted.append(item)
                continue
            counts["поднято по группе"] += 1
            lifted.append(
                replace(
                    item,
                    verdict=route(
                        item.computed,
                        quarantined=False,
                        stop_factors=item.stop_factors,
                        financing_structure=item.inn in spv,
                        operating_profit=_operating_profit(
                            item.inn, item.report_date, conn
                        ),
                        latest_annual=item.report_date,
                        assessed_class=assessed.get(item.inn),
                        branch=item.branch,
                        group=item.group,
                        events=item.events,
                        group_under_review=(item.group, leader.name),
                        today=today,
                        policy=policy,
                        routing=routing,
                        types=types,
                    ),
                )
            )
        rows = lifted
    return rows, counts


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
