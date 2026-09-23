"""Зазор у границы: чем гасится дребезг меры рефинансирования. **Только замер.**

    uv run python eval/refinancing_gap_run.py > data/output/refinancing_gap.md

**В маршрут отсюда не вводится ничего.** Замер отвечает на вопрос, заданный
пересчитанной историей: из возвратов внутри окна почти все — одна мера,
платежи ближайшего года против денежных средств. Окно едет вместе с днём,
и у эмитента с покрытием на самой границе один платёж, стоящий почти в году
от сегодня, вносит основание и выносит обратно.

Считаются два способа унять это, и оба — с ценой:

* **зазор по величине** (гистерезис): основание ставится, когда покрытие ниже
  единицы, и снимается, только когда оно поднялось выше объявленного порога.
  Одна граница превращается в две, и между ними состояние зависит от того,
  с какой стороны пришли;
* **подтверждение устойчивости** «K из N»: основание ставится, когда условие
  держалось в K точках из последних N. Граница остаётся одна, но срабатывание
  запаздывает — ровно на то, чем оно подтверждается.

**Цена меряется тремя числами, а не одним.** Сколько возвратов остаётся —
это польза; сколько эмитентов при этом меняет корзину — это цена сегодня;
на сколько дней запаздывает срабатывание у тех, кто потом допустил дефолт, —
это цена в том единственном случае, ради которого мера и заведена.

**Ряд собирается боевым путём.** Величины берёт `routing_rows` с названной
датой — то же место, что список, пересчёт и распределение; второй путь
к покрытию разошёлся бы с первым. Собранный ряд кладётся на диск, и повторный
прогон считает по нему, не обходя год заново.

**Записанной истории корзин для этого мало, и это не придирка.** Корзину
называют основания той тяжести, по которой она выбрана, поэтому у эмитента
в «Разборе» основание рефинансирования в перечне не стоит вовсе — оно есть,
но корзину не называет. Ряд, собранный по записанным основаниям, объявил бы
таких эмитентов несрабатывающими.
"""

import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from routing_backfill_run import grid  # noqa: E402

from finlib.db import connection  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import default_records, issues_of  # noqa: E402

logger = logging.getLogger(__name__)

SERIES = Path("data/output/refinancing_series.json")

# Основание, о котором идёт речь. Второе (оферты) считается рядом: отсечка
# у него та же, и дребезжать оно обязано так же.
GAP = "refinancing_gap"
OFFERS = "refinancing_offers"

# Пороги снятия для зазора по величине: покрытие выше этого — основание
# снимается. **Числа здесь не методика, а предмет замера**: решение о том,
# вводить ли зазор и какой, принимает человек по этим числам.
CLEARS = (Decimal("1.00"), Decimal("1.05"), Decimal("1.10"))

# Подтверждение устойчивости: сколько точек из последних скольких. Сетка
# пересчёта недельная, поэтому N точек — это N недель, и «7 из 10» рыночной
# методики означает здесь два с половиной месяца.
CONFIRMATIONS = ((2, 3), (3, 5), (7, 10))


def collect() -> dict:
    """Ряд покрытия и срабатываний по сетке пересчёта; читается с диска.

    Собирается он тем же вызовом, которым строится список, и с той же датой:
    `as_of` делает маршрут таким, каким он был бы в тот день.
    """
    if SERIES.exists():
        return json.loads(SERIES.read_text(encoding="utf-8"))
    routing = load_routing()
    rule = routing.history
    dates, _ = grid(date.today(), rule.step_days, rule.depth_days)
    cover: dict[str, list[str | None]] = defaultdict(lambda: [None] * len(dates))
    fired: dict[str, list[int]] = defaultdict(lambda: [0] * len(dates))
    offers: dict[str, list[int]] = defaultdict(lambda: [0] * len(dates))
    basket: dict[str, list[str]] = defaultdict(lambda: [""] * len(dates))
    others: dict[str, list[int]] = defaultdict(lambda: [0] * len(dates))
    memo: dict = {}
    with connection() as conn:
        for number, moment in enumerate(dates):
            rows, _ = routing_rows(conn, moment, as_of=moment, memo=memo)
            for row in rows:
                money = row.refinance
                if money is not None and money.due and money.cash is not None:
                    # Покрытие, а не отношение долга: так сказано условие
                    # методики — «денежных средств не хватает на платежи».
                    cover[row.inn][number] = str(money.cash / money.due)
                grounds = {item.ground for item in row.verdict.findings}
                fired[row.inn][number] = int(GAP in grounds)
                offers[row.inn][number] = int(OFFERS in grounds)
                basket[row.inn][number] = row.verdict.basket
                # Прочие основания корзины: без них не сказать, сменится ли
                # корзина, если снять основание рефинансирования.
                others[row.inn][number] = len(
                    {item for item in row.verdict.grounds} - {GAP, OFFERS}
                )
            logger.info("%s: строк %d", moment, len(rows))
    found = {
        "dates": [f"{item}" for item in dates],
        "cover": cover,
        "fired": fired,
        "offers": offers,
        "basket": basket,
        "others": others,
    }
    SERIES.parent.mkdir(parents=True, exist_ok=True)
    SERIES.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
    return found


