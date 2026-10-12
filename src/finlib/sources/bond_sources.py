"""Выпуски эмитента в обращении по каждому источнику с диска: файл, перечень, срез, поручительство.

**Источник один — ошибка одна** (no-bonds-impact 09.10.2026). Файл выпусков
эмитента (`emissions_<ИНН>.json`) устаревает: новый выпуск в нём не появляется,
и у ПИК-СЗ с выпусками в обращении он говорил «выпусков нет». Поэтому число
выпусков считается у каждого источника порознь — файл эмитента, перечень
Cbonds в обращении, последний срез биржи не позже дня, — а у поручителя ещё
и выпуски в обращении тех, по чьему долгу он отвечает: у финансирующей
структуры выпуски свои, а отвечает поручитель.

**Решения здесь нет — только счёт.** Какие статусы выпуска считать «в
обращении» и какие виды поручительства — отвечающими по долгу, называет
вызывающий из своего справочника: модуль общий для маршрута и пакета
экспертной проверки, и перечень статусов у них не обязан совпадать.

Только диск: ни Cbonds, ни биржа не спрашиваются (`cbonds.fetch` берёт
перечень из кэша; кэша нет — его отказ поднимается как есть).
"""

import contextlib
import json
from collections.abc import Iterable
from datetime import date

from finlib.sources import moex
from finlib.sources.cbonds import outstanding_universe
from finlib.sources.cbonds_events import guarantees_of, issues_of

OWN_FILE = "файл эмитента"
LISTED = "перечень Cbonds"
BOARD = "срез биржи"
BACKING = "поручительство"
BOND_SOURCES = (OWN_FILE, LISTED, BOARD, BACKING)


def board_isins(day: date) -> set[str]:
    """Бумаги последнего среза биржи не позже дня; срезов нет — пусто."""
    found = []
    for path in moex.CACHE.glob("xsec_*.json"):
        if "_p" in path.name:
            continue
        with contextlib.suppress(ValueError):
            on = date.fromisoformat(path.name[len("xsec_") : -len(".json")])
            if on <= day:
                found.append((on, path))
    if not found:
        return set()
    rows = json.loads(max(found)[1].read_text(encoding="utf-8")).get("history") or []
    return {str(row.get("SECID") or "") for row in rows} - {""}


def bond_sources(
    inns: Iterable[str],
    day: date,
    statuses: Iterable[str],
    guarantee_statuses: Iterable[str],
) -> dict[str, dict[str, int]]:
    """Число выпусков эмитента в обращении по каждому источнику (`BOND_SOURCES`).

    Файл эмитента — выпуски в названных статусах; перечень Cbonds — ISIN
    эмитента в перечне в обращении; срез — ISIN эмитента (из файла и перечня),
    торговавшиеся в последний срез не позже дня. Поручительство — число
    поручительств названных видов за эмитентов, у которых хоть один источник
    выпуски видит; самопоручительство не в счёт.
    """
    wanted = set(statuses)
    accepted = frozenset(guarantee_statuses)
    listed: dict[str, set[str]] = {}
    for row in outstanding_universe():
        listed.setdefault(str(row.get("emitent_inn") or "").strip(), set()).add(
            str(row.get("isin_code") or "").strip()
        )
    board = board_isins(day)
    counts: dict[str, dict[str, int]] = {}
    for inn in inns:
        issues, _ = issues_of(inn)
        own = {item.isin or item.emission_id for item in issues if item.status in wanted}
        known = {item.isin for item in issues if item.isin} | listed.get(inn, set())
        counts[inn] = {
            OWN_FILE: len(own),
            LISTED: len(listed.get(inn, set())),
            BOARD: len(known & board),
            BACKING: 0,
        }
    for inn, said in list(counts.items()):
        if not any(said[name] for name in (OWN_FILE, LISTED, BOARD)):
            continue
        for item in guarantees_of(inn, accepted):
            if item.inn and item.inn != inn:
                counts.setdefault(item.inn, dict.fromkeys(BOND_SOURCES, 0))[BACKING] += 1
    return counts
