"""Разведка торгов: есть ли котировки, за какой срок и виден ли сигнал.

    uv run python eval/cbonds_trades_probe.py [--sample 60]

**Правил из этого не делается.** Разведка отвечает на три вопроса и на них
останавливается: каким методом приходят котировки, у скольких выпусков списка
они есть за последний месяц и был ли рыночный сигнал до дефолта у ЕвроТранса
и Кириллицы. Порог, признак и корзина — решение человека.

**Котировки не в `get_emissions`.** Из 209 полей записи выпуска торгов
касаются только площадки и объём размещения. Отвечает `get_tradings_new`
(87 полей: цена покупки и продажи, средняя, спред, доходности, дюрация,
оборот); `get_tradings` существует, но доступа к нему нет; шесть прочих
угаданных имён источник не знает.

**Глубина хранения — около сорока дней, и это её свойство, а не наше.**
Отбор по дате окна не расширяет: на 01.03.2026 метод отдаёт те же записи,
что и без отбора. Значит, история торгов — такой же случай, как история
рейтингов: её не будет, пока не начнём снимать снимки.

**Выборка называется числом.** «У N выпусков котировки есть» без знаменателя
не значит ничего, а спросить обо всех 1 207 — это 1 207 запросов по тридцать
в минуту.
"""

import json
import logging
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402

logger = logging.getLogger(__name__)

CACHE = Path("data/raw/cbonds")
METHOD = "get_tradings_new"
WANTED = ("в обращении", "размещается")
# Эмитенты записки: у первого дефолт признаком карточки, у второго — статусом
# выпуска с погашением 22.08.2026.
NAMED: dict[str, str] = {
    "5029169023": "ЕвроТранс",
    "4004021785": "Кириллица",
}


def quotes(emission: str) -> list[dict] | None:
    """Котировки выпуска; None — ответа нет, причина в журнале."""
    try:
        found = cbonds.fetch(
            METHOD,
            f"tradings_{emission}",
            filters=({"field": "emission_id", "operator": "eq", "value": emission},),
            limit=1000,
        )
    except cbonds.CbondsError as failure:
        logger.error("%s %s: %s", METHOD, emission, str(failure)[:90])
        return None
    except httpx.HTTPError as failure:
        logger.error("%s %s: обрыв связи (%s)", METHOD, emission, type(failure).__name__)
        return None
    return found.get("items") or []


def series(records: list[dict]) -> dict[str, tuple[float, float]]:
    """Цена в процентах от номинала по дням: наименьшая и наибольшая."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for item in records:
        price = (
            item.get("indicative_price")
            or item.get("avar_price")
            or item.get("last_price")
        )
        if price in (None, "", "0"):
            continue
        by_day[str(item.get("date"))[:10]].append(float(price))
    return {day: (min(rows), max(rows)) for day, rows in sorted(by_day.items())}


def main() -> int:
    """Печатает разведку торгов; 1 — если метод не ответил ни по одному выпуску."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    size = (
        int(sys.argv[sys.argv.index("--sample") + 1]) if "--sample" in sys.argv else 60
    )
    today = date.today()
    edge = today - timedelta(days=30)
    cards = json.loads((CACHE / "emitents.json").read_text(encoding="utf-8"))

    outstanding: list[tuple[str, str, str]] = []
    for inn in cards:
        if not (CACHE / f"emissions_{inn}.json").exists():
            continue
        for issue in issues_of(inn)[0]:
            if issue.status in WANTED:
                outstanding.append((inn, issue.emission_id, issue.name))

    print("# Разведка торгов: котировки выпусков\n")
    print(
        f"Выпусков в обращении и размещаемых — {len(outstanding)}. Котировки "
        f"приходят методом `{METHOD}`: в `get_emissions` их нет, из 209 полей "
        "записи выпуска торгов касаются только площадки и объём размещения.\n"
    )

    # **Выборка, а не весь перечень.** Спросить обо всех — 1 207 запросов;
    # шаг выборки объявлен, чтобы её можно было повторить.
    step = max(len(outstanding) // size, 1)
    sample = outstanding[::step][:size]
    answered = recent = silent = 0
    earliest: list[str] = []
    for _, emission, _ in sample:
        records = quotes(emission)
        if records is None:
            continue
        answered += 1
        if not records:
            silent += 1
            continue
        days = series(records)
        if not days:
            silent += 1
            continue
        if max(days) >= edge.isoformat():
            recent += 1
        earliest.append(min(days))

    print(
        f"**Выборка {len(sample)} выпусков, шаг {step}.** Ответ получен по "
        f"{answered}; котировки за последние 30 дней есть у **{recent}**, "
        f"котировок нет вовсе у {silent}. Самая ранняя дата в ответах — "
        f"{min(earliest) if earliest else 'ответов с ценой нет'}: глубина "
        "хранения около сорока дней, и отбор по дате её не расширяет. Историю "
        "торгов, как и историю рейтингов, придётся снимать снимками.\n"
    )

    for inn, label in NAMED.items():
        print(f"## {label}\n")
        found = False
        for issue in issues_of(inn)[0]:
            if issue.status not in WANTED and not issue.defaulted:
                continue
            records = quotes(issue.emission_id)
            if not records:
                continue
            days = series(records)
            if not days:
                continue
            found = True
            print(f"**{issue.name}** ({issue.status}), дней с ценой {len(days)}\n")
            print("| День | Цена, % от номинала |")
            print("|---|---|")
            for day, (low, high) in days.items():
                shown = (
                    f"{low:.2f}" if low == high else f"{low:.2f} … {high:.2f}"
                )
                print(f"| {day} | {shown} |")
            print()
        if not found:
            print("Котировок у выпусков эмитента нет ни за один день.\n")
    return 0 if answered else 1


if __name__ == "__main__":
    sys.exit(main())
