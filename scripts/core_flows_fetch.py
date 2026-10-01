"""Графики платежей и оферты бумаг ядра ориентира, которых нет на диске.

    uv run python scripts/core_flows_fetch.py --dry            # счёт, без сети
    uv run python scripts/core_flows_fetch.py --budget 3000    # доставка

**Зачем.** Z-спред дисконтирует поток бумаги кривой ОФЗ, и поток берётся
из графика Cbonds. Графики лежат только у выпусков, бывших в обращении при
доставке (`flows_fetch.py` спрашивает действующие), а ядро ориентира за два
года — это и погашенные бумаги: на ноябрь 2024 года покрыто 87 из 273
(план Z-спреда, `BACKLOG.md`). Без них ориентир прошлого считался бы
на трети ядра.

**Ядро — то же правило, что у ряда** (`sources.market.build`): бумага
не отброшена правилом сравнимости, доходность и дюрация есть, спред
не выше потолка, сделок и оборота не меньше порогов `liquid_core`.
Второго перечня условий здесь нет: условия берутся у методики.

**Выпуск без идентификатора у нас** (бумага не наших эмитентов) ищется
одним запросом по ISIN; клиент сверяет ответ с отбором сам
(`cbonds.fetch`), и пустой ответ называется, а не пропускается молча.

**Бюджет — до запроса, а не после.** `--budget` — сколько запросов
разрешено этому прогону; на каждый выпуск их до трёх (поиск, график,
оферты). Выпуск, на который бюджета не хватает целиком, не начинается.
Файл прогона (`core_flows_delta_ГГГГ-ММ-ДД_ЧЧММСС.json`) называет, что спрошено,
что не найдено и сколько потрачено.

**Доставка докачиваемая.** Выпуск считается доставленным, когда на диске
есть оба ответа — график и оферты; при обрыве повтор спрашивает только
недостающий из двух. Поиск по ISIN кэшируется тем же клиентом и повторно
не тратит запроса. Подряд `STOP_AFTER` отказов — источник отказал,
и доставка останавливается, а не перебирает перечень впустую.
"""

import json
import logging
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finlib.sources import cbonds  # noqa: E402
from finlib.sources.cbonds_events import issues_of  # noqa: E402
from finlib.sources.market import (  # noqa: E402
    CACHE as MOEX,
)
from finlib.sources.market import (  # noqa: E402
    _number,
    curve_at,
    curve_of,
    excluded,
    load_market,
    universe,
)

logger = logging.getLogger(__name__)

CACHE = cbonds.CACHE
# Запросов на выпуск без идентификатора — три (поиск, график, оферты),
# с идентификатором — два.
PER_ISSUE = 2
PER_LOOKUP = 1
STOP_AFTER = 5


def core_isins() -> dict[str, int]:
    """ISIN бумаг ядра ориентира за всю историю срезов → число дней в ядре."""
    policy = load_market()
    core = policy.benchmark["liquid_core"]
    ceiling = Decimal(str(policy.spread["ceiling_bp"]))
    curves = json.loads((MOEX / "zcyc_by_day.json").read_text(encoding="utf-8"))
    found: Counter[str] = Counter()
    for path in sorted(MOEX.glob("xsec_*.json")):
        if "_p" in path.name:
            continue
        name = path.name[len("xsec_") : -len(".json")]
        points = curves.get(name, {}).get("yearyields")
        if not points:
            continue
        curve = curve_of(points)
        for row in json.loads(path.read_text(encoding="utf-8")).get("history") or []:
            if excluded(row, policy):
                continue
            got = _number(row.get("YIELDATWAP") or row.get("YIELDCLOSE"))
            days = _number(row.get("DURATION"))
            if got is None or not days:
                continue
            level, _ = curve_at(curve, days / Decimal(365))
            if (got - level) * 100 > ceiling:
                continue
            trades = _number(row.get("NUMTRADES")) or Decimal(0)
            turnover = _number(row.get("VALUE")) or Decimal(0)
            if trades >= core["min_trades"] and turnover >= core["min_turnover_rub"]:
                found[str(row.get("SECID") or "")] += 1
    found.pop("", None)
    return dict(found)


def emission_ids() -> dict[str, str]:
    """ISIN → идентификатор выпуска по выпускам эмитентов справочника."""
    found: dict[str, str] = {}
    for inn in universe():
        issues, known = issues_of(inn)
        if not known:
            continue
        for item in issues:
            if item.isin:
                found[item.isin] = item.emission_id
    return found


def plan() -> tuple[list[str], list[str], int]:
    """Что спрашивать: выпуски с идентификатором без графика, ISIN без него, всего ядра."""
    core = core_isins()
    known = emission_ids()
    missing = sorted(
        {
            known[isin]
            for isin in core
            if isin in known and not _delivered(known[isin])
        }
    )
    unknown = sorted(isin for isin in core if isin not in known)
    return missing, unknown, len(core)


