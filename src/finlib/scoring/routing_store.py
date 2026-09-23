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

**Универсум задаётся долгом, а не отчётностью** (дорожная карта, фаза 1,
22.09.2026). Прежде перечень собирался из доставок МСФО, и список видел
206 эмитентов из 702 с выпусками в обращении; 403 раскрывают только РСБУ,
а 93 не раскрывают у источника ничего — и отсутствие их не было видно даже
как отсутствие. Теперь перечень — эмитенты с выпусками в обращении вместе
с теми, чья отчётность у нас загружена: эмитент без долга из списка
не выбрасывается, но и в сводные доли не идёт — маршрут спрашивает, нужен ли
человек, а нужен он там, где есть долг.

**База маршрута выбирается порядком предпочтения стандартов**
(`standards.yaml`, `base_standard.preference`): консолидированная отчётность
описывает периметр деятельности, отчётность юридического лица — то, чем долг
привлечён. Правило одно на оценку и на маршрут, и второго порядка здесь
не заводится. Отчётности нет ни по одному стандарту — маршрут строится
по событиям и рейтингам: они от стандарта не зависят вовсе.
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
from finlib.scoring.routing_catalogue import RoutingCatalogue, catalogue_for
from finlib.sources.cbonds import bond_issuers
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
from finlib.standards import Standard, load_standards

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

# **Выборка называет стандарт — графой, а не условием.** Те же коды проверок
# нуля пишет доставка РСБУ, и запись о её комплекте отправляла бы эмитента
# в разбор по комплекту МСФО; но и отбрасывать её нельзя — маршрут строится
# теперь и по РСБУ. Стандарт входит в ключ, и совпадать он обязан с тем,
# по которому маршрут построен.
_ZERO_FAILED = """
SELECT DISTINCT d.inn, s.standard, s.report_year
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.status = 'fail' AND d.check_code IN (
    'cbonds_identity_mismatch', 'cbonds_sections_mismatch', 'cbonds_zero_total'
)
"""

# Денежные средства комплекта: знаменатель рефинансирования. Выборка называет
# стандарт и предпочтение источника, как всякая выборка по ИНН.
# Величина строки комплекта вместе со способом получения: ноль от агрегатора
# означает и нераскрытие, и судить по нему нельзя. Стандарт и код строки —
# доводы: у РСБУ денежные средства стоят строкой 1250, у МСФО позицией
# `ifrs.cash`, и второго запроса на тот же вопрос здесь не заводится.
_LINE = """
SELECT f.value, s.source FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(d)s
  AND f.line_code = %(code)s AND s.is_actual AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""

# Несколько строк комплекта разом, каждая — с предпочтением источника.
# Спрашивают этим запросом двое: признак нераскрытого долга (ноль по всем
# строкам заёмных средств у эмитента с выпусками) и запасной признак
# холдинга (вложения, активы, выручка). Вопрос у них один — «какие
# величины стоят в этих строках», — и второго запроса к нему не заводится.
_LINES = """
SELECT DISTINCT ON (f.line_code) f.line_code, f.value, s.source
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(d)s
  AND f.line_code = ANY(%(codes)s) AND s.is_actual AND s.status <> 'quarantine'
ORDER BY f.line_code, source_rank(s.source)
"""

_SOURCES = """
SELECT DISTINCT source, unit_code, reporting_type FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND is_actual
  AND status <> 'quarantine' AND report_year = %(year)s
"""

# Эмитенты, по которым комплект до нас дошёл — в любом состоянии. Отличает
# «источник отчётности не отдаёт» от «отчётность есть и отбракована нами».
_HAS_SETS = "SELECT DISTINCT inn FROM src_file"

# Основной вид деятельности из ЕГРЮЛ: у холдинга отчётность РСБУ описывает
# управляющую компанию, а не группу. Код приходит от ГИР БО; наименование
# вида объявлено методикой, а не берётся из выписки — там оно пишется свободно.
_OKVED = "SELECT okved FROM organization WHERE inn = %(inn)s"

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
WHERE valid_until >= %(today)s
ORDER BY inn, standard, decided_on DESC, id DESC
"""

