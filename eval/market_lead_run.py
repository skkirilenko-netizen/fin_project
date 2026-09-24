"""Упреждение трёх слоёв: рынок против отчётности и рейтингов. **Только замер.**

    uv run python eval/market_lead_run.py > data/output/market_lead.md

**Вопрос, ради которого рыночный слой и заводится.** Годовая отчётность
о событиях между отчётными датами не говорит ничего: у Кириллицы величины
за 2025 год здоровые, а 07.09.2026 не исполнено погашение. Замер отвечает,
у скольких эмитентов с событием рынок сказал раньше отчётности и рейтинга
и на сколько дней.

**Три упреждения меряются одной мерой.** Для каждого эмитента с датированным
неисполненным событием берётся первый день, когда слой о нём высказался,
и считается разница до события. Слои при этом разной природы, и это названо:
отчётность и рейтинги читаются из **записанной истории корзин** — там лежит
перечень всех сработавших оснований на каждую дату, — а рынок считается
здесь, потому что кода у него ещё нет.

**Это и есть объявленный долг модуля.** Счёт спреда живёт в замере, пока
методика не утверждена; когда она утверждена, счёт переезжает в `src/`,
а замер начинает звать его. Держать два счёта нельзя — расхождения не видно,
пока их не сравнить, — и потому здесь он один и временный.

**Пороги отсюда не берутся.** Лестница печатается распределением: какую долю
рынка отсекает каждая ступень и на какой перцентиль кратности приходится.
События проверяют упреждение, а не назначают порог: подогнанный под полсотни
наблюдений порог меряет набор.
"""

import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.config import settings  # noqa: E402
from finlib.db import connection, fetch_all  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import default_records, issues_of  # noqa: E402
from finlib.sources.moex import CACHE  # noqa: E402

logger = logging.getLogger(__name__)

SERIES = Path("data/output/market_series.json")
MARKET = settings.methodology_dir / "market.yaml"

# Слои основания: перечень уже объявлен методикой маршрута (`ground_sources`),
# и второго здесь не заводится. Берутся два рода — отчётность и рейтинг.
_REPORTING = ("отчётность",)
_RATING = ("рейтинг",)

_HISTORY = """
SELECT inn, as_of, grounds_all FROM routing_history
WHERE kind = 'backfill' ORDER BY inn, as_of
"""


def rules() -> dict:
    """Методика рыночного слоя; величины отсюда только печатаются."""
    return yaml.safe_load(MARKET.read_text(encoding="utf-8"))


def curve_of(points: list[dict]) -> list[tuple[float, float]]:
    """Опубликованные точки кривой парами «годы, доходность»."""
    return sorted(
        (float(item["period"]), float(item["value"]))
        for item in points
        if item.get("period") is not None and item.get("value") is not None
    )


def curve_at(points: list[tuple[float, float]], years: float) -> tuple[float, bool]:
    """Кривая в точке дюрации и признак «это край, а не значение».

    Линейная интерполяция между опубликованными точками; за их пределами
    берётся крайняя точка, и строка помечается — продлевать кривую собственным
    правилом мы не будем.
    """
    if years <= points[0][0]:
        return points[0][1], True
    if years >= points[-1][0]:
        return points[-1][1], True
    for (left, low), (right, high) in zip(points, points[1:], strict=False):
        if left <= years <= right:
            share = (years - left) / (right - left)
            return low + (high - low) * share, False
    return points[-1][1], True


def ours() -> dict[str, str]:
    """ISIN → ИНН по всем выпускам эмитентов списка, включая погашенные."""
    found: dict[str, str] = {}
    for inn in bond_issuers():
        issues, known = issues_of(inn)
        if not known:
            continue
        for item in issues:
            if item.isin:
                found[item.isin] = inn
    return found


def excluded(row: dict, rule: dict) -> bool:
    """Не считается ли спред у этой бумаги — по признакам самой биржи."""
    for item in rule["comparability"]["exclude"]:
        if item.get("keep_only"):
            if str(row.get(item["by"]) or "") not in item["keep_only"]:
                return True
            continue
        if str(row.get(item["by"]) or "") in item.get("values", ()):
            return True
        field = item.get("also_when_null")
        if field and row.get(field) is None:
            return True
    return False


