"""Ценовой признак: «цена < 60 % номинала» против «цена / PV потока по КБД < 0,6». Только диск.

    uv run python eval/zspread_rows.py      # строки дней с отношением (раз)
    uv run python eval/price_ratio_run.py > отчёт.md

**Порог 0,6 не подбирается** (решение владельца 01.10.2026): сравниваются
две величины при одном пороге. PV — будущий поток графика Cbonds,
дисконтированный опубликованной кривой ОФЗ без надбавки (допущения —
`sources.zspread`); цена — та же, что у признака, плюс НКД биржи.
Правила сравнимости цены прежние: меняется одна величина.

У бумаги, где потока нет (флоатер, купон не объявлен, графика нет),
отношение не определено. Вариант печатается дважды: с подстановкой цены
от номинала там, где отношения нет, и без неё — чтобы охват был виден.

**Замер не считает сам**: признак — `scoring.market.findings` по ряду,
в котором у точки подменена цена; календарь — поточечная мера
`market_lead_run`; корзины — `routing_rows`.
"""

import contextlib
import io
import logging
import pickle
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import route_variants  # noqa: E402
from market_lead_run import _market_pointwise, events, systemic_issuers  # noqa: E402
from zspread_rows import OUT  # noqa: E402

from finlib.db import fetch_all  # noqa: E402
from finlib.scoring.market import findings  # noqa: E402
from finlib.sources.market import Market, load_market, series  # noqa: E402

logger = logging.getLogger(__name__)

ALFA = "7728168971"
PERESVET = "7703074601"


def variant(base: Market, rows: dict, fallback: bool) -> tuple[Market, int, int]:
    """Ряд, где цена точки — наименьшее отношение цены к PV в процентах."""
    prices: dict[str, dict[date, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    used = fell = 0
    for name, found in rows.items():
        day = date.fromisoformat(name)
        for item in found:
            if not item[1] or item[7] is None:
                continue
            if item[10] is not None:
                prices[item[1]][day].append(item[10] * 100)
                used += 1
            elif fallback:
                prices[item[1]][day].append(item[7])
                fell += 1
    issuers = {
        inn: {
            day: replace(
                point,
                price=min(prices[inn][day]) if prices.get(inn, {}).get(day) else None,
            )
            for day, point in own.items()
        }
        for inn, own in base.issuers.items()
    }
    market = replace(
        base, issuers=issuers, sorted_by_day={}, days_cache=[], number_cache={}
    )
    return market, used, fell


def main() -> int:
    """Печатает календарь событий, корзины и разбор Альфа-Банка."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    with Path(OUT).open("rb") as handle:
        rows = pickle.load(handle)["rows"]
    policy = load_market()
    base = series()
    when = events()
    with_fallback, used, fell = variant(base, rows, True)
    pure, _, _ = variant(base, rows, False)
    print("# Ценовой признак: цена от номинала против цены к PV по КБД\n")
    print(
        f"Строк наших бумаг с ценой: отношение определено у **{used}**, нет "
        f"(флоатер, купон не объявлен, графика нет) у **{fell}** "
        f"({fell / max(used + fell, 1):.1%}).\n"
    )
    variants = (
        ("цена < 60 % номинала (как сейчас)", base),
        ("цена / PV < 0,6, без отношения — цена от номинала", with_fallback),
        ("цена / PV < 0,6, только где отношение есть", pure),
    )
    moments = route_variants.monthly_moments(base)
    memo: dict = {}
    said: dict[str, dict] = {}
    for name, market in variants:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _market_pointwise(policy, market, when)
        lines = [
            line
            for line in buffer.getvalue().splitlines()
            if line.startswith("| цена") or line.startswith("| Слой") or line.startswith("|---")
            or line.startswith("| Основание") or "Интервалы" in line
        ]
        print(f"## Календарь событий: {name}\n")
        print("\n".join(lines) + "\n")
        with route_variants.patched(policy, market):
            said[name] = route_variants.baskets(moments, memo)
    first = variants[0][0]
    for name, _ in variants[1:]:
        found = route_variants.changes(said[first], said[name])
        print(f"## Смены корзины: {name}\n")
        print(route_variants.summary(found, moments) + "\n")
        for moment, inn, was, now in found:
            if moment == max(moments):
                print(f"- {moment:%d.%m.%Y} {inn}: {was} → {now}")
        print()

    systemic = systemic_issuers()
    today = max(base.benchmark)
    names = {
        row["inn"]: row["name"]
        for row in fetch_all("SELECT inn, name FROM organization")
    }
    print(f"## Держатели ценового основания на {today:%d.%m.%Y}\n")
    for name, market in variants:
        holders = sorted(
            inn
            for inn in market.issuers
            if any(
                item.ground == policy.distress_zone.ground
                for item in findings(policy, market, inn, today, systemic=inn in systemic)
            )
        )
        print(f"- {name}: **{len(holders)}** — " + ", ".join(
            f"{names.get(inn, inn)}" for inn in holders[:40]
        ))
    print()
    print("## Альфа-Банк и Пересвет: отношение по выпускам с ценой ниже 70 %\n")
    print(
        "Последний день, в котором у бумаги есть цена, за последние 40 дней ряда; "
        "у Альфа-Банка купон 0,01 % (почти бескупонные), у Пересвета 0,51 % "
        "после реструктуризации.\n"
    )
    print("| Эмитент | Бумага | День | Цена, % | Цена / PV |")
    print("|---|---|---|---|---|")
    recent = sorted(rows)[-40:]
    seen: dict[tuple[str, str], tuple] = {}
    for name in recent:
        for item in rows[name]:
            if item[1] in (ALFA, PERESVET) and item[7] is not None and item[7] < 70:
                seen[(item[1], item[0])] = (name, item)
    for (inn, secid), (name, item) in sorted(seen.items()):
        ratio = "—" if item[10] is None else f"{float(item[10]):.3f}"
        label = "Альфа-Банк" if inn == ALFA else "Пересвет"
        print(f"| {label} | {secid} | {name} | {float(item[7]):.2f} | {ratio} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
