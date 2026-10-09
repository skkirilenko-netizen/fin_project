"""Диагностика расхождений маршрута: рынок, SPV, объём в обращении, статус карточки.

    uv run python eval/audit_diag.py all [--days 10] [--tolerance 0.05]
        [--as-of ГГГГ-ММ-ДД] [--out ПУТЬ] [--focus ИМЯ=ИНН ...]
    uv run python eval/audit_diag.py market-gaps | spv-coverage | outstanding | card-status

**Только диагностика.** Скрипт ничего не исправляет и в базу не пишет:
маршрут читается из `routing_day` (последняя дата либо `--as-of`), строки
маршрута на сегодня для `spv-coverage` строит боевой путь (`routing_rows`)
с откатом транзакции; запуск на рабочей базе — под
`PGOPTIONS='-c default_transaction_read_only=on'`
(`~/Work/fin-runs/audit-diag/run.sh`). Прочее — с диска: карточки
и выпуски Cbonds, рыночный ряд и срезы биржи.

Итог — `data/output/audit_diag_<дата>.xlsx`, по листу на подкоманду
и «Сводка»; существующий файл не перезаписывается. Сводка печатается.

**Фокус обязателен к разбору** (поручение владельца 09.10.2026): Газпром,
ПИК, НОВАТЭК — в `market-gaps`; Газпром Капитал — в `spv-coverage`;
Мираторг Финанс — в `outstanding`; ЛОЭСК — в `card-status`. Эмитент фокуса
попадает на лист, даже если расхождения у него нет, и тогда это сказано.
Опознание — по наименованию карточки; не опознан либо опознан неоднозначно —
строка «не опознан» с кандидатами, а не молчание (`--focus ИМЯ=ИНН` снимает
неоднозначность).

**Пороги здесь — параметры диагностики, а не методики**: окно `--days`
и допуск `--tolerance` названы владельцем в поручении и в маршрут не идут.
"""

import argparse
import json
import logging
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from finlib.db import connection, fetch_all
from finlib.scoring.routing import RoutingPolicy, load_routing
from finlib.scoring.routing_store import _outstanding, cards, exclusions, routing_rows
from finlib.sources import cbonds, moex
from finlib.sources.cbonds import bond_issuers, outstanding_universe
from finlib.sources.cbonds_events import issues_of
from finlib.sources.market import SERIES, Market, excluded, holders, load_market, series

logger = logging.getLogger(__name__)

OUT = Path("data/output")
COMMANDS = ("market-gaps", "spv-coverage", "outstanding", "card-status")
# Фокус по подкоманде: наименование, как его ищут в карточке.
FOCUS: dict[str, tuple[str, ...]] = {
    "market-gaps": ("Газпром", "ПИК", "НОВАТЭК"),
    "spv-coverage": ("Газпром Капитал",),
    "outstanding": ("Мираторг Финанс",),
    "card-status": ("ЛОЭСК",),
}
# Статусы выпуска, которые маршрут складывает в объём в обращении
# (`routing_store._outstanding`): повторены здесь только для показа графы.
ROUTE_STATUSES = ("в обращении", "размещается")

_ROUTE = """
SELECT inn, basket, subgroup, grounds, grounds_all, as_of
FROM routing_day
WHERE as_of = COALESCE(%(as_of)s, (SELECT max(as_of) FROM routing_day))
"""

_FORMS = re.compile(r"^(ПАО|ОАО|АО|ЗАО|ООО|НАО|ПК|МКПАО|МФК|СЗ|СФО)\s+", re.IGNORECASE)


# --- опознание фокуса ---------------------------------------------------------


def bare_name(name: str) -> str:
    """Наименование без кавычек и организационно-правовой формы, в нижнем регистре."""
    said = re.sub(r"[«»\"'“”]", "", name or "").strip()
    said = _FORMS.sub("", said).strip()
    return re.sub(r"\s+", " ", said).lower()