def switches(series: list[int]) -> int:
    """Сколько раз состояние переключилось: и включений, и выключений."""
    return sum(
        1
        for first, second in zip(series, series[1:], strict=False)
        if first != second
    )


def returns_of(series: list[int], dates: list[date], window: int) -> int:
    """Переключения, отменённые обратно внутри окна.

    Определение то же, что у возвратов корзины: смена, которую отменили
    в пределах объявленного окна, и считается она по паре переключений.
    """
    marks = [
        (number, series[number])
        for number in range(1, len(series))
        if series[number] != series[number - 1]
    ]
    found = 0
    for first, second in zip(marks, marks[1:], strict=False):
        if (dates[second[0]] - dates[first[0]]).days <= window:
            found += 1
    return found


def with_gap(cover: list[str | None], clear: Decimal) -> list[int]:
    """Состояние с зазором: ставится ниже единицы, снимается выше порога.

    **Точка без покрытия состояния не меняет.** Величины может не быть —
    графика нет на диске, денежные средства не раскрыты, — и объявлять
    основание снятым по отсутствию величины значило бы решать по пробелу.
    """
    state = 0
    found: list[int] = []
    for value in cover:
        if value is None:
            # **Пробел не срабатывание и не снятие.** Маршрут при отсутствии
            # величины основания не ставит вовсе — это «данных недостаточно»,
            # другое обстоятельство, — поэтому в точке печатается ноль,
            # а запомненное состояние ждёт возвращения величины. Иначе зазор
            # держал бы основание там, где его не поставил бы и маршрут,
            # и разница мерила бы пробелы, а не границу.
            found.append(0)
            continue
        ratio = Decimal(value)
        if state == 0 and ratio < 1:
            state = 1
        elif state == 1 and ratio > clear:
            state = 0
        found.append(state)
    return found


def with_confirmation(fired: list[int], need: int, window: int) -> list[int]:
    """Состояние с подтверждением «K из N»: и постановка, и снятие.

    Подтверждается не только срабатывание, но и его снятие: правило,
    подтверждающее одну сторону, превращается в тот же зазор, только
    с неявной второй границей.
    """
    state = 0
    found: list[int] = []
    for number in range(len(fired)):
        seen = fired[max(0, number - window + 1) : number + 1]
        if len(seen) >= need:
            if sum(seen) >= need:
                state = 1
            elif len(seen) - sum(seen) >= need:
                state = 0
        found.append(state)
    return found


def _defaulted() -> dict[str, date]:
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


def _first_fire(series: list[int], dates: list[date]) -> date | None:
    """Когда основание сработало впервые; None — не срабатывало вовсе."""
    for number, value in enumerate(series):
        if value:
            return dates[number]
    return None


def _basket_moves(
    base: list[int], other: list[int], basket: list[str], others: list[int]
) -> int:
    """Сколько точек, где корзина сменилась бы от снятия либо постановки.

    Корзину меняет только то основание, которое её и называет: у эмитента
    с другим основанием внимания снятие рефинансирования корзины не трогает,
    у эмитента в разборе — тем более.
    """
    found = 0
    for number, (was, now) in enumerate(zip(base, other, strict=False)):
        if was == now or others[number] or basket[number] not in ("attention", "clear"):
            continue
        found += 1
    return found


def baskets_with(
    state: list[int], basket: list[str], others: list[int]
) -> list[str]:
    """Корзина каждой точки при названном состоянии основания.

    **Выводится она из записанного вердикта, а не считается заново.** Корзину
    меняет только то основание, которое её называет: где есть другое основание
    той же тяжести либо корзина тяжелее внимания, состояние рефинансирования
    ничего не решает. Проверяется вывод на самом себе: при состоянии, которое
    дал маршрут, выведенный ряд обязан совпасть с записанным.
    """
    found: list[str] = []
    for number, value in enumerate(state):
        current = basket[number]
        if others[number] or current not in ("attention", "clear"):
            found.append(current)
            continue
        found.append("attention" if value else "clear")
    return found


