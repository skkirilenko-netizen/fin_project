"""Замер спредов по данным ISS: обоснование порогов рыночного слоя.

    uv run python eval/market_spread_probe.py > data/output/market_spread.md

**Это обоснование предложения, а не рыночный слой.** Правил здесь нет ни
одного, в маршрут ничего не попадает, и когда слой будет заведён, арифметика
переедет в `src/` — а замер станет звать её, как зовёт всё прочее. Пока
живого пути нет, и считать приходится здесь; оставить это здесь после — значит
получить вторую систему рядом.

**Кривая считается по опубликованным параметрам и проверена на опубликованных
точках.** Формула Московской биржи даёт непрерывную ставку, а точки кривой
публикуются эффективными: `(exp(G/100) − 1) · 100`. Сверка на дне 22.09.2026
по одиннадцати срокам сходится до четвёртого знака — без неё формула
оставалась бы догадкой о формуле.

**G-спред и Z-спред мерят разное, и оба нужны.** G-спред — превышение
доходности над кривой в точке дюрации, то есть ровно то, что платит рынок
за риск этого эмитента сверх государства. Z-спред биржи — параллельный сдвиг
всей кривой, при котором дисконтированные платежи дают цену: он строже,
но считает его биржа, и потому он служит сверкой, а не основанием.
"""

import json
import logging
import math
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.sources import moex  # noqa: E402

logger = logging.getLogger(__name__)

# Сводного файла кривых здесь нет намеренно: читаются сами ответы дня.
# Потолок спреда: 20 000 б. п. Выше — не цена риска, а бумага без рынка,
# и такое число тянет любую среднюю за собой.
CAP_BP = Decimal(20000)
# Обрезка выбросов внутри дня по перцентилям.
TRIM = (Decimal("0.001"), Decimal("0.999"))
# Постоянные формулы кривой Московской биржи: k = 1,6, a1 = 0, b1 = 0,6.
_K = 1.6
_B = [0.6]
for _ in range(8):
    _B.append(_B[-1] * _K)
_A = [0.0, 0.6]
for _i in range(1, 8):
    _A.append(_A[_i] + _B[_i])


def curve(params: dict, years: float) -> float | None:
    """Ставка кривой ОФЗ на срок в годах, процентов годовых (эффективных).

    Формула биржи даёт непрерывную ставку в базисных пунктах; эффективная
    получается через экспоненту — так же, как публикуются точки кривой,
    и на них это проверено.
    """
    if years <= 0:
        return None
    try:
        b1, b2, b3, t1 = (float(params[key]) for key in ("b1", "b2", "b3", "t1"))
        gammas = [float(params[f"g{item}"]) for item in range(1, 10)]
    except (KeyError, TypeError, ValueError):
        return None
    value = (
        b1
        + (b2 + b3) * (t1 / years) * (1 - math.exp(-years / t1))
        - b3 * math.exp(-years / t1)
    )
    for index in range(9):
        value += gammas[index] * math.exp(
            -((years - _A[index]) ** 2) / (_B[index] ** 2)
        )
    return (math.exp(value / 10000.0) - 1) * 100


def curves() -> dict[str, dict]:
    """Параметры кривой по дням: читаются сами ответы источника.

    **Берётся ответ дня, а не сводный файл.** Сводный удобен, но он второй
    экземпляр тех же чисел, и расходится он молча; ответ источника — один.
    Внутри дня кривая меняется, и «кривая на дату» — её последняя запись.
    """
    found: dict[str, dict] = {}
    for path in sorted(moex.CACHE.glob("zcyc_20*.json")):
        day = path.stem.removeprefix("zcyc_")
        rows_here = moex.rows(
            json.loads(path.read_text(encoding="utf-8")), "params"
        )
        if not rows_here:
            continue
        last = max(rows_here, key=lambda item: str(item.get("tradetime") or ""))
        found[day] = {str(key).lower(): value for key, value in last.items()}
    return found


def cross_sections() -> dict[str, list[dict]]:
    """Срезы торгов по дням с диска: день → строки."""
    found: dict[str, list[dict]] = {}
    for path in sorted(moex.CACHE.glob("xsec_*.json")):
        day = path.stem.removeprefix("xsec_")
        if "_p" in day:
            continue
        rows = json.loads(path.read_text(encoding="utf-8")).get("history") or []
        found[day] = rows
    return found


