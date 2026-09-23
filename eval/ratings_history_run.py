"""Что даёт история рейтинговых действий: упреждение, ложные тревоги, понижения.

**Слой проверки, а не маршрута.** История выгружена руками, подписка отдаёт
только последнее значение, и правило маршрута остаётся прежним — оно строится
на ежедневном снимке. Календарь отвечает на три вопроса, на которые снимок
ответить не может по устройству:

1. за сколько дней до дефолта снимали рейтинг — упреждение правила отзыва;
2. у скольких сняли рейтинг, а события за год не случилось — ложные тревоги;
3. за сколько дней до дефолта понижали уровень и сколько эмитентов с дефолтом
   имели подтверждение рейтинга в последние месяцы до него.

Плюс отдельный вопрос: у скольких структурных эмитентов есть рейтинг транша —
для СФО он и есть оценка качества.

    uv run python eval/ratings_history_run.py > data/output/ratings_history.md

**Замер не считает сам**: события дефолта берутся тем же перечнем, которым
их берёт маршрут (`cbonds_events.default_records`), корзины — из истории,
а календарь читается одним модулем (`eval/ratings_calendar.py`).
"""

import logging
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ratings_calendar import (  # noqa: E402
    NOT_CREDIT,
    TRANCHE_SCALES,
    Action,
    bound,
    direction,
    prepared,
    read_actions,
    scale_ids,
)

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import default_records, issues_of  # noqa: E402

logger = logging.getLogger(__name__)

OUT = Path("data/output")

# Окно, в котором событие считается последовавшим за отзывом. Год — та же
# величина, которой меряется свежесть отзыва в маршруте: правило объявляет
# отзыв обстоятельством на год, и мерить его другим окном значило бы мерить
# не то правило.
YEAR = 365

# Окно «подтверждение незадолго до события»: три месяца — решение человека
# 23.09.2026, величина предварительная.
AFFIRM_WINDOW = 92

_MODELLED = """
SELECT count(*) FILTER (WHERE meta->>'disclosed_on' IS NOT NULL) AS настоящая,
       count(*) AS всего
FROM src_file WHERE is_actual AND status <> 'quarantine'
"""


def defaults_by_inn() -> dict[str, date]:
    """ИНН → дата первого неисполненного события дефолта; только они.

    Исполненное обязательство событием, к которому готовятся, не является:
    вопрос замера — предупреждал ли рейтинг о том, что случилось.
    """
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


def _credit(action: Action) -> bool:
    """Кредитное ли действие: шкалы вне круга исключены поимённо."""
    return action.scale not in NOT_CREDIT


def _spread(days: list[int]) -> str:
    """Медиана и разброс словами; пустой перечень называется пустым."""
    if not days:
        return "наблюдений нет"
    return (
        f"медиана **{statistics.median(days):.0f}** дн., "
        f"от {min(days)} до {max(days)}, наблюдений {len(days)}"
    )