def collect() -> dict:
    """Ряд спредов по эмитентам и ориентир дня; читается с диска.

    Один проход по срезам: 507 дней и полтора миллиона строк. Хранится
    только сведённое — по эмитенту на дату, — иначе ряд не помещается
    ни в память, ни в осмысленный файл.
    """
    if SERIES.exists():
        return json.loads(SERIES.read_text(encoding="utf-8"))
    rule = rules()
    core = rule["benchmark"]["liquid_core"]
    percentile = int(rule["benchmark"]["percentile"])
    ceiling = float(rule["spread"]["ceiling_bp"])
    holders = ours()
    curves = json.loads(
        (CACHE / "zcyc_by_day.json").read_text(encoding="utf-8")
    )
    by_issuer: dict[str, dict[str, dict]] = defaultdict(dict)
    benchmark: dict[str, float] = {}
    counted = {"строк": 0, "исключено правилом сравнимости": 0, "без дюрации": 0,
               "без доходности": 0, "край кривой": 0, "выше потолка": 0}
    for path in sorted(CACHE.glob("xsec_*.json")):
        if "_p" in path.name:
            continue
        day = path.name[len("xsec_") : -len(".json")]
        points = curves.get(day, {}).get("yearyields")
        if not points:
            continue
        curve = curve_of(points)
        market: list[float] = []
        mine: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
        for row in json.loads(path.read_text(encoding="utf-8")).get("history") or []:
            counted["строк"] += 1
            if excluded(row, rule):
                counted["исключено правилом сравнимости"] += 1
                continue
            got = row.get("YIELDATWAP") or row.get("YIELDCLOSE")
            if got is None:
                counted["без доходности"] += 1
                continue
            days = row.get("DURATION")
            if not days:
                counted["без дюрации"] += 1
                continue
            level, edge = curve_at(curve, float(days) / 365)
            counted["край кривой"] += int(edge)
            spread = (float(got) - level) * 100
            if spread > ceiling:
                counted["выше потолка"] += 1
                continue
            trades = float(row.get("NUMTRADES") or 0)
            turnover = float(row.get("VALUE") or 0)
            if trades >= core["min_trades"] and turnover >= core["min_turnover_rub"]:
                market.append(spread)
            inn = holders.get(str(row.get("SECID") or ""))
            if inn is not None:
                price = row.get("LEGALCLOSEPRICE") or row.get("CLOSE")
                mine[inn].append(
                    (spread, turnover, float(price) if price is not None else 0.0)
                )
        if len(market) < 5:
            # Ядро из трёх бумаг ориентиром не является: день остаётся
            # без ориентира, и спреды этого дня в кратность не идут.
            continue
        benchmark[day] = _percentile(sorted(market), percentile)
        for inn, rows in mine.items():
            weight = sum(item[1] for item in rows) or float(len(rows))
            spread = sum(item[0] * (item[1] or 1) for item in rows) / (
                sum(item[1] or 1 for item in rows)
            )
            by_issuer[inn][day] = {
                "spread": round(spread, 1),
                "price": round(min(item[2] for item in rows if item[2]), 2)
                if any(item[2] for item in rows)
                else None,
                "weight": round(weight),
            }
    found = {
        "benchmark": {day: round(value, 1) for day, value in benchmark.items()},
        "issuers": by_issuer,
        "counted": counted,
    }
    SERIES.parent.mkdir(parents=True, exist_ok=True)
    SERIES.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    return found


def _percentile(values: list[float], percent: int) -> float:
    """Перцентиль отсортированного ряда; ряд пуст — вызывающий не спрашивает."""
    if len(values) == 1:
        return values[0]
    place = (len(values) - 1) * percent / 100
    low = int(place)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (place - low)


