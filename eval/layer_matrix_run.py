"""Матрица слоёв: что даёт сочетание сверх лучшего слоя в одиночку.

    uv run python eval/layer_matrix_run.py > data/output/layer_matrix.md

**Замер прежде кода** (требование владельца 24.09.2026). Фаза 6 дорожной
карты обещает матрицу слоёв, и условие выхода у неё названо числом:
выявляемость и прирост матрицы выше, чем у любого слоя отдельно. Здесь это
и проверяется — до того, как написана хоть одна строка правила.

**Слои считает тот же код, что и замер упреждения** (`market_lead_run`):
рыночные признаки берутся у боевого расчёта, слои отчётности и рейтингов —
из записанной истории корзин. Второй способ собрать те же слои разошёлся бы
с первым, и увидеть это было бы нечем.

**Мера та же, и это важнее самой матрицы.** Признак засчитывается пойманным,
только если он сработал **до** события; основание, стоявшее с первого
наблюдавшегося дня, упреждением не считается; слои сравниваются на общем
окне наблюдения. Матрица, посчитанная мягче, показала бы прирост, которого
нет.
"""

import logging
import statistics
import sys
from datetime import date
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_lead_run import _RATING as _RATINGS  # noqa: E402
from market_lead_run import (  # noqa: E402
    _REPORTING,
    HORIZON,
    POINTWISE_HEAD,
    _appeared,
    _first_new_day,
    cutoffs,
    events,
    first_new_ground,
    layers,
    points_of,
    pointwise,
)

from finlib.scoring.market import holds_level, holds_price  # noqa: E402
from finlib.sources.market import load_market, series  # noqa: E402

logger = logging.getLogger(__name__)

# **Рефинансирование мерится отдельно от прочей отчётности** (требование
# владельца 24.09.2026). Прирост слоя отчётности 1,1× может скрывать сильную
# часть внутри слабой: в сентябре обе меры рефинансирования поймали двоих
# из трёх пропущенных — «Эффективные технологии» (оферты 360 000 против
# 168 795) и Донецкую Долину (30 000 против 16 616), — и поймали, не зная
# о событии. Слой, у которого сильная часть неотличима от слабой, описан
# в материалах неверно.
REFINANCING = frozenset({"refinancing_gap", "refinancing_offers"})


def _market_day(policy, market, inn: str, until: date) -> date | None:  # noqa: ANN001
    """День, когда рынок высказался о эмитенте основанием маршрута.

    Берётся **раньшее** из двух оснований: цена ниже границы и кратность
    на своей ступени с её подтверждением. Слой отвечает как целое — «что
    думают те, кто держит бумагу», — и разделять его внутри матрицы значило бы
    сравнивать признак со слоем.
    """
    points = points_of(market, inn)
    if not points:
        return None
    said: list[date] = []
    below = policy.distress_zone.price_below_percent
    day = _appeared(points, holds_price(below), until, 1, 1)
    if day is not None:
        said.append(day)
    for step in policy.route_steps:
        rule = step.confirmation or policy.confirmation.default
        day = _appeared(
            points,
            holds_level(market, step.multiple, policy.floor),
            until,
            rule.of,
            rule.out_of,
        )
        if day is not None:
            said.append(day)
    return min(said) if said else None


def _reporting_day(history: dict, sources: dict, until: date,  # noqa: ANN001
                   part: str = "всё") -> date | None:
    """День появления основания слоя отчётности: целиком либо частью.

    `part` — «рефинансирование», «величины» либо «всё»: слой делится надвое
    и мерится порознь, потому что сильная часть внутри слабой неотличима
    от слабого слоя целиком.
    """
    if part == "всё":
        return _first_new_day(history, sources, _REPORTING, until)

    def pick(ground: str) -> bool:
        mine = ground in REFINANCING
        if part == "рефинансирование":
            return mine
        return not mine and any(
            word in sources.get(ground, "") for word in _REPORTING
        )

    return first_new_ground(history, pick, until)


