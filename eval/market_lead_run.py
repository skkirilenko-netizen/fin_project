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
from collections import Counter, defaultdict
from datetime import date, timedelta
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


def excluded(row: dict, rule: dict) -> str:
    """Почему спред у этой бумаги не считается; пусто — считается.

    Возвращается **код правила**, а не «да/нет»: перечень отброшенного
    печатается построчно, и без кода нельзя сказать, что именно отсекло
    половину рынка.
    """
    for item in rule["comparability"]["exclude"]:
        if item.get("keep_only"):
            if str(row.get(item["by"]) or "") not in item["keep_only"]:
                return item["code"]
            continue
        if str(row.get(item["by"]) or "") not in item.get("values", ()):
            continue
        # Оговорка правила: то же значение при другом горизонте правомерно.
        spare = item.get("unless")
        if spare and str(row.get(spare["by"]) or "") in spare["values"]:
            continue
        return item["code"]
    return ""


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
    counted: dict[str, int] = defaultdict(int)
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
            if why := excluded(row, rule):
                counted[f"отброшено: {why}"] += 1
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
        "counted": dict(counted),
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
    history: dict[date, set[str]],
    sources: dict[str, str],
    words: tuple[str, ...],
    until: date = date.max,
) -> date | None:
    """Первый день, когда слой высказался; None — не высказывался вовсе."""
    for when in sorted(history):
        if when > until:
            return None
        for ground in history[when]:
            source = sources.get(ground, "")
            if any(word in source for word in words):
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


def first_day_when(own: dict, holds, until: date, of: int = 1, out_of: int = 1):  # noqa: ANN001, ANN201
    """Первый день, когда признак держался в K точках из последних N.

    **Без подтверждения «сработал хотя бы раз за два года» насыщается.**
    У эмитента пятьсот наблюдений, и порог, отсекающий на дне процент рынка,
    за два года срабатывает почти у каждого: перцентиль p99 дал 272 эмитента
    из 431. Мера «хотя бы раз» отвечает не на вопрос о признаке, а на вопрос
    о длине ряда. Подтверждение объявлено методикой (`confirmation`), здесь
    оно только применяется; `of=1, out_of=1` означает «без подтверждения».
    """
    days = sorted(own)
    seen: list[bool] = []
    for number, day in enumerate(days):
        when = date.fromisoformat(day)
        if when > until:
            return None
        seen.append(bool(holds(own, days, number)))
        window = seen[-out_of:]
        if len(window) >= of and sum(window) >= of:
            return when
    return None


def _holds_level(benchmark: dict, multiple: float):  # noqa: ANN201
    """Признак дня: кратность спреда к ориентиру не ниже названной."""

    def holds(own: dict, days: list[str], number: int) -> bool:
        level = benchmark.get(days[number])
        if not level or level <= 0:
            return False
        return own[days[number]]["spread"] / level >= multiple

    return holds


def _holds_widening(growth: float, back: int, calendar: bool):  # noqa: ANN201
    """Признак дня: спред вырос на долю `growth` от прежнего наблюдения.

    Прежнее берётся либо по числу наблюдений, либо по календарю: у неликвидной
    бумаги четыре наблюдения растягиваются на месяцы, и два способа отвечают
    на разные вопросы. Отрицательный прежний спред сравнением не годится —
    рост от −20 до +100 в долях не выражается.
    """

    def holds(own: dict, days: list[str], number: int) -> bool:
        if calendar:
            edge = date.fromisoformat(days[number]) - timedelta(days=back)
            earlier = [
                item for item in days[:number] if date.fromisoformat(item) <= edge
            ]
            if not earlier:
                return False
            was = own[earlier[-1]]["spread"]
        else:
            if number < back:
                return False
            was = own[days[number - back]]["spread"]
        now = own[days[number]]["spread"]
        return was > 0 and (now - was) / was >= growth

    return holds


def _holds_own_norm(multiple: float, window: int, least: int):  # noqa: ANN201
    """Признак дня: спред выше собственной нормы эмитента кратностью.

    Норма — медиана спреда за прошедшие дни у него же. Признак отвечает
    на вопрос «дорого **для него**», а не «дорого вообще»: у бумаги, всегда
    стоявшей втрое дороже рынка, кратность к ориентиру говорит об отрасли
    и размере, а не о перемене.
    """

    def holds(own: dict, days: list[str], number: int) -> bool:
        edge = date.fromisoformat(days[number]) - timedelta(days=window)
        past = [
            own[item]["spread"]
            for item in days[:number]
            if date.fromisoformat(item) >= edge
        ]
        if len(past) < least:
            return False
        norm = statistics.median(past)
        return norm > 0 and own[days[number]]["spread"] / norm >= multiple

    return holds