_DECISIONS_EXPIRED = """
SELECT count(DISTINCT inn) AS n FROM routing_decision
WHERE valid_until < %(today)s
  AND inn NOT IN (
      SELECT inn FROM routing_decision WHERE valid_until >= %(today)s
  )
"""

SOURCE_NAMES = {"file": "PDF", "gir_bo": "ГИР БО", "cbonds": "Cbonds"}


def decisions(conn: PgConnection, today: date) -> dict[str, ManualFloor]:
    """Действующие решения человека о корзине: ИНН → решение.

    Решение принимается командой с автором и уходит в журнал; маршрут берёт
    последнее действующее. Истёкшее решение не применяется, но из журнала
    не исчезает — журнал доказательная база.

    **Выборка называет стандарт строкой, а не одним на всех.** Прежде здесь
    стоял `Standard.IFRS`, и это было верно ровно до того дня, когда
    универсум задали долгом: у 439 эмитентов маршрут строится по отчётности
    юридического лица, и решение человека о любом из них не нашлось бы
    вовсе — то есть правило не срабатывало бы никогда, а по журналу
    выглядело бы записанным.
    """
    found: dict[str, dict[str, ManualFloor]] = {}
    for row in fetch_all(_DECISIONS, {"today": today}, conn=conn):
        found.setdefault(row["inn"], {})[row["standard"]] = ManualFloor(
            basket=row["basket"],
            author=row["author"],
            reason=row["reason"],
            decided_on=row["decided_on"],
            valid_until=row["valid_until"],
        )
    return found


def floor_for(
    decided: dict[str, dict[str, ManualFloor]],
    inn: str,
    standard: Standard | None,
) -> ManualFloor | None:
    """Действующее решение человека об этом эмитенте по его стандарту.

    **У эмитента без отчётности стандарта нет вовсе**, и решение о нём берётся
    какое есть: спутать его не с чем — комплекта, о котором оно сказало бы
    другое, не существует. Там, где стандарт известен, берётся решение
    его стандарта: ряды по РСБУ и по МСФО несопоставимы, и решение о группе
    по консолидированной отчётности о комплекте юридического лица
    не говорит.
    """
    by_standard = decided.get(inn)
    if not by_standard:
        return None
    if standard is None:
        return next(iter(by_standard.values()))
    return by_standard.get(standard.value)


@dataclass(frozen=True, slots=True)
class RoutingRow:
    """Эмитент со своими входами маршрута и вердиктом."""

    inn: str
    name: str
    # Отчётная дата комплекта, по которому построен маршрут. `None` — маршрут
    # построен без отчётности, по событиям и рейтингам: ноль здесь означал бы
    # дату, а даты нет вовсе.
    report_date: date | None
    verdict: Verdict
    computed: tuple[MetricValue, ...]
    # Чем маршрут построен: стандарт отчётности и его периметр. Графа
    # обязательна — «МСФО · консолидированная» у строки, посчитанной по РСБУ,
    # было бы утверждением о другом предмете.
    standard: Standard | None = None
    basis: str = ""
    # Есть ли у эмитента выпуски в обращении: строки без долга показываются
    # отдельным разделом и в сводные доли не идут.
    has_bonds: bool = True
    # Величины строки, напечатанные один раз: код показателя, наименование
    # и величина в единице комплекта. Набирать их второй раз нельзя —
    # справочник показателей у стандартов свой, и `cur_liq` МСФО зовётся
    # иначе, чем `cur_liq` РСБУ. Код остаётся при них для выгрузки: графа
    # там названа кодом, и по наименованию её не найти.
    shown_values: tuple[tuple[str, str, str], ...] = ()
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
    # Есть ли хоть один поручитель в самом списке: «поручителя нет» и «его
    # отчётности у нас нет» — разные сведения, и графа обязана их различать.
    guarantor_listed: bool = False
    # Тип эмитента и признак, которым он опознан. По корзине тип
    # не восстановить: структурный эмитент с дефолтом стоит в «Разборе»
    # наравне с обычным.
    issuer_type: str = ""
    type_marker: str = ""
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


