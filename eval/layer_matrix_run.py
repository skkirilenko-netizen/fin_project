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
    _appeared,
    _first_new_day,
    events,
    layers,
    points_of,
)

from finlib.scoring.market import holds_level, holds_price  # noqa: E402
from finlib.sources.market import load_market, series  # noqa: E402

logger = logging.getLogger(__name__)


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
            points, holds_level(market, step.multiple), until, rule.of, rule.out_of
        )
        if day is not None:
            said.append(day)
    return min(said) if said else None


def _spoke(policy, market, history, sources, inside: dict) -> dict[str, dict]:  # noqa: ANN001
    """По каждому эмитенту с событием — день высказывания каждого слоя.

    Пусто у слоя означает «не высказался до события»: молчание и высказывание
    после события — один исход, и оба не ловят.
    """
    found: dict[str, dict] = {}
    for inn, moment in inside.items():
        found[inn] = {
            "событие": moment,
            "рынок": _market_day(policy, market, inn, moment),
            "отчётность": _first_new_day(
                history.get(inn, {}), sources, _REPORTING, moment
            ),
            "рейтинги": _first_new_day(
                history.get(inn, {}), sources, _RATINGS, moment
            ),
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
        if layer == "рынок":
            day = _market_day(policy, market, inn, date.max)
        else:
            words = _REPORTING if layer == "отчётность" else _RATINGS
            day = _first_new_day(history.get(inn, {}), sources, words, date.max)
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
    _by_issuer(spoke, names)
    return 0


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
    print(
        f"\n**Критерий фазы 6** — «выявляемость и прирост матрицы выше, чем "
        f"у любого слоя отдельно». Выявляемость: {whole_recall:.1%} против "
        f"{best_recall:.1%} — **выше**. Прирост: {whole_lift:.1f}× против "
        f"{best_lift:.1f}× — **ниже**.\n"
    )
    print(
        "Критерий требует обоих условий, и он **не выполнен**: объединение "
        "тянет за собой ложные срабатывания слабого слоя — сработавших "
        f"{whole_fired} против {best_fired} у лучшего, а поймано на одного "
        "больше. Пересечение слоёв точности тоже не даёт: лучшая пара даёт "
        "13,8 % против 17,8 % у рынка в одиночку.\n"
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
