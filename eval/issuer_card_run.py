"""Карточка эмитента: всё, что мы о нём знаем, на одной странице.

**Карточку открывают после отчёта изменений**, когда он назвал эмитента:
вопрос читателя здесь другой — не «что изменилось», а «что с ним вообще».
Поэтому карточка собирает разом то, что в списке свёрнуто до одной строки:
корзину с основаниями, величины маршрута, историю корзин, события, выпуски,
комплекты отчётности — и то, чего мы не знаем.

**Карточка ничего не считает.** Корзину и величины даёт боевая маршрутизация,
историю — записанная история корзин, события — тот же перечень, которым
их берёт маршрут. Второй путь к любому из этих ответов разошёлся бы с первым,
и увидеть это было бы нечем.

**Календарь рейтинговых действий показан отдельным разделом и помечен.**
Он слой проверки, в маршрут не входит, и смешивать его с основаниями корзины
нельзя: читатель иначе решит, что маршрут его видит.

    uv run python eval/issuer_card_run.py 9703024202 4004021785 7726588547
"""

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.display import foreign_units, money  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

OUT = Path("data/output/cards")

_HISTORY = """
SELECT as_of, kind, basket, subgroup, grounds, report_date
FROM routing_history WHERE inn = %(inn)s ORDER BY as_of
"""

_SETS = """
SELECT standard, source, report_year, reporting_kind, status, unit_code,
       correction_version, is_actual, meta->>'disclosed_on' AS disclosed
FROM src_file WHERE inn = %(inn)s
ORDER BY standard, report_year DESC, source
"""


def _changes(rows: list[dict]) -> list[dict]:
    """Точки, в которых корзина изменилась; первая — начало наблюдения.

    История печатается сменами, а не всеми точками: двести двадцать три
    строки, из которых двести двадцать одинаковы, читатель пролистает.
    """
    found: list[dict] = []
    previous = ""
    for row in rows:
        if row["basket"] != previous:
            found.append(row)
            previous = row["basket"]
    return found