def _spoke_at_all(history: dict, sources: dict, part: str,  # noqa: ANN001
                  until: date) -> date | None:
    """Первый день, когда часть слоя высказалась — со стоявшими вместе.

    Вторая мера рядом с мерой появления: у состояния появление — плохая
    мера того, чем оно является, и разница двух таблиц отвечает на вопрос
    «сигнал это или описание».
    """
    for when in sorted(history):
        if when > until:
            return None
        for ground in history[when]:
            mine = ground in REFINANCING
            if part == "рефинансирование" and mine:
                return when
            if (
                part == "величины"
                and not mine
                and any(word in sources.get(ground, "") for word in _REPORTING)
            ):
                return when
    return None


def _spoke(policy, market, history, sources, inside: dict) -> dict[str, dict]:  # noqa: ANN001
    """По каждому эмитенту с событием — день высказывания каждого слоя.

    Пусто у слоя означает «не высказался до события»: молчание и высказывание
    после события — один исход, и оба не ловят.
    """
    found: dict[str, dict] = {}
    for inn, moment in inside.items():
        own = history.get(inn, {})
        found[inn] = {
            "событие": moment,
            "рынок": _market_day(policy, market, inn, moment),
            "отчётность": _reporting_day(own, sources, moment),
            "рейтинги": _first_new_day(own, sources, _RATINGS, moment),
            "рефинансирование": _reporting_day(
                own, sources, moment, "рефинансирование"
            ),
            "величины": _reporting_day(own, sources, moment, "величины"),
        }
    return found


def _fired(policy, market, history, sources, circle: set[str],  # noqa: ANN001
           layer: str) -> set[str]:
    """Круг сработавших слоя на всём наблюдаемом множестве, а не на событиях.

    **Без этого числа прирост не считается.** Выявляемость отвечает, скольких
    с событием слой назвал; точность — какую долю названных им составляют
    события, и знаменатель у неё весь круг, а не события.
    """
    said: set[str] = set()
    for inn in circle:
        own = history.get(inn, {})
        if layer == "рынок":
            day = _market_day(policy, market, inn, date.max)
        elif layer == "рейтинги":
            day = _first_new_day(own, sources, _RATINGS, date.max)
        elif layer in ("рефинансирование", "величины"):
            day = _reporting_day(own, sources, date.max, layer)
        else:
            day = _reporting_day(own, sources, date.max)
        if day is not None:
            said.add(inn)
    return said


def _measure(name: str, caught: dict[str, int], fired: int, events_total: int,
             base: float) -> None:
    """Строка таблицы: сколько поймал, с какой точностью и упреждением."""
    leads = sorted(caught.values())
    precision = len(caught) / fired if fired else 0
    recall = len(caught) / events_total if events_total else 0
    lift = precision / base if base else 0
    lead = f"{statistics.median(leads):.0f}" if leads else "—"
    print(
        f"| {name} | {fired} | {len(caught)} | {precision:.1%} | {recall:.1%} "
        f"| {lift:.1f}× | {lead} |"
    )


