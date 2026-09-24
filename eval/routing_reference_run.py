"""Эталон списка наблюдения: ожидания уровня проекта. **Расхождение — отказ.**

    uv run python eval/routing_reference_run.py

Проверяет то, за что отвечает проект: слой отчётности. Пустая долговая
нагрузка, ноль вместо нераскрытого, поглощённый эмитент в списке,
неположительная EBITDA в «Без внимания» — каждое из четырёх было найдено
экспертной проверкой 22.09.2026, и каждое проект исправить может.

**Ожидания контура здесь не проверяются, и это объявлено в самом составе**
(`eval/routing_reference.yaml`, раздел `circuit`): дефолт между отчётными
датами и отзыв рейтинга приходят из событийного слоя, которого в проекте нет.
Держать эталон красным по причине, которую проект исправить не может, значило
бы приучить не читать его вовсе.

**Замер не считает сам**: корзины берёт боевая маршрутизация через
`scoring.routing_store.routing_rows` — то же место, что список и распределение.
"""

import json
import logging
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import issuer_card_run as issuer_card  # noqa: E402

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.display import foreign_units  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import cards, exclusions, routing_rows  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.ratings_calendar import bound, read_actions, transitions  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

# Дата в формулировке основания: рыночная величина без дня, с которого она
# такая, не отличает вчерашнее падение от полугодового состояния.
_RU_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")

# Порядок корзин тяжести: чем больше число, тем мягче. Очереди («установить
# статус», «вне периметра», «структурный эмитент», «добрать поручителя») сюда
# не входят вовсе — они задают другой вопрос, а не меньшую тяжесть.
_WEAKER = {"review": 0, "attention": 1, "clear": 2}


def _market_baskets() -> dict[str, str]:
    """Корзина, объявленная у каждого рыночного основания самой методикой."""
    from finlib.sources.market import load_market

    policy = load_market()
    found = {step.ground: step.basket for step in policy.route_steps}
    found[policy.distress_zone.ground] = policy.distress_zone.basket
    return found


_MARKET_BASKETS = _market_baskets()

REFERENCE = Path(__file__).resolve().parent / "routing_reference.yaml"

# Масштаб величин, объявленный источником построчно. Наименование единицы
# по коду ОКЕИ берёт справочник строк; здесь — только перевод множителя
# источника в то же наименование, и второй таблицы единиц не заводится.
_SCALE_NAMES = {"1000": "384", "1000000": "385", "1000000000": "386"}

# Корзины, которые вправе стоять у эмитента с рейтингом категории дефолта:
# сама корзина разбора и очереди, которые старше её по порядку показа.
# Календарь на этот выбор не влияет вовсе — он датирует основание.
_SNAPSHOT_BASKETS = frozenset(
    {"review", "status_unknown", "structural_pool", "out_of_scope", "guarantor_missing"}
)

_GROUND_CHANGES = """
SELECT inn, as_of, basket, grounds
FROM routing_history WHERE kind = 'backfill' ORDER BY inn, as_of
"""


@lru_cache(maxsize=1)
def ground_only_changes() -> dict[str, tuple[date, ...]]:
    """Точки истории, где корзина та же, а перечень оснований другой.

    **Это и есть предмет ожидания об истории карточки.** Смена корзины видна
    и по списку; смена основания при той же корзине не видна нигде, кроме
    карточки, — и прежде она не была видна и там.
    """
    from finlib.db import connection as _connection
    from finlib.db import fetch_all as _fetch_all

    found: dict[str, list[date]] = {}
    with _connection() as conn:
        rows = _fetch_all(_GROUND_CHANGES, {}, conn=conn)
    before: dict[str, tuple[str, frozenset[str]]] = {}
    for row in rows:
        key = (str(row["basket"]), frozenset(row["grounds"] or ()))
        was = before.get(row["inn"])
        if was is not None and was[0] == key[0] and was[1] != key[1]:
            found.setdefault(row["inn"], []).append(row["as_of"])
        before[row["inn"]] = key
    return {inn: tuple(dates) for inn, dates in found.items()}


