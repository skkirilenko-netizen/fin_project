"""Рынок против отчётности: у кого стоит одно основание без другого. Только разведка.

    uv run python eval/market_vs_reporting_run.py > data/output/market_vs_reporting.md

**Маршрут не трогается, выводы в методику не вносятся.** Основания берутся
из записанной истории пересчёта (`routing_history.grounds_all`, все сработавшие),
слой основания — из `routing.yaml` (`ground_sources`): «рынок» — рыночный слой,
«отчётность» — основания по величинам (без «неприменимо здесь», срока, пробела
и периметра). Рефинансирование («выпуск и отчётность») — свой слой, в счёт
не идёт.

**Два среза.** Упреждающий — состояние на 01.07.2026 (раздел калибровки)
и события после него до конца истории; эмитент с событием до среза из круга
исключён. Описательный — последняя точка истории, без событий вперёд.
Событие — объявленный неплатёж (`market_lead_run.events`), как у всех замеров.
"""

import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_lead_run import events  # noqa: E402

from finlib.db import fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402

CUT = date(2026, 7, 1)
_AT = """
SELECT inn, grounds_all FROM routing_history
WHERE kind = 'backfill' AND as_of = (
    SELECT max(as_of) FROM routing_history WHERE kind = 'backfill' AND as_of <= %(day)s)
"""
_LAST = "SELECT max(as_of) AS day FROM routing_history WHERE kind = 'backfill'"
KINDS = ("только рынок", "только величины", "оба", "ни того ни другого")


def _kind(grounds: set[str], market: set[str], values: set[str]) -> str:
    """Род эмитента по сработавшим основаниям."""
    has_market, has_values = bool(grounds & market), bool(grounds & values)
    if has_market and has_values:
        return "оба"
    if has_market:
        return "только рынок"
    if has_values:
        return "только величины"
    return "ни того ни другого"


def main() -> int:
    """Печатает оба среза."""
    sources = load_routing().ground_sources
    market = {code for code, source in sources.items() if source == "рынок"}
    values = {
        code
        for code, source in sources.items()
        if source == "отчётность" and code != "inapplicable_here"
    }
    end = fetch_all(_LAST, {})[0]["day"]
    calendar = events()
    rows = fetch_all(_AT, {"day": CUT})
    circle = {
        row["inn"]: set(row["grounds_all"] or ())
        for row in rows
        if not (row["inn"] in calendar and calendar[row["inn"]] <= CUT)
    }
    ahead = {inn for inn in circle if inn in calendar and CUT < calendar[inn] <= end}
    base = len(ahead) / len(circle) if circle else 0
    print("# Рынок против отчётности\n")
    print(f"Рыночные основания: {', '.join(sorted(market))}.\n")
    print(f"Основания по величинам: {', '.join(sorted(values))}.\n")
    print(
        f"## Упреждающий срез: состояние на {CUT:%d.%m.%Y}, события до {end:%d.%m.%Y}\n"
    )
    print(
        f"Круг {len(circle)} эмитентов (с событием до среза исключены), событий после "
        f"среза {len(ahead)}, базовая доля {base * 100:.1f} %.\n"
    )
    print("| Род | Эмитентов | С событием | Доля | Прирост |")
    print("|---|---|---|---|---|")
    kinds = {inn: _kind(grounds, market, values) for inn, grounds in circle.items()}
    count, caught = Counter(kinds.values()), Counter(kinds[inn] for inn in ahead)
    for kind in KINDS:
        share = caught[kind] / count[kind] if count[kind] else 0
        lift = f"{share / base:.1f}×" if base and count[kind] else "—"
        print(f"| {kind} | {count[kind]} | {caught[kind]} | {share * 100:.1f} % | {lift} |")
    print(
        "\nС событием и родом: "
        + "; ".join(f"{inn} — {kinds[inn]}" for inn in sorted(ahead))
        + ".\n"
    )
    last = fetch_all(_AT, {"day": end})
    now = Counter(
        _kind(set(row["grounds_all"] or ()), market, values) for row in last
    )
    print(f"## Описательный срез на {end:%d.%m.%Y}\n")
    print("| Род | Эмитентов |")
    print("|---|---|")
    for kind in KINDS:
        print(f"| {kind} | {now[kind]} |")
    print(
        "\n**Оговорка.** Рыночное основание p99 стоит, если хоть раз подтвердилось "
        "за двухлетний ряд (`scoring.market.first_day_when`), и у большинства "
        "нынешних держателей оно подтвердилось осенью 2024 года, когда ориентир "
        "рынка был близок к нулю. «Только рынок» поэтому частью мерит длину ряда."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
