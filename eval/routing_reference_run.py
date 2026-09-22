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

import logging
import sys
from datetime import date
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import foreign_units  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

REFERENCE = Path(__file__).resolve().parent / "routing_reference.yaml"


def main() -> int:
    """Печатает исход сверки; 1 — при первом же расхождении."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    declared = yaml.safe_load(REFERENCE.read_text(encoding="utf-8"))
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
    by_inn = {item.inn: item for item in rows}

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
        for inn in issuers:
            checked += 1
            row = by_inn.get(inn)
            if expect == "absent":
                if row is not None:
                    divergences.append(
                        f"{code}: {inn} в списке есть, а ожидалось отсутствие"
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
                printed = " ".join(
                    entry.text
                    for entry in tuple(row.verdict.findings) + tuple(row.verdict.notes)
                )
                wrong = foreign_units(printed, row.unit)
                if wrong:
                    divergences.append(
                        f"{code}: у {row.name} ({inn}) напечатана единица "
                        f"«{', '.join(wrong)}», а комплект составлен "
                        f"в «{row.unit}»"
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