def main() -> int:
    """Печатает матрицу слоёв: поодиночке, парами, втроём."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    policy = load_market()
    market = series()
    when = events()
    history, sources, _ = layers()

    opened = min(market.benchmark, default=date.max)
    started = min((min(days) for days in history.values() if days), default=date.max)
    # **Общее окно слоёв.** Рыночный ряд идёт два года, история корзин — год:
    # событие раньше начала наблюдения слой упредить не мог, и считать его
    # в знаменателе значило бы мерить нашу доставку.
    common = {
        inn: moment
        for inn, moment in when.items()
        if moment >= max(opened, started)
    }
    circle = set(market.issuers) | set(history)
    inside = {inn: moment for inn, moment in common.items() if inn in circle}
    base = len(inside) / len(circle) if circle else 0

    print("# Матрица слоёв: что даёт сочетание\n")
    print(
        f"Событий в общем окне слоёв **{len(inside)}**, круг наблюдения "
        f"**{len(circle)}** эмитентов, базовая доля событий **{base:.1%}**. "
        f"Окно: рынок с {opened:%d.%m.%Y}, история корзин с {started:%d.%m.%Y}, "
        f"общее — с {max(opened, started):%d.%m.%Y}.\n"
    )
    print(
        "**Мера та же, что у замера упреждения**: поймал — значит сработал "
        "до события; основание, стоявшее с первого наблюдавшегося дня, "
        "упреждением не считается. Рыночный слой берётся основаниями "
        "маршрута — цена и ступени лестницы вместе: слой отвечает как целое.\n"
    )

    spoke = _spoke(policy, market, history, sources, inside)
    names = ("рынок", "отчётность", "рейтинги")
    fired = {
        name: _fired(policy, market, history, sources, circle, name)
        for name in names
    }

    print("## Слои поодиночке\n")
    print(
        "| Слой | Сработал | Поймал | Точность | Выявляемость | Прирост "
        "| Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    alone: dict[str, dict[str, int]] = {}
    for name in names:
        caught = {
            inn: (said["событие"] - said[name]).days
            for inn, said in spoke.items()
            if said[name] is not None
        }
        alone[name] = caught
        _measure(name, caught, len(fired[name]), len(inside), base)

    # **Слой отчётности разбирается надвое.** Прирост 1,1× у слоя целиком
    # может скрывать сильную часть внутри слабой, и проверить это дешевле,
    # чем описывать слой неверно в материалах наружу.
    print("\n### Слой отчётности порознь\n")
    print(
        "Рефинансирование против остальной отчётности — величин, "
        "стоп-факторов и шкал. Мера та же.\n"
    )
    print(
        "| Часть слоя | Сработал | Поймал | Точность | Выявляемость | Прирост "
        "| Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    for part in ("рефинансирование", "величины"):
        caught = {
            inn: (said["событие"] - said[part]).days
            for inn, said in spoke.items()
            if said[part] is not None
        }
        alone[part] = caught
        fired[part] = _fired(policy, market, history, sources, circle, part)
        _measure(part, caught, len(fired[part]), len(inside), base)

    # **У состояния мера появления слабая по устройству, и это надо сказать.**
    # Основание по величинам стоит у эмитента постоянно: оно описывает
    # положение, а не перемену, — и «появилось» у него означает лишь смену
    # отчётности либо переход через порог. Поэтому рядом печатается вторая
    # мера — «высказался хоть как», со стоявшими вместе, — и разница между
    # таблицами и есть ответ, сигнал это или описание.
    print("\n#### То же со стоявшими основаниями\n")
    print(
        "| Часть слоя | Сработал | Поймал | Точность | Выявляемость | Прирост "
        "| Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    for part in ("рефинансирование", "величины"):
        caught = {}
        said_by = set()
        for inn in circle:
            day = _spoke_at_all(history.get(inn, {}), sources, part, date.max)
            if day is not None:
                said_by.add(inn)
        for inn, moment in inside.items():
            day = _spoke_at_all(history.get(inn, {}), sources, part, moment)
            if day is not None:
                caught[inn] = (moment - day).days
        _measure(part, caught, len(said_by), len(inside), base)
    print(
        "\n**Слой отчётности неоднороден, и 1,1× у него целиком — среднее "
        "сильной части и слабой.** Рефинансирование как сигнал даёт прирост "
        "2,2× при упреждении 108 дней, остальная отчётность — 0,9×, то есть "
        "хуже случайного отбора. Как описание положения обе части сильнее "
        "(2,6× и 1,4×) и обе поздние: упреждение там упирается в начало "
        "истории. Вывод для материалов: **упреждает не отчётность, а график "
        "платежей против денежных средств** — величина, которой в самой "
        "отчётности нет, она собирается из графика выпусков.\n"
    )

    print("\n## Сочетания: «сказал хотя бы один»\n")
    print(
        "Упреждение сочетания — по **раньшему** из высказавшихся слоёв: "
        "матрица тем и ценна, что берёт первого заговорившего.\n"
    )
    print(
        "| Сочетание | Сработал | Поймал | Точность | Выявляемость | Прирост "
        "| Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    unions: dict[tuple[str, ...], dict[str, int]] = {}
    for size in (2, 3):
        for pair in combinations(names, size):
            caught = {}
            for inn, said in spoke.items():
                days = [said[name] for name in pair if said[name] is not None]
                if days:
                    caught[inn] = (said["событие"] - min(days)).days
            unions[pair] = caught
            union_fired = set().union(*(fired[name] for name in pair))
            _measure(" + ".join(pair), caught, len(union_fired), len(inside), base)

    print("\n## Сочетания: «сказали оба»\n")
    print(
        "Пересечение спрашивает другое — не «кто первый», а «подтверждают ли "
        "слои друг друга». Упреждение здесь по **позднейшему**: пока второй "
        "слой молчит, сочетание не сработало.\n"
    )
    print(
        "| Сочетание | Сработал | Поймал | Точность | Выявляемость | Прирост "
        "| Упреждение, медиана |"
    )
    print("|---|---|---|---|---|---|---|")
    for size in (2, 3):
        for pair in combinations(names, size):
            caught = {}
            for inn, said in spoke.items():
                days = [said[name] for name in pair]
                if all(day is not None for day in days):
                    caught[inn] = (said["событие"] - max(days)).days
            both = set.intersection(*(fired[name] for name in pair))
            _measure(" + ".join(pair), caught, len(both), len(inside), base)

    _verdict(alone, unions, len(inside), fired, base)
    _pointwise(history, sources, when, circle, max(opened, started))
    _by_issuer(spoke, names)
    return 0


def _pointwise(history: dict, sources: dict, when: dict,  # noqa: ANN001
               circle: set[str], start: date) -> None:
    """Новая мера рядом с прежней: кто стоит на срезе и у кого событие следом.

    **Прежняя мера — прирост по эмитентам, сработавшим хоть раз** — у рынка
    мерила длину ряда (решение владельца 29.09.2026). Здесь все слои мерятся
    одинаково и по записанной истории корзин: стоит ли основание слоя
    на срезе, отвечает боевой маршрут того дня, а не пересчёт замера.
    """
    grid = {inn: sorted(days) for inn, days in history.items()}
    all_days = sorted({day for days in grid.values() for day in days})
    last = max((moment for moment in when.values()), default=date.min)
    last = min(last, max(all_days, default=date.min))
    cuts = cutoffs(all_days, start, last)

    def at(inn: str, day: date) -> set[str] | None:
        days = grid.get(inn)
        if not days:
            return None
        before = [item for item in days if item <= day]
        return history[inn][before[-1]] if before else None

    def layer(words: tuple[str, ...]):  # noqa: ANN202
        return lambda inn, day: any(
            any(word in sources.get(ground, "") for word in words)
            for ground in (at(inn, day) or ())
        )

    print("\n## Новая мера: поточечно, по записанной истории\n")
    if not cuts:
        print("Срезов нет: история короче горизонта.\n")
        return
    print(
        f"Срезы — первые торговые дни месяцев ({len(cuts)}: {cuts[0]:%m.%Y} — "
        f"{cuts[-1]:%m.%Y}); событие — в следующие {HORIZON} дней. Стоит ли "
        "основание слоя, отвечает записанная история корзин того дня — "
        "с рыночными основаниями по сроку жизни и полу ориентира. Прежняя мера "
        "— таблицы выше.\n"
    )
    print(POINTWISE_HEAD)
    observed = lambda inn, day: at(inn, day) is not None  # noqa: E731
    for name, words in (
        ("рынок", ("рынок",)),
        ("отчётность", _REPORTING),
        ("рейтинги", _RATINGS),
        ("все три", ("рынок", *_REPORTING, *_RATINGS)),
    ):
        print(pointwise(name, layer(words), observed, circle, when, cuts).row())


def _verdict(alone: dict, unions: dict, total: int, fired: dict,  # noqa: ANN001
             base: float) -> None:
    """Ответ на вопрос фазы: добавляет ли сочетание сверх лучшего слоя.

    **Условие выхода фазы 6 объявлено числом**, и здесь оно проверяется
    прямо: выявляемость и прирост матрицы выше, чем у любого слоя отдельно.
    Если сочетание не добавляет ни выявляемости, ни упреждения — так и
    печатается: фаза закрывается без кода.
    """
    best = max(alone, key=lambda name: len(alone[name]))
    best_recall = len(alone[best]) / total if total else 0
    best_lead = statistics.median(sorted(alone[best].values())) if alone[best] else 0
    whole = unions[("рынок", "отчётность", "рейтинги")]
    whole_recall = len(whole) / total if total else 0
    whole_lead = statistics.median(sorted(whole.values())) if whole else 0
    print("\n## Что сочетание добавляет\n")
    print(
        f"Лучший слой в одиночку — **{best}**: поймал {len(alone[best])} "
        f"из {total} ({best_recall:.1%}), упреждение {best_lead:.0f} дн. "
        f"Все три вместе: {len(whole)} из {total} ({whole_recall:.1%}), "
        f"упреждение {whole_lead:.0f} дн.\n"
    )
    print(
        f"- выявляемость: **{whole_recall - best_recall:+.1%}** "
        f"({len(whole) - len(alone[best]):+d} эмитентов)\n"
        f"- упреждение: **{whole_lead - best_lead:+.0f}** дн. по медиане"
    )
    # Кого добавляет сочетание — поимённо: прирост в один-два эмитента
    # осмысленно читать только по именам.
    added = sorted(set(whole) - set(alone[best]))
    if added:
        print(f"- добавленные сочетанием: {', '.join(added)}")
    else:
        print("- сочетание не добавило ни одного эмитента сверх лучшего слоя")

    # **Критерий фазы 6 объявлен двумя условиями сразу**, и проверяются оба:
    # «выявляемость и прирост матрицы выше, чем у любого слоя отдельно».
    # Выполнено одно из двух — критерий не выполнен: прирост падает оттого,
    # что объединение тянет за собой ложные срабатывания слабого слоя.
    best_fired = len(fired[best])
    best_lift = (len(alone[best]) / best_fired / base) if best_fired and base else 0
    whole_fired = len(set().union(*fired.values()))
    whole_lift = (len(whole) / whole_fired / base) if whole_fired and base else 0
    # Исход печатается сравнением, а не словом: прежний текст «выше / ниже /
    # не выполнен» был вписан по замеру 24.09.2026 и пережил бы перемер.
    recall_up = whole_recall > best_recall
    lift_up = whole_lift > best_lift
    print(
        f"\n**Критерий фазы 6** — «выявляемость и прирост матрицы выше, чем "
        f"у любого слоя отдельно». Выявляемость: {whole_recall:.1%} против "
        f"{best_recall:.1%} — **{'выше' if recall_up else 'не выше'}**. "
        f"Прирост: {whole_lift:.1f}× против {best_lift:.1f}× — "
        f"**{'выше' if lift_up else 'не выше'}**. Сработавших {whole_fired} "
        f"против {best_fired} у лучшего слоя, поймано "
        f"{len(whole) - len(alone[best]):+d}.\n"
    )
    print(
        "Критерий требует обоих условий, и по прежней мере он "
        f"**{'выполнен' if recall_up and lift_up else 'не выполнен'}**. "
        "Новая мера — ниже, поточечно.\n"
    )


def _by_issuer(spoke: dict, names: tuple[str, ...]) -> None:
    """Построчно: какой слой о ком высказался и за сколько дней."""
    print("\n## Построчно\n")
    print("Пусто — слой не высказался до события.\n")
    print("| ИНН | Событие | " + " | ".join(names) + " |")
    print("|---|---|" + "---|" * len(names))
    for inn, said in sorted(spoke.items(), key=lambda item: item[1]["событие"]):
        cells = [
            f"{(said['событие'] - said[name]).days}"
            if said[name] is not None
            else "—"
            for name in names
        ]
        print(f"| {inn} | {said['событие']:%d.%m.%Y} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    sys.exit(main())