def _delivered(emission: str) -> bool:
    """Доставлен ли выпуск: на диске есть и график, и оферты."""
    return all(
        (CACHE / f"{kind}_{emission}.json").exists() for kind in ("flow", "offert")
    )


def _lookup(isin: str) -> str | None:
    """Идентификатор выпуска по ISIN одним запросом; None — источник не знает.

    **У ОФЗ код торгов биржи не ISIN** (`SU26207RMFS9` против
    `RU000A0JS3W6`), и поиск по нему пуст. ОФЗ ищется по номеру
    государственной регистрации, который код торгов содержит: `26207RMFS`
    (проверено 01.10.2026 одним запросом — выпуск 25817, ISIN совпал).
    """
    if isin.startswith("SU") and len(isin) == 12:
        number = isin[2:-1]
        found = cbonds.fetch(
            "get_emissions",
            f"emission_regnum_{number}",
            filters=({"field": "state_reg_number", "operator": "eq", "value": number},),
            limit=10,
        )
        items = found.get("items", [])
        return str(items[0]["id"]) if items else None
    found = cbonds.fetch(
        "get_emissions",
        f"emission_isin_{isin}",
        filters=({"field": "isin_code", "operator": "eq", "value": isin},),
        limit=10,
    )
    items = found.get("items", [])
    return str(items[0]["id"]) if items else None


def _deliver(emission: str) -> int:
    """График и оферты выпуска; возвращает число отказов."""
    failed = 0
    for method, name, limit in (
        ("get_flow_new", f"flow_{emission}", 500),
        ("get_offert", f"offert_{emission}", 100),
    ):
        if (CACHE / f"{name}.json").exists():
            continue
        try:
            cbonds.fetch(
                method,
                name,
                filters=({"field": "emission_id", "operator": "eq", "value": emission},),
                limit=limit,
            )
        except (cbonds.CbondsError, httpx.TransportError) as failure:
            failed += 1
            logger.error("%s %s: %s", method, emission, str(failure)[:120])
    return failed


def main() -> int:
    """Считает план и, если не `--dry`, доставляет в пределах бюджета."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    missing, unknown, total = plan()
    need = len(missing) * PER_ISSUE + len(unknown) * (PER_LOOKUP + PER_ISSUE)
    print(
        f"ядро ориентира за историю: бумаг {total}; без графика с известным "
        f"выпуском {len(missing)}, без выпуска у нас {len(unknown)}; запросов "
        f"не больше {need} ({PER_ISSUE} на выпуск, +{PER_LOOKUP} на поиск по ISIN)"
    )
    if "--dry" in sys.argv:
        return 0
    if "--budget" not in sys.argv:
        print("назовите бюджет запросов: --budget N")
        return 1
    budget = int(sys.argv[sys.argv.index("--budget") + 1])
    asked: list[dict] = []
    not_found: list[str] = []
    failed = 0
    streak = 0
    # Найденные поиском, у которых график и оферты уже на диске: это не
    # «не хватило бюджета», а докачка, которой не потребовалось.
    already = 0
    for isin in unknown:
        if cbonds.pace.requested + PER_LOOKUP + PER_ISSUE > budget or streak >= STOP_AFTER:
            break
        try:
            emission = _lookup(isin)
        except (cbonds.CbondsError, httpx.TransportError) as failure:
            failed += 1
            streak += 1
            logger.error("get_emissions %s: %s", isin, str(failure)[:120])
            continue
        if emission is None:
            not_found.append(isin)
            continue
        if _delivered(emission):
            already += 1
            continue
        refused = _deliver(emission)
        failed += refused
        streak = streak + refused if refused else 0
        asked.append({"isin": isin, "emission_id": emission, "why": "не наш выпуск"})
    for emission in missing:
        if cbonds.pace.requested + PER_ISSUE > budget or streak >= STOP_AFTER:
            break
        refused = _deliver(emission)
        failed += refused
        streak = streak + refused if refused else 0
        asked.append({"emission_id": emission, "why": "графика нет"})
    left = len(missing) + len(unknown) - len(asked) - len(not_found) - already
    # Файл прогона назван временем, а не днём: второй запуск того же дня
    # (докачка) затирал бы перечень первого.
    (CACHE / f"core_flows_delta_{datetime.now():%Y-%m-%d_%H%M%S}.json").write_text(
        json.dumps(
            {
                "asked": asked,
                "not_found": not_found,
                "already_on_disk": already,
                "failed": failed,
                "left_for_budget": left,
                "requests": cbonds.pace.requested,
                "stopped_on_refusals": streak >= STOP_AFTER,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"доставлено выпусков {len(asked)}, уже на диске {already}, источник не знает "
        f"ISIN {len(not_found)}, отказов {failed}, не хватило бюджета на {left}, запросов "
        f"{cbonds.pace.requested}"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