def focus_inns(
    known: dict[str, dict], wanted: Iterable[str], given: dict[str, str]
) -> dict[str, tuple[list[str], list[str]]]:
    """Наименование фокуса → (опознанные ИНН, кандидаты при неоднозначности).

    Опознан — ровно одна карточка с тем же наименованием без формы. Иначе
    опознанных нет, а кандидаты — карточки, где наименование входит
    подстрокой: «Газпром» не должен молча стать «Газпром нефтью».
    """
    found: dict[str, tuple[list[str], list[str]]] = {}
    for name in wanted:
        if name in given:
            found[name] = ([given[name]], [])
            continue
        key = bare_name(name)
        exact = sorted(
            inn for inn, card in known.items() if bare_name(str(card.get("name_rus"))) == key
        )
        if len(exact) == 1:
            found[name] = (exact, [])
            continue
        near = sorted(
            inn for inn, card in known.items() if key in bare_name(str(card.get("name_rus")))
        )
        found[name] = ([], exact or near)
    return found


# --- market-gaps --------------------------------------------------------------


@dataclass
class IsinState:
    """Бумага эмитента в срезах биржи за окно: есть ли строки, сделки, цена."""

    isin: str
    status: str
    maturity: date | None
    mapped: bool
    rows: int = 0
    traded: int = 0
    priced: int = 0
    last_seen: date | None = None
    last_trade: date | None = None
    refused: set[str] = field(default_factory=set)


def gap_reason(
    states: list[IsinState],
    slices_behind: int,
    days: int,
    unbenchmarked: int,
) -> str:
    """Причина молчания рынка у эмитента, если её видно; первая подходящая.

    Порядок — от общего к частному: доставка срезов отстаёт у всех;
    выпусков в обращении нет; ISIN нет; бумаг нет в срезах; сделок нет;
    цена отброшена правилом сравнимости; дни без ориентира; иначе — ряд
    не пересобран после доставки.
    """
    if slices_behind > days:
        return f"сбой загрузки: последний срез биржи {slices_behind} раб. дн. назад"
    live = [item for item in states if item.status in ROUTE_STATUSES]
    if not live:
        said = sorted({item.status or "статус не назван" for item in states})
        return "выпусков в обращении нет" + (f" ({', '.join(said)})" if said else "")
    if not any(item.isin for item in live):
        return "у выпусков в обращении нет ISIN"
    if not any(item.mapped for item in live if item.isin):
        return "ISIN не сопоставлен эмитенту (holders): выпуски не в справочнике"
    if not any(item.rows for item in live):
        return "бумаг нет в срезах биржи за окно (не торгуются на бирже)"
    if not any(item.traded for item in live):
        return "в срезах есть, сделок нет"
    if not any(item.priced for item in live):
        codes = sorted({code for item in live for code in item.refused})
        return "цена отброшена правилом сравнимости: " + (", ".join(codes) or "код не назван")
    if unbenchmarked:
        return f"дней без ориентира в окне: {unbenchmarked} — точки дня не пишутся"
    return "сделки с ценой есть, точки нет: ряд не пересобран после доставки?"


def _weekdays_between(start: date, end: date) -> int:
    """Рабочих дней после `start` до `end` включительно; праздники не учитываются."""
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        count += day.weekday() < 5
    return count


def _slices(window: int) -> list[tuple[date, Path]]:
    """Последние срезы биржи с диска: день и файл."""
    found = []
    for path in moex.CACHE.glob("xsec_*.json"):
        if "_p" in path.name:
            continue
        try:
            found.append((date.fromisoformat(path.name[len("xsec_") : -len(".json")]), path))
        except ValueError:
            continue
    return sorted(found)[-window:]


