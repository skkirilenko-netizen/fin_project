"""Распределение эмитентов по корзинам маршрута — замер черновых правил.

Отвечает на два вопроса: **сколько эмитентов вообще поддаётся решению «нужен
ли человек»** и чем наполняется каждая корзина. Правила — черновик
(`methodology/routing.yaml`, `status: draft`), и замер печатает статус вместе
с распределением: корзина, выданная как решение методики, неотличима
от согласованной, а согласована она не была.

    uv run python eval/cbonds_routing_run.py > data/output/cbonds_routing.md

**Замер не считает сам.** Показатели берёт боевой расчёт по фактам
(`metrics.ifrs_store.compute_from_facts`), корзину — боевая маршрутизация
(`scoring.routing.route`). Замер считает исходы и печатает примеры.
"""

import logging
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.ifrs_store import compute_from_facts  # noqa: E402
from finlib.normalize.cbonds_mapping import load_cbonds_mapping  # noqa: E402
from finlib.normalize.ifrs_metrics import load_ifrs_metrics  # noqa: E402
from finlib.scoring.ifrs_store import stop_factors_of  # noqa: E402
from finlib.scoring.routing import load_routing, route  # noqa: E402

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

_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date,
       max(o.name) AS name,
       bool_or(s.status = 'quarantine') AS has_quarantine
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
GROUP BY f.inn
"""

# **Основание называется «провал проверки нуля», и считать надо его, а не
# карантин вообще.** Комплект уходит в карантин и по другим причинам —
# неопознанная позиция, потерянная страница, — и у О'КЕЙ, Автодора и Самолёта
# признак, взятый по карантину, отправлял в разбор эмитентов, у которых
# проверки нуля как раз сошлись. Графа считает то, как называется.
_ZERO_FAILED = """
SELECT DISTINCT d.inn, s.report_year
FROM dq_log d JOIN src_file s ON s.id = d.src_file_id
WHERE d.status = 'fail' AND d.check_code IN (
    'cbonds_identity_mismatch', 'cbonds_sections_mismatch', 'cbonds_zero_total'
)
"""

_OPERATING_PROFIT = """
SELECT f.value FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'ifrs' AND f.report_date = %(d)s
  AND f.line_code = 'ifrs.operating_profit' AND s.is_actual
  AND s.status <> 'quarantine'
ORDER BY source_rank(s.source)
LIMIT 1
"""

_WITHOUT_SET = """
SELECT count(DISTINCT d.inn) AS issuers
FROM dq_log d
WHERE d.check_code = 'cbonds_set_rejected'
  AND d.inn NOT IN (
      SELECT s.inn FROM src_file s
      WHERE s.standard = 'ifrs' AND s.is_actual AND s.status <> 'quarantine'
  )
"""

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

    baskets: Counter[str] = Counter()
    grounds: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    known: dict[str, str] = {}
    substituted = 0

    with connection() as conn:
        outside = fetch_all(_WITHOUT_SET, {}, conn=conn)
        quarantined = {
            (row["inn"], row["report_year"])
            for row in fetch_all(_ZERO_FAILED, {}, conn=conn)
        }
        rows = fetch_all(_LATEST, {}, conn=conn)
        for row in rows:
            inn, moment = row["inn"], row["report_date"]
            computed = compute_from_facts(inn, moment, conn, policy)
            profit = fetch_all(_OPERATING_PROFIT, {"inn": inn, "d": moment}, conn=conn)
            stops = stop_factors_of(inn, moment, computed, conn)
            verdict = route(
                computed,
                quarantined=(inn, moment.year) in quarantined,
                stop_factors=stops.triggered,
                financing_structure=inn in spv,
                operating_profit=Decimal(profit[0]["value"]) if profit else None,
                latest_annual=moment,
                today=today,
                policy=policy,
                routing=routing,
            )
            baskets[verdict.basket] += 1
            for ground in verdict.grounds:
                grounds[verdict.basket][ground] += 1
            name = (row["name"] or inn).strip()
            if len(examples[verdict.basket]) < 5:
                examples[verdict.basket].append(
                    f"{name} ({inn}), {moment:%d.%m.%Y}: "
                    + ("; ".join(verdict.details) or "оснований нет")
                )
            if inn in KNOWN:
                known[inn] = (
                    f"{verdict.basket_name} — "
                    + (", ".join(verdict.grounds) or "оснований нет")
                    + (f"; {'; '.join(verdict.details)}" if verdict.details else "")
                )

    measured = {row["inn"] for row in rows}
    total = sum(baskets.values())
    aside = int(outside[0]["issuers"]) if outside else 0
    print("# Маршрутизация: распределение по корзинам (черновик правил)\n")
    print(
        f"Правила — `methodology/routing.yaml`, версия {routing.version}, "
        f"**статус: {routing.status}**. Пороги взяты из существующих шкал: "
        "нижняя часть калибровочной шкалы — граница `bands.lower_below` "
        "из `theses.yaml`, порог вывода по границе — нижняя опорная точка шкалы "
        "долговой нагрузки. Новый порог один и назван отдельно: срок сдачи "
        f"годовой отчётности ({routing.freshness.annual_due}).\n"
    )
    print(
        f"Эмитентов в замере: **{total}** — те, у кого есть комплект вне "
        f"карантина. Сверх них **{aside}** комплекта не имеют вовсе: их "
        "отчётность отклонена на приёме, и в доли они не входят.\n"
    )
    print(
        f"Признак финансирующей структуры измерен по {cards} карточкам справочника "
        f"эмитентов, помечено SPV {len(spv)}, из них с комплектом в замере "
        f"{len(spv & measured)}. Прочие сюда не попадают не потому, что признак "
        "редок, а потому, что их отчётность по МСФО неконсолидированная "
        "и комплектом не становится.\n"
    )

    print("| Корзина | Эмитентов | Доля |")
    print("|---|---|---|")
    for basket in routing.ordered():
        count = baskets.get(basket.code, 0)
        print(f"| {basket.name} | {count} | {count / total * 100:.0f} % |")

    for basket in routing.ordered():
        found = grounds.get(basket.code)
        print(f"\n## {basket.name} — {baskets.get(basket.code, 0)}\n")
        print(f"{' '.join(basket.meaning.split())}\n")
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
