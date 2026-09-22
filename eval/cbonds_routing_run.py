"""Распределение эмитентов по корзинам маршрута.

Отвечает на два вопроса: **сколько эмитентов вообще поддаётся решению «нужен
ли человек»** и чем наполняется каждая корзина. Структура правил утверждена
человеком, пороги остаются предварительными, и замер печатает оба сведения
вместе с распределением: предварительный порог, выданный как калиброванный,
неотличим от проверенного.

    uv run python eval/cbonds_routing_run.py > data/output/cbonds_routing.md

**Замер не считает сам.** Показатели берёт боевой расчёт по фактам
(`metrics.ifrs_store.compute_from_facts`), корзину — боевая маршрутизация
(`scoring.routing.route`). Замер считает исходы и печатает примеры.
"""

import logging
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.normalize.cbonds_mapping import load_cbonds_mapping  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_catalogue import catalogue_for  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

# Известные случаи: куда попали те эмитенты, которых мы разбирали руками.
KNOWN = {
    "7736216869": "ФосАгро",
    "7838360491": "ЛСР",
    "9703024202": "Сегежа",
    "7717151380": "Автодор",
    "9731004688": "Самолёт",
    "7826087713": "О'КЕЙ",
}





# **Перечень отвергнутых на приёме больше не нужен этому замеру.** Прежде
# он считал эмитентов, которых нет в списке вовсе, — их было 111, и они
# стояли «сверх» замера. Теперь такой эмитент в списке есть: маршрут строится
# по РСБУ либо по событиям, а разложение причин, по которым отчётность
# не поступила, печатает замер охвата (`watchlist_coverage_run.py`).

# Признак SPV справочника эмитентов: карточки собирает `cbonds_emitents.py`
# и складывает на диск. Наименование в признак не идёт — «…Финанс» примета,
# а не признак.
_CARDS = Path("data/raw/cbonds/emitents.json")


def spv_issuers() -> tuple[set[str], int]:
    """ИНН с признаком SPV и число карточек, по которым он измерен.

    Число карточек печатается рядом: признак, измеренный по части набора,
    занижен, и «сработал у одного» без знаменателя читается как «почти
    не бывает».
    """
    import json

    if not _CARDS.exists():
        return set(), 0
    cards = json.loads(_CARDS.read_text(encoding="utf-8"))
    return (
        {inn for inn, card in cards.items() if str(card.get("emitent_spv")) == "1"},
        len(cards),
    )


