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
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import foreign_units  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import cards, exclusions, routing_rows  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.standards import Standard  # noqa: E402

logger = logging.getLogger(__name__)

REFERENCE = Path(__file__).resolve().parent / "routing_reference.yaml"

# Масштаб величин, объявленный источником построчно. Наименование единицы
# по коду ОКЕИ берёт справочник строк; здесь — только перевод множителя
# источника в то же наименование, и второй таблицы единиц не заводится.
_SCALE_NAMES = {"1000": "384", "1000000": "385", "1000000000": "386"}


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


def main() -> int:
    """Печатает исход сверки; 1 — при первом же расхождении."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    declared = yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
    by_inn = {item.inn: item for item in rows}
    # **Состав универсума и журнал исключений — предмет ожиданий фазы 1.**
    # Берутся они теми же вызовами, что и список: второй перечень исключённых
    # разошёлся бы с первым, и увидеть это было бы нечем.
    known = cards()
    bonds = bond_issuers()
    left, unconfirmed = exclusions(known, load_routing())

    print("# Эталон списка наблюдения: уровень проекта\n")
    print(
        f"Эмитентов в списке {counts['эмитентов']}, вышло из списка "
        f"{counts['вышло из списка']}, карточек справочника "
        f"{counts['карточек']}.\n"
    )
    divergences: list[str] = []
    checked = 0
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