def basket_returns(series: list[str], dates: list[date], window: int) -> int:
    """Смены корзины, отменённые обратно внутри окна.

    Определение то же, что у возвратов в замере истории: пара соседних смен,
    вторая из которых возвращает прежнюю корзину не позже окна.
    """
    moves = [
        (number, series[number - 1], series[number])
        for number in range(1, len(series))
        if series[number] != series[number - 1]
    ]
    found = 0
    for first, second in zip(moves, moves[1:], strict=False):
        if second[2] == first[1] and (dates[second[0]] - dates[first[0]]).days <= window:
            found += 1
    return found


def main() -> int:
    """Печатает цену двух способов унять дребезг меры рефинансирования."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    routing = load_routing()
    window = routing.history.window_days
    found = collect()
    dates = [date.fromisoformat(item) for item in found["dates"]]
    fired = found["fired"]
    basket, others, offers = found["basket"], found["others"], found["offers"]
    # **Эмитент без единой измеренной точки в ряду покрытия отсутствует, а не
    # стоит нулями.** Разница существенна: нуль означал бы «покрытие ноль»,
    # то есть срабатывание, тогда как это пробел — графика на диске нет либо
    # денежные средства не раскрыты.
    cover = {
        inn: found["cover"].get(inn) or [None] * len(dates) for inn in fired
    }
    # **Вторая мера — чужое основание, и корзину она держит сама.** Варианты
    # трогают только «платежи года»; оферты остаются как есть, и считать их
    # снятыми вместе с первой мерой значило бы приписать зазору чужую работу.
    # Сверка вывода на самом себе это и поймала: 1 576 точек расхождения
    # были ровно эмитентами, у которых внимание держат оферты.
    others = {
        inn: [own + offers[inn][number] for number, own in enumerate(series)]
        for inn, series in others.items()
    }
    events = _defaulted()

    print("# Зазор у границы: цена двух способов унять дребезг\n")
    print(
        f"Точек {len(dates)} за {dates[0]:%d.%m.%Y} — {dates[-1]:%d.%m.%Y}, "
        f"эмитентов {len(fired)}. Окно отмены {window} дней — то же, которым "
        "меряются возвраты корзин. **В маршрут отсюда не вводится ничего.**\n"
    )

    base_switches = sum(switches(series) for series in fired.values())
    base_returns = sum(returns_of(series, dates, window) for series in fired.values())
    blinking = sum(1 for series in fired.values() if returns_of(series, dates, window))
    measured = sum(
        1 for inn in fired if any(value is not None for value in cover[inn])
    )
    # **Вывод корзины проверяется на самом себе.** При состоянии, которое дал
    # маршрут, выведенный ряд обязан совпасть с записанным вердиктом: иначе
    # сравнивать с ним варианты нельзя — расхождение мерило бы вывод, а не
    # зазор.
    wrong = sum(
        1
        for inn, series in fired.items()
        for was, now in zip(
            baskets_with(series, basket[inn], others[inn]), basket[inn], strict=False
        )
        if was != now
    )
    base_basket_returns = sum(
        basket_returns(baskets_with(series, basket[inn], others[inn]), dates, window)
        for inn, series in fired.items()
    )
    print("## Как дребезжит мера сейчас\n")
    print(
        f"Переключений основания «платежи года» **{base_switches}**, из них "
        f"отменённых внутри окна **{base_returns}**; дребезжащих эмитентов "
        f"{blinking}. Возвратов **корзины**, которые они дают, "
        f"**{base_basket_returns}**. Покрытие удалось измерить хотя бы в одной "
        f"точке у {measured} эмитентов из {len(fired)} — у остальных нет либо "
        "графика на диске, либо раскрытых денежных средств.\n"
    )
    print(
        f"Вывод корзины из состояния основания сверен с записанным вердиктом: "
        f"расхождений {wrong} из {len(dates) * len(fired)} точек. "
        "Без этой сверки сравнение вариантов мерило бы вывод, а не зазор.\n"
    )
    offer_switches = sum(switches(series) for series in offers.values())
    offer_returns = sum(returns_of(series, dates, window) for series in offers.values())
    print(
        f"Для сравнения, вторая мера (оферты): переключений {offer_switches}, "
        f"отменённых внутри окна {offer_returns}. Отсечка у неё та же, "
        "и зазор, введённый у первой, к ней относился бы наравне.\n"
    )

    print("## Вариант А: зазор по величине\n")
    print(
        "Основание ставится, когда покрытие ниже единицы, и снимается, только "
        "когда покрытие поднялось выше порога снятия.\n"
    )
    print(
        "**Запаздывание здесь равно нулю по устройству, а не по замеру**: "
        "порог постановки тот же, зазор относится только к снятию. В таблице "
        "оно оставлено, чтобы разница с вариантом Б была видна числом, "
        "а не выводилась читателем из устройства правила.\n"
    )
    print(
        "| Порог снятия | Возвратов корзины | Возвратов основания | "
        "Точек со сменой корзины | Эмитентов со сменой | Запаздывание "
        "у дефолтных |"
    )
    print("|---|---|---|---|---|---|")
    for clear in CLEARS:
        print(
            f"| {clear} "
            + _cost(
                {inn: with_gap(cover[inn], clear) for inn in fired},
                fired,
                basket,
                others,
                dates,
                window,
                events,
            )
        )

    print("\n## Вариант Б: подтверждение устойчивости «K из N»\n")
    print(
        "Основание ставится, когда условие держалось в K точках из последних N, "
        "и снимается тем же правилом с другой стороны. Сетка пересчёта "
        "недельная, поэтому N точек — это N недель.\n"
    )
    print(
        "**Здесь запаздывание настоящее, и оно в опасную сторону**: "
        "подтверждение откладывает саму постановку основания, то есть тот "
        "случай, ради которого мера и заведена. Зазор по величине откладывает "
        "снятие — ошибку в сторону лишнего внимания.\n"
    )
    print(
        "| K из N | Возвратов корзины | Возвратов основания | "
        "Точек со сменой корзины | Эмитентов со сменой | Запаздывание "
        "у дефолтных |"
    )
    print("|---|---|---|---|---|---|")
    for need, span in CONFIRMATIONS:
        print(
            f"| {need} из {span} "
            + _cost(
                {
                    inn: with_confirmation(series, need, span)
                    for inn, series in fired.items()
                },
                fired,
                basket,
                others,
                dates,
                window,
                events,
            )
        )

    print(
        f"\n**Знаменатель запаздывания.** Эмитентов с неисполненным событием "
        f"дефолта {len(events)}, из них основание «платежи года» срабатывало "
        f"хотя бы раз у {sum(1 for inn in events if inn in fired and any(fired[inn]))}. "
        "У остальных запаздывать нечему: мера у них не срабатывала вовсе, "
        "и ноль в графе означал бы обратное.\n"
    )
    return 0


def _cost(
    variant: dict[str, list[int]],
    fired: dict[str, list[int]],
    basket: dict[str, list[str]],
    others: dict[str, list[int]],
    dates: list[date],
    window: int,
    events: dict[str, date],
) -> str:
    """Строка таблицы: чем обходится вариант — пользой и ценой.

    **Пять чисел, а не одно.** Возвраты корзины — то, ради чего всё затеяно;
    возвраты основания — тот же дребезг ниже уровнем, он остаётся, даже когда
    корзина его больше не показывает; точки и эмитенты со сменой корзины —
    цена сегодня; запаздывание у тех, кто потом допустил дефолт, — цена в том
    случае, ради которого мера и заведена.
    """
    rest = moves = touched = ground = 0
    delays: list[int] = []
    for inn, other in variant.items():
        rest += basket_returns(
            baskets_with(other, basket[inn], others[inn]), dates, window
        )
        ground += returns_of(other, dates, window)
        count = _basket_moves(fired[inn], other, basket[inn], others[inn])
        moves += count
        touched += int(bool(count))
        if inn in events:
            was, now = _first_fire(fired[inn], dates), _first_fire(other, dates)
            if was is not None and now is not None:
                delays.append((now - was).days)
    return f"| {rest} | {ground} | {moves} | {touched} | {_said(delays)} |"


def _said(delays: list[int]) -> str:
    """Запаздывание словами: медиана и край; пустое — сказано пустым."""
    if not delays:
        return "наблюдений нет"
    worse = max(delays)
    return (
        f"медиана {statistics.median(delays):.0f} дн., до {worse} дн., "
        f"наблюдений {len(delays)}"
    )


if __name__ == "__main__":
    sys.exit(main())