def built(row: object) -> str | None:
    """Текст карточки строки списка; None — не собралась.

    Карточка собирается тем же кодом, которым её собирает команда: второй
    её сборщик разошёлся бы с первым, и эталон проверял бы не то, что читают.
    Собранное запоминается: одна и та же карточка проверяется несколькими
    ожиданиями, а сборка идёт двумя запросами к базе.
    """
    inn = str(getattr(row, "inn", ""))
    if inn in _CARDS:
        return _CARDS[inn]
    try:
        with connection() as conn:
            text = issuer_card.card(row, _policy(), conn, _actions(), _bound())
    except Exception as failure:  # noqa: BLE001 — эталон называет отказ, а не падает
        logger.error("карточка %s: %s", inn, failure)
        _CARDS[inn] = None
        return None
    _CARDS[inn] = text
    return text


_CARDS: dict[str, str | None] = {}


@lru_cache(maxsize=1)
def _policy() -> object:
    """Справочник маршрутизации: один на прогон."""
    return load_routing()


@lru_cache(maxsize=1)
def _actions() -> tuple:
    """Календарь рейтинговых действий: читается один раз."""
    try:
        return read_actions()
    except FileNotFoundError:
        return ()


@lru_cache(maxsize=1)
def _bound() -> dict[str, str]:
    """Привязка наименований календаря к ИНН: один раз на прогон."""
    if not _actions():
        return {}
    names, _, _ = bound(_actions())
    return names


@lru_cache(maxsize=1)
def source_units() -> dict[tuple[str, date], set[str]]:
    """Единица, объявленная источником у каждой строки отчётности по МСФО.

    **Независимый ответ на тот же вопрос.** Единицу комплекта пишет загрузчик,
    и сверять её с самой собой бессмысленно; здесь она читается из сохранённого
    ответа источника — оттуда же, откуда пришла, но другим путём.
    """
    from finlib.normalize.lines import load_lines

    path = Path("data/raw/cbonds/msfo_real_universe.json")
    if not path.exists():
        return {}
    units = load_lines().units
    found: dict[tuple[str, date], set[str]] = {}
    for row in json.loads(path.read_text(encoding="utf-8")).get("items", []):
        inn = (row.get("emitent_inn") or "").strip()
        code = _SCALE_NAMES.get(str(row.get("ln105")))
        if not inn or not code:
            continue
        try:
            moment = date.fromisoformat(str(row.get("date")))
        except ValueError:  # pragma: no cover — дата у строки всегда есть
            continue
        found.setdefault((inn, moment), set()).add(units.name_of(code))
    return found


_RUNNING = "SELECT id, kind FROM routing_run WHERE status = 'running' ORDER BY id"


def writing() -> str:
    """Идущий прогон, который пишет историю; пусто — таких нет.

    **Эталон не работает, пока история пишется** (решение человека
    24.09.2026). Сводный ряд смен оснований и карточка читают одну и ту же
    таблицу порознь, и пересчёт, дописывающий её между этими чтениями, даёт
    расхождение, которого нет: 24.09.2026 так покраснели Арагон и Почта
    России. Красный по гонке эталон приучает не читать его вовсе — а эталон
    это то, чему верят.
    """
    with connection() as conn:
        found = fetch_all(_RUNNING, {}, conn=conn)
    return ", ".join(f"{item['kind']} №{item['id']}" for item in found)


