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

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from finlib.db import PgConnection, fetch_all
from finlib.metrics.ifrs import MetricValue
from finlib.metrics.interim import Rolling
from finlib.normalize.lines import load_lines
from finlib.scoring.market import MarketFinding
from finlib.scoring.market import findings as market_findings
from finlib.scoring.routing import (
    IssuerType,
    ManualFloor,
    Overrides,
    Refinance,
    RoutingPolicy,
    Verdict,
    led_by_guarantor,
    load_routing,
    route,
)
from finlib.scoring.routing_catalogue import RoutingCatalogue, catalogue_for
from finlib.sources import floating
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
    point_order,
    read_snapshot,
)
from finlib.sources.cbonds_flows import refinancing
from finlib.sources.market import Market, load_market
from finlib.sources.market import series as _market_series
from finlib.sources.moex_risk import RiskSector, risk_sectors
from finlib.sources.ratings_calendar import Transition, transitions
from finlib.standards import Standard, load_standards

logger = logging.getLogger(__name__)

# Карточки эмитентов источника: признак поглощения и отрасль. Файл собирает
# `eval/cbonds_emitents.py` и складывает на диск; в сеть отсюда не ходим.
CARDS = Path("data/raw/cbonds/emitents.json")

# **Комплект виден не с отчётной даты, а с даты раскрытия.** При пересчёте
# истории назад это решает всё: отчётность за 2025 год 15 февраля 2026-го
# ещё не существовала, и маршрут, построенный по ней на ту дату, был бы
# предсказанием, а не наблюдением. Настоящей даты раскрытия у массовых
# данных нет — отсрочка берётся сроком закона (`routing.history.known_from`),
# и это помечается у каждой точки истории.
_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date,
       max(COALESCE(NULLIF(btrim(o.name), ''), NULLIF(btrim(o.short_name), ''), f.inn)) AS name
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
  -- Основание маршрута — годовой комплект (`standards.yaml`,
  -- `period_preference.basis`): промежуточный служит наблюдением между
  -- годовыми, а шкалы откалиброваны на годовых величинах.
  AND COALESCE(s.reporting_kind, 'full') <> 'interim'
  AND (
      %(as_of)s::date IS NULL
      -- **Настоящая дата раскрытия старше смоделированной.** ГИР БО её
      -- сообщает, и там, где она есть, срок закона не спрашивается вовсе:
      -- правило берётся только там, где источник о дате молчит.
      OR COALESCE(
          (s.meta->>'disclosed_on')::date,
          f.report_date + (
              CASE WHEN s.reporting_kind = 'interim'
                   THEN %(interim)s ELSE %(annual)s END
          )
      ) <= %(as_of)s::date
  )
GROUP BY f.inn
"""

# **Свежий промежуточный комплект по каждому стандарту** — кандидат в базу
# маршрута (`standards.yaml`, `period_preference.basis: ltm`). Виден он с дня
# появления записи у агрегатора (`meta.cbonds.created_at`, решение владельца
# 25.09.2026); настоящая дата раскрытия старше и её, а срок закона — только
# там, где нет ни той, ни другой. Графа `by_law` называет такие комплекты:
# их число печатается.
_LATEST_INTERIM = """
SELECT DISTINCT ON (s.inn, s.standard)
       s.inn, s.standard, s.period_end,
       (s.meta->>'disclosed_on') IS NULL
         AND (s.meta->'cbonds'->>'created_at') IS NULL AS by_law
FROM src_file s
WHERE s.is_actual AND s.status <> 'quarantine' AND s.reporting_kind = 'interim'
  AND (
      %(as_of)s::date IS NULL
      OR COALESCE(
          (s.meta->>'disclosed_on')::date,
          (s.meta->'cbonds'->>'created_at')::date,
          s.period_end + CASE s.standard WHEN 'ifrs' THEN %(ifrs)s ELSE %(rsbu)s END
      ) <= %(as_of)s::date
  )