def market_gaps(
    route: list[dict], known: dict[str, dict], focus: set[str], days: int, as_of: date
) -> tuple[list[list], list[list], dict[str, object]]:
    """Эмитенты маршрута с последней рыночной точкой старше окна; фокус — всегда."""
    policy = load_market()
    market: Market = series()
    mine = holders()
    calendar = market.calendar()
    last_bench = calendar[-1] if calendar else None
    window = _slices(max(days * 3, 30))
    slices_behind = _weekdays_between(window[-1][0], as_of) if window else 10**6
    states: dict[str, dict[str, IsinState]] = {}
    for item in route:
        issues, _ = issues_of(item["inn"])
        states[item["inn"]] = {
            issue.isin or f"без ISIN: {issue.name}": IsinState(
                isin=issue.isin,
                status=issue.status,
                maturity=issue.maturity,
                mapped=bool(issue.isin) and mine.get(issue.isin) == item["inn"],
            )
            for issue in issues
        }
    by_isin = {
        isin: state for own in states.values() for isin, state in own.items() if state.isin
    }
    for day, path in window:
        for row in json.loads(path.read_text(encoding="utf-8")).get("history") or []:
            state = by_isin.get(str(row.get("SECID") or ""))
            if state is None:
                continue
            state.rows += 1
            state.last_seen = max(filter(None, (state.last_seen, day)))
            if (Decimal(str(row.get("NUMTRADES") or 0))) > 0:
                state.traded += 1
                state.last_trade = max(filter(None, (state.last_trade, day)))
                if row.get("LEGALCLOSEPRICE") or row.get("CLOSE"):
                    if refused := excluded(row, policy, "price"):
                        state.refused.add(refused)
                    else:
                        state.priced += 1
    in_window = {day for day, _ in window}
    unbenchmarked = len(in_window - set(market.benchmark))
    rows: list[list] = []
    detail: list[list] = []
    gaps = 0
    reasons: dict[str, int] = {}
    for item in sorted(route, key=lambda entry: entry["inn"]):
        inn = item["inn"]
        points = market.ordered(inn)
        last = points[-1].day if points else None
        age = (
            market.day_number(last_bench) - market.day_number(last)
            if last is not None and last_bench is not None
            else None
        )
        gap = age is None or age > days
        if not gap and inn not in focus:
            continue
        own = list(states[inn].values())
        reason = gap_reason(own, slices_behind, days, unbenchmarked) if gap else "разрыва нет"
        if gap:
            gaps += 1
            head = reason.split(":")[0]
            reasons[head] = reasons.get(head, 0) + 1
        rows.append([
            inn,
            str(known.get(inn, {}).get("name_rus") or ""),
            "да" if inn in focus else "",
            item["basket"],
            last,
            age if age is not None else "точек нет",
            max((state.last_trade for state in own if state.last_trade), default=None),
            ", ".join(state.isin for state in own if state.status in ROUTE_STATUSES and state.isin),
            reason,
            market.silence(inn) if not points else "",
        ])
        for state in own:
            detail.append([
                inn,
                state.isin or "—",
                state.status,
                state.maturity,
                "да" if state.mapped else "нет",
                state.rows,
                state.traded,
                state.priced,
                state.last_seen,
                state.last_trade,
                ", ".join(sorted(state.refused)),
            ])
    summary = {
        "окно, торговых дней": days,
        "последний день с ориентиром": last_bench,
        "последний срез биржи": window[-1][0] if window else "срезов нет",
        "эмитентов в маршруте": len(route),
        "с разрывом рынка": gaps,
        **{f"причина: {key}": value for key, value in sorted(reasons.items())},
    }
    return rows, detail, summary


# --- spv-coverage -------------------------------------------------------------


def _raw_guarantors(inn: str) -> tuple[list[dict], bool]:
    """Записи поручительств с диска, все статусы; второе — файл есть."""
    path = cbonds.CACHE / f"guarantors_{inn}.json"
    if not path.exists():
        return [], False
    return json.loads(path.read_text(encoding="utf-8")).get("items", []), True