def events() -> dict[str, date]:
    """ИНН → дата первого неисполненного события дефолта."""
    records = default_records()
    first: dict[str, date] = {}
    for inn in bond_issuers():
        issues, known = issues_of(inn)
        if not known:
            continue
        for issue in issues:
            for item in records.get(issue.emission_id, ()):
                if item.settled or item.moment is None:
                    continue
                if inn not in first or item.moment < first[inn]:
                    first[inn] = item.moment
    return first


def layers() -> tuple[dict[str, dict[date, set[str]]], dict[str, str]]:
    """Сработавшие основания по дням из записанной истории и их слои."""
    from finlib.scoring.routing import load_routing

    routing = load_routing()
    sources = routing.ground_sources
    with connection() as conn:
        rows = fetch_all(_HISTORY, {}, conn=conn)
    found: dict[str, dict[date, set[str]]] = defaultdict(dict)
    for row in rows:
        found[row["inn"]][row["as_of"]] = set(row["grounds_all"] or ())
    return found, sources


def _first_day(
    history: dict[date, set[str]], sources: dict[str, str], words: tuple[str, ...]
) -> date | None:
    """Первый день, когда слой высказался; None — не высказывался вовсе."""
    for when in sorted(history):
        for ground in history[when]:
            source = sources.get(ground, "")
            if any(word in source for word in words):
                return when
    return None


def _first_market(series: dict[str, dict], benchmark: dict[str, float],
                  multiple: float, until: date) -> date | None:
    """Первый день, когда кратность к ориентиру не ниже названной."""
    for day in sorted(series):
        when = date.fromisoformat(day)
        if when > until:
            return None
        level = benchmark.get(day)
        if not level or level <= 0:
            continue
        if series[day]["spread"] / level >= multiple:
            return when
    return None


def _first_price(series: dict[str, dict], below: float, until: date) -> date | None:
    """Первый день, когда цена ушла ниже границы зоны дефолта."""
    for day in sorted(series):
        when = date.fromisoformat(day)
        if when > until:
            return None
        price = series[day].get("price")
        if price is not None and price < below:
            return when
    return None


def _said(days: list[int]) -> str:
    """Упреждение словами: медиана, край и знаменатель."""
    if not days:
        return "наблюдений нет"
    return (
        f"медиана **{statistics.median(days):.0f}** дн., "
        f"от {min(days)} до {max(days)}, наблюдений {len(days)}"
    )