def main() -> int:
    """Печатает распределение по корзинам."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    policy = load_ifrs_metrics()
    routing = load_routing()
    mapping = load_cbonds_mapping()
    spv, cards = spv_issuers()
    today = date.today()
    # **Стоп-факторы берутся у обоих справочников.** Маршрут строится и по
    # РСБУ, и у неё перечень свой: `weak_coverage` печатался кодом, а градация
    # ограничения класса — прочерком, то есть графа считала не то, как
    # называется. Перечни складываются, а не подменяют друг друга: код
    # у стандартов бывает один и тот же (`negative_equity`), и означает он
    # одно и то же.
    caps: dict[str, str] = {}
    factor_names: dict[str, str] = {}
    for standard in Standard:
        for factor in catalogue_for(standard).stop_factors:
            caps.setdefault(factor.code, factor.cap)
            factor_names.setdefault(factor.code, factor.name)

    baskets: Counter[str] = Counter()
    grounds: dict[str, Counter[str]] = defaultdict(Counter)
    senior: dict[str, Counter[str]] = defaultdict(Counter)
    overlap: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    known: dict[str, str] = {}
    by_factor: Counter[str] = Counter()
    off_scale: Counter[str] = Counter()
    off_scale_issuers: dict[str, set[str]] = {}
    with_nwc: set[str] = set()
    silenced: Counter[str] = Counter()
    silenced_issuers = 0
    with_factor = 0
    substituted = 0

    with connection() as conn:
        # **Входы маршрута собирает боевой путь** (`scoring.routing_store`):
        # прежде замер и экран наблюдения собирали их порознь одними и теми же
        # запросами, и расхождение между отчётом и списком увидеть было нечем.
        rows, selection = routing_rows(conn, today)

    for item in rows:
        inn, moment, verdict = item.inn, item.report_date, item.verdict
        if item.stop_factors:
            with_factor += 1
            for code in item.stop_factors:
                by_factor[code] += 1
        baskets[verdict.basket] += 1
        for ground in verdict.grounds:
            grounds[verdict.basket][ground] += 1
        if verdict.subgroup:
            senior[verdict.basket][verdict.subgroup] += 1
            if len(verdict.subgroups) > 1:
                overlap[verdict.basket][verdict.subgroup] += 1
        # Предмет основания берётся у самого основания (`Finding.subject`),
        # а не вытаскивается из прозы: второй разбор того же ответа
        # разошёлся бы с первым и молча.
        for entry in verdict.findings:
            if entry.ground == "level_off_scale":
                off_scale[entry.subject] += 1
                off_scale_issuers.setdefault(inn, set()).add(entry.subject)
        if "negative_nwc" in item.stop_factors:
            with_nwc.add(inn)
        # Гашение основания стоп-фактором считается наравне со сработавшим:
        # правило, гасящее молча, неотличимо от невыполненного.
        for code in verdict.spoken_for:
            silenced[code] += 1
        if verdict.spoken_for:
            silenced_issuers += 1
        where = (
            " [" + ", ".join(verdict.subgroup_names) + "]"
            if verdict.subgroup_names
            else ""
        )
        if len(examples[verdict.basket]) < 5:
            examples[verdict.basket].append(
                f"{item.name} ({inn}), "
                f"{f'{moment:%d.%m.%Y}' if moment is not None else 'отчётности нет'}"
                f"{where}: "
                + ("; ".join(verdict.details) or "оснований нет")
            )
        if inn in KNOWN:
            known[inn] = (
                f"{verdict.basket_name}{where} — "
                + (", ".join(verdict.grounds) or "оснований нет")
                + (f"; {'; '.join(verdict.details)}" if verdict.details else "")
            )

    measured = {item.inn for item in rows}
    total = sum(baskets.values())
    print("# Маршрутизация: распределение по корзинам\n")
    print(
        f"Правила — `methodology/routing.yaml`, версия {routing.version}, "
        f"**структура: {routing.status}** ({routing.approved_by or 'не утверждена'}), "
        f"**пороги: {routing.thresholds}**. Пороги взяты из существующих шкал: "
        "нижняя часть калибровочной шкалы — граница `bands.lower_below` "
        "из `theses.yaml`, порог вывода по границе — нижняя опорная точка шкалы "
        "долговой нагрузки. Новый порог один и назван отдельно: срок сдачи "
        f"годовой отчётности ({routing.freshness.annual_due}).\n"
    )
    print(
        f"Эмитентов в замере: **{total}** — весь универсум маршрута: эмитенты "
        "с выпусками в обращении вместе с теми, чья отчётность у нас "
        "загружена. Маршрут построен по консолидированной отчётности "
        f"у {selection['маршрут по МСФО']}, по отчётности юридического лица "
        f"у {selection['маршрут по РСБУ']}, по одним событиям и рейтингам "
        f"у {selection['маршрут по событиям и рейтингам']}: отчётности "
        "у последних нет ни по одному стандарту, и корзина у них построена "
        "на том, что от стандарта не зависит.\n"
    )
    print(
        f"Признак финансирующей структуры измерен по {cards} карточкам справочника "
        f"эмитентов, помечено SPV {len(spv)}, из них в замере "
        f"{len(spv & measured)}.\n"
    )

    print(
        "Оценка по разобранному документу есть у "
        f"**{sum(1 for item in rows if item.assessed_class)}** эмитентов; "
        "класс из неё старше любого признака агрегатора и берётся тем, "
        "который присвоен за последний оценённый период. Из списка вышло "
        f"**{selection['вышло из списка']}** эмитентов — ликвидированных "
        "с известным преемником, — и у "
        f"**{selection['статус не подтверждён']}** статус не подтверждён: "
        "они остались в очереди статуса, а не исчезли.\n"
    )

    print("| Корзина | Эмитентов | Доля |")
    print("|---|---|---|")
    for basket in routing.ordered():
        count = baskets.get(basket.code, 0)
        print(f"| {basket.name} | {count} | {count / total * 100:.0f} % |")

    print("\n### Одно обстоятельство — одно решение: сколько раз сработало\n")
    print(
        f"Основание по величине погашено стоп-фактором того же показателя "
        f"у **{silenced_issuers}** эмитентов, всего погашено величин "
        f"**{sum(silenced.values())}**"
        + (
            ": " + ", ".join(f"{code} — {count}" for code, count in silenced.most_common())
            if silenced
            else ""
        )
        + ". Считается это наравне со сработавшим: правило, гасящее молча, "
        "неотличимо от невыполненного.\n"
    )

    print("\n## Стоп-факторы по видам\n")
    print(
        f"Стоп-фактор сработал у **{with_factor}** эмитентов из {total}. "
        "Градация ограничения класса объявлена у самого стоп-фактора "
        "(`ifrs_issuer_type.yaml`, поле `cap`), и маршрут берёт её оттуда: "
        "ограничение низшим (E) и неустойчивым (D) — разбор, ограничение "
        "средним (C) — внимание.\n"
    )
    print("| Стоп-фактор | Ограничение класса | Корзина | Эмитентов |")
    print("|---|---|---|---|")
    for code, count in by_factor.most_common():
        cap = caps.get(code, "—")
        where = "разбор" if routing.severity.severe(cap) else "внимание"
        print(f"| {factor_names.get(code, code)} | {cap} | {where} | {count} |")

    for basket in routing.ordered():
        found = grounds.get(basket.code)
        print(f"\n## {basket.name} — {baskets.get(basket.code, 0)}\n")
        print(f"{' '.join(basket.meaning.split())}\n")
        if basket.groups:
            # **Корзина одной строкой скрыла бы три природы обстоятельств.**
            # Эмитент показывается по старшей подгруппе, и рядом стоит число
            # тех, у кого сработала не одна: без него подгруппы читались бы
            # как разделение набора, а они пересекаются.
            print("| Подгруппа | Действие | Эмитентов | Из них с другими |")
            print("|---|---|---|---|")
            for group in sorted(basket.groups, key=lambda item: item.order):
                print(
                    f"| {group.name} | {group.action} | "
                    f"{senior[basket.code][group.code]} | "
                    f"{overlap[basket.code][group.code]} |"
                )
            print("")
        if found:
            print("| Основание | Эмитентов |")
            print("|---|---|")
            names = {item.code: item.name for item in basket.grounds}
            for code, count in found.most_common():
                print(f"| {names.get(code, code)} | {count} |")
        elif basket.code != "clear":
            print("Оснований не сработало ни одного.\n")
        if examples.get(basket.code):
            print("\nПримеры:\n")
            for line in examples[basket.code]:
                print(f"- {line}")

    # **Корзину держит основание, а не доля.** Доля разбора — следствие,
    # и подгонять порог под неё значило бы мерить не эмитентов, а наше
    # желание получить круглое число. Поэтому рядом с долей называется
    # основание, которым корзина наполнена.
    review = baskets.get("review", 0)
    if review * 3 > total:
        top = grounds["review"].most_common(1)
        names = {item.code: item.name for item in routing.basket("review").grounds}
        if top:
            code, count = top[0]
            print(
                f"\n**Разбор больше трети ({review} из {total}), и держит его "
                f"основание «{names.get(code, code)}» — {count} эмитентов.** "
                "Порог под долю не подбирается: основание объявлено методикой, "
                "и если оно срабатывает часто, это свойство универсума, "
                "а не настройка.\n"
            )
    if off_scale:
        print("\n### Чем именно кончилась шкала\n")
        print("| Показатель | Опорная точка | Эмитентов |")
        print("|---|---|---|")
        scales = policy.calibration_points.metrics
        metric_names = {item.code: item.name for item in policy.metrics}
        for code, count in off_scale.most_common():
            edge = scales[code].points[0][0] if code in scales else "—"
            print(f"| {metric_names.get(code, code)} | {edge} | {count} |")
        only_liquidity = {
            inn for inn, codes in off_scale_issuers.items() if codes == {"cur_liq"}
        }
        if only_liquidity:  # pragma: no cover — печатается, пока правило не решено
            print(
                f"\nТолько текущей ликвидностью держится "
                f"**{len(only_liquidity)}** эмитентов, из них "
                f"**{len(only_liquidity & with_nwc)}** со сработавшим "
                "отрицательным чистым оборотным капиталом.\n"
            )

    print("\n## Известные случаи\n")
    print("| Эмитент | Корзина и основания |")
    print("|---|---|")
    for inn, name in KNOWN.items():
        print(f"| {name} | {known.get(inn, 'комплекта вне карантина нет')} |")

    print(
        f"\nЗамена EBITDA полем источника объявлена справочником сопоставления "
        f"(«{mapping.substitutions['ebitda'].shown_as}») и в балл не идёт: "
        "она участвует только в решении о маршруте."
    )
    if substituted:  # pragma: no cover — печатается, когда замена применялась
        print(f"Применена у {substituted} эмитентов.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