def coverage_verdict(
    spv: bool,
    raw: list[dict],
    delivered: bool,
    accepted: Iterable[str],
    listed: set[str],
    led: bool,
) -> str:
    """Учтено ли покрытие в маршруте; пусто — учтено.

    Различаются: файла поручительств нет (не доставлено), записи есть, но
    вида, которого методика не берёт (оферент), поручитель без ИНН,
    поручитель не в списке, и SPV, чья корзина не взята у поручителя.
    """
    taken = set(accepted)
    if not delivered:
        return "поручительства не доставлены: файла нет"
    backing = [item for item in raw if str(item.get("status_name_rus") or "").strip() in taken]
    if not backing:
        if not raw:
            return "поручителя нет у источника" if spv else ""
        kinds = sorted({str(item.get("status_name_rus") or "").strip() for item in raw})
        return f"записи есть, но вида, который маршрут не берёт: {', '.join(kinds)}"
    inns = {str(item.get("guarantor_inn") or "").strip() for item in backing}
    if inns == {""}:
        return "поручитель без ИНН: сопоставить не с кем"
    if not inns & listed:
        return "поручитель вне списка маршрута"
    if spv and not led:
        return "SPV: корзина не взята у поручителя"
    return ""


def spv_coverage(
    known: dict[str, dict], focus: set[str], routing: RoutingPolicy, today: date
) -> tuple[list[list], dict[str, object]]:
    """SPV и эмитенты с поручительством: назван ли поручитель, учтён ли он маршрутом."""
    with connection() as conn:
        rows, _ = routing_rows(conn, today)
        conn.rollback()
    by_inn = {item.inn: item for item in rows}
    listed = set(by_inn)
    accepted = routing.events.guarantee_statuses
    found: list[list] = []
    counts = {"SPV в маршруте": 0, "с поручительством": 0, "покрытие не учтено": 0}
    for item in sorted(rows, key=lambda entry: entry.inn):
        spv = str(known.get(item.inn, {}).get("emitent_spv")) == "1"
        raw, delivered = _raw_guarantors(item.inn)
        if not spv and not item.guarantees and item.inn not in focus:
            continue
        counts["SPV в маршруте"] += spv
        counts["с поручительством"] += bool(item.guarantees)
        backing = [by_inn[entry.inn] for entry in item.guarantees if entry.inn in by_inn]
        # Корзина взята у поручителя — предмет основания SPV назван его именем
        # (`led_by_guarantor`); своя очередь типа называет код типа.
        led = any(
            finding.ground == "financing_structure"
            and finding.subject in {entry.name for entry in backing}
            for finding in item.verdict.findings
        )
        verdict = coverage_verdict(spv, raw, delivered, accepted, listed, led)
        counts["покрытие не учтено"] += bool(verdict)
        found.append([
            item.inn,
            item.name,
            "да" if item.inn in focus else "",
            "да" if spv else "нет",
            "; ".join(f"{entry.name} ({entry.inn or 'ИНН нет'}, {entry.status})"
                      for entry in item.guarantees) or "не указан",
            "; ".join(sorted({str(entry.get("status_name_rus") or "") for entry in raw})),
            "; ".join(f"{entry.name}: {entry.verdict.basket}" for entry in backing),
            item.verdict.basket,
            "да" if led else "нет",
            verdict or "учтено",
        ])
    return found, {**counts, "строк маршрута на сегодня": len(rows)}


# --- outstanding --------------------------------------------------------------


def _number(value: object) -> Decimal | None:
    """Число поля источника; пусто и нечисло — None."""
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


def share_gap(base: Decimal | None, other: Decimal | None) -> Decimal | None:
    """Относительное расхождение к `base`; None — сравнивать нечего."""
    if base is None or other is None:
        return None
    if base == 0:
        return None if other == 0 else Decimal(1)
    return abs(other - base) / abs(base)


@dataclass
class Volumes:
    """Объём в обращении эмитента тремя способами и оговорки к ним."""

    route: Decimal | None
    by_issues: Decimal | None
    amortized: Decimal | None
    universe: Decimal | None
    foreign: list[str]
    without_volume: int
    only_issuer: list[str]
    only_universe: list[str]