def card(item, routing, conn) -> str:  # noqa: ANN001
    """Собирает карточку одного эмитента."""
    verdict = item.verdict
    basket = routing.basket(verdict.basket)
    names = {
        ground.code: ground.name
        for entry in routing.baskets
        for ground in entry.grounds
    }
    said: list[str] = []
    add = said.append

    add(f"# {item.name} ({item.inn})\n")
    add(
        f"**{basket.name}**"
        + (f" · {verdict.subgroup_names[0]}" if verdict.subgroup_names else "")
        + f" · {item.basis or 'отчётности нет'}"
        + (f" · {item.issuer_type}" if item.issuer_type else "")
        + "\n"
    )
    if verdict.actions:
        add(f"Действие: {verdict.actions[0]}\n")

    add("\n## Основания корзины\n")
    if not verdict.findings:
        add("оснований нет — это и означает «человек не нужен».\n")
    # **Основания корзины и обстоятельства эмитента — разные перечни.**
    # Корзину называют только свои основания, а сработало обычно больше:
    # они остаются в карточке, потому что человеку нужны все, — но помечены,
    # иначе читатель решит, что корзину назвало каждое.
    own = {ground.code for ground in basket.grounds}
    for entry in verdict.findings:
        mark = "" if entry.ground in own else " *(сведение)*"
        add(f"- **{names.get(entry.ground, entry.ground)}**{mark}: {entry.text}")
    if verdict.notes:
        add("\n**Справочно** — корзину не называет, но и не исчезает:\n")
        for entry in verdict.notes:
            add(f"- {entry.text}")
    if verdict.inapplicable:
        add(
            "\n**Неприменимо к типу эмитента**: "
            + ", ".join(
                sorted({names.get(code, code) for code in verdict.inapplicable})
            )
            + ".\n"
        )

    add("\n## Величины маршрута\n")
    if not item.shown_values:
        add("не рассчитано ни одной: отчётности нет либо она в карантине.\n")
    else:
        # **После наименования единицы точка не ставится**: оно кончается
        # точкой само, и выходит «тыс. руб..».
        add(f"Единица комплекта — {item.unit or 'не названа'}\n")
        add("\n| Показатель | Значение |")
        add("|---|---|")
        for _, name, shown in item.shown_values:
            add(f"| {name} | {shown} |")
    if item.refinance is not None and item.refinance.due is not None:
        # **Величины печатаются той же единой точкой округления**, что
        # в списке и в документе: набранные здесь во второй раз, они пришли бы
        # в карточку сырым `Decimal` — «20546.803158840000000» вместо
        # «20 547».
        cash, offered = item.refinance.cash, item.refinance.offered
        add(
            f"\nВ единице комплекта: платежи ближайшего года "
            f"{money(item.refinance.due)}, оферты "
            f"{money(offered) if offered is not None else '—'}, денежные "
            f"средства {money(cash) if cash is not None else 'не раскрыты'}\n"
        )

    add("\n## История корзин\n")
    rows = fetch_all(_HISTORY, {"inn": item.inn}, conn=conn)
    moves = _changes(rows)
    if not rows:
        add("истории нет: пересчёт не выполнялся.\n")
    else:
        add(
            f"Точек {len(rows)} за "
            f"{rows[0]['as_of']:%d.%m.%Y} — {rows[-1]['as_of']:%d.%m.%Y}, "
            f"смен корзины {len(moves) - 1}.\n"
        )
        add("\n| Дата | Корзина | Основания |")
        add("|---|---|---|")
        for row in moves:
            listed = ", ".join(names.get(code, code) for code in row["grounds"])
            add(
                f"| {row['as_of']:%d.%m.%Y} | "
                f"{routing.basket(row['basket']).name} | {listed or '—'} |"
            )

    add("\n## Выпуски и события\n")
    events = item.events
    if events is None or not events.issues_known:
        add("перечня выпусков на диске нет — это не «выпусков нет».\n")
    else:
        alive = [issue for issue in events.issues if issue.status == "в обращении"]
        add(
            f"Выпусков всего {len(events.issues)}, в обращении {len(alive)}; "
            f"событий дефолта {len(events.records)}, из них неисполненных "
            f"{len(events.open_records)}.\n"
        )
        for record in sorted(
            events.open_records, key=lambda entry: entry.moment or date.min, reverse=True
        )[:10]:
            issue = next(
                (
                    entry.name
                    for entry in events.issues
                    if entry.emission_id == record.emission_id
                ),
                record.emission_id,
            )
            when = f"{record.moment:%d.%m.%Y}" if record.moment else "дата не названа"
            add(f"- {issue}: {record.kind.lower()} {when}, не исполнено")
        if events.ratings_known:
            live = events.live
            add(
                f"\nДействующих кредитных рейтингов {len(live)}"
                + (
                    ": " + ", ".join(
                        f"{entry.agency} {entry.point}"
                        + (f" ({entry.outlook})" if entry.outlook else "")
                        for entry in live
                    )
                    if live
                    else " — ни одного"
                )
                + "."
            )
            left, when = events.left_unrated()
            if left:
                add(
                    "Рейтинги отозваны всеми агентствами"
                    + (f" {when:%d.%m.%Y}" if when else "")
                    + ": внешнего мнения о качестве эмитента нет."
                )

    add("\n## Комплекты отчётности\n")
    sets = fetch_all(_SETS, {"inn": item.inn}, conn=conn)
    if not sets:
        add("комплектов нет: отчётность до нас не дошла.\n")
    else:
        add("| Стандарт | Год | Откуда | Вид | Состояние | Раскрыта |")
        add("|---|---|---|---|---|---|")
        for row in sets:
            add(
                f"| {row['standard']} | {row['report_year']} | {row['source']} "
                f"| {row['reporting_kind'] or '—'} | {row['status']} "
                f"| {row['disclosed'] or 'дата не сообщена — срок закона'} |"
            )

    add("\n## Чего мы не знаем\n")
    for line in routing.limitations:
        add(f"- {' '.join(line.split())}")
    return "\n".join(said) + "\n"


def main() -> int:
    """Собирает карточки названных эмитентов."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    wanted = [item for item in sys.argv[1:] if item.isdigit()]
    if not wanted:
        print("назовите ИНН: карточка собирается по эмитенту, а не по всем сразу")
        return 1
    routing = load_routing()
    OUT.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        rows, _ = routing_rows(conn, date.today())
        found = {row.inn: row for row in rows}
        for inn in wanted:
            item = found.get(inn)
            if item is None:
                print(f"{inn}: в списке нет — карточку собирать не из чего")
                continue
            text = card(item, routing, conn)
            # **Единица проверяется и здесь.** Вопрос у всех выходов один —
            # не напечатана ли единица чужого комплекта, — и второй экземпляр
            # проверки разошёлся бы с первым.
            wrong = [
                name
                for unit, said in item.verdict.by_unit(item.unit)
                for name in foreign_units(said, unit)
            ]
            if wrong:
                raise ValueError(
                    f"{item.name} ({inn}): напечатана единица "
                    f"«{', '.join(wrong)}», а комплект составлен в «{item.unit}»"
                )
            path = OUT / f"{inn}.md"
            path.write_text(text, encoding="utf-8")
            print(f"{path}: {item.name}, {routing.basket(item.verdict.basket).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