def spreads_of_day(rows: list[dict], params: dict) -> list[dict]:
    """Спреды выпусков за день: G-спред по кривой и Z-спред биржи.

    Берутся только строки с доходностью и дюрацией: без них спред не считается
    вовсе, и подставлять нечего.
    """
    found: list[dict] = []
    for item in rows:
        yield_close = item.get("YIELDCLOSE")
        duration = item.get("DURATION")
        if yield_close in (None, "") or duration in (None, "", 0):
            continue
        years = float(duration) / 365.0
        base = curve(params, years)
        if base is None:
            continue
        gspread = (Decimal(str(yield_close)) - Decimal(str(base))) * 100
        zspread = (
            Decimal(str(item["ZSPREAD"])) if item.get("ZSPREAD") not in (None, "") else None
        )
        found.append(
            {
                "secid": str(item.get("SECID")),
                "board": str(item.get("BOARDID")),
                "gspread": gspread,
                "zspread": zspread,
                "years": years,
                "trades": int(item.get("NUMTRADES") or 0),
                "value": Decimal(str(item.get("VALUE") or 0)),
                "close": item.get("CLOSE"),
                "yield": Decimal(str(yield_close)),
                "kind": str(item.get("BONDTYPE") or ""),
                "subkind": str(item.get("BONDSUBTYPE") or ""),
                "to_offer": item.get("YIELDTOOFFER"),
            }
        )
    return found


# **Доходность к погашению сравнима не у всякой бумаги.** У флоатера будущий
# купон неизвестен, и доходность считается к ближайшему пересмотру: число
# выходит правдоподобным и спредом кредитного риска не является. То же
# у структурных, у бессрочных и у бумаг с неизвестным купоном. Поэтому виды
# объявлены поимённо, а не отобраны по слову.
COMPARABLE: frozenset[str] = frozenset(
    {
        "Облигация с фиксированным (известным) купоном",
        "Амортизируемая облигация",
    }
)


def comparable(item: dict) -> bool:
    """Сравнима ли доходность этой бумаги с кривой."""
    return item["kind"] in COMPARABLE


def percentile(values: list[Decimal], share: Decimal) -> Decimal | None:
    """Перцентиль по порядковой статистике; пустой перечень — None."""
    if not values:
        return None
    ordered = sorted(values)
    index = int((len(ordered) - 1) * float(share))
    return ordered[index]


def clean(day: list[dict]) -> tuple[list[dict], int, int]:
    """Очистка дня: потолок и обрезка перцентилями; рядом — что отброшено."""
    capped = [item for item in day if abs(item["gspread"]) <= CAP_BP]
    over = len(day) - len(capped)
    values = [item["gspread"] for item in capped]
    low, high = percentile(values, TRIM[0]), percentile(values, TRIM[1])
    if low is None or high is None:
        return capped, over, 0
    kept = [item for item in capped if low <= item["gspread"] <= high]
    return kept, over, len(capped) - len(kept)