def volumes_of(
    issues: list[dict], universe_rows: list[dict], route: Decimal | None, repaid: Iterable[str]
) -> Volumes:
    """Объём по выпускам эмитента и по перечню источника рядом с маршрутом.

    `by_issues` — сумма `outstanding_volume` по всем непогашенным выпускам
    эмитента (маршрут берёт только «в обращении» и «размещается»);
    `amortized` — та же сумма с текущим номиналом вместо исходного там,
    где источник называет оба (оценка амортизации, не правило);
    `universe` — сумма по перечню выпусков в обращении источника.
    """
    gone = set(repaid)
    live = [
        item for item in issues
        if str(item.get("status_name_rus") or "").strip().lower() not in gone
    ]
    total = amortized = Decimal(0)
    seen = without = 0
    foreign = []
    for item in live:
        volume = _number(item.get("outstanding_volume"))
        if volume is None:
            without += 1
            continue
        seen += 1
        total += volume
        now = _number(item.get("outstanding_nominal_price"))
        was = _number(item.get("nominal_price"))
        amortized += volume * now / was if now is not None and was else volume
        currency = str(item.get("currency_name") or "").strip()
        if currency and currency.upper() not in ("RUB", "РУБ", "RUR"):
            foreign.append(f"{item.get('isin_code') or item.get('id')}: {currency}")
    mine = {str(item.get("isin_code") or "") for item in issues if item.get("isin_code")}
    theirs = {str(item.get("isin_code") or "") for item in universe_rows if item.get("isin_code")}
    in_route = {
        str(item.get("isin_code") or "")
        for item in issues
        if str(item.get("status_name_rus") or "").strip().lower() in ROUTE_STATUSES
    }
    listed = [_number(item.get("outstanding_volume")) for item in universe_rows]
    return Volumes(
        route=route,
        by_issues=total if seen else None,
        amortized=amortized if seen else None,
        universe=sum((item for item in listed if item is not None), Decimal(0))
        if any(item is not None for item in listed)
        else None,
        foreign=foreign,
        without_volume=without,
        only_issuer=sorted(in_route - theirs - {""}),
        only_universe=sorted(theirs - mine),
    )


def outstanding(
    route: list[dict], known: dict[str, dict], focus: set[str], tolerance: Decimal,
    routing: RoutingPolicy,
) -> tuple[list[list], dict[str, object]]:
    """Объём в обращении: маршрут против суммы по выпускам и против перечня Cbonds."""
    by_inn: dict[str, list[dict]] = {}
    for item in outstanding_universe():
        by_inn.setdefault(str(item.get("emitent_inn") or "").strip(), []).append(item)
    found: list[list] = []
    off = 0
    for item in sorted(route, key=lambda entry: entry["inn"]):
        inn = item["inn"]
        path = cbonds.CACHE / f"emissions_{inn}.json"
        issues = (
            json.loads(path.read_text(encoding="utf-8")).get("items", []) if path.exists() else []
        )
        got = volumes_of(issues, by_inn.get(inn, []), _outstanding(inn),
                         routing.events.repaid_statuses)
        gaps = [share_gap(got.route, other) for other in (got.by_issues, got.universe)]
        worst = max((gap for gap in gaps if gap is not None), default=None)
        diverged = worst is not None and worst > tolerance
        off += diverged
        if not diverged and inn not in focus and not got.foreign:
            continue
        found.append([
            inn,
            str(known.get(inn, {}).get("name_rus") or ""),
            "да" if inn in focus else "",
            got.route,
            got.by_issues,
            got.amortized,
            got.universe,
            gaps[0],
            gaps[1],
            "да" if diverged else ("разрыва нет" if worst is not None else "сравнивать нечего"),
            "; ".join(got.foreign),
            got.without_volume,
            ", ".join(got.only_issuer),
            ", ".join(got.only_universe),
            "" if path.exists() else "выпусков эмитента на диске нет",
        ])
    return found, {"эмитентов в маршруте": len(route), f"расхождение > {tolerance:%}": off}


# --- card-status --------------------------------------------------------------