def main() -> int:
    """Печатает упреждение трёх слоёв и распределение кратности."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    rule = rules()
    found = collect()
    benchmark = found["benchmark"]
    series = found["issuers"]
    when = events()
    history, sources = layers()

    print("# Упреждение слоёв: рынок против отчётности и рейтингов\n")
    print(
        f"Дней с ориентиром **{len(benchmark)}**, эмитентов с рыночным рядом "
        f"**{len(series)}**, эмитентов с неисполненным датированным событием "
        f"**{len(when)}**. Строк среза прочитано {found['counted']['строк']}.\n"
    )
    print(
        "**Ни одно правило отсюда в маршрут не входит.** Замер отвечает "
        "на вопрос, ради которого слой заводится, и на этом останавливается.\n"
    )

    print("## Что отброшено правилом сравнимости\n")
    print("| Причина | Строк |")
    print("|---|---|")
    for name, count in found["counted"].items():
        print(f"| {name} | {count} |")
    print(
        "\nОтброшенное названо числом: правило, отсекающее половину рынка "
        "молча, неотличимо от ошибки чтения.\n"
    )

    print("## Лестница кратности: что отсекает каждая ступень\n")
    print(
        "Доля рынка и перцентиль считаются по дням с ориентиром: у каждой даты "
        "своя доля, печатается медиана по дням. **Это и есть то обоснование, "
        "которого лестница ждёт**; события в него не входят.\n"
    )
    print("| Ступень | Кратность | Доля рынка | Перцентиль кратности |")
    print("|---|---|---|---|")
    shares = _ladder(rule, series, benchmark)
    for code, multiple, share, place in shares:
        print(f"| {code} | {multiple}× | {share:.2%} | {place:.1f} |")

    print("\n## Упреждение у эмитентов с событием\n")
    market_leads: dict[float, list[int]] = {}
    for step in rule["ladder"]["steps"]:
        multiple = float(step["multiple"])
        market_leads[multiple] = []
    price_leads: list[int] = []
    reporting_leads: list[int] = []
    rating_leads: list[int] = []
    rows: list[tuple] = []
    for inn, moment in sorted(when.items(), key=lambda item: item[1]):
        own = series.get(inn, {})
        market: dict[float, date | None] = {
            multiple: _first_market(own, benchmark, multiple, moment)
            for multiple in market_leads
        }
        price = _first_price(
            own, float(rule["distress_zone"]["price_below_percent"]), moment
        )
        reporting = _first_day(history.get(inn, {}), sources, _REPORTING)
        rating = _first_day(history.get(inn, {}), sources, _RATING)
        for multiple, day in market.items():
            if day is not None:
                market_leads[multiple].append((moment - day).days)
        if price is not None:
            price_leads.append((moment - price).days)
        if reporting is not None and reporting <= moment:
            reporting_leads.append((moment - reporting).days)
        if rating is not None and rating <= moment:
            rating_leads.append((moment - rating).days)
        rows.append((inn, moment, own, market, price, reporting, rating))

    print(
        f"Эмитентов с событием {len(when)}, из них с рыночным рядом "
        f"**{sum(1 for item in rows if item[2])}**: у остальных выпуск "
        "за два года не торговался ни дня, и упреждать рынку нечем.\n"
    )
    print("| Слой | Упреждение |")
    print("|---|---|")
    for multiple in sorted(market_leads):
        print(f"| рынок, кратность {multiple}× | {_said(market_leads[multiple])} |")
    print(
        f"| рынок, цена ниже "
        f"{rule['distress_zone']['price_below_percent']} % | {_said(price_leads)} |"
    )
    print(f"| отчётность | {_said(reporting_leads)} |")
    print(f"| рейтинги | {_said(rating_leads)} |")

    print("\n## Построчно: кто что сказал и когда\n")
    print(
        "Пусто — слой не высказался до события вовсе. Даты слоёв отчётности "
        "и рейтингов — из записанной истории корзин, рыночные — из срезов.\n"
    )
    print("| ИНН | Событие | Рынок 2× | Рынок 3× | Цена | Отчётность | Рейтинги |")
    print("|---|---|---|---|---|---|---|")
    for inn, moment, own, market, price, reporting, rating in rows:
        if not own:
            continue
        print(
            f"| {inn} | {moment:%d.%m.%Y} "
            f"| {_lead(market.get(2.0), moment)} | {_lead(market.get(3.0), moment)} "
            f"| {_lead(price, moment)} | {_lead(reporting, moment)} "
            f"| {_lead(rating, moment)} |"
        )
    return 0


def _lead(day: date | None, moment: date) -> str:
    """Упреждение днями; пусто — слой не высказался."""
    if day is None or day > moment:
        return "—"
    return f"{(moment - day).days}"


def _ladder(rule: dict, series: dict, benchmark: dict) -> list[tuple]:
    """Доля рынка и перцентиль кратности у каждой ступени лестницы."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for own in series.values():
        for day, item in own.items():
            level = benchmark.get(day)
            if level and level > 0:
                by_day[day].append(item["spread"] / level)
    found: list[tuple] = []
    for step in rule["ladder"]["steps"]:
        multiple = float(step["multiple"])
        shares = []
        places = []
        for values in by_day.values():
            if len(values) < 20:
                continue
            above = sum(1 for item in values if item >= multiple)
            shares.append(above / len(values))
            places.append(100 * (1 - above / len(values)))
        found.append(
            (
                step["code"],
                multiple,
                statistics.median(shares) if shares else Decimal(0),
                statistics.median(places) if places else 0.0,
            )
        )
    return found


if __name__ == "__main__":
    sys.exit(main())