def main() -> int:
    """Печатает исход сверки; 1 — при первом же расхождении."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    if busy := writing():
        print("# Эталон списка наблюдения: уровень проекта\n")
        print(
            f"**Прогон не выполнялся: история пишется прогоном {busy}.** "
            "Сверка читает `routing_history` дважды — сводным рядом и по "
            "карточке, — и запись между этими чтениями даёт расхождение, "
            "которого нет. Запусти после того, как прогон кончится."
        )
        return 1
    declared = yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
    by_inn = {item.inn: item for item in rows}
    # **Состав универсума и журнал исключений — предмет ожиданий фазы 1.**
    # Берутся они теми же вызовами, что и список: второй перечень исключённых
    # разошёлся бы с первым, и увидеть это было бы нечем.
    known = cards()
    bonds = bond_issuers()
    routing = load_routing()
    left, unconfirmed = exclusions(known, routing)

    print("# Эталон списка наблюдения: уровень проекта\n")
    print(
        f"Эмитентов в списке {counts['эмитентов']}, вышло из списка "
        f"{counts['вышло из списка']}, карточек справочника "
        f"{counts['карточек']}.\n"
    )
    divergences: list[str] = []
    checked = 0
    # **Код ожидания — имя, и двух одинаковых быть не должно.** Отчёт печатает
    # их построчно, и два ожидания под одним кодом читались бы как одно
    # проверенное дважды: первое из них при этом можно удалить, не заметив.
    codes = [item["code"] for item in declared["project"]]
    duplicated = sorted({code for code in codes if codes.count(code) > 1})
    if duplicated:
        divergences.append(
            f"эталон: код ожидания повторяется — {', '.join(duplicated)}"
        )
    for item in declared["project"]:
        expect, code = item["expect"], item["code"]
        issuers = list(item.get("issuers") or ())
        if item.get("rule") == "every_issuer":
            issuers = [row.inn for row in rows]
        if item.get("rule") == "every_issuer_with_non_positive_ebitda":
            issuers = [
                row.inn
                for row in rows
                if (value := row.values.get("ebitda")) is not None and value <= 0
            ]
        # **Каждый эмитент с долгом в обращении — предмет фазы 1.** Либо
        # у него есть корзина, либо он вышел из списка записью в журнале
        # исключений: третьего исхода нет, и молчание — не исход.
        if item.get("rule") == "every_bond_issuer":
            issuers = sorted(bonds)
        # Строки, маршрут которых построен по консолидированной отчётности:
        # единицу у них объявляет источник построчно, и сверить её есть с чем.
        if item.get("rule") == "every_ifrs_row":
            issuers = sorted(
                row.inn
                for row in rows
                if row.standard is Standard.IFRS and row.report_date is not None
            )
        # Эмитенты, у которых поле преемника заполнено, а статус карточки —
        # действующий. Прочитанное как «поглощён», поле вывело из списка
        # 24 живых эмитента; правило требует, чтобы они в нём стояли.
        if item.get("rule") == "every_live_issuer_with_successor_field":
            # **Круг сужен до эмитентов с долгом намеренно.** Ожидание о том,
            # что поле преемника не выводит живого эмитента из списка,
            # а не о том, что в списке стоят все карточки справочника:
            # у Самараэнерго и Саратовэнерго поле заполнено и статус
            # действующий, но выпусков в обращении нет и отчётности у нас
            # тоже — их отсутствие говорит о составе списка, а не о правиле.
            issuers = sorted(
                inn
                for inn, card in known.items()
                if str(card.get("emitents_id_absorption") or "").strip()
                not in ("", "0", "None")
                and inn in bonds
                and inn not in left
                and inn not in unconfirmed
            )
        if item.get("rule") == "every_excluded_issuer":
            issuers = sorted(left)
        # --- фаза 2-бис: круги ожиданий карточки ----------------------------
        # Основания, отброшенные типом эмитента: у них и проверяется, что
        # собственные величины не исчезли, а стали сведениями.
        if item.get("rule") == "every_issuer_with_inapplicable":
            issuers = [row.inn for row in rows if row.verdict.inapplicable]
        # **Круг может оказаться пустым, и это объявляется, а не молчит.**
        # Точка, в которой корзина та же, а основания другие, берётся
        # из записанной истории: выдумывать её нельзя, а нулевой круг
        # означает «проверять нечего», а не «проверено».
        if item.get("rule") == "every_issuer_with_ground_only_change":
            issuers = sorted(ground_only_changes())
        if item.get("rule") == "every_issuer_with_ratings":
            issuers = [
                row.inn
                for row in rows
                if row.events is not None and row.events.ratings
            ]
        # --- фаза 3: круг рыночного слоя ------------------------------------
        # Эмитенты, у которых сработало рыночное основание. Круг пустой
        # означал бы «доставки срезов нет», а не «рынок молчал», и объявляется
        # он тем же способом, что и прочие: числом проверенных.
        if item.get("rule") == "every_issuer_with_market_ground":
            issuers = [
                row.inn
                for row in rows
                if any(
                    entry.ground.startswith("market_")
                    for entry in row.verdict.findings
                )
            ]
        if item.get("rule") == "every_issuer_with_transition":
            moved = {holder for holder, _, _, _ in transitions()}
            issuers = [row.inn for row in rows if row.inn in moved]
        for inn in issuers:
            checked += 1
            row = by_inn.get(inn)
            if expect == "absent":
                if row is not None:
                    divergences.append(
                        f"{code}: {inn} в списке есть, а ожидалось отсутствие"
                    )
                continue
            # **Третьего исхода нет.** Эмитент с долгом либо имеет корзину,
            # либо назван в журнале исключений с причиной и преемником.
            # Молчание — не исход, и ровно им список однажды и уменьшился.
            if expect == "routed_or_excluded":
                if row is None and inn not in left:
                    divergences.append(
                        f"{code}: эмитент {inn} с выпусками в обращении "
                        "не имеет ни корзины, ни записи в журнале исключений"
                    )
                continue
            if expect == "excluded_with_reason":
                record = left.get(inn)
                if record is None or not record.reason or not record.successor:
                    divergences.append(
                        f"{code}: выход эмитента {inn} объявлен без причины "
                        "либо без преемника"
                    )
                continue
            if row is None:
                divergences.append(f"{code}: {inn} в списке нет, проверить нечем")
                continue
            if expect == "present":
                # Дальше проверять нечего: ожидание было именно о присутствии,
                # и оно уже выполнено — строка в списке есть.
                continue
            if expect == "review" and row.verdict.basket != "review":
                divergences.append(
                    f"{code}: {row.name} ({inn}) в «{row.verdict.basket_name}», "
                    "а ожидался разбор"
                )
            if expect == "attention":
                # Корзина и основание проверяются вместе: внимание, полученное
                # по другой причине, о правиле давности не говорит ничего.
                if row.verdict.basket != "attention":
                    divergences.append(
                        f"{code}: {row.name} ({inn}) в «{row.verdict.basket_name}», "
                        "а ожидалось внимание"
                    )
                fired = {entry.ground for entry in row.verdict.findings}
                if item["ground"] not in fired:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) основание "
                        f"{item['ground']} не сработало"
                    )
            # **«Не ниже разбора» и «не разбор» — два края одного ожидания.**
            # Первое ловит пропажу обстоятельства, второе — разрастание
            # правила: «Разбор», выросший до половины списка, первым
            # не ловится вовсе.
            if expect == "not_below_review":
                order = routing.basket(row.verdict.basket).order
                if order > routing.basket("review").order:
                    divergences.append(
                        f"{code}: {row.name} ({inn}) в "
                        f"«{row.verdict.basket_name}», а ожидалось не ниже "
                        "разбора"
                    )
            if expect == "not_review" and row.verdict.basket == "review":
                divergences.append(
                    f"{code}: {row.name} ({inn}) в «Разборе», "
                    "а разбор о нём сказан быть не мог"
                )
            if expect == "not_clear" and row.verdict.basket == "clear":
                divergences.append(
                    f"{code}: {row.name} ({inn}) в «Без внимания», "
                    f"а ожидалось не ниже внимания"
                )
            if expect == "shows_bound":
                # Граница обязана быть видна, а «данных недостаточно»
                # по долговой нагрузке — не сработать: пробелом граница
                # не является.
                if row.values.get("net_debt_op_profit") is None:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) границы нет в величинах "
                        "строки — печатать нечего"
                    )
                fired = {
                    entry.subject
                    for entry in row.verdict.findings
                    if entry.ground == "data_insufficient"
                }
                if "долговая нагрузка" in fired:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) долговая нагрузка названа "
                        "недостающей, хотя граница есть"
                    )
            if expect == "unit_named":
                # Сверяет та же функция, что документ: вопрос у трёх выходов
                # один — не напечатана ли единица чужого комплекта.
                # Основание, перенесённое от поручителя, названо в его
                # единице: сверяется каждое со своей, а не все с единицей
                # строки. Свалить их в одну строку значило бы объявить
                # расхождением верную печать.
                wrong = [
                    name
                    for unit, text in row.verdict.by_unit(row.unit)
                    for name in foreign_units(text, unit)
                ]
                if wrong:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) напечатана единица "
                        f"«{', '.join(wrong)}», а комплект составлен "
                        f"в «{row.unit}»"
                    )
            # **Единица строки сверяется с объявленной источником.** Проверка
            # `unit_named` спрашивает другое — не напечатана ли в тексте чужая
            # единица; она ловит расхождение внутри строки и молчит, если
            # неверна сама графа. Умолчание «тыс. руб.» однажды подписало
            # тысячами миллионы, и вопрос «а не вернулось ли оно» этой
            # проверкой не закрывается.
            if expect == "unit_matches_source":
                told = source_units().get((inn, row.report_date))
                if not told:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) единицу источника "
                        "сверить нечем: строки за этот период в ответе нет"
                    )
                elif row.unit not in told:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) в строке «{row.unit}», "
                        f"а источник объявил «{', '.join(sorted(told))}»"
                    )
            if expect == "no_ground":
                fired = {entry.ground for entry in row.verdict.findings}
                if item["ground"] in fired:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) сработало основание "
                        f"{item['ground']}, а его быть не должно"
                    )
            # --- фаза 2-бис: ожидания карточки ------------------------------
            if expect == "card_builds":
                text = built(row)
                if text is None:
                    divergences.append(
                        f"{code}: карточка {row.name} ({inn}) не собирается"
                    )
                elif inn not in text:
                    divergences.append(
                        f"{code}: в карточке {inn} нет его же ИНН — "
                        "собралась чужая"
                    )
            if expect == "own_grounds_kept":
                said = [
                    entry
                    for entry in row.verdict.notes
                    if entry.ground == "inapplicable_here"
                ]
                if not said:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) тип отбросил основания "
                        f"{', '.join(row.verdict.inapplicable)}, а сведения "
                        "о них исчезли"
                    )
            if expect == "history_shows_ground_change":
                text = built(row) or ""
                missing = [
                    f"{when:%d.%m.%Y}"
                    for when in ground_only_changes().get(inn, ())
                    if f"{when:%d.%m.%Y}" not in text
                ]
                if missing:
                    divergences.append(
                        f"{code}: в истории {row.name} ({inn}) нет точек "
                        f"смены оснований {', '.join(missing)}"
                    )
            # --- фаза 3: рыночное основание -----------------------------
            # **Рыночная величина без даты и без ориентира не говорит
            # ничего.** Спред двигается ежедневно, и «кратность 31×» без
            # ориентира дня — число неизвестного смысла; «цена 30 %» без дня,
            # с которого она такая, не отличает вчерашнее падение
            # от полугодового состояния.
            if expect == "market_ground_speaks":
                for entry in row.verdict.findings:
                    if not entry.ground.startswith("market_"):
                        continue
                    if not _RU_DATE.search(entry.text):
                        divergences.append(
                            f"{code}: у {row.name} ({inn}) основание "
                            f"{entry.ground} без даты: «{entry.text}»"
                        )
                    if entry.ground.startswith("market_spread") and (
                        "ориентире" not in entry.text
                    ):
                        divergences.append(
                            f"{code}: у {row.name} ({inn}) спред напечатан "
                            f"без ориентира дня: «{entry.text}»"
                        )
                # Корзина не мягче той, которую основание называет: рыночное
                # основание либо названо корзиной, либо погашено — молча
                # исчезнуть оно не вправе.
                grounds = set(row.verdict.grounds) | set(row.verdict.muted)
                # **Корзину называют основания той тяжести, по которой она
                # выбрана.** Основание внимания у эмитента в разборе корзины
                # не называет — и это не потеря, а порядок: потерей было бы
                # обратное, корзина мягче собственного основания.
                lost = [
                    entry.ground
                    for entry in row.verdict.findings
                    if entry.ground.startswith("market_")
                    and entry.ground not in grounds
                    and _WEAKER.get(row.verdict.basket, 9)
                    > _WEAKER.get(_MARKET_BASKETS.get(entry.ground, ""), 9)
                ]
                # **Очередь старше корзин тяжести, и это не потеря.** У эмитента
                # в очереди статуса вопрос другой — что с ним стало, — и корзину
                # рыночное основание там не называет. Но исчезнуть оно не вправе:
                # в карточке оно обязано стоять, иначе читатель увидит очередь
                # и не увидит, что рынок о нём говорит.
                if lost and row.verdict.basket in ("review", "attention", "clear"):
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) рыночное основание "
                        f"{', '.join(lost)} не назвало корзины и не погашено: "
                        f"корзина «{row.verdict.basket}»"
                    )
                elif lost:
                    text = built(row) or ""
                    unseen = [
                        entry.text
                        for entry in row.verdict.findings
                        if entry.ground in lost and entry.text not in text
                    ]
                    if unseen:
                        divergences.append(
                            f"{code}: у {row.name} ({inn}) корзина "
                            f"«{row.verdict.basket}» старше рыночного основания, "
                            "а самого основания в карточке нет"
                        )
            if expect == "ratings_split_by_object":
                text = built(row) or ""
                if "Рейтинги эмитента (снимок)" not in text:
                    divergences.append(
                        f"{code}: в карточке {row.name} ({inn}) рейтинги есть, "
                        "а раздела снимка нет"
                    )
                other = [item for item in row.events.ratings if not item.credit]
                if other and "Некредитных рейтингов" not in text:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) {len(other)} некредитных "
                        "рейтингов, и карточка о них молчит"
                    )
                if "Рейтингов **выпусков**" not in text:
                    divergences.append(
                        f"{code}: карточка {row.name} ({inn}) не называет, "
                        "что рейтингов выпусков у нас нет"
                    )
            if expect == "calendar_dates_only":
                dated = [
                    entry
                    for entry in row.verdict.findings
                    if entry.ground == "rating_default" and " с " in entry.text
                ]
                if dated and row.verdict.basket not in _SNAPSHOT_BASKETS:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) основание датировано "
                        f"календарём, а корзина «{row.verdict.basket}» — "
                        "не та, что даёт категория снимка"
                    )
        print(f"- {code}: проверено эмитентов {len(issuers)} — {item['where'].strip()}")

    print(
        f"\nПроверено ожиданий уровня проекта {checked}, расхождений "
        f"{len(divergences)}."
    )
    for line in divergences:
        print(f"  РАСХОЖДЕНИЕ {line}")
    print(
        f"\nОжиданий уровня контура объявлено {len(declared['circuit'])}, "
        "и они здесь **не проверяются**: события и рынок приходят не из проекта."
    )
    for item in declared["circuit"]:
        print(f"- {item['code']}: эмитентов {len(item['issuers'])}")
    return 1 if divergences else 0


if __name__ == "__main__":
    sys.exit(main())