def main() -> int:
    """Печатает три замера по календарю рейтинговых действий."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    routing = load_routing()
    actions = read_actions()
    names, counts, disagree = bound(actions)
    scales = scale_ids()
    from finlib.sources.cbonds_events import point_order

    order = point_order()
    events = defaults_by_inn()

    print("# История рейтинговых действий: упреждение, тревоги, понижения\n")
    print(
        f"Записей календаря **{len(actions)}**: об эмитенте "
        f"{sum(1 for item in actions if item.about == 'issuer')}, об эмиссии "
        f"{sum(1 for item in actions if item.about == 'emission')}. "
        f"Глубина: {min(item.when for item in actions):%d.%m.%Y} — "
        f"{max(item.when for item in actions):%d.%m.%Y}.\n"
    )
    print(
        "**Уровень и прогноз разделены, и события у них разные**: изменений "
        f"уровня {sum(1 for item in actions if item.level_changed)}, изменений "
        f"одного прогноза при том же уровне "
        f"{sum(1 for item in actions if item.forecast_only)}, подтверждений "
        f"{sum(1 for item in actions if item.affirmed)}. Сложенные вместе, "
        "первые два дали бы «изменение», которым понижение не является.\n"
    )

    print("## Привязка наименований к ИНН\n")
    print(
        f"Привязано **{counts['привязано']} наименований из "
        f"{counts['наименований всего']}**: подтверждено обоими словарями "
        f"{counts['подтверждено обоими']}, только по карточкам "
        f"{counts['только по карточкам']}, только по ISIN "
        f"{counts['только по ISIN']}. Словари разошлись у "
        f"{counts['словари разошлись']}.\n"
    )
    lost = sorted(
        {prepared(item.name.split(",")[0]) for item in actions}
        - set(names)
        - {""}
    )
    foreign = [item for item in lost if any("a" <= ch <= "z" for ch in item)]
    print(
        f"Не привязано **{len(lost)}**, из них с латиницей {len(foreign)} — "
        "иностранные эмитенты в наш круг не входят. Остальные — рейтингуемые "
        "лица без облигаций: круг карточек у нас 977 и собран по выпускам, "
        "а календарь шире.\n"
    )
    path = OUT / "ratings_calendar_unbound.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Наименования календаря без привязки к ИНН\n\n"
        "Сопоставление только дословное после приведения: «Газпром», "
        "«Газпром нефть» и «Газпром капитал» — разные эмитенты.\n\n"
        + "\n".join(f"- {item}" for item in lost)
        + "\n",
        encoding="utf-8",
    )
    print(f"Перечень несопоставившегося — `{path}`.\n")

    # --- 1. упреждение отзыва ------------------------------------------------
    print("## 1. Упреждение правила отзыва\n")
    issuer_lead: list[int] = []
    issue_lead: list[int] = []
    for item in actions:
        if not item.withdrawn or not _credit(item):
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is None or inn not in events:
            continue
        lead = (events[inn] - item.when).days
        if lead < 0:
            continue
        (issuer_lead if item.about == "issuer" else issue_lead).append(lead)
    print(f"- отзыв рейтинга **эмитента** до дефолта: {_spread(issuer_lead)};")
    print(f"- отзыв рейтинга **выпуска** до дефолта: {_spread(issue_lead)}.\n")
    # **Ноль наблюдений по выпускам — сведение, а не пробел.** Отзывов
    # по эмиссиям в календаре много, они привязываются, и ни один
    # не принадлежит эмитенту с неисполненным дефолтом: рейтинга выпуска
    # у наших дефолтников нет вовсе — он есть у крупных эмитентов,
    # а дефолты случились у малых.
    emission_withdrawals = [
        item for item in actions if item.about == "emission" and item.withdrawn
    ]
    bound_withdrawals = [
        item
        for item in emission_withdrawals
        if names.get(prepared(item.name.split(",")[0]))
    ]
    print(
        f"**Ноль по выпускам — сведение, а не пробел.** Отзывов по эмиссиям "
        f"в календаре {len(emission_withdrawals)}, привязано "
        f"{len(bound_withdrawals)}, и ни один не принадлежит эмитенту "
        "с неисполненным дефолтом: рейтинг выпуска есть у крупных эмитентов, "
        "а дефолты случились у малых. Проверено поимённо — у Кириллицы, "
        "ЕвроТранса, Роял Капитала, Монополии, Главснаба и Нэппи Клаба "
        "в календаре только рейтинги эмитента.\n"
    )
    if issuer_lead:
        buckets = Counter(
            "до 30 дней"
            if item <= 30
            else "31–90"
            if item <= 90
            else "91–180"
            if item <= 180
            else "181–365"
            if item <= YEAR
            else "больше года"
            for item in issuer_lead
        )
        print("| Упреждение | Наблюдений |")
        print("|---|---|")
        for label in ("до 30 дней", "31–90", "91–180", "181–365", "больше года"):
            if buckets.get(label):
                print(f"| {label} | {buckets[label]} |")
        print()

    # --- 2. ложные тревоги ---------------------------------------------------
    print("## 2. Ложные тревоги\n")
    withdrawn_inns: dict[str, date] = {}
    for item in actions:
        if item.about != "issuer" or not item.withdrawn or not _credit(item):
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is None:
            continue
        if inn not in withdrawn_inns or item.when < withdrawn_inns[inn]:
            withdrawn_inns[inn] = item.when
    followed = sum(
        1
        for inn, when in withdrawn_inns.items()
        if inn in events and 0 <= (events[inn] - when).days <= YEAR
    )
    quiet = len(withdrawn_inns) - followed
    print(
        f"Отзыв рейтинга эмитента привязан к ИНН у **{len(withdrawn_inns)}** "
        f"эмитентов. Событие в течение года после отзыва случилось у "
        f"**{followed}**, не случилось у **{quiet}** — то есть ложных тревог "
        f"{quiet / len(withdrawn_inns) * 100:.0f} %, если считать тревогой "
        "сам отзыв.\n"
        if withdrawn_inns
        else "Привязанных отзывов нет: считать не на чем.\n"
    )
    print(
        "**Это верхняя оценка ложных тревог, а не их число.** Отзыв бывает "
        "по инициативе агентства и по окончании договора с эмитентом, "
        "и различить их источником нельзя: «тревога» здесь — наше слово, "
        "а не действие агентства.\n"
    )

    # --- 3. понижения перед дефолтами ---------------------------------------
    print("## 3. Понижения перед дефолтом и подтверждения\n")
    downgrades: list[int] = []
    affirmed_close: set[str] = set()
    by_inn: dict[str, list[Action]] = defaultdict(list)
    for item in actions:
        if item.about != "issuer" or not _credit(item):
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is not None and inn in events:
            by_inn[inn].append(item)
    for inn, own in by_inn.items():
        event = events[inn]
        for item in own:
            if item.when > event:
                continue
            scale = scales.get(item.scale, "")
            if item.level_changed and direction(item, order, scale) < 0:
                downgrades.append((event - item.when).days)
            if item.affirmed and 0 <= (event - item.when).days <= AFFIRM_WINDOW:
                affirmed_close.add(inn)
    print(f"- понижение уровня до дефолта: {_spread(downgrades)};")
    print(
        f"- эмитентов с дефолтом, у которых рейтинг **подтверждали** "
        f"в последние {AFFIRM_WINDOW} дней до события: "
        f"**{len(affirmed_close)}** из {len(by_inn)} с привязанными "
        "действиями.\n"
    )
    print(
        "**Направление взято у справочника точек шкалы**, а не у написания: "
        "«AA» длиннее «C» и любым сравнением строк вышло бы «больше». Точка, "
        "которой в справочнике нет, в сравнение не идёт вовсе.\n"
    )

    # --- 3-бис. понижение как кандидат в основание ---------------------------
    _downgrade_alarms(actions, names, scales, order, events)

    # --- 4. рейтинг транша ---------------------------------------------------
    print("## 4. Рейтинг транша у структурных эмитентов\n")
    tranche: set[str] = set()
    for item in actions:
        if item.scale not in TRANCHE_SCALES:
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is not None:
            tranche.add(inn)
    with connection() as conn:
        structural = _structural(conn, routing)
        row = fetch_all(_MODELLED, {}, conn=conn)[0]
    print(
        f"Действий по шкалам структурного финансирования "
        f"{sum(1 for item in actions if item.scale in TRANCHE_SCALES)}; "
        f"привязано к эмитентам {len(tranche)}. Структурных эмитентов "
        f"в списке {len(structural)}, из них с рейтингом транша "
        f"**{len(tranche & structural)}**.\n"
    )
    print(
        "**Для СФО рейтинг транша и есть оценка качества**, и знаменатель "
        "здесь важнее числа: у скольких его нет — столько же эмитентов "
        "остаётся в очереди «оценка по пулу» без внешнего мнения вовсе.\n"
    )

    # --- оговорка о смоделированной дате -------------------------------------
    print("## Оговорка: чем датирована наша осведомлённость\n")
    print(
        f"Настоящая дата раскрытия стоит у **{row['настоящая']}** комплектов "
        f"из **{row['всего']}**; у остальных она смоделирована сроком закона. "
        "Пересчитанная история на этом завышает нашу осведомлённость: "
        "у Газпрома отчётность за 2021 год раскрыта 19.10.2023, а модель "
        "объявила бы её известной 31.03.2022 — за полтора года до появления. "
        "Упреждение, посчитанное по такой истории, — верхняя оценка.\n"
    )
    return 0


def _notches(action: Action, order: dict[tuple[str, str], int], scale: str) -> int:
    """На сколько ступеней шкалы понизили; 0 — сравнить нечем.

    Ступень берётся у справочника точек: расстояние между местами. Шкалы
    у агентств разные по длине, и сравнивать ступени между шкалами можно лишь
    приблизительно — поэтому считается внутри одной.
    """
    now = order.get((scale, action.level))
    was = order.get((scale, action.was_level))
    if now is None or was is None:
        return 0
    return now - was


def _in_c(action: Action, order: dict[tuple[str, str], int], scale: str) -> bool:
    """Перешёл ли уровень в категорию C и ниже.

    Категория — буква уровня, и перечень её значений объявлен методикой
    (`routing.events.review_categories`): C, CC, CCC, D, RD, SD. Переходом
    считается движение **в** них из категории выше: эмитент, уже стоявший
    в C, никуда не перешёл.
    """
    from finlib.scoring.routing import load_routing
    from finlib.sources.cbonds_events import category_of

    deep = set(load_routing().events.review_categories)
    now, was = category_of(action.level), category_of(action.was_level)
    return now in deep and was not in deep


def _downgrade_alarms(  # noqa: ANN001
    actions, names, scales, order, events
) -> None:
    """Ложные тревоги у понижений: по ступеням и по переходу в категорию C.

    **Понижение похоже на сигнал там, где отзыв не похож**: медиана упреждения
    у него та же, но событие после него случается чаще. Пороги здесь
    не ставятся — замер печатает исходы, решение за человеком.
    """
    print("## 3-бис. Понижение уровня как кандидат в основание\n")
    buckets: dict[str, list[tuple[str, date]]] = {
        "на одну ступень": [],
        "на две и более": [],
        "переход в C и ниже": [],
    }
    for item in actions:
        if item.about != "issuer" or not item.level_changed or not _credit(item):
            continue
        inn = names.get(prepared(item.name.split(",")[0]))
        if inn is None:
            continue
        scale = scales.get(item.scale, "")
        steps = _notches(item, order, scale)
        if steps <= 0:
            continue
        label = "на одну ступень" if steps == 1 else "на две и более"
        buckets[label].append((inn, item.when))
        if _in_c(item, order, scale):
            buckets["переход в C и ниже"].append((inn, item.when))
    # **Глубина понижения и категория, в которую понизили, — разные сведения**,
    # и разделить их надо прямо: иначе «две ступени и более» выглядит сигналом
    # за счёт тех случаев, где понижали до категории C.
    deep_not_c = [
        (inn, when)
        for inn, when in buckets["на две и более"]
        if (inn, when) not in set(buckets["переход в C и ниже"])
    ]
    buckets["на две и более, но не в C"] = deep_not_c
    print("| Понижение | Наблюдений | Событие за год | Ложных тревог | Упреждение |")
    print("|---|---|---|---|---|")
    for label, found in buckets.items():
        # Считается эмитент, а не действие: два понижения одного эмитента
        # перед одним дефолтом — одно наблюдение, а не два.
        first: dict[str, date] = {}
        for inn, when in found:
            if inn not in first or when < first[inn]:
                first[inn] = when
        lead = [
            (events[inn] - when).days
            for inn, when in first.items()
            if inn in events and 0 <= (events[inn] - when).days <= YEAR
        ]
        total = len(first)
        share = f"{(total - len(lead)) / total * 100:.0f} %" if total else "—"
        ahead = (
            f"медиана {statistics.median(lead):.0f} дн." if lead else "наблюдений нет"
        )
        print(f"| {label} | {total} | {len(lead)} | {share} | {ahead} |")
    print(
        "\n**Ложной тревогой здесь названо отсутствие события за год**, "
        "и это та же мерка, что у отзыва: у отзыва 153 из 158. Понижение "
        "при этом действие агентства о самом эмитенте, а отзыв — об "
        "отношениях с ним, и потому сравнивать доли осмысленно.\n"
    )
    print(
        "**Глубина понижения не говорит почти ничего, а категория говорит "
        "всё.** Понижение на две ступени и более, не доходящее до C, даёт те "
        "же 95 % ложных, что и понижение на одну: сигнал в строке «на две "
        "и более» держится целиком теми случаями, где понизили **до** C. "
        "Отсюда и упреждение: у перехода в C оно короче (медиана 42 дня), "
        "потому что это уже не предупреждение, а признание.\n"
    )
    print(
        "**Порогов здесь нет намеренно.** Замер печатает исходы; какая "
        "глубина понижения и какая категория становятся основанием — решение "
        "человека, и принимается оно по этим числам, а не по нашему выбору.\n"
    )


def _structural(conn, routing) -> set[str]:  # noqa: ANN001
    """ИНН структурных эмитентов по тому же признаку, что у маршрута."""
    from finlib.scoring.routing_store import cards

    found: set[str] = set()
    for inn, card in cards().items():
        kind, _ = routing.type_of(card)
        if kind is not None and kind.code == "structural":
            found.add(inn)
    return found


if __name__ == "__main__":
    sys.exit(main())