def card_mismatch(
    status: str | None, basket: str | None, routing: RoutingPolicy, successor: bool
) -> str:
    """Расхождение статуса карточки с корзиной маршрута; пусто — согласованы.

    `basket` None — эмитента нет в маршруте. Маршрут сам смотрит лишь
    на «ликвидирована» (выход с преемником) и на неопознанный статус
    (очередь статуса); прочие недействующие статусы корзину не называют,
    и «Без внимания» при них — расхождение, банкротство не в «Разборе» —
    тоже.
    """
    universe = routing.universe
    unknown = routing.universe.unconfirmed_to
    if status is None:
        return "карточки нет" if basket is not None else ""
    name = universe.status_of(status)
    if not universe.known_status(status):
        return "" if basket == unknown else f"статус «{name}», а корзина {basket or 'нет'}"
    if status in universe.exclude_statuses:
        if successor:
            return "" if basket is None else f"«{name}» с преемником, а в маршруте: {basket}"
        return "" if basket == unknown else f"«{name}» без преемника, а корзина {basket or 'нет'}"
    if basket is None:
        return f"«{name}», а в маршруте эмитента нет"
    if name == "действующая":
        return "" if basket != unknown else "«действующая», а корзина — очередь статуса"
    if "банкрот" in name and basket != "review":
        return f"«{name}», а корзина {basket}"
    if basket == "clear":
        return f"«{name}», а корзина «Без внимания»"
    return ""


def card_status(
    route: list[dict], known: dict[str, dict], focus: set[str], routing: RoutingPolicy
) -> tuple[list[list], dict[str, object]]:
    """Статус эмитента в карточке против корзины последнего маршрута."""
    basket = {item["inn"]: item["basket"] for item in route}
    skip, _ = exclusions(known, routing)
    by_id = {str(card.get("id")): card for card in known.values()}
    subjects = sorted(set(basket) | set(bond_issuers()) | focus)
    found: list[list] = []
    counts: dict[str, int] = {}
    for inn in subjects:
        card = known.get(inn)
        status = str(card.get("emitent_statuses_id") or "") if card is not None else None
        target = str((card or {}).get("emitents_id_absorption") or "").strip()
        why = card_mismatch(status, basket.get(inn), routing, target in by_id)
        if not why and inn not in focus:
            continue
        if why:
            head = why.split(",")[0]
            counts[head] = counts.get(head, 0) + 1
        found.append([
            inn,
            str((card or {}).get("name_rus") or ""),
            "да" if inn in focus else "",
            routing.universe.status_of(status) if status is not None else "карточки нет",
            str((card or {}).get("updating_date") or "")[:10],
            basket.get(inn) or "нет в маршруте",
            skip[inn].reason if inn in skip else "",
            why or "согласовано",
        ])
    return found, {
        "проверено эмитентов": len(subjects),
        "расхождений": sum(counts.values()),
    }


# --- выгрузка -----------------------------------------------------------------

HEADERS = {
    "market-gaps": ("ИНН", "наименование", "фокус", "корзина", "последняя точка ряда",
                    "торговых дней назад", "последняя сделка в окне", "ISIN в обращении",
                    "причина", "перепись ряда"),
    "market-gaps выпуски": ("ИНН", "ISIN", "статус выпуска", "погашение", "сопоставлен",
                            "строк в окне", "дней со сделками", "дней с ценой",
                            "последний день в срезах", "последняя сделка",
                            "цена отброшена правилом"),
    "spv-coverage": ("ИНН", "наименование", "фокус", "SPV", "поручители (маршрут)",
                     "виды записей у источника", "корзина поручителя", "корзина",
                     "корзина взята у поручителя", "вывод"),
    "outstanding": ("ИНН", "наименование", "фокус", "маршрут", "по выпускам (непогашенные)",
                    "по выпускам, текущий номинал (оценка)", "перечень Cbonds в обращении",
                    "расхождение: выпуски", "расхождение: Cbonds", "выше допуска",
                    "не в рублях", "выпусков без объёма", "только у эмитента",
                    "только в перечне", "оговорка"),
    "card-status": ("ИНН", "наименование", "фокус", "статус карточки", "карточка обновлена",
                    "корзина", "вышел из списка", "расхождение"),
}