ORDER BY s.inn, s.standard, s.period_end DESC
"""

# **Выборка называет стандарт — графой, а не условием.** Те же коды проверок
# нуля пишет доставка РСБУ, и запись о её комплекте отправляла бы эмитента
# в разбор по комплекту МСФО; но и отбрасывать её нельзя — маршрут строится
# теперь и по РСБУ. Стандарт входит в ключ, и совпадать он обязан с тем,
# по которому маршрут построен.
#
# **Ключ — отчётная дата комплекта, а не год.** С тех пор как в ключ комплекта
# вошёл период, за год у эмитента бывает четыре комплекта, и провал проверки
# нуля у квартала, отобранный по году, ложился на годовой: 25.09.2026
# АвтоМоё Опт ушла в «Разбор» по двум промежуточным комплектам при годовом,
# проверку прошедшем.
_ZERO_FAILED = """
SELECT DISTINCT d.inn, s.standard, s.period_end
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
SELECT f.value, s.source, s.unit_code FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
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

# Комплекты той отчётной даты, по которой построен маршрут, — не года:
# единица и вид отчётности промежуточного комплекта того же года о годовом
# не говорят.
_SOURCES = """
SELECT DISTINCT source, unit_code, reporting_type FROM src_file
WHERE inn = %(inn)s AND standard = %(standard)s AND is_actual
  AND status <> 'quarantine' AND period_end = %(period_end)s
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
        AND s.period_end = a.report_date
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
-- **Решение действует со дня, когда принято, а не раньше.** Прежде отбор
-- спрашивал только срок, и в пересчёте истории решение человека от сентября
-- 2026 года стояло у эмитента весь предыдущий год: маршрут задним числом
-- знал то, чего тогда никто не решал. У Кириллицы это давало «Разбор»
-- без единого основания за год до события.
WHERE decided_on <= %(today)s AND valid_until >= %(today)s
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


def zero_failed(conn: PgConnection) -> set[tuple[str, str, date]]:
    """Комплекты с проваленной проверкой нуля: ИНН, стандарт, отчётная дата."""
    return {
        (row["inn"], row["standard"], row["period_end"])
        for row in fetch_all(_ZERO_FAILED, {}, conn=conn)
    }


def sources_of(
    inn: str, standard: Standard, moment: date, conn: PgConnection
) -> list[dict]:
    """Способ получения, единица и вид актуальных комплектов на эту дату."""
    return fetch_all(
        _SOURCES,
        {"inn": inn, "standard": standard.value, "period_end": moment},
        conn=conn,
    )


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
    # Оговорка о базе: пусто — годовая аудированная; иначе LTM
    # на промежуточную дату, неаудированная (`interim.yaml`, `confidence`).
    basis_note: str = ""
    # Годовой комплект — опора базы: срок раскрытия, заключение аудитора.
    annual_date: date | None = None
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
    # Отпечаток доводов маршрута: им разводятся три причины изменения вердикта.
    fingerprint: str = ""
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
        *rule.alongside,
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
    as_of: date | None = None,
    memo: dict | None = None,
    variants: "Mapping[str, Overrides] | None" = None,
    verdicts: dict[str, dict[str, Verdict]] | None = None,
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

    **`as_of` строит маршрут на прошлую дату по тому, что было известно
    на неё.** Отчётность видна с даты раскрытия, а не с отчётной; события
    и рейтинговые действия — с даты события; признаки карточки истории
    не имеют вовсе и потому в пересчёте **не участвуют**: сегодняшний признак
    дефолта, применённый к прошлому году, объявил бы эмитента дефолтным весь
    год. Пусто — сегодня, и видно всё загруженное.

    **`memo` — память пересчёта, а не кэш расчёта.** Показатели зависят
    от пары «эмитент, отчётная дата», а не от дня маршрута: при обходе года
    по неделям один и тот же комплект считался бы пятьдесят раз подряд.
    Словарь живёт один проход и передаётся снаружи: боевой прогон его
    не передаёт вовсе, и второго пути к величинам не появляется — путь тот же,
    просто ответ не спрашивается дважды об одном.

    **`variants` — пороги вариантов калибровки, `verdicts` — куда положить
    их ответы** (фаза 6). Входы собираются один раз, и `route` зовётся
    на них ещё раз на каждый вариант: собрать входы заново стоило бы восьми
    секунд на дату на вариант, а сам маршрут — сотой доли. Ответ варианта —
    вердикт первого прохода, а у финансирующей структуры — взятый у её
    поручителя при том же варианте, как в боевом втором проходе; прочее
    во втором проходе (поднятие по поручителю, группа) добавляет основания
    о других эмитентах, а оснований по величинам не трогает. Строки
    и счётчики остаются боевыми: вариант в них не
    попадает. Ключ тот же, что у `blind`, — довод замера: боевой вызов
    его не передаёт.
    """
    memo = memo if memo is not None else {}
    from finlib.metrics.ifrs_store import compute_from_facts, compute_ltm_from_facts
    from finlib.normalize.ifrs_metrics import load_ifrs_metrics
    from finlib.scoring.ifrs_store import stop_factors_of
    from finlib.scoring.interim import load_interim
    from finlib.scoring.rsbu_routing import latest_annual

    today = today or date.today()
    policy = load_ifrs_metrics()
    routing = load_routing()
    known = cards()
    skip, unconfirmed = exclusions(known, routing)
    spv = {
        inn for inn, card in known.items() if str(card.get("emitent_spv")) == "1"
    }
    taken = read_snapshot()
    on, snapshot = taken.on, taken.issuers
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
    # **Рыночный ряд читается один раз на прогон.** Пустой ряд означает, что
    # срезов биржи на диске нет вовсе, — и это не «рынок молчал»: доставка
    # идёт `scripts/moex_market_fetch.py`, а пересчёт ряда — `sources.market`.
    market_rules = load_market()
    market: Market = _market_series()
    if not market.issuers:
        logger.warning(
            "рыночного ряда на диске нет: основания рынка в маршрут не "
            "попадут, и это отсутствие данных, а не отсутствие сигнала"
        )
    # Даты перехода в нынешнюю рейтинговую категорию: выгрузка ручная,
    # и пустой словарь означает «календаря на диске нет», а не «переходов
    # не было». Корзину он не двигает — только датирует основание.
    moves: dict[str, dict[tuple[str, str, str], Transition]] = {}
    for (holder, agency, scale, category), moved in transitions().items():
        moves.setdefault(holder, {})[(agency, scale, category)] = moved
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
    quarantined = zero_failed(conn)

    # **Универсум: эмитенты с выпусками в обращении и те, чья отчётность
    # у нас загружена.** Первое — предмет маршрута, второе — то, о чём нам
    # уже есть что сказать: эмитент, погасивший долг, из списка молча
    # не исчезает, но в сводные доли не идёт.
    bonds = bond_issuers()
    disclosed = routing.history.known_from
    ifrs_latest = {
        row["inn"]: (row["report_date"], (row["name"] or row["inn"]).strip())
        for row in fetch_all(
            _LATEST,
            {
                "as_of": as_of,
                "annual": disclosed.days(Standard.IFRS, interim=False),
                "interim": disclosed.days(Standard.IFRS, interim=True),
            },
            conn=conn,
        )
    }
    rsbu_latest = latest_annual(conn, as_of)
    interim_latest = {
        (row["inn"], row["standard"]): (row["period_end"], bool(row["by_law"]))
        for row in fetch_all(
            _LATEST_INTERIM,
            {
                "as_of": as_of,
                "ifrs": disclosed.days(Standard.IFRS, interim=True),
                "rsbu": disclosed.days(Standard.RSBU, interim=True),
            },
            conn=conn,
        )
    }
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
        # База маршрута: LTM по промежуточному комплекту либо годовая.
        # Отказ LTM и видимость по сроку закона считаются рядом — иначе
        # «база LTM у N» не говорит, у скольких её не сложилось.
        "база LTM": 0,
        "база годовая: LTM не сложился": 0,
        "база LTM: видимость по сроку закона": 0,
        "холдингов на одной РСБУ": 0,
        # Четвёртый признак «ноль не означает нуля»: считается вместе
        # со знаменателем, как всякое правило — иначе ноль срабатываний
        # неотличим от невыполненного.
        "долг не раскрыт при выпусках в обращении": 0,
        # Вклад рыночного слоя порознь: у кого он единственное обстоятельство
        # и у кого он добавился к уже известному.
        "рынок открыл": 0,
        "рынок поднял тяжесть": 0,
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
        # **База маршрута — LTM на последнюю отчётную дату** (`standards.yaml`,
        # `period_preference.basis: ltm`). Промежуточный комплект того же
        # стандарта становится базой, только если он новее годового; годовой
        # того же года, раскрытый позже, возвращает базу себе — он новее.
        # Годовой при этом остаётся опорой: срок раскрытия, заключение
        # аудитора и его стоп-факторы, признак холдинга.
        annual_moment = moment
        basis_note = ""
        interim = interim_base(inn, standard, moment, interim_latest)
        if interim is not None:
            operating = _operating_ltm(inn, interim[0], conn, standard)
            if operating.known:
                moment = interim[0]
                basis_note = load_interim().confidence.said("interim", moment)
                counts["база LTM"] += 1
                counts["база LTM: видимость по сроку закона"] += int(interim[1])
            else:
                # **Не сложился LTM — база остаётся годовой, и причина
                # называется**: подставить полугодие вместо года значило бы
                # мерить годовой шкалой половину года.
                counts["база годовая: LTM не сложился"] += 1
                logger.info(
                    "%s: промежуточный комплект на %s новее годового, но LTM "
                    "не сложился (%s) — база годовая",
                    inn,
                    interim[0],
                    operating.reason,
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
        # Показатели зависят от пары «эмитент, отчётная дата», а не от дня
        # маршрута: при обходе года по неделям один и тот же комплект
        # считался бы полсотни раз подряд.
        counted = memo.setdefault("metrics", {})
        key = (inn, standard.value if standard else "", moment)
        ltm = bool(basis_note)
        if standard is Standard.IFRS:
            if key not in counted:
                found = (
                    compute_ltm_from_facts(inn, moment, annual_moment, conn, policy)
                    if ltm
                    else compute_from_facts(inn, moment, conn, policy)
                )
                counted[key] = (
                    found,
                    {},
                    "",
                    # Заключение аудитора и тип эмитента — у годового
                    # комплекта: промежуточный их не несёт.
                    stop_factors_of(inn, annual_moment, found, conn).triggered,
                )
            computed, fired, okved, triggered = counted[key]
            counts["маршрут по МСФО"] += 1
        elif standard is Standard.RSBU:
            if key not in counted:
                found, values, activity = _rsbu_inputs(inn, moment, conn, ltm=ltm)
                counted[key] = (
                    found,
                    values,
                    activity,
                    tuple(dict.fromkeys(values)),
                )
            computed, fired, okved, triggered = counted[key]
            counts["маршрут по РСБУ"] += 1
            counts["холдингов на одной РСБУ"] += int(routing.holdings.holds(okved))
        else:
            counts["маршрут по событиям и рейтингам"] += 1
        # Выпуски и рейтинги эмитента читаются с диска: перечень один и тот же
        # на весь проход, а дата маршрута отсекает их уже после чтения.
        known_events = memo.setdefault("events", {})
        if inn not in known_events:
            known_events[inn] = events_of(
                inn, snapshot, credit, order, defaults, taken.observed
            )
        events = known_events[inn]
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
        if as_of is not None:
            events = _known_at(events, as_of)
        # Поручительства читаются у всех, а не только у финансирующих
        # структур: у обычного эмитента поручитель в разборе — такое же
        # обстоятельство, как эмитент своей группы. Корзина берётся вторым
        # проходом, а имя нужно уже здесь: формулировка SPV без него говорила
        # бы о группе там, где речь о том, кто отвечает по долгу.
        backing = memo.setdefault("guarantees", {})
        if inn not in backing:
            backing[inn] = guarantees_of(
                inn, frozenset(routing.events.guarantee_statuses)
            )
        secured = backing[inn]
        delivered = (
            sources_of(inn, standard or Standard.IFRS, moment, conn)
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
        # **Окно скользящее: от сегодня на объявленное число дней.** Край едет
        # посуточно, и платёж входит в него однажды — не выходит, пока
        # не будет заплачен. Дребезг вносила ступень на первое число месяца,
        # а не движение края: она вносила и выносила целый месяц платежей.
        # Денежные средства при этом остаются на отчётную дату: моменты
        # расходятся намеренно — это предмет меры, а не её изъян.
        coupons = routing.refinancing.floating_coupons
        plan = refinancing(
            events.issues,
            routing.refinancing.days,
            today,
            offer_kinds=routing.refinancing.offer_kinds,
            estimator=floating.estimator(coupons) if coupons else None,
        )
        refinance = Refinance(
            due=in_unit(plan.scheduled, unit_code) if plan.known else None,
            cash=cash,
            unit=unit,
            days=routing.refinancing.days,
            # Вторая мера: те же платежи при предъявлении оферт. Приводится
            # к единице комплекта тем же правилом — объём выпуска источник
            # отдаёт в рублях, а отчётность бывает в миллионах.
            offered=in_unit(plan.offered, unit_code) if plan.known else None,
            issues=plan.issues,
            without_schedule=plan.without_schedule,
            without_offers=plan.without_offers,
            estimated=(in_unit(plan.estimated, unit_code) or Decimal(0))
            if plan.known
            else Decimal(0),
            unknown=plan.unknown,
            bases=plan.bases,
            by_terms=(in_unit(plan.by_terms, unit_code) or Decimal(0))
            if plan.known
            else Decimal(0),
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
                and (inn, standard.value, moment) in quarantined
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
                (
                    _operating_ltm(inn, moment, conn, standard).value
                    if basis_note
                    else _operating_profit(inn, moment, conn, standard)
                )
                if standard is not None
                else None
            ),
            # Срок раскрытия спрашивается о годовой отчётности: промежуточная
            # его не отменяет.
            latest_annual=annual_moment,
            basis_note=basis_note,
            assessed_class=assessed.get(inn),
            branch=str(card.get("branch_name_rus") or ""),
            group=str(card.get("group_name_rus") or ""),
            okved=okved,
            # Признак холдинга — строение организации, и читается он
            # по годовому комплекту: выручка в нём — поток, и промежуточный
            # о строении ничего нового не скажет.
            holding_lines=(
                _holding_lines(inn, annual_moment, conn, standard, routing)
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
            # **Календарь рейтинговых действий даёт одно — дату перехода.**
            # Категорию по-прежнему называет ежедневный снимок, и корзина
            # от этого довода не зависит: он попадает только в формулировку.
            # В пересчёте переход виден с его собственной даты — иначе
            # сегодняшнее знание выдавалось бы за прошлогоднее наблюдение.
            rating_since={
                key: moved
                for key, moved in moves.get(inn, {}).items()
                if as_of is None or moved.since <= as_of
            },
            # **Перевод биржи датирован, и в пересчёте он виден с даты
            # перевода.** Недатированный перевод в историю не идёт вовсе:
            # поставить его на произвольный день значило бы выдумать событие.
            # **Рыночные основания считаются на дату маршрута.** В пересчёте
            # это дата обхода: ряд идёт по дням, и взять сегодняшнюю цену
            # для прошлогодней точки значило бы дать маршруту знание, которого
            # в тот день не было.
            market=market_findings(
                market_rules,
                market,
                inn,
                as_of or today,
                systemic=inn in systemic,
            ),
            risk_sector=tuple(
                replace(risky[item.isin], name=item.name)
                for item in events.issues
                if item.isin
                and item.isin in risky
                and (
                    as_of is None
                    or (
                        risky[item.isin].since is not None
                        and risky[item.isin].since <= as_of
                    )
                )
            ),
            routing=routing,
        )
        verdict = route(computed, **inputs)
        given[inn] = inputs
        if variants and verdicts is not None:
            for name, over in variants.items():
                verdicts.setdefault(name, {})[inn] = route(
                    computed, **inputs, thresholds=over
                )
        counts["эмитентов"] += 1
        # **Рынок открывает эмитента либо поднимает ему тяжесть, и это разные
        # сведения** (решение владельца 24.09.2026). Из 74 пришедших в «Разбор»
        # 52 уже стояли во «Внимании» по нерыночным основаниям: слой не нашёл
        # их, а сказал о них тяжелее. Одно число на оба случая читалось бы как
        # «рынок нашёл семьдесят четыре».
        mine = [code for code in verdict.grounds if code.startswith("market_")]
        if mine:
            others = [
                code
                for code in verdict.grounds
                if not code.startswith("market_")
            ]
            counts["рынок поднял тяжесть" if others else "рынок открыл"] += 1
        rows.append(
            RoutingRow(
                inn=inn,
                name=name,
                report_date=moment,
                verdict=verdict,
                computed=computed,
                standard=standard,
                basis=catalogue.label if standard is not None else _NO_REPORTING,
                basis_note=basis_note,
                annual_date=annual_moment,
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
                fingerprint=fingerprint(inputs),
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
    # Вердикты вариантов первого прохода: поручитель берётся из них, как
    # боевой проход берёт его из `by_inn`, а не из уже переписанного.
    first = (
        {name: dict(found) for name, found in verdicts.items()}
        if variants and verdicts is not None
        else {}
    )
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
            # **У варианта корзина SPV берётся у того же поручителя** — его
            # собственного вердикта при том же пороге. Иначе основания
            # финансирующей структуры в варианте были бы её собственными,
            # а в истории — поручителя, и «прежний» вариант с историей
            # не сошёлся бы (пилот 28.09.2026: 31 расхождение из 4 497).
            for name, found in first.items():
                if item.inn in found and heaviest.inn in found:
                    verdicts[name][item.inn] = led_by_guarantor(
                        found[item.inn],
                        heaviest.name,
                        found[heaviest.inn],
                        item.group,
                        routing,
                        heaviest.unit,
                    )
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


# **Доводы, которые не данные, а устройство.** Справочники и день расчёта
# в отпечаток не входят: первые два — наша методика (их изменение и есть
# «причина у нас», и она называется версией), третий меняется у каждой точки
# по построению, и отпечаток от него отличался бы всегда.
_NOT_DATA = frozenset({"routing", "catalogue", "today"})


class UnknownInputError(TypeError):
    """Довод маршрута, который отпечаток не умеет назвать.

    **Умолчание здесь запрещено.** Пропущенный довод — это изменение, которое
    произошло и не объяснилось ничем: оно попадёт в беспричинные, то есть
    в остановку. Лучше упасть на новом доводе, чем молча его не заметить.
    """


def _rendered(value: object) -> str:
    """Довод маршрута строкой — для отпечатка входов.

    Разбирается только то, что маршруту действительно передаётся; всё
    остальное — ошибка, а не пропуск.
    """
    if value is None or isinstance(value, str | int | float | bool | Decimal | date):
        return str(value)
    if isinstance(value, Mapping):
        # Ключ бывает не строкой — «агентство, шкала, категория» приходит
        # тройкой, — и приводить его к строке надо вместе с его значением:
        # приведённый отдельно, он в словаре уже не находится.
        return "{" + ";".join(
            sorted(f"{key}={_rendered(item)}" for key, item in value.items())
        ) + "}"
    if isinstance(value, tuple | list | set | frozenset):
        items = sorted(_rendered(item) for item in value)
        return "[" + ";".join(items) + "]"
    if isinstance(value, MetricValue):
        return f"{value.code}={value.value}"
    if isinstance(value, IssuerEvents):
        return _rendered(
            (
                tuple(
                    f"{item.emission_id}:{item.status}:{item.defaulted}"
                    for item in value.issues
                ),
                tuple(
                    f"{item.emission_id}:{item.moment}:{item.met}"
                    for item in value.records
                ),
                tuple(
                    f"{item.agency}:{item.point}:{item.assigned}"
                    for item in value.ratings
                ),
            )
        )
    if isinstance(value, Guarantee):
        return f"{value.inn}:{value.status}"
    if isinstance(value, ManualFloor):
        return f"{value.author}:{value.basket}:{value.decided_on}:{value.valid_until}"
    if isinstance(value, Refinance):
        return f"{value.due}:{value.offered}:{value.cash}:{value.unit}"
    if isinstance(value, IssuerType):
        return value.code
    if isinstance(value, RiskSector):
        return f"{value.isin}:{value.board}:{value.since}"
    if isinstance(value, Transition):
        return f"{value.agency}:{value.since}:{value.was_level}:{value.direction}"
    if isinstance(value, MarketFinding):
        # Величина в отпечаток входит вместе с днём, с которого признак
        # держится: спред двигается ежедневно, и отпечаток без него менялся бы
        # каждый день — то есть объявлял бы изменение у эмитента всякий раз.
        return f"{value.ground}:{value.since}:{value.threshold}"
    raise UnknownInputError(
        f"довод маршрута {type(value).__name__} в отпечаток не входит: "
        "пропущенный довод даёт изменение, которое не объяснится ничем"
    )


def fingerprint(inputs: Mapping[str, object]) -> str:
    """Отпечаток доводов маршрута: им разводятся три причины изменения.

    **Изменился отпечаток — причина у эмитента; тот же при изменившихся
    версиях кода и методики — причина у нас; тот же при тех же версиях —
    беспричинное изменение**, то есть дефект недетерминированности, и это
    остановка, а не строка отчёта.

    Берётся он в одном месте — там, где доводы и собираются, — и второго
    пути к нему нет: отпечаток, набранный вторым перечнем, разошёлся бы
    с первым ровно тогда, когда появился бы новый довод.
    """
    said = ";".join(
        f"{key}={_rendered(inputs[key])}"
        for key in sorted(inputs)
        if key not in _NOT_DATA
    )
    return hashlib.sha256(said.encode("utf-8")).hexdigest()[:32]


def _known_at(events: IssuerEvents, as_of: date) -> IssuerEvents:
    """События эмитента так, как они были известны на названную дату.

    **Признак карточки истории не имеет, и потому в пересчёт не идёт вовсе.**
    Сегодняшний признак дефолта, применённый к прошлому году, объявил бы
    эмитента дефолтным весь год — то есть выдал бы нынешнее знание
    за наблюдение. Остаются датированные события и рейтинговые действия,
    случившиеся не позже названного дня; статус выпуска гасится тем же
    правилом, которым он гасится у замера без событий.

    **Рейтинг восстанавливается на один шаг назад, и не больше.** До даты
    последнего действия известно лишь то, что нынешнего рейтинга не было;
    какой был — неизвестно, и оснований по рейтингу на ту дату не ставится.
    Это не «рейтинга нет», а «рейтинг неизвестен», и разница объявлена здесь.
    """
    return replace(
        events.as_of(as_of),
        issues=tuple(_without_default(item) for item in events.issues),
        ratings=tuple(
            item
            for item in events.ratings
            if item.assigned is not None and item.assigned <= as_of
        ),
    )


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
    # **Нераскрытая строка величиной не является.** В базе она стоит `NULL`,
    # и различие это инвариант проекта: подстановка нуля запрещена, а ноль
    # здесь означал бы операционный убыток — утверждение об эмитенте,
    # сделанное по величине, которой он не раскрыл. Прежде запрос отдавал
    # такую строку наравне с раскрытой, и пересчёт истории спотыкался о неё
    # на первом же периоде, где строка не раскрыта.
    if not found or found[0]["value"] is None:
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


def interim_base(
    inn: str,
    standard: Standard | None,
    annual: date | None,
    interim_latest: Mapping[tuple[str, str], tuple[date, bool]],
) -> tuple[date, bool] | None:
    """Промежуточный комплект, становящийся базой; None — база годовая.

    **Стандарт у базы один**: кандидат берётся того же стандарта, что выбран
    правилом предпочтения, и промежуточный РСБУ базой эмитента с годовой
    МСФО не становится — ряды несопоставимы. **Базой он становится, только
    если новее годового**: годовой того же года, раскрытый позже, новее
    любого промежуточного этого года и возвращает базу себе.
    """
    if standard is None or annual is None:
        return None
    found = interim_latest.get((inn, standard.value))
    if found is None or found[0] <= annual:
        return None
    return found


_TREND = """
SELECT DISTINCT ON (f.report_date, f.line_code) f.report_date, f.line_code, f.value,
       s.unit_code
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.line_code = ANY(%(codes)s)
  AND s.is_actual AND s.status <> 'quarantine'
ORDER BY f.report_date, f.line_code, source_rank(s.source)
"""


@dataclass(frozen=True, slots=True)
class TrendPoint:
    """Отчётная дата тренда: LTM по строкам и динамика с начала года."""

    moment: date
    ltm: dict[str, Rolling]
    # Изменение с начала года к тому же периоду прошлого года, долей;
    # None — нет прошлогодней величины либо она неположительна.
    ytd_change: dict[str, Decimal | None]
    # Единица рядов (код ОКЕИ): последнего комплекта, к которой приведены
    # все слагаемые (`trend_series`). Печатающий переводит из неё.
    unit_code: str | None = None


def trend_series(rows: list[dict], codes: list[str]) -> dict[str, dict[date, Decimal | None]]:
    """Ряды строк тренда по датам — в единице последнего комплекта.

    **Единица у одного эмитента меняется**: у Брусники комплекты агрегатора
    до 30.06.2024 в тысячах, дальше в миллионах, и LTM на 30.06.2025
    складывал 35 985 млн с 31 359 436 тыс. — выходило −31 247 619. Печатается
    тренд в единице шапки документа, то есть последнего комплекта. Единица
    не известна — величина не сравнима ни с чем, и её нет.
    """
    from finlib.metrics.interim import in_unit

    series: dict[str, dict[date, Decimal | None]] = {code: {} for code in codes}
    if not rows:
        return series
    target = trend_unit(rows)
    for row in rows:
        series[row["line_code"]][row["report_date"]] = in_unit(
            row["value"], row["unit_code"], target
        )
    return series


def trend_unit(rows: list[dict]) -> str | None:
    """Единица рядов тренда: последнего комплекта; рядов нет — единицы нет."""
    if not rows:
        return None
    return str(max(rows, key=lambda row: row["report_date"])["unit_code"])


def ltm_trend(inn: str, standard: Standard, conn: PgConnection) -> list[TrendPoint]:
    """Тренд LTM на последние отчётные даты (`interim.yaml`, `trend`).

    Величины — тем же тождеством, что база маршрута (`metrics.interim`),
    и из той же выборки фактов: с предпочтением первоисточника, вне карантина.
    """
    from finlib.metrics.interim import rolling_flow, same_ytd_year_before
    from finlib.scoring.interim import load_interim

    rule = load_interim().trend
    codes = sorted(rule.lines.get(standard.value, {}))
    if not codes:
        return []
    found = fetch_all(
        _TREND, {"inn": inn, "standard": standard.value, "codes": codes}, conn=conn
    )
    unit_code = trend_unit(found)
    series = trend_series(found, codes)
    dates = sorted({day for values in series.values() for day in values}, reverse=True)
    points: list[TrendPoint] = []
    for moment in dates[: rule.quarters]:
        change: dict[str, Decimal | None] = {}
        for code in codes:
            now = series[code].get(moment)
            before = series[code].get(same_ytd_year_before(moment))
            change[code] = (
                now / before - 1 if now is not None and before and before > 0 else None
            )
        points.append(
            TrendPoint(
                moment,
                {code: rolling_flow(series[code], moment) for code in codes},
                change,
                unit_code,
            )
        )
    return points


def _operating_ltm(
    inn: str, moment: date, conn: PgConnection, standard: Standard
) -> Rolling:
    """Операционный результат за скользящие двенадцать месяцев на эту дату.

    Три слагаемых тождества берутся тем же правилом, что операционный
    результат периода (`_operating_profit`): ноль агрегатора величиной
    не считается, и слагаемое без величины отменяет сумму целиком.
    На годовую дату — годовая величина как есть.
    """
    from finlib.metrics.interim import in_unit, rolling_flow, same_ytd_year_before

    days = {moment, date(moment.year - 1, 12, 31), same_ytd_year_before(moment)}
    code = catalogue_for(standard).rule.operating_line
    units = {}
    for day in days:
        found = fetch_all(
            _LINE, {"inn": inn, "d": day, "code": code, "standard": standard.value}, conn=conn
        )
        units[day] = found[0]["unit_code"] if found else None
    # Слагаемые — в единице комплекта на дату базы (`metrics.interim.in_unit`).
    return rolling_flow(
        {
            day: in_unit(_operating_profit(inn, day, conn, standard), units[day], units[moment])
            for day in days
        },
        moment,
    )


def _rsbu_inputs(
    inn: str, moment: date, conn: PgConnection, *, ltm: bool
) -> tuple[tuple[MetricValue, ...], dict[str, str], str]:
    """Показатели, сработавшие стоп-факторы и вид деятельности эмитента РСБУ.

    Величины считает боевой расчёт, стоп-факторы — та же функция, что
    и при оценке. Здесь только сборка: перечень, написанный второй раз,
    разошёлся бы с первым. `ltm` — база на промежуточную дату: потоки
    за скользящие двенадцать месяцев, баланс на дату. Довод обязательный:
    молча не переданная база неотличима от годовой.
    """
    from finlib.metrics.definitions import load_metrics
    from finlib.scoring.definitions import load_scoring
    from finlib.scoring.engine import triggered_stop_factors
    from finlib.scoring.rsbu_routing import computed_ltm, computed_of, with_denominator

    if ltm:
        computed, _ = computed_ltm(inn, moment, conn)
        profit = _operating_ltm(inn, moment, conn, Standard.RSBU).value
    else:
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