def _holds_price(below: float):  # noqa: ANN201
    """Признак дня: цена ушла ниже границы зоны дефолта."""

    def holds(own: dict, days: list[str], number: int) -> bool:
        price = own[days[number]].get("price")
        return price is not None and price < below

    return holds


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
    print(
        "\n**Обратная таблица — то, чего лестнице не хватает.** Ступень, "
        "объявленная кратностью, отвечает «сколько отсекается»; калибровке "
        "нужен обратный вопрос — какая кратность стоит на нужном перцентиле.\n"
    )
    print("| Перцентиль кратности | Кратность |")
    print("|---|---|")
    for place, value in _quantiles(series, benchmark):
        print(f"| {place} | {value:.2f}× |")

    # **Событие раньше первого дня доставки рынок упредить не мог.** У ДВМП
    # дефолт датирован 2018 годом, у двух эмитентов — 2009 и 2016: истории
    # торгов до 24.09.2024 у нас нет вовсе, и ноль упреждения там означал бы
    # «рынок молчал», тогда как молчим мы. Оговорка стоит в каждом замере
    # рынка (требование владельца 24.09.2026).
    first_day = min(benchmark) if benchmark else "9999-12-31"
    inside = {
        inn: moment for inn, moment in when.items() if f"{moment}" >= first_day
    }
    print("\n## Признаки: точность, выявляемость, прирост, упреждение\n")
    print(
        f"**В окне доставки — {len(inside)} событий из {len(when)}.** Событие "
        f"раньше {first_day} рынок упредить не мог: истории торгов до этого дня "
        "у нас нет вовсе, и ноль упреждения там означал бы «рынок молчал», "
        "тогда как молчим мы.\n"
    )
    known = set(series)
    base = len(set(inside) & known) / len(known) if known else 0
    print(
        f"Круг рыночных признаков — {len(known)} эмитентов с рядом, из них "
        f"с событием в окне {len(set(inside) & known)}: базовая доля "
        f"**{base:.1%}**. Прирост — точность признака к этой доле.\n"
    )
    # **Подтверждение объявлено методикой, и мерится с ним и без него.**
    # Разница между таблицами и есть цена подтверждения: сколько ложных оно
    # снимает и на сколько дней откладывает.
    of, out_of = (
        int(rule["confirmation"]["default"]["of"]),
        int(rule["confirmation"]["default"]["out_of"]),
    )
    for title, hold_of, hold_out in (
        ("### Без подтверждения: сработал хотя бы раз", 1, 1),
        (f"### С подтверждением {of} из {out_of}", of, out_of),
    ):
        print(f"\n{title}\n")
        print(
            "| Признак | Сработал | С событием | Ложных | Точность | "
            "Выявляемость | Прирост | Упреждение |"
        )
        print("|---|---|---|---|---|---|---|---|")
        for name, holds in _signals(series, benchmark):
            _row(
                name,
                lambda own, until, h=holds, a=hold_of, b=hold_out: first_day_when(
                    own, h, until, a, b
                ),
                series,
                inside,
                known,
                base,
            )
        # Слои отчётности и рейтингов мерятся тем же способом и на своём
        # круге: у них он шире — 900 эмитентов истории против 431 с рядом.
        # Подтверждение к ним не применяется: основание маршрута — не дневная
        # величина, и «7 из 10» у него не определено.
        if hold_of == 1:
            for name, words in (("отчётность: любое основание", _REPORTING),
                                ("рейтинги: любое основание", _RATING)):
                _row(
                    name,
                    lambda own, until, w=words: _first_day(own, sources, w, until),
                    history,
                    inside,
                    set(history),
                    len(set(inside) & set(history)) / len(history) if history else 0,
                )

    print("\n## Пересечение слоёв на событиях в окне\n")
    print(
        "Вопрос матрицы слоёв: что она добавляет. Рынок здесь — цена ниже "
        f"{rule['distress_zone']['price_below_percent']} %, как признак, "
        "который разделяет; уровень и расширение в пересечение не берутся "
        "порознь, чтобы не считать один слой дважды.\n"
    )
    _overlap(rule, series, benchmark, history, sources, inside)

    print("\n## Построчно: кто что сказал и когда\n")
    print(
        "Пусто — слой не высказался до события вовсе. Даты слоёв отчётности "
        "и рейтингов — из записанной истории корзин, рыночные — из срезов.\n"
    )
    print(
        "| ИНН | Событие | Расширение | Своя норма | Цена < 60 % "
        "| Отчётность | Рейтинги |"
    )
    print("|---|---|---|---|---|---|---|")
    for inn, moment in sorted(inside.items(), key=lambda item: item[1]):
        own = series.get(inn, {})
        if not own:
            continue
        print(
            f"| {inn} | {moment:%d.%m.%Y} "
            f"| {_lead(first_day_when(own, _holds_widening(0.6, 4, False), moment), moment)} "
            f"| {_lead(first_day_when(own, _holds_own_norm(2.0, 90, 20), moment), moment)} "
            f"| {_lead(_first_price(own, 60.0, moment), moment)} "
            f"| {_lead(_first_day(history.get(inn, {}), sources, _REPORTING, moment), moment)} "
            f"| {_lead(_first_day(history.get(inn, {}), sources, _RATING, moment), moment)} |"
        )
    return 0