def focus_rows(
    command: str, matched: dict[str, tuple[list[str], list[str]]]
) -> list[list]:
    """Строки фокуса, который не опознан: наименование и кандидаты."""
    width = len(HEADERS[command])
    return [
        ["—", name, "не опознан", *([""] * (width - 4)),
         "кандидаты: " + (", ".join(near) or "нет")]
        for name, (inns, near) in matched.items()
        if not inns
    ]


def write_book(path: Path, sheets: dict[str, list[list]], summary: list[tuple]) -> None:
    """Книга: «Сводка» первой, затем лист на подкоманду."""
    book = Workbook()
    head = book.active
    head.title = "Сводка"
    for line in summary:
        head.append(list(line))
    for name, rows in sheets.items():
        sheet = book.create_sheet(name[:31])
        sheet.append(list(HEADERS[name]))
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append([str(value) if isinstance(value, Decimal) else value for value in row])
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)


def main(argv: list[str] | None = None) -> int:
    """Разбор доводов, подкоманды, книга и сводка."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=(*COMMANDS, "all"))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--tolerance", type=Decimal, default=Decimal("0.05"))
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--focus", action="append", default=[], metavar="ИМЯ=ИНН")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    today = date.today()
    out = args.out or OUT / f"audit_diag_{today:%Y-%m-%d}.xlsx"
    if out.exists():
        print(f"{out} уже есть: не перезаписываю", file=sys.stderr)
        return 1
    # **В сеть диагностика не ходит**: без перечня на диске `outstanding_universe`
    # спросил бы Cbonds, а это уже доставка, а не диагностика.
    if not (cbonds.CACHE / "emissions_ru_outstanding.json").exists():
        print("перечня выпусков Cbonds на диске нет: диагностика в сеть не ходит",
              file=sys.stderr)
        return 1
    # Без ряда на диске `series()` пересобрал бы его и записал — диагностика
    # ничего не пишет, кроме своей книги.
    if not SERIES.exists():
        print(f"рыночного ряда на диске нет ({SERIES}): диагностика его не строит",
              file=sys.stderr)
        return 1
    given = dict(item.split("=", 1) for item in args.focus)
    routing = load_routing()
    known = cards()
    with connection() as conn:
        route = fetch_all(_ROUTE, {"as_of": args.as_of}, conn=conn)
        conn.rollback()
    if not route:
        print("в routing_day нет точек на эту дату: диагностировать нечего", file=sys.stderr)
        return 1
    as_of = route[0]["as_of"]
    chosen = COMMANDS if args.command == "all" else (args.command,)
    sheets: dict[str, list[list]] = {}
    summary: list[tuple] = [
        ("Диагностика", f"{today:%d.%m.%Y}"),
        ("Маршрут (routing_day) на", f"{as_of:%d.%m.%Y}"),
        ("Эмитентов в маршруте", len(route)),
        ("Только чтение", "в базу не пишется ничего"),
    ]
    for command in chosen:
        matched = focus_inns(known, FOCUS[command], given)
        focus = {inn for inns, _ in matched.values() for inn in inns}
        summary.append(("", ""))
        summary.append((command, "фокус: " + "; ".join(
            f"{name} — {', '.join(inns) or 'не опознан'}" for name, (inns, _) in matched.items()
        )))
        if command == "market-gaps":
            rows, detail, counts = market_gaps(route, known, focus, args.days, as_of)
            sheets["market-gaps"] = rows + focus_rows(command, matched)
            sheets["market-gaps выпуски"] = detail
        elif command == "spv-coverage":
            rows, counts = spv_coverage(known, focus, routing, today)
            sheets[command] = rows + focus_rows(command, matched)
        elif command == "outstanding":
            rows, counts = outstanding(route, known, focus, args.tolerance, routing)
            sheets[command] = rows + focus_rows(command, matched)
        else:
            rows, counts = card_status(route, known, focus, routing)
            sheets[command] = rows + focus_rows(command, matched)
        summary.extend((f"  {key}", value) for key, value in counts.items())
    write_book(out, sheets, summary)
    for key, value in summary:
        print(f"{key}: {value}" if key else "")
    print(f"\nкнига: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