# Чем маршрут построен, когда отчётности нет вовсе. Графа обязана называть
# это прямо: пустое место читалось бы как «стандарт не указан», а маршрут
# при этом построен — по событиям и рейтингам.
_NO_REPORTING = "События и рейтинги · отчётности нет"


def _row_metrics(catalogue: "RoutingCatalogue") -> tuple[str, ...]:
    """Величины, которые строка списка печатает порознь.

    **Чистый долг и результат называются отдельно от отношения.**
    Отрицательное отношение означает либо чистую денежную позицию, либо
    убыток, и по одному отношению их не различить.
    """
    rule = catalogue.rule
    # **Совокупный долг — знаменатель доли оферт**, и печатается он порознь
    # от чистого: мера рефинансирования называет долю словами, а строка даёт
    # её пересчитать. Без знаменателя доля остаётся утверждением без опоры.
    named = (
        "debt_total",
        "net_debt",
        rule.earnings,
        rule.burden,
        rule.bound,
        *rule.metrics,
    )
    return tuple(dict.fromkeys(code for code in named if code))


def _shown_values(
    catalogue: "RoutingCatalogue", computed: tuple[MetricValue, ...], unit: str
) -> tuple[tuple[str, str, str], ...]:
    """Код, наименование и напечатанная величина каждого показателя строки.

    Набирается один раз и здесь: справочник показателей у стандартов свой,
    и выход, набравший их сам, печатал бы наименования чужого справочника —
    ровно так в приложении по МСФО стояло «Коэффициент текущей ликвидности»
    вместо «Текущая ликвидность».
    """
    found: list[tuple[str, str, str]] = []
    for code in _row_metrics(catalogue):
        value = value_of(computed, code)
        if value is None:
            continue
        found.append(
            (code, catalogue.name_of(code), catalogue.shown(code, value, unit))
        )
    return tuple(found)