def _signals(series: dict, benchmark: dict) -> list[tuple]:
    """Перечень рыночных признаков: имя и признак дня.

    **Ступени лестницы стоят на перцентилях распределения**, а не на круглых
    числах: замер 24.09.2026 показал, что 1,5× отсекает три четверти рынка,
    то есть мерит фон. Ниже p75 в маршрут не идёт ничего (решение владельца).
    """
    steps = dict(_quantiles(series, benchmark))
    found: list[tuple] = []
    for place in (75, 90, 95, 99):
        multiple = steps.get(place)
        if multiple is not None:
            found.append(
                (
                    f"уровень: кратность ≥ {multiple:.2f}× (p{place})",
                    _holds_level(benchmark, multiple),
                )
            )
    # Расширение: то же движение, померенное наблюдениями и календарём.
    for back, calendar, name in (
        (4, False, "4 наблюдения"),
        (14, True, "две недели"),
        (30, True, "месяц"),
    ):
        found.append(
            (
                f"расширение: спред +60 % за {name}",
                _holds_widening(0.6, back, calendar),
            )
        )
    for multiple in (1.5, 2.0, 3.0):
        found.append(
            (
                f"своя норма: спред ≥ {multiple}× медианы за 90 дней",
                _holds_own_norm(multiple, 90, 20),
            )
        )
    for below in (75.0, 60.0, 40.0):
        found.append(
            (f"цена ниже {below:.0f} % номинала", _holds_price(below))
        )
    return found


def _row(name: str, first, source: dict, inside: dict, known: set, base: float) -> None:  # noqa: ANN001
    """Строка сравнения признаков: пять чисел и упреждение."""
    fired = {inn for inn, own in source.items() if first(own, date.max) is not None}
    hit = fired & set(inside)
    leads = [
        (inside[inn] - day).days
        for inn in hit
        if (day := first(source[inn], inside[inn])) is not None
    ]
    precision = len(hit) / len(fired) if fired else 0
    recall = len(hit) / len(set(inside) & known) if set(inside) & known else 0
    lift = precision / base if base else 0
    print(
        f"| {name} | {len(fired)} | {len(hit)} | {len(fired) - len(hit)} "
        f"| {precision:.1%} | {recall:.1%} | {lift:.1f}× | {_said(leads)} |"
    )


def _overlap(rule: dict, series: dict, benchmark: dict, history: dict,
             sources: dict, inside: dict) -> None:
    """Кто ловит событие: только отчётность, только рейтинги, только рынок.

    **Это и есть ответ на вопрос, нужна ли матрица слоёв.** Если каждый
    эмитент с событием ловится всеми тремя, матрица не добавляет ничего;
    если у каждого свой слой — она и есть ответ.
    """
    counted: Counter = Counter()
    for inn, moment in inside.items():
        own = series.get(inn, {})
        market = _first_price(own, 60.0, moment) is not None if own else False
        reporting = (
            _first_day(history.get(inn, {}), sources, _REPORTING, moment) is not None
        )
        rating = (
            _first_day(history.get(inn, {}), sources, _RATING, moment) is not None
        )
        said = tuple(
            name
            for name, yes in (
                ("рынок", market), ("отчётность", reporting), ("рейтинги", rating)
            )
            if yes
        )
        counted[said or ("никто",)] += 1
    print("| Кто сказал до события | Эмитентов |")
    print("|---|---|")
    for names, count in sorted(counted.items(), key=lambda item: -item[1]):
        print(f"| {', '.join(names)} | {count} |")
    alone = {
        name: counted[(name,)] for name in ("рынок", "отчётность", "рейтинги")
    }
    print(
        f"\nТолько рынок — {alone['рынок']}, только отчётность — "
        f"{alone['отчётность']}, только рейтинги — {alone['рейтинги']}, "
        f"никто — {counted[('никто',)]} из {len(inside)}."
    )


def _lead(day: date | None, moment: date) -> str:
    """Упреждение днями; пусто — слой не высказался."""
    if day is None or day > moment:
        return "—"
    return f"{(moment - day).days}"


def _quantiles(series: dict, benchmark: dict) -> list[tuple[int, float]]:
    """Кратность на опорных перцентилях распределения по дням.

    Считается по дням и усредняется медианой: распределение кратности
    двигается вместе с рынком, и один перцентиль по всей истории смешал бы
    спокойный год с кризисным месяцем.
    """
    by_day: dict[str, list[float]] = defaultdict(list)
    for own in series.values():
        for day, item in own.items():
            level = benchmark.get(day)
            if level and level > 0:
                by_day[day].append(item["spread"] / level)
    found: list[tuple[int, float]] = []
    for place in (50, 75, 90, 95, 99):
        values = [
            _percentile(sorted(items), place)
            for items in by_day.values()
            if len(items) >= 20
        ]
        if values:
            found.append((place, statistics.median(values)))
    return found


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
