"""Строки срезов с G и Z по дням — общий вход замера Z-спреда. Только диск.

    uv run python eval/zspread_rows.py      # пишет data/market/zspread_rows.pkl

**Отбор строк тот же, что у ряда** (`sources.market.build`): правило
сравнимости, доходность и дюрация, потолок спреда, ядро по сделкам и обороту.
Z считается у каждой строки, у которой есть G; строка без Z называет причину.
Расчёт по дням идёт параллельно: в `Decimal` полный проход в один поток
занял бы больше часа.
"""

import json
import logging
import os
import pickle
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

from finlib.config import settings
from finlib.sources.market import (
    CACHE,
    _number,
    curve_at,
    curve_of,
    excluded,
    holders,
    load_market,
)
from finlib.sources.zspread import (
    bond_flow,
    emission_map,
    price_to_pv,
    z_spread,
)

logger = logging.getLogger(__name__)

OUT = settings.data_dir / "market" / "zspread_rows.pkl"

_STATE: dict = {}


def _init(by_code: dict[str, str], mine: dict[str, str]) -> None:
    """Состояние рабочего процесса: соответствия передаются один раз."""
    _STATE["by_code"] = by_code
    _STATE["mine"] = mine
    _STATE["policy"] = load_market()


def _flow(
    row: dict, day: date, curve: list, price: Decimal | None
) -> tuple[list, list, Decimal, str]:
    """Поток строки, ставки кривой в его сроках, грязная цена и причина отказа."""
    pairs, rates, dirty, _, why = bond_flow(
        row, day, lambda years: curve_at(curve, years)[0], price, _STATE["by_code"]
    )
    return pairs, rates, dirty, why


def _z(row: dict, day: date, curve: list, compounding: str) -> tuple[Decimal | None, str]:
    """Z-спред строки и причина отказа; цена — пара к доходности ряда."""
    price = _number(row.get("WAPRICE")) if row.get("YIELDATWAP") else None
    if price is None:
        price = _number(row.get("CLOSE")) or _number(row.get("LEGALCLOSEPRICE"))
    pairs, rates, dirty, why = _flow(row, day, curve, price)
    if why:
        return None, why
    found = z_spread(pairs, rates, dirty, compounding)
    return (found, "") if found is not None else (None, "Ньютон не сошёлся")


def _ratio(row: dict, day: date, curve: list, price: Decimal) -> Decimal | None:
    """Грязная цена к приведённой стоимости потока по КБД без надбавки; None — потока нет."""
    return price_to_pv(
        row, day, lambda years: curve_at(curve, years)[0], price, _STATE["by_code"]
    ).ratio


def day_rows(path_name: str, curve_points: list) -> tuple[str, list[tuple]]:
    """Строки дня: код, ИНН, G, Z, причина, ядро, оборот, цена, Z непрерыв., Z биржи, цена/PV."""
    policy = _STATE["policy"]
    core = policy.benchmark["liquid_core"]
    ceiling = Decimal(str(policy.spread["ceiling_bp"]))
    path = CACHE / path_name
    day = date.fromisoformat(path_name[len("xsec_") : -len(".json")])
    curve = curve_of(curve_points)
    found: list[tuple] = []
    for row in json.loads(path.read_text(encoding="utf-8")).get("history") or []:
        secid = str(row.get("SECID") or "")
        inn = _STATE["mine"].get(secid, "")
        turnover = _number(row.get("VALUE")) or Decimal(0)
        trades = _number(row.get("NUMTRADES")) or Decimal(0)
        price = _number(row.get("LEGALCLOSEPRICE")) or _number(row.get("CLOSE"))
        if price is not None and (
            excluded(row, policy, "price")
            or (policy.comparability["price_needs_trade"] and trades <= 0)
        ):
            price = None
        spread: Decimal | None = None
        if not excluded(row, policy):
            got = _number(row.get("YIELDATWAP") or row.get("YIELDCLOSE"))
            days = _number(row.get("DURATION"))
            if got is not None and days:
                level, _ = curve_at(curve, days / Decimal(365))
                spread = (got - level) * 100
                if spread > ceiling:
                    spread = None
        if spread is None and (not inn or price is None):
            continue
        in_core = bool(
            spread is not None
            and trades >= core["min_trades"]
            and turnover >= core["min_turnover_rub"]
        )
        z = why = None
        z_cont = None
        if spread is not None and (inn or in_core):
            z, why = _z(row, day, curve, "annual")
            if secid.startswith("SU"):
                z_cont, _ = _z(row, day, curve, "continuous")
        exchange = _number(row.get("ZSPREADATWAPRICE") or row.get("ZSPREAD"))
        ratio = _ratio(row, day, curve, price) if inn and price is not None else None
        found.append(
            (secid, inn, spread, z, why or "", in_core, turnover, price, z_cont, exchange, ratio)
        )
    return day.isoformat(), found


def main() -> int:
    """Считает строки всех дней и пишет их одним файлом."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    by_code, kind = emission_map()
    mine = holders()
    curves = json.loads((CACHE / "zcyc_by_day.json").read_text(encoding="utf-8"))
    jobs = []
    for path in sorted(CACHE.glob("xsec_*.json")):
        if "_p" in path.name:
            continue
        points = curves.get(path.name[len("xsec_") : -len(".json")], {}).get("yearyields")
        if points:
            jobs.append((path.name, points))
    if "--days" in sys.argv:
        jobs = jobs[-int(sys.argv[sys.argv.index("--days") + 1]) :]
    rows: dict[str, list[tuple]] = {}
    with ProcessPoolExecutor(
        max_workers=max((os.cpu_count() or 2) - 2, 1),
        initializer=_init,
        initargs=(by_code, mine),
    ) as pool:
        for day, found in pool.map(day_rows, *zip(*jobs, strict=True), chunksize=4):
            rows[day] = found
    reasons = Counter(item[4] for found in rows.values() for item in found if item[2] is not None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with Path(OUT).open("wb") as handle:
        pickle.dump({"rows": rows, "kind": kind, "by_code": by_code}, handle)
    print(f"дней {len(rows)}; строк со спредом по причинам Z: {dict(reasons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