def _why_no_reporting(inn: str, delivered: set[str]) -> str:
    """Почему отчётности нет: её не поступало либо комплекты в карантине.

    Различие содержательное: в первом случае данных нет у источника,
    во втором они есть и отбракованы нами. Молчание об этом выдало бы
    наш карантин за пробел источника.

    **Пустой строки здесь быть не может.** Довод `reporting_unavailable`
    проверяется на истинность, и пустая строка читалась бы как «отчётность
    есть»: у 102 эмитентов без отчётности вместо одного основания
    «отчётность недоступна» печатались три «данных для маршрута нет» —
    то есть перечень следствий вместо причины.
    """
    return "quarantined" if inn in delivered else "default"


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
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.scoring.ifrs_store import stop_factors_of
    from finlib.scoring.rsbu_routing import latest_annual

    today = today or date.today()
    policy = load_ifrs_metrics()
    routing = load_routing()
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
        (row["inn"], row["standard"], row["report_year"])
        for row in fetch_all(_ZERO_FAILED, {}, conn=conn)
    }

    # **Универсум: эмитенты с выпусками в обращении и те, чья отчётность
    # у нас загружена.** Первое — предмет маршрута, второе — то, о чём нам
    # уже есть что сказать: эмитент, погасивший долг, из списка молча
    # не исчезает, но в сводные доли не идёт.
    bonds = bond_issuers()
    ifrs_latest = {
        row["inn"]: (row["report_date"], (row["name"] or row["inn"]).strip())
        for row in fetch_all(_LATEST, {}, conn=conn)
    }
    rsbu_latest = latest_annual(conn)
    universe = sorted(set(bonds) | set(ifrs_latest) | set(rsbu_latest))
    preference = load_standards().base_standard
    # Комплекты, которые до нас дошли, — независимо от их состояния.
    # «Отчётности у источника нет» и «отчётность отбракована нами» — разные
    # сведения, и различает их этот перечень.
    has_sets = {row["inn"] for row in fetch_all(_HAS_SETS, {}, conn=conn)}

    # **Верхний десяток по объёму долга в обращении.** Доля считается
    # от эмитентов, у которых объём известен и положителен: у эмитента
    # без облигаций величины нет вовсе, и в знаменателе он мерил бы состав
    # списка, а не долг.
    volumes = {
        inn: total
        for inn in universe
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
        # Состав универсума: знаменатель всех долей списка. «В списке 347»
        # без «из 702» выглядит полнотой.
        "универсум": len(universe),
        "с выпусками в обращении": len(bonds),
        "без выпусков в обращении": len(set(universe) - set(bonds)),
        # Чем построен маршрут у каждого: три исхода, и ноль в любом из них
        # означает сведение, а не пустоту.
        "маршрут по МСФО": 0,
        "маршрут по РСБУ": 0,
        "маршрут по событиям и рейтингам": 0,
        "холдингов на одной РСБУ": 0,
        # Четвёртый признак «ноль не означает нуля»: считается вместе
        # со знаменателем, как всякое правило — иначе ноль срабатываний
        # неотличим от невыполненного.
        "долг не раскрыт при выпусках в обращении": 0,
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
                {"today": today},
                conn=conn,
            )
            or [{"n": 0}]
        )[0]["n"]
    )
    rows: list[RoutingRow] = []
    # Доводы маршрута по каждому эмитенту: второй проход добавляет к ним
    # обстоятельство другого эмитента, а не набирает перечень заново.
    given: dict[str, dict[str, object]] = {}
    # **Кто в списке — известно до маршрута, и сверяется это по ИНН.**
    # Корзина поручителя берётся вторым проходом, а вопрос «есть ли он
    # в списке вообще» решается перечнем и не требует его вердикта. Прежде
    # ответа на него не было вовсе, и формулировка объявляла поручителя
    # отсутствующим по одному тому, что имя известно.
    listed = {inn for inn in universe if inn not in skip}
    for inn in universe:
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
        # **База маршрута выбирается порядком предпочтения стандартов.**
        # Правило одно на оценку и на маршрут (`standards.yaml`), и второго
        # порядка здесь не заводится.
        standard = preference.choose(
            {
                item
                for item, found in (
                    (Standard.IFRS, ifrs_latest.get(inn)),
                    (Standard.RSBU, rsbu_latest.get(inn)),
                )
                if found is not None
            }
        )
        moment = (
            (ifrs_latest if standard is Standard.IFRS else rsbu_latest)[inn][0]
            if standard is not None
            else None
        )
        card = known.get(inn, {})
        # **Тип эмитента берётся у данных карточки, а не у наименования.**
        # Признак, которым он опознан, идёт вместе с ним: «структурный»
        # без признака читался бы как наше суждение.
        kind, marker = routing.type_of(card)
        if kind is not None:
            counts[f"тип: {kind.name}"] = counts.get(f"тип: {kind.name}", 0) + 1
        name = (
            (ifrs_latest.get(inn) or rsbu_latest.get(inn) or (None, ""))[1]
            or bonds.get(inn)
            or str(card.get("name_rus") or "")
            or inn
        )
        catalogue = catalogue_for(standard or Standard.IFRS)
        computed: tuple[MetricValue, ...] = ()
        fired: dict[str, str] = {}
        triggered: tuple[str, ...] = ()
        okved = ""
        if standard is Standard.IFRS:
            computed = compute_from_facts(inn, moment, conn, policy)
            stops = stop_factors_of(inn, moment, computed, conn)
            triggered = stops.triggered
            counts["маршрут по МСФО"] += 1
        elif standard is Standard.RSBU:
            computed, fired, okved = _rsbu_inputs(inn, moment, conn)
            triggered = tuple(dict.fromkeys(fired))
            counts["маршрут по РСБУ"] += 1
            counts["холдингов на одной РСБУ"] += int(routing.holdings.holds(okved))
        else:
            counts["маршрут по событиям и рейтингам"] += 1
        events = events_of(inn, snapshot, credit, order, defaults)
        # **Ноль по всем строкам заёмных средств у эмитента с выпусками
        # в обращении — нераскрытие, а не отсутствие долга.** Признак внешний:
        # он опирается на перечень выпусков, которого загрузчик не знает,
        # и применяется здесь — там, где известно и то и другое.
        if standard is not None and _has_outstanding(events):
            computed, hidden = _without_undisclosed_debt(
                computed, catalogue, inn, moment, standard, conn
            )
            counts["долг не раскрыт при выпусках в обращении"] += int(bool(hidden))
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
        delivered = (
            fetch_all(
                _SOURCES,
                {
                    "inn": inn,
                    "year": moment.year,
                    "standard": (standard or Standard.IFRS).value,
                },
                conn=conn,
            )
            if moment is not None
            else []
        )
        unit_code = next(
            (item["unit_code"] for item in delivered if item["unit_code"]), None
        )
        cash = (
            _cash(inn, moment, conn, standard) if standard is not None else None
        )
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
            # Вторая мера: те же платежи при предъявлении оферт. Приводится
            # к единице комплекта тем же правилом — объём выпуска источник
            # отдаёт в рублях, а отчётность бывает в миллионах.
            offered=in_unit(plan.offered, unit_code) if plan.known else None,
            issues=plan.issues,
            without_schedule=plan.without_schedule,
            without_offers=plan.without_offers,
        )
        # **Доводы маршрута набираются один раз и переиспользуются вторым
        # проходом.** Прежде второй проход собирал перечень доводов заново
        # руками, и в нём недоставало четырёх: у поднятого эмитента исчезали
        # основания рефинансирования, крупного долга, сектора повышенного
        # риска и неподтверждённого статуса. Это тот же «второй путь к одному
        # ответу», только незаметный — корзина при этом получалась правдоподобной.
        inputs: dict[str, object] = dict(
            unit=unit,
            quarantined=(
                moment is not None
                and standard is not None
                and (inn, standard.value, moment.year) in quarantined
            ),
            stop_factors=triggered,
            stop_factor_values=fired,
            financing_structure=inn in spv,
            guarantor=", ".join(sorted({item.name for item in secured})),
            guarantor_inns=", ".join(
                sorted({item.inn for item in secured if item.inn})
            )
            or "",
            guarantor_listed=any(
                item.inn in listed and item.inn != inn for item in secured
            ),
            issuer_type=kind,
            type_marker=marker,
            operating_profit=(
                _operating_profit(inn, moment, conn, standard)
                if standard is not None
                else None
            ),
            latest_annual=moment,
            assessed_class=assessed.get(inn),
            branch=str(card.get("branch_name_rus") or ""),
            group=str(card.get("group_name_rus") or ""),
            okved=okved,
            holding_lines=(
                _holding_lines(inn, moment, conn, standard, routing)
                if standard is Standard.RSBU
                else None
            ),
            reporting_unavailable=_why_no_reporting(inn, has_sets)
            if standard is None
            else "",
            events=events,
            today=today,
            catalogue=catalogue,
            refinance=refinance,
            systemic_volume=systemic.get(inn),
            status_unconfirmed=unconfirmed.get(inn, ""),
            manual_floor=floor_for(decided, inn, standard),
            risk_sector=tuple(
                replace(risky[item.isin], name=item.name)
                for item in events.issues
                if item.isin and item.isin in risky
            ),
            routing=routing,
        )
        verdict = route(computed, **inputs)
        given[inn] = inputs
        counts["эмитентов"] += 1
        rows.append(
            RoutingRow(
                inn=inn,
                name=name,
                report_date=moment,
                verdict=verdict,
                computed=computed,
                standard=standard,
                basis=catalogue.label if standard is not None else _NO_REPORTING,
                has_bonds=inn in bonds,
                shown_values=_shown_values(catalogue, computed, unit),
                stop_factors=triggered,
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
                guarantor_listed=bool(inputs.get("guarantor_listed")),
                issuer_type=kind.name if kind is not None else "",
                type_marker=marker,
                cash=cash,
                refinance=refinance,
                # **Состав величин строки объявлен стандартом, а не кодом.**
                # У РСБУ долговой нагрузки нет вовсе, и перечень кодов МСФО
                # дал бы пустую графу там, где величина есть, — только под
                # другим именем.
                values={
                    code: value
                    for code in _row_metrics(catalogue)
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
    # **Корзина, взятая у поручителя, обязана пережить третий проход.** Групповой
    # контур маршрутизирует строку заново от исходных доводов, и вердикт SPV,
    # полученный у поручителя, при этом терялся: у Газпром Капитала возвращался
    # разбор с формулировкой «поручитель в списке отсутствует». Поэтому
    # поручитель запоминается, и после повторной маршрутизации корзина берётся
    # у него снова.
    led: dict[str, RoutingRow] = {}
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
            led[item.inn] = heaviest
            secured_rows.append(
                replace(
                    item,
                    verdict=led_by_guarantor(
                        item.verdict,
                        heaviest.name,
                        heaviest.verdict,
                        item.group,
                        routing,
                        heaviest.unit,
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
            again = route(
                item.computed,
                **{
                    **given[item.inn],
                    "group_under_review": (item.group, leader.name),
                },
            )
            backing = led.get(item.inn)
            if backing is not None:
                again = led_by_guarantor(
                    again,
                    backing.name,
                    backing.verdict,
                    item.group,
                    routing,
                    backing.unit,
                )
            lifted.append(replace(item, verdict=again))
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


def _cash(
    inn: str, moment: date, conn: PgConnection, standard: Standard
) -> Decimal | None:
    """Денежные средства комплекта; None — величина не раскрыта.

    Ноль здесь остаётся нулём: денежные средства бывают нулевыми, а правило
    нераскрытия относится к величинам, которые ломают тождество отчётности
    либо равны нулю у итога при ненулевом составе. Знаменатель из нуля
    отношения не даёт, и решает это тот, кто делит.

    Код строки берётся у стандарта: у МСФО это позиция `ifrs.cash`,
    у РСБУ строка 1250.
    """
    code = catalogue_for(standard).rule.cash_line
    found = fetch_all(
        _LINE,
        {"inn": inn, "d": moment, "code": code, "standard": standard.value},
        conn=conn,
    )
    return found[0]["value"] if found else None


def _operating_profit(
    inn: str, moment: date, conn: PgConnection, standard: Standard
) -> Decimal | None:
    """Операционный результат периода; ноль от агрегатора величиной не считается.

    **Ноль у агрегатора означает и нераскрытие**, и судить по нему нельзя:
    у ЯКОВЛЕВА ноль читался как операционный убыток, то есть как утверждение
    об эмитенте, сделанное по величине, которой источник не раскрыл. Правило
    то же, что у контролей сходимости, и объявлено там же.

    У МСФО это операционная прибыль, у РСБУ — прибыль от продаж (строка 2200):
    знаменатель вывода по границе и он же знак операционного результата.
    """
    code = catalogue_for(standard).rule.operating_line
    found = fetch_all(
        _LINE,
        {"inn": inn, "d": moment, "code": code, "standard": standard.value},
        conn=conn,
    )
    if not found:
        return None
    value, source = Decimal(found[0]["value"]), found[0]["source"]
    if value == 0 and _zero_is_unknown(source):
        logger.info(
            "%s за %s: операционный результат (%s) доставлен нулём (%s) — "
            "величиной не считается",
            inn,
            moment,
            code,
            source,
        )
        return None
    return value


def _holding_lines(
    inn: str,
    moment: date,
    conn: PgConnection,
    standard: Standard,
    routing: RoutingPolicy,
) -> dict[str, Decimal | None]:
    """Строки запасного признака холдинга: вложения, активы, выручка.

    Перечень берётся у методики, а не пишется здесь: строка, названная
    в коде, разошлась бы со справочником при первой же правке признака.
    """
    rule = routing.holdings.fallback
    codes = [*rule.financial_investments, rule.assets, rule.revenue]
    rows = fetch_all(
        _LINES,
        {"inn": inn, "d": moment, "codes": codes, "standard": standard.value},
        conn=conn,
    )
    return {row["line_code"]: row["value"] for row in rows}


def _has_outstanding(events: IssuerEvents | None) -> bool:
    """Есть ли у эмитента выпуски в обращении либо размещаемые."""
    if events is None:
        return False
    return any(
        item.status in ("в обращении", "размещается") for item in events.issues
    )


def _without_undisclosed_debt(
    computed: tuple[MetricValue, ...],
    catalogue: RoutingCatalogue,
    inn: str,
    moment: date,
    standard: Standard,
    conn: PgConnection,
) -> tuple[tuple[MetricValue, ...], tuple[str, ...]]:
    """Убирает величины долга, если долг у агрегатора не раскрыт.

    **Отрицательный чистый долг читается как чистая денежная позиция**, то есть
    как довод в пользу эмитента, — и получен он вычитанием денежных средств
    из долга, которого источник не раскрыл. Поэтому убирается не отношение,
    а все величины, считающиеся из долга: оставить сам долг и убрать отношение
    значило бы напечатать «чистый долг −42 882» рядом с «нагрузка неизвестна».

    Величина не подменяется нулём и не занижается — она объявляется
    нерассчитанной, и маршрут называет недостающее основанием «данных
    недостаточно». Возвращается вместе с перечнем убранного: правило,
    сработавшее молча, неотличимо от невыполненного.
    """
    from dataclasses import replace

    from finlib.normalize.facts import debt_undisclosed

    rule = catalogue.rule
    rows = fetch_all(
        _LINES,
        {
            "inn": inn,
            "d": moment,
            "codes": list(rule.debt_lines),
            "standard": standard.value,
        },
        conn=conn,
    )
    lines = {row["line_code"]: (row["value"], row["source"]) for row in rows}
    if not debt_undisclosed(lines, has_bonds=True):
        return computed, ()
    hidden = tuple(
        item.code
        for item in computed
        if item.code in rule.debt_metrics and item.calculable
    )
    if not hidden:
        return computed, ()
    logger.info(
        "%s за %s: долг не раскрыт (ноль по %s при выпусках в обращении) — "
        "величины %s не считаются",
        inn,
        moment,
        ", ".join(sorted(lines)),
        ", ".join(hidden),
    )
    return (
        tuple(
            replace(item, value=None, reason=None)
            if item.code in hidden
            else item
            for item in computed
        ),
        hidden,
    )


def _rsbu_inputs(
    inn: str, moment: date, conn: PgConnection
) -> tuple[tuple[MetricValue, ...], dict[str, str], str]:
    """Показатели, сработавшие стоп-факторы и вид деятельности эмитента РСБУ.

    Величины считает боевой расчёт, стоп-факторы — та же функция, что
    и при оценке. Здесь только сборка: перечень, написанный второй раз,
    разошёлся бы с первым.
    """
    from finlib.metrics.definitions import load_metrics
    from finlib.scoring.definitions import load_scoring
    from finlib.scoring.engine import triggered_stop_factors
    from finlib.scoring.rsbu_routing import computed_of, with_denominator

    computed = computed_of(inn, moment, conn)
    profit = _operating_profit(inn, moment, conn, Standard.RSBU)
    bound = catalogue_for(Standard.RSBU).rule.bound
    computed = with_denominator(computed, bound, profit)
    values = {item.code: item.value for item in computed}
    fired = dict(triggered_stop_factors(values, load_metrics(), load_scoring()))
    row = fetch_all(_OKVED, {"inn": inn}, conn=conn)
    return computed, fired, str((row[0]["okved"] if row else "") or "")


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