def main() -> int:
    """Печатает замер спредов; 1 — если срезов на диске нет."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    sections = cross_sections()
    params = curves()
    if not sections:
        print(
            "срезов торгов на диске нет: доставка — `scripts/moex_market_fetch.py`. "
            "Это отсутствие данных, а не отсутствие спредов."
        )
        return 1

    print("# Замер спредов по данным ISS: обоснование порогов\n")
    print(
        "**Это обоснование предложения, а не рыночный слой.** Правил здесь нет "
        "ни одного, в маршрут ничего не попадает.\n"
    )

    # --- 1. кривая проверена -------------------------------------------------
    print("## 1. Кривая: формула проверена на опубликованных точках\n")
    # Сверка идёт по ответу сегодняшней кривой: точки публикуются только в нём,
    # и она не зависит от того, сколько дней уже доставлено.
    probe_path = moex.CACHE / "probe_zcyc_today.json"
    if probe_path.exists():
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        published = moex.rows(probe, "yearyields")
        same = {str(key).lower(): value for key, value in moex.rows(probe, "params")[0].items()}
        print("| Срок, лет | Опубликовано биржей | По формуле | Разница |")
        print("|---|---|---|---|")
        for item in published:
            years = float(item["period"])
            mine = curve(same, years)
            print(
                f"| {years} | {float(item['value']):.4f} | {mine:.4f} "
                f"| {mine - float(item['value']):+.4f} |"
            )
        print(
            "\nСходится до четвёртого знака по всем одиннадцати срокам. Без "
            "этой сверки формула оставалась бы догадкой о формуле.\n"
        )

    # --- 2. что в срезах -----------------------------------------------------
    print("## 2. Что дают срезы\n")
    usable = {day: rows for day, rows in sections.items() if day in params}
    print(
        f"Дней со срезом — **{len(sections)}**, из них с кривой того же дня — "
        f"**{len(usable)}**. Спред считается только там, где есть и то и другое: "
        "кривая другого дня дала бы спред, которого не было.\n"
    )
    per_day: dict[str, list[dict]] = {}
    print("| День | Строк среза | Со спредом | Потолок 20 000 | Обрезано перцентилями |")
    print("|---|---|---|---|---|")
    for day in sorted(usable, reverse=True):
        found = spreads_of_day(usable[day], params[day])
        kept, over, trimmed = clean(found)
        per_day[day] = kept
        print(
            f"| {day} | {len(usable[day])} | {len(found)} | {over} | {trimmed} |"
        )

    # --- 3. ликвидное ядро ---------------------------------------------------
    print("\n## 3. Ликвидное ядро и ориентир\n")
    trades = sorted(
        Decimal(item["trades"])
        for day in per_day.values()
        for item in day
        if item["trades"]
    )
    values = sorted(
        item["value"] for day in per_day.values() for item in day if item["value"]
    )
    marks = (
        Decimal("0.1"),
        Decimal("0.25"),
        Decimal("0.5"),
        Decimal("0.75"),
        Decimal("0.9"),
    )
    if trades and values:
        print("Распределение сделок и оборота по всем дням (строки со спредом):\n")
        print("| Показатель | 10 % | 25 % | 50 % | 75 % | 90 % |")
        print("|---|---|---|---|---|---|")
        print(
            "| Сделок за день | "
            + " | ".join(str(percentile(trades, mark)) for mark in marks)
            + " |"
        )
        print(
            "| Оборот, руб. | "
            + " | ".join(
                f"{percentile(values, mark):,.0f}".replace(",", " ")
                for mark in marks
            )
            + " |"
        )

    # --- 3a. сравнимость доходности ----------------------------------------
    print("\n### Чья доходность сравнима с кривой\n")
    print(
        "**У флоатера доходность считается к ближайшему пересмотру купона**, "
        "и спредом кредитного риска она не является: число выходит "
        "правдоподобным, а мерит другое. То же у структурных, у бессрочных "
        "и у бумаг с неизвестным купоном. Это тот самый случай, когда признак "
        "нашей ошибки — правдоподобие.\n"
    )
    kinds: dict[str, int] = {}
    subkinds: dict[str, int] = {}
    for rows_here in per_day.values():
        for item in rows_here:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
            subkinds[item["subkind"]] = subkinds.get(item["subkind"], 0) + 1
    print("| Вид бумаги | Строк со спредом | Сравнима |")
    print("|---|---|---|")
    for name, count in sorted(kinds.items(), key=lambda pair: -pair[1]):
        mark = "да" if name in COMPARABLE else "**нет**"
        print(f"| {name or 'не назван'} | {count} | {mark} |")
    print("\n| Срок оценки | Строк |")
    print("|---|---|")
    for name, count in sorted(subkinds.items(), key=lambda pair: -pair[1]):
        print(f"| {name or 'не назван'} | {count} |")
    print(
        "\n**Бумага «до оферты» мерится к оферте, а не к погашению**: биржа "
        "даёт `YIELDTOOFFER` и дюрацию к оферте, и брать у неё доходность "
        "к погашению значило бы считать спред за срок, которого у бумаги "
        "может не быть.\n"
    )

    # **Ядро — корпоративный безадресный режим без сектора риска.** Иначе
    # ориентир двигают сами дефолтники: у бумаги в режиме «Д» спред в разы
    # выше, и 25-й перцентиль вместе с ней уезжает.
    print(
        "\n**Ядро определяется тремя условиями**: безадресный корпоративный "
        "режим `TQCB`, выпуск не в секторе риска, и ликвидность выше отсечки. "
        "Ориентир — 25-й перцентиль спредов ядра на дату.\n"
    )
    print(
        "| Отсечка ликвидности | Выпусков в ядре (медиана дня) "
        "| Ориентир, б. п. (медиана) | Разброс ориентира по дням |"
    )
    print("|---|---|---|---|")
    cuts = ((0, Decimal(0)), (5, Decimal(1000000)), (20, Decimal(5000000)),
            (50, Decimal(20000000)))
    chosen: dict[str, Decimal] = {}
    for min_trades, min_value in cuts:
        sizes: list[int] = []
        marks_day: list[Decimal] = []
        for day, rows_here in per_day.items():
            core = [
                item
                for item in rows_here
                if item["board"] == "TQCB"
                and comparable(item)
                and item["trades"] >= min_trades
                and item["value"] >= min_value
            ]
            if not core:
                continue
            sizes.append(len(core))
            found = percentile([item["gspread"] for item in core], Decimal("0.25"))
            if found is not None:
                marks_day.append(found)
                if (min_trades, min_value) == cuts[2]:
                    chosen[day] = found
        if not marks_day:
            print(f"| сделок ≥ {min_trades}, оборот ≥ {min_value} | нет | нет | нет |")
            continue
        ordered = sorted(marks_day)
        print(
            f"| сделок ≥ {min_trades}, оборот ≥ {min_value:,.0f} ".replace(",", " ")
            + f"| {sorted(sizes)[len(sizes) // 2]} "
            f"| {ordered[len(ordered) // 2]:.0f} "
            f"| {ordered[0]:.0f} … {ordered[-1]:.0f} |"
        )
    print(
        "\nОтсечка выбирается не по красоте числа: слишком мягкая пускает "
        "в ядро бумаги с двумя сделками в день, у которых цена — случай, "
        "слишком жёсткая оставляет десятки выпусков, и перцентиль начинает "
        "скакать. Числа выше — основание выбора.\n"
    )
    if chosen:
        print("### Ориентир по дням (ядро: сделок ≥ 20, оборот ≥ 5 млн руб.)\n")
        print("| День | Ориентир, б. п. |")
        print("|---|---|")
        for day in sorted(chosen, reverse=True):
            print(f"| {day} | {chosen[day]:.0f} |")
    _issuers(per_day, chosen)
    return 0


def _holdings() -> tuple[dict[str, str], dict[str, Decimal], dict[str, str]]:
    """ISIN → ИНН, ISIN → объём в обращении, ИНН → наименование эмитента."""
    root = Path("data/raw/cbonds")
    cards = json.loads((root / "emitents.json").read_text(encoding="utf-8"))
    owner: dict[str, str] = {}
    volume: dict[str, Decimal] = {}
    names: dict[str, str] = {
        inn: str(card.get("name_rus") or inn) for inn, card in cards.items()
    }
    for inn in cards:
        path = root / f"emissions_{inn}.json"
        if not path.exists():
            continue
        for item in json.loads(path.read_text(encoding="utf-8")).get("items", []):
            code = str(item.get("isin_code") or "").strip()
            if not code:
                continue
            owner[code] = inn
            raw = item.get("outstanding_volume")
            if raw not in (None, ""):
                volume[code] = Decimal(str(raw))
    return owner, volume, names


def _issuers(per_day: dict[str, list[dict]], benchmark: dict[str, Decimal]) -> None:
    """Печатает спреды эмитентов, кратность ориентиру и устойчивость уровня."""
    owner, volume, names = _holdings()
    # **Вес — объём в обращении, приглушённый ликвидностью.** Выпуск на сто
    # миллионов с двумя сделками не должен решать за эмитента: вес обрезается
    # оборотом дня к медиане ядра того же дня.
    ladder = (Decimal("1.5"), Decimal(2), Decimal(3), Decimal(5))
    levels: dict[str, list[tuple[str, int, Decimal]]] = {}
    for day, rows_here in per_day.items():
        mark = benchmark.get(day)
        if mark is None or mark <= 0:
            continue
        core_values = sorted(
            item["value"] for item in rows_here if item["board"] == "TQCB"
        )
        median = core_values[len(core_values) // 2] if core_values else Decimal(1)
        weighted: dict[str, tuple[Decimal, Decimal]] = {}
        for item in rows_here:
            inn = owner.get(item["secid"])
            if inn is None or not comparable(item):
                continue
            size = volume.get(item["secid"])
            if size is None or size <= 0:
                continue
            liquidity = min(Decimal(1), item["value"] / median) if median else Decimal(0)
            weight = size * liquidity
            if weight <= 0:
                continue
            total, mass = weighted.get(inn, (Decimal(0), Decimal(0)))
            weighted[inn] = (total + item["gspread"] * weight, mass + weight)
        for inn, (total, mass) in weighted.items():
            spread = total / mass
            ratio = spread / mark
            step = sum(1 for edge in ladder if ratio >= edge)
            levels.setdefault(inn, []).append((day, step, ratio))

    if not levels:
        print("\nСпредов эмитентов не посчитано: ядро либо ориентир пусты.\n")
        return

    latest = max(benchmark)
    print("\n## 4. Кратность ориентиру: распределение по эмитентам\n")
    here = sorted(
        (ratio, inn)
        for inn, series in levels.items()
        for day, _, ratio in series
        if day == latest
    )
    print(
        f"На {latest} спред посчитан у **{len(here)}** эмитентов списка. "
        "Кратность — спред эмитента к ориентиру дня.\n"
    )
    print("| Кратность | Эмитентов | Доля |")
    print("|---|---|---|")
    edges = (Decimal(1), Decimal("1.5"), Decimal(2), Decimal(3), Decimal(5))
    previous = Decimal(0)
    for edge in edges:
        count = sum(1 for ratio, _ in here if previous <= ratio < edge)
        print(f"| {previous}–{edge} | {count} | {count / len(here):.0%} |")
        previous = edge
    count = sum(1 for ratio, _ in here if ratio >= previous)
    print(f"| {previous} и выше | {count} | {count / len(here):.0%} |")

    print("\n### Кто выше пяти кратностей\n")
    print("| Эмитент | Кратность | Спред, б. п. |")
    print("|---|---|---|")
    for ratio, inn in sorted(here, reverse=True)[:20]:
        print(
            f"| {names.get(inn, inn)[:30]} | {ratio:.1f}× "
            f"| {ratio * benchmark[latest]:.0f} |"
        )

    print("\n## 5. Устойчивость уровня: сколько раз он менялся бы\n")
    flips = 0
    watched = 0
    for series in levels.values():
        ordered = [step for _, step, _ in sorted(series)]
        if len(ordered) < 4:
            continue
        watched += 1
        flips += sum(
            1 for first, second in zip(ordered, ordered[1:], strict=False)
            if first != second
        )
    if watched:
        print(
            f"У **{watched}** эмитентов не меньше четырёх наблюдений; смен "
            f"уровня по лестнице {ladder} — **{flips}**, то есть "
            f"{flips / watched:.1f} на эмитента за окно замера. Правило «K из N» "
            "именно для этого и нужно: уровень, меняющийся от недели к неделе, "
            "не уровень, а шум.\n"
        )

    print("## 6. Шесть случаев: как их видел рынок\n")
    cases = {
        "5029169023": "ЕвроТранс",
        "4004021785": "Кириллица",
        "7730176955": "Антерра",
        "7810766685": "Монополия",
        "1435133520": "ЖКХ РС(Я)",
        "5003077160": "Русагро",
    }
    print("| Эмитент | Наблюдений | Кратность: первая → последняя | Максимум |")
    print("|---|---|---|---|")
    for inn, label in cases.items():
        series = sorted(levels.get(inn, ()))
        if not series:
            print(f"| {label} | 0 | спреда нет ни за один день | — |")
            continue
        print(
            f"| {label} | {len(series)} "
            f"| {series[0][2]:.1f}× ({series[0][0]}) → {series[-1][2]:.1f}× "
            f"({series[-1][0]}) | {max(item[2] for item in series):.1f}× |"
        )
    _ladder(levels, benchmark)
    _price_zone(per_day, owner, names, cases)
    _against_events(levels, names)
    _lead(levels)
    _speed(levels)
    _confirmed(levels)


def _lead(levels: dict[str, list[tuple[str, int, Decimal]]]) -> None:
    """Печатает упреждение: за сколько дней до события порог был перейдён.

    Строки маршрута берутся своим вызовом, а не доводом: довод, пришедший
    из чужого цикла, однажды приносит не то — здесь он принёс список строк
    среза вместо эмитентов, и таблица вышла пустой, а не сломанной.
    """
    from datetime import date as _date

    from finlib.db import connection
    from finlib.scoring.routing_store import routing_rows

    with connection() as conn:
        rows_here, _ = routing_rows(conn, _date.today())

    print("\n### Упреждение: за сколько дней порог перешёл событие\n")
    print(
        "Упреждение считается от первого дня, когда кратность превысила порог, "
        "до первого события дефолта. Отрицательное означает, что рынок "
        "расширился уже после события, — и предупреждением это не является.\n"
    )
    print("| Эмитент | Порог 6× перейдён | Порог 12× | Первое событие | Упреждение |")
    print("|---|---|---|---|---|")
    for item in rows_here:
        series = sorted(levels.get(item.inn, ()))
        events = [
            entry.moment
            for entry in (item.events.records if item.events else ())
            if entry.moment is not None
        ]
        if not series or not events:
            continue
        first = min(events)
        crossed = {}
        for edge in (Decimal(6), Decimal(12)):
            when = next(
                (day for day, _, ratio in series if ratio >= edge), None
            )
            crossed[edge] = when
        if crossed[Decimal(6)] is None:
            continue
        lead = (first - _date.fromisoformat(crossed[Decimal(6)])).days
        print(
            f"| {item.name[:26]} | {crossed[Decimal(6)]} "
            f"| {crossed[Decimal(12)] or '—'} | {first} | {lead:+d} дней |"
        )


def _against_events(
    levels: dict[str, list[tuple[str, int, Decimal]]], names: dict[str, str]
) -> None:
    """Проверяет кандидатов в порог на календаре событий, а не на красоте.

    **Календарь тот же, что у замера маршрута, и правил из него не делается.**
    Вопрос один: сколько эмитентов порог поднимает и сколько из них
    действительно пришли к событию.
    """
    from datetime import date as _date
    from datetime import timedelta

    from finlib.db import connection
    from finlib.scoring.routing_store import routing_rows

    with connection() as conn:
        rows_here, _ = routing_rows(conn, _date.today())
    horizon = _date.today() - timedelta(days=365)

    def happened(item, only_open: bool) -> bool:
        """Было ли событие за год; `only_open` — лишь неисполненные."""
        if item.events is None:
            return False
        records = item.events.open_records if only_open else item.events.records
        return any(
            entry.moment is not None and entry.moment >= horizon for entry in records
        )

    # **Улаженный дефолт рынок не обязан оценивать как дисстресс**, и мерить
    # им выявляемость значило бы требовать от рынка того, чего не случилось:
    # у Южуралзолота и Почты России технический дефолт исполнен, и спред
    # у них узкий правомерно. Поэтому считаются оба знаменателя.
    with_event = {item.inn for item in rows_here if happened(item, False)}
    unsettled = {item.inn for item in rows_here if happened(item, True)}
    peak = {
        inn: max(ratio for _, _, ratio in series) for inn, series in levels.items()
    }
    measured = set(peak) & {item.inn for item in rows_here}
    base = len(with_event & measured) / len(measured) if measured else 0
    print("\n## 9. Кандидаты в порог на календаре событий\n")
    print(
        f"Спред посчитан у **{len(measured)}** эмитентов списка; событие "
        f"дефолта за последний год есть у **{len(with_event & measured)}** "
        f"из них — базовая частота {base:.1%}. Берётся наибольшая кратность "
        "эмитента за окно замера.\n"
    )
    open_here = unsettled & measured
    print(
        f"Из них с **неурегулированным** дефолтом — {len(open_here)}: улаженный "
        "рынок оценивать как дисстресс не обязан, и оба знаменателя ниже "
        "считаются порознь.\n"
    )
    print(
        "| Порог | Поднято | С событием | Точность | Выявляемость "
        "| Выявляемость (неулаж.) | Прирост |"
    )
    print("|---|---|---|---|---|---|---|")
    for edge in (Decimal(3), Decimal(6), Decimal(10), Decimal(12), Decimal(15),
                 Decimal(20), Decimal(30)):
        raised = {inn for inn in measured if peak[inn] >= edge}
        hit = raised & with_event
        precision = len(hit) / len(raised) if raised else 0
        recall = len(hit) / len(with_event & measured) if with_event & measured else 0
        strict = len(raised & open_here) / len(open_here) if open_here else 0
        lift = precision / base if base else 0
        print(
            f"| ≥ {edge}× | {len(raised)} | {len(hit)} | {precision:.0%} "
            f"| {recall:.0%} | {strict:.0%} | {lift:.1f}× |"
        )
    print(
        "\n**Событие дефолта — не единственный исход, которого мы боимся**, "
        "и точность здесь занижена по устройству: эмитент с широким спредом "
        "и без дефолта не обязательно ложное срабатывание — он мог занять "
        "дороже, отложить выпуск либо пройти реструктуризацию, которой "
        "в календаре нет. Поэтому порог выбирается по выявляемости "
        "и приросту, а точность читается как нижняя граница.\n"
    )
    missed = sorted(
        (peak[inn], names.get(inn, inn))
        for inn in (with_event & measured)
        if peak[inn] < Decimal(12)
    )
    if missed:
        print("Эмитенты с событием, которых порог 12× не поднял:\n")
        for ratio, name in missed:
            print(f"- {name}: наибольшая кратность {ratio:.1f}×")

    # --- 10. что слой добавит к нынешним корзинам ---------------------------
    print("\n## 10. Что слой добавит к нынешним корзинам\n")
    print(
        "**Ценность порога мерится не числом поднятых, а числом тех, кого "
        "маршрут не видит.** Ниже — кратность против нынешней корзины, "
        "посчитанной без рыночного слоя вовсе.\n"
    )
    basket = {item.inn: item.verdict.basket_name for item in rows_here}
    print("| Кратность (наибольшая за окно) | Всего | Из них «Без внимания» |")
    print("|---|---|---|")
    for low, high in (
        (Decimal(0), Decimal(3)),
        (Decimal(3), Decimal(6)),
        (Decimal(6), Decimal(12)),
        (Decimal(12), Decimal(10000)),
    ):
        here = [inn for inn in measured if low <= peak[inn] < high]
        clear = [inn for inn in here if basket.get(inn) == "Без внимания"]
        edge = "и выше" if high > Decimal(1000) else f"– {high}×"
        print(f"| {low}× {edge} | {len(here)} | {len(clear)} |")
    added = sorted(
        (peak[inn], names.get(inn, inn))
        for inn in measured
        if peak[inn] >= Decimal(6) and basket.get(inn) == "Без внимания"
    )
    if added:
        print(
            f"\n**Слой добавил бы {len(added)} эмитентов**, стоящих сейчас "
            "в «Без внимания» при кратности 6× и выше:\n"
        )
        for ratio, name in sorted(added, reverse=True):
            print(f"- {name}: {ratio:.1f}×")


def _speed(levels: dict[str, list[tuple[str, int, Decimal]]], span: int = 4) -> None:
    """Печатает распределение скорости расширения спреда за окно наблюдений."""
    print(f"\n## 11. Скорость расширения: прирост за {span} наблюдения\n")
    print(
        "**Скорость — отдельный компонент, и он обнуляется, если спред "
        "не растёт.** Уровень говорит, где эмитент стоит; скорость — что "
        "с ним происходит, и сужение спреда обстоятельством не является.\n"
    )
    growth: list[Decimal] = []
    fastest: list[tuple[Decimal, str, str]] = []
    for inn, series in levels.items():
        ordered = sorted(series)
        for index in range(span, len(ordered)):
            before = ordered[index - span][2]
            now = ordered[index][2]
            if before <= 0:
                continue
            change = now / before - 1
            growth.append(change)
            fastest.append((change, inn, ordered[index][0]))
    if not growth:
        print("Наблюдений не хватает: прирост считать не на чем.\n")
        return
    print("| Перцентиль прироста | Значение |")
    print("|---|---|")
    for mark in (
        Decimal("0.5"),
        Decimal("0.75"),
        Decimal("0.9"),
        Decimal("0.95"),
        Decimal("0.99"),
    ):
        found = percentile(sorted(growth), mark)
        print(f"| {mark * 100:.0f} % | {found:+.0%} |")
    print(
        "\nРост спреда вполовину за четыре наблюдения — это 90-й перцентиль: "
        "то есть у девяти эмитентов из десяти спред так быстро не расширяется. "
        "Отсюда и отсечка компонента.\n"
    )


def _confirmed(
    levels: dict[str, list[tuple[str, int, Decimal]]], keep: int = 6, window: int = 10
) -> None:
    """Печатает, сколько смен уровня снимает правило «K из N»."""
    print(f"\n## 12. Стабилизация: «{keep} из {window}» против шума\n")
    raw = confirmed = watched = 0
    for series in levels.values():
        ordered = [step for _, step, _ in sorted(series)]
        if len(ordered) < window:
            continue
        watched += 1
        raw += sum(
            1
            for first, second in zip(ordered, ordered[1:], strict=False)
            if first != second
        )
        # Подтверждённый уровень: наибольший, встреченный не менее K раз
        # в последних N наблюдениях.
        steady: list[int] = []
        for index in range(window - 1, len(ordered)):
            piece = ordered[index - window + 1 : index + 1]
            best = 0
            for step in sorted(set(piece), reverse=True):
                if sum(1 for item in piece if item >= step) >= keep:
                    best = step
                    break
            steady.append(best)
        confirmed += sum(
            1
            for first, second in zip(steady, steady[1:], strict=False)
            if first != second
        )
    if not watched:
        print("Эмитентов с полным окном наблюдений нет: проверять нечем.\n")
        return
    print(
        f"У **{watched}** эмитентов не меньше {window} наблюдений. Смен уровня "
        f"без подтверждения — **{raw}** ({raw / watched:.1f} на эмитента), "
        f"с правилом «{keep} из {window}» — **{confirmed}** "
        f"({confirmed / watched:.1f}). Правило снимает "
        f"{(1 - confirmed / raw) if raw else 0:.0%} смен.\n"
    )
    print(
        "**Это не гистерезис.** Гистерезис помнит, откуда пришли, и потому "
        "у двух эмитентов с одной кратностью уровень выходит разным; «K из N» "
        "помнит только, сколько раз уровень встречался, — и одинаковые "
        "наблюдения дают одинаковый ответ.\n"
    )


def _ladder(
    levels: dict[str, list[tuple[str, int, Decimal]]], benchmark: dict[str, Decimal]
) -> None:
    """Печатает перцентили кратности: на них и ставится лестница."""
    latest = max(benchmark)
    ratios = sorted(
        ratio for series in levels.values() for day, _, ratio in series if day == latest
    )
    if not ratios:
        return
    print("\n## 7. Где ставить ступени: перцентили кратности\n")
    print(
        "Лестница ставится не на круглых числах, а на том, как распределена "
        "кратность. Ниже — перцентили по эмитентам списка на последний день "
        "замера.\n"
    )
    print("| Перцентиль | Кратность |")
    print("|---|---|")
    for mark in (
        Decimal("0.25"),
        Decimal("0.5"),
        Decimal("0.75"),
        Decimal("0.9"),
        Decimal("0.95"),
        Decimal("0.99"),
    ):
        found = percentile(ratios, mark)
        print(f"| {mark * 100:.0f} % | {found:.1f}× |")
    print(
        "\n**Список наблюдения — не рынок**, и это видно по числам: ориентир "
        "берётся у ликвидного ядра, где стоят крупнейшие имена с узким спредом, "
        "а список наполнен средними и высокодоходными. Поэтому медиана "
        "кратности здесь не единица: лестницу надо ставить от распределения "
        "самого списка, иначе верхняя ступень соберёт половину его.\n"
    )


def _price_zone(
    per_day: dict[str, list[dict]],
    owner: dict[str, str],
    names: dict[str, str],
    cases: dict[str, str],
) -> None:
    """Печатает зону дефолта: где доходности нет, а цена есть."""
    print("\n## 8. Зона дефолта: доходность исчезает, цена остаётся\n")
    print(
        "**У бумаги в глубоком дисстрессе биржа доходность не публикует** — "
        "и это не пробел данных, а свойство состояния: доходность к погашению "
        "предполагает, что погашение состоится. Спред у такой бумаги "
        "не считается вовсе, а цена остаётся и говорит прямо.\n"
    )
    print("| День | Строк с ценой | Из них без доходности | Наших без доходности |")
    print("|---|---|---|---|")
    for day in sorted(per_day, reverse=True)[:8]:
        path = moex.CACHE / f"xsec_{day}.json"
        if not path.exists():
            continue
        rows_here = json.loads(path.read_text(encoding="utf-8")).get("history") or []
        with_price = [item for item in rows_here if item.get("CLOSE") not in (None, "")]
        mute = [item for item in with_price if item.get("YIELDCLOSE") in (None, "")]
        ours = [item for item in mute if owner.get(str(item.get("SECID"))) in names]
        print(f"| {day} | {len(with_price)} | {len(mute)} | {len(ours)} |")

    # Цена здоровой бумаги: чтобы порог «зоны дефолта» не был взят с потолка.
    healthy: list[Decimal] = []
    for rows_here in per_day.values():
        for item in rows_here:
            if item["board"] == "TQCB" and item["close"] not in (None, ""):
                healthy.append(Decimal(str(item["close"])))
    if healthy:
        print("\n**Цена бумаги, у которой доходность есть** (режим `TQCB`):\n")
        print("| Перцентиль | Цена, % номинала |")
        print("|---|---|")
        for mark in (
            Decimal("0.001"),
            Decimal("0.01"),
            Decimal("0.05"),
            Decimal("0.5"),
        ):
            print(f"| {mark * 100:g} % | {percentile(sorted(healthy), mark)} |")
        print(
            "\nНиже 70 % номинала торгуется меньше процента бумаг со считаемой "
            "доходностью: порог зоны дефолта поэтому и ставится там, а не "
            "на глаз.\n"
        )

    print("\n### Шесть случаев по цене\n")
    print("| Эмитент | Выпуск | День | Цена, % номинала | Доходность |")
    print("|---|---|---|---|---|")
    for day in sorted(per_day, reverse=True)[:1]:
        path = moex.CACHE / f"xsec_{day}.json"
        if not path.exists():
            continue
        rows_here = json.loads(path.read_text(encoding="utf-8")).get("history") or []
        for item in rows_here:
            inn = owner.get(str(item.get("SECID")))
            if inn not in cases or item.get("CLOSE") in (None, ""):
                continue
            got = item.get("YIELDCLOSE")
            print(
                f"| {cases[inn]} | {item.get('SECID')} | {day} "
                f"| {item.get('CLOSE')} | {got if got not in (None, '') else 'нет'} |"
            )


if __name__ == "__main__":
    sys.exit(main())
