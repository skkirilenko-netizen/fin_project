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
from finlib.normalize.ifrs_issuer_type import load_issuer_types  # noqa: E402
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

# **Оценка из базы — только по разобранному документу.** Класс, присвоенный
# нами, опирается на состав величин самой отчётности; комплект агрегатора
# оценки не получает вовсе, и требование к источнику здесь не формальность,
# а то, что делает основание сильнее признака.
_ASSESSED = """
SELECT a.inn, a.class_code, a.report_date
FROM assessment a
WHERE a.standard = 'ifrs' AND a.class_code IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM src_file s
      WHERE s.inn = a.inn AND s.standard = 'ifrs' AND s.source <> 'cbonds'
        AND s.report_year = EXTRACT(YEAR FROM a.report_date)::int
  )
ORDER BY a.inn, a.report_date DESC
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
    types = load_issuer_types()
    mapping = load_cbonds_mapping()
    spv, cards = spv_issuers()
    today = date.today()
    caps = {factor.code: factor.cap for factor in types.stop_factors}
    factor_names = {factor.code: factor.name for factor in types.stop_factors}

    baskets: Counter[str] = Counter()
    grounds: dict[str, Counter[str]] = defaultdict(Counter)
    examples: dict[str, list[str]] = defaultdict(list)
    known: dict[str, str] = {}
    by_factor: Counter[str] = Counter()
    off_scale: Counter[str] = Counter()
    off_scale_issuers: dict[str, set[str]] = {}
    with_nwc: set[str] = set()
    with_factor = 0
    substituted = 0

    with connection() as conn:
        outside = fetch_all(_WITHOUT_SET, {}, conn=conn)
        assessed: dict[str, str] = {}
        for row in fetch_all(_ASSESSED, {}, conn=conn):
            assessed.setdefault(row["inn"], row["class_code"])
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
            if stops.triggered:
                with_factor += 1
                for code in stops.triggered:
                    by_factor[code] += 1
            verdict = route(
                computed,
                quarantined=(inn, moment.year) in quarantined,
                stop_factors=stops.triggered,
                financing_structure=inn in spv,
                operating_profit=Decimal(profit[0]["value"]) if profit else None,
                latest_annual=moment,
                assessed_class=assessed.get(inn),
                today=today,
                policy=policy,
                routing=routing,
                types=types,
            )
            baskets[verdict.basket] += 1
            for ground in verdict.grounds:
                grounds[verdict.basket][ground] += 1
            # Предмет основания берётся у самого основания (`Finding.subject`),
            # а не вытаскивается из прозы: второй разбор того же ответа
            # разошёлся бы с первым и молча.
            for item in verdict.findings:
                if item.ground == "level_off_scale":
                    off_scale[item.subject] += 1
                    off_scale_issuers.setdefault(inn, set()).add(item.subject)
            if "negative_nwc" in stops.triggered:
                with_nwc.add(inn)
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

    print(
        f"Оценка по разобранному документу есть у **{len(assessed)}** эмитентов; "
        "класс из неё старше любого признака агрегатора и берётся тем, "
        "который присвоен за последний оценённый период.\n"
    )

    print("| Корзина | Эмитентов | Доля |")
    print("|---|---|---|")
    for basket in routing.ordered():
        count = baskets.get(basket.code, 0)
        print(f"| {basket.name} | {count} | {count / total * 100:.0f} % |")

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
        print(
            f"\nТолько текущей ликвидностью за концом шкалы держится "
            f"**{len(only_liquidity)}** эмитентов, и у **{len(only_liquidity & with_nwc)}** "
            "из них сработал отрицательный чистый оборотный капитал — тот самый "
            "стоп-фактор, который по тяжести отправлен во внимание. Величина "
            "и стоп-фактор здесь об одном и том же: краткосрочные обязательства "
            "выше оборотных активов. Развилка записана в NIGHT_QUESTIONS.md; "
            "порог не трогается.\n"
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
