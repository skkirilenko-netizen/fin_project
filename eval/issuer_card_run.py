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
Он слой проверки: корзину не называет, а в основание отдаёт одно — дату
перехода в нынешнюю категорию. Смешать его со снимком нельзя, потому что
читатель иначе решит, что по нему принимается решение.

**Чего мы не знаем — два разных перечня, и путать их нельзя.** Пробел
у этого эмитента исправляется доставкой: график не дошёл, денежные средства
не раскрыты, рейтингов нет. Граница метода не исправляется ничем — она
свойство самого маршрута и стоит у каждой карточки одинаково. Слитые
в один список, они читаются как один род: читатель либо пойдёт добирать
неисправимое, либо сочтёт исправимое свойством метода.

    uv run python eval/issuer_card_run.py 9703024202 4004021785 7726588547
    uv run python eval/issuer_card_run.py --all
"""

import logging
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.metrics.display import digits, foreign_units, money  # noqa: E402
from finlib.report.market_chart import as_image, charts  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import SOURCE_NAMES, cards, routing_rows  # noqa: E402
from finlib.sources.market import load_market as _market_rules  # noqa: E402
from finlib.sources.market import series as _market_series  # noqa: E402
from finlib.sources.ratings_calendar import (  # noqa: E402
    NOT_CREDIT,
    bound,
    prepared,
    read_actions,
    transitions,
)
from finlib.utils import marked_by  # noqa: E402

logger = logging.getLogger(__name__)

OUT = Path("data/output/cards")
# Полный текст границ метода: он одинаков у всех карточек, и место у него одно.
LIMITS = OUT / "_границы_метода.md"

# История наблюдения и история пересчёта не сравниваются: первая говорит,
# что мы видели, вторая — что было бы видно, если бы мы смотрели. В таблице
# показывается пересчёт, наблюдение называется числом рядом.
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


def _moves(rows: list[dict]) -> list[tuple[dict, dict | None]]:
    """Точки, в которых изменилась корзина **или перечень оснований**.

    **Смена корзины — не единственное изменение, о котором стоит знать.**
    У эмитента, весь год простоявшего во «Внимании», основание менялось:
    было «нет графика платежей», стало «платежи выше денежных средств» —
    корзина та же, обстоятельство другое. Прежде карточка печатала одну
    строку на год и выглядела так, будто ничего не происходило.
    """
    found: list[tuple[dict, dict | None]] = []
    before: dict | None = None
    for row in rows:
        same = (
            before is not None
            and row["basket"] == before["basket"]
            and set(row["grounds"] or ()) == set(before["grounds"] or ())
        )
        if not same:
            found.append((row, before))
        before = row
    return found


def _changed(row: dict, before: dict | None, names: dict[str, str]) -> str:
    """Чем точка отличается от предыдущей: что появилось и что исчезло."""
    if before is None:
        return "начало наблюдения"
    now = set(row["grounds"] or ())
    was = set(before["grounds"] or ())
    said: list[str] = []
    if row["basket"] != before["basket"]:
        said.append("корзина сменилась")
    if now - was:
        said.append("появилось: " + ", ".join(names.get(x, x) for x in sorted(now - was)))
    if was - now:
        said.append("исчезло: " + ", ".join(names.get(x, x) for x in sorted(was - now)))
    return "; ".join(said) or "без изменений"


def _newest_list() -> Path | None:
    """Свежайший собранный список наблюдения; None — списка на диске нет.

    Карточки лежат в подкаталоге списка, поэтому ссылка ведёт на уровень
    выше — `../watchlist_<дата>.html`.
    """
    found = sorted(OUT.parent.glob("watchlist_*.html"))
    if not found:
        return None
    return Path("..") / found[-1].name


def _cell(text: object) -> str:
    """Ячейка таблицы: вертикальная черта в величине ломает строку.

    Уровни национальных шкал печатаются с ней — «BBB-|ru|», — и без защиты
    одна строка таблицы разъезжается на шесть столбцов.
    """
    return str(text).replace("|", r"\|")


def _title(inn: str, item, card: dict) -> tuple[str, str]:  # noqa: ANN001
    """Краткое наименование для заголовка и фирменное — строкой под ним.

    **В заголовке стоит то, чем эмитента зовут.** «ПУБЛИЧНОЕ АКЦИОНЕРНОЕ
    ОБЩЕСТВО ГРУППА КОМПАНИЙ "СЕГЕЖА"» — запись реестра, и читать по ней
    список карточек нельзя. Фирменное при этом не выбрасывается: им эмитент
    опознаётся в документах, и оно стоит следующей строкой.
    """
    short = str(card.get("name_rus") or "").strip()
    full = str(card.get("full_name_rus") or "").strip() or item.name
    said = full if full != (short or item.name) else ""
    # **Фирменное наименование — реквизит, и искажённым оно не печатается
    # молча.** У «Простой еды» (1215229593) источник отдаёт «Общество
    # с ограниченной откровенностью», у «СибСульфура» (2466127447) —
    # «Ответсвенностью»: это ошибка агрегатора, а не наша и не эмитента,
    # и проверяется она строением — развёрнутая в наименовании
    # организационная форма обязана совпасть с объявленной у той же карточки.
    if said and _form_broken(said):
        said += (
            " — **организационная форма в наименовании написана неверно "
            "у источника; приведено как есть, не исправлено**"
        )
    return (short or item.name), said


# **Сверяется одно устойчивое сочетание, а не форма целиком.** Полное
# наименование и организационная форма у карточки расходятся законно:
# «Акционерное общество "Волга"» при форме «Непубличное акционерное
# общество» — это запись реестра против классификации, и таких 66 из 977.
# Слова «с ограниченной ответственностью» предписаны ФЗ № 14-ФЗ, статья 4,
# и разойтись не могут: их расхождение — опечатка источника.
_LIMITED = "с ограниченной"
_LIABILITY = "с ограниченной ответственностью"


def _form_broken(name: str) -> bool:
    """Искажена ли организационная форма в фирменном наименовании.

    Найдено на двух карточках из 977: «Общество с ограниченной
    **откровенностью** "Простая еда"» (1215229593) и «с Ограниченной
    **Ответсвенностью** "СибСульфур"» (2466127447). Реквизит организации
    мы не исправляем — ошибка не наша и не эмитента, — но и печатать её
    молча нельзя.
    """
    return marked_by(name, (_LIMITED,), str.lower) and not marked_by(
        name, (_LIABILITY,), str.lower
    )


def _ratings(item, routing, said: list) -> None:  # noqa: ANN001
    """Рейтинги снимка: кредитные порознь от некредитных, объект назван."""
    events = item.events
    add = said.append
    if events is None or not events.ratings_known:
        add("\nСнимка рейтингов нет — это не «рейтингов нет».")
        return
    live = events.live
    moves = {
        (agency, scale, category): moved
        for (holder, agency, scale, category), moved in transitions().items()
        if holder == item.inn
    }
    add("\n### Рейтинги эмитента (снимок)\n")
    if not live:
        add("Действующих кредитных рейтингов нет ни одного.")
    else:
        add("| Агентство | Шкала | Рейтинг | Категория | Прогноз | Обновлён |")
        add("|---|---|---|---|---|---|")
        for entry in live:
            when = f"{entry.assigned:%d.%m.%Y}" if entry.assigned else "дата не указана"
            add(
                f"| {entry.agency} | {entry.scale} | {_cell(entry.point)} "
                f"| {entry.category} | {entry.outlook or '—'} | {when} |"
            )
        for entry in live:
            moved = moves.get((entry.agency, entry.scale, entry.category))
            if moved is not None:
                add(
                    f"\nВ категории {entry.category} ({entry.agency}) "
                    f"с {moved.since:%d.%m.%Y}"
                    + (
                        f", прежний уровень {moved.was_level}"
                        if moved.was_level
                        else ""
                    )
                    + " — по календарю рейтинговых действий."
                )
    left, when = events.left_unrated()
    if left:
        add(
            "\nРейтинги отозваны всеми агентствами"
            + (f" {when:%d.%m.%Y}" if when else "")
            + ": внешнего мнения о качестве эмитента нет, причина отзыва "
            "источником не раскрывается."
        )
    # **Объект рейтинга различается, и молчать о некредитных нельзя.**
    # «Действующих кредитных рейтингов 0» у эмитента с ESG-рейтингом читается
    # как «его никто не оценивает», а оценивают — только не кредитоспособность.
    other = [item for item in events.ratings if not item.credit]
    if other:
        add(
            "\nНекредитных рейтингов "
            + f"{len(other)}: "
            + ", ".join(sorted({f"{x.agency} {x.point} ({x.scale})" for x in other}))
            + " — в градацию кредитного риска они не идут."
        )
    # **Объект рейтинга назван, потому что снимок и календарь говорят
    # о разном.** В снимке лежат рейтинги эмитента: отбор по эмитенту метод
    # рейтингов выпуска пропускает молча, и перечня по нему у нас нет вовсе.
    # Действия по выпускам видны только в календаре, и у структурного
    # эмитента, где оценка качества и есть рейтинг транша, это пробел.
    add(
        "\nВ снимке — рейтинги **эмитента**. Рейтингов **выпусков** (траншей) "
        "у нас нет: отбор по эмитенту источник в них пропускает молча. "
        "Действия по выпускам видны ниже, в календаре, — но это история, "
        "а не действующее значение."
    )
    # **«Рейтинга не было вовсе» при рейтингованных выпусках — неверно.**
    # У Новоленской ТЭС (1400018759) снимок рейтингов эмитента пуст,
    # а по выпуску 001Р-01 стоит ruAAA от 14.07.2026. Сказать «не оценивают»
    # значило бы сказать о предмете, о котором речь не шла: оценивают выпуск.
    # Значение берётся из календаря и помечено им: это последнее известное
    # действие, а не действующее значение снимка.
    for line in _issue_ratings(item):
        add(f"\n{line}")


def _issue_ratings(item) -> list[str]:  # noqa: ANN001
    """Действующие рейтинги выпусков по календарю; пусто — их нет.

    **Берётся последнее действие по каждому выпуску**, и отозванный рейтинг
    действующим не считается: отзыв — значение шкалы, а не признак. Источник
    назван прямо — календарь, слой проверки, а не снимок.
    """
    actions, names = _actions(), _bound()
    if not actions:
        return []
    mine = [
        entry
        for entry in actions
        if entry.about == "emission"
        and entry.scale not in NOT_CREDIT
        and names.get(prepared(entry.name.split(",")[0])) == item.inn
    ]
    latest: dict[str, object] = {}
    for entry in sorted(mine, key=lambda x: x.when):
        latest[entry.name] = entry
    live = [
        entry for entry in latest.values() if not entry.withdrawn  # type: ignore[attr-defined]
    ]
    if not live:
        return []
    said = [
        "Рейтинги **выпусков** по календарю: "
        + "; ".join(
            f"{entry.name} — {_cell(entry.level)} ({entry.agency}, "  # type: ignore[attr-defined]
            f"{entry.when:%d.%m.%Y})"  # type: ignore[attr-defined]
            for entry in sorted(live, key=lambda x: x.name)[:6]  # type: ignore[attr-defined]
        )
        + (f" и ещё {len(live) - 6}" if len(live) > 6 else "")
        + ". Это последнее известное действие, а не действующее значение "
        "снимка: снимок рейтингов выпусков источник не отдаёт."
    ]
    return said


@lru_cache(maxsize=1)
def _actions() -> tuple:
    """Календарь рейтинговых действий: читается один раз на прогон."""
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


def _calendar(item, said: list, actions, names: dict[str, str]) -> None:  # noqa: ANN001
    """Календарь рейтинговых действий: слой проверки, помеченный как слой."""
    add = said.append
    add("\n## Календарь рейтинговых действий\n")
    if not actions:
        add("выгрузки календаря на диске нет — это не «действий не было».\n")
        return
    mine = [
        entry
        for entry in actions
        if names.get(prepared(entry.name.split(",")[0])) == item.inn
    ]
    add(
        "**Слой проверки: корзину он не называет.** Из него берётся одно — "
        "с какого дня эмитент в нынешней категории; решение принимается "
        "по ежедневному снимку.\n"
    )
    if not mine:
        add("\nДействий этого эмитента в выгрузке нет.")
        return
    add(f"\nДействий {len(mine)}, показаны последние десять.\n")
    add("| Дата | Агентство | Объект | Шкала | Было → стало | Что это |")
    add("|---|---|---|---|---|---|")
    for entry in sorted(mine, key=lambda x: x.when, reverse=True)[:10]:
        what = (
            "отзыв"
            if entry.withdrawn
            else "подтверждение"
            if entry.affirmed
            else "изменение уровня"
            if entry.level_changed
            else "изменение прогноза"
            if entry.forecast_only
            else "первая запись"
        )
        about = "эмитент" if entry.about == "issuer" else entry.name
        mark = " *(некредитная шкала)*" if entry.scale in NOT_CREDIT else ""
        add(
            f"| {entry.when:%d.%m.%Y} | {entry.agency} | {_cell(about)} "
            f"| {entry.scale}{mark} "
            f"| {_cell(entry.was_level or '—')} → {_cell(entry.level)} "
            f"| {what} |"
        )


def _gaps(item, routing, sets: list[dict], said: list) -> None:  # noqa: ANN001
    """Чего мы не знаем **об этом эмитенте**: пробел исправляется доставкой."""
    add = said.append
    add("\n## Чего мы не знаем об этом эмитенте\n")
    holes: list[str] = []
    events = item.events
    if events is None or not events.issues_known:
        holes.append("перечня выпусков на диске нет — это не «выпусков нет»")
    if events is not None and not events.records_known:
        holes.append("перечня событий дефолта на диске нет")
    if events is not None and not events.ratings_known:
        holes.append("снимка рейтингов нет")
    elif events is not None and events.never_rated:
        holes.append(
            "кредитного рейтинга не было вовсе — насколько видно источнику: "
            "снятого до начала его наблюдений в снимке нет"
        )
    if item.refinance is None or item.refinance.due is None:
        holes.append(
            "графиков платежей на диске нет — «к погашению ноль» сказать нечем"
        )
    else:
        gap = item.refinance
        if gap.without_schedule:
            holes.append(
                f"графика платежей нет у {gap.without_schedule} выпусков "
                f"из {gap.issues}"
            )
        if gap.without_offers:
            holes.append(
                f"ответа об офертах нет по {gap.without_offers} выпускам "
                f"из {gap.issues}"
            )
        if gap.cash is None:
            holes.append(
                "денежные средства не раскрыты — знаменателя рефинансирования нет"
            )
    if item.report_date is None:
        holes.append("отчётности нет вовсе: маршрут построен по событиям и рейтингам")
    if not any(row["disclosed"] for row in sets):
        holes.append(
            "дата раскрытия отчётности источником не сообщена: известность "
            "в пересчёте моделируется сроком закона"
        )
    if not item.branch:
        holes.append(
            "отрасли источник не называет: отраслевой гаситель стоп-фактора "
            "к нему не применим"
        )
    if not item.has_bonds:
        holes.append(
            "выпусков в обращении нет: в сводные доли списка эмитент не идёт"
        )
    # **У структурного эмитента пробел рейтинга транша — не мелочь.** Риск
    # такого эмитента лежит в пуле активов и структуре траншей, и оценка
    # качества у него — именно рейтинг транша, которого источник по эмитенту
    # не отдаёт.
    structural = next(
        (kind.name for kind in routing.issuer_types if kind.code == "structural"), ""
    )
    if item.issuer_type and item.issuer_type == structural:
        holes.append(
            "рейтингов траншей у нас нет вовсе, а у структурного эмитента "
            "оценка качества — именно они: балансовые показатели сделку "
            "не описывают"
        )
    if not holes:
        add("пробелов доставки нет: всё, что маршрут спрашивает, у нас есть.\n")
    for line in holes:
        add(f"- {line}")


def card(item, routing, conn, actions, bound_names) -> str:  # noqa: ANN001
    """Собирает карточку одного эмитента."""
    verdict = item.verdict
    basket = routing.basket(verdict.basket)
    names = {
        ground.code: ground.name
        for entry in routing.baskets
        for ground in entry.grounds
    } | {ground.code: ground.name for ground in routing.reference}
    said: list[str] = []
    add = said.append

    short, full = _title(item.inn, item, cards().get(item.inn, {}))
    add(f"# {short} ({item.inn})\n")
    if full:
        add(f"{full}\n")
    add(
        f"**{basket.name}**"
        + (f" · {verdict.subgroup_names[0]}" if verdict.subgroup_names else "")
        + f" · {item.basis or 'отчётности нет'}"
        + (f" · {item.issuer_type}" if item.issuer_type else "")
        + "\n"
    )
    # **Дата оценки стоит в карточке.** Файл открывают через неделю после
    # сборки, и корзина в нём — ответ того дня, а не сегодняшнего.
    add(
        f"Оценка на {date.today():%d.%m.%Y}"
        + (
            f" по комплекту за {item.report_date:%d.%m.%Y}"
            if item.report_date is not None
            else " без отчётности"
        )
        + (f", источник {', '.join(item.sources)}" if item.sources else "")
        + ".\n"
    )
    if verdict.actions:
        # **Действие знает то, что знает карточка.** «Проверить, есть ли
        # выпуски в обращении» рядом с «Выпусков всего 0» отправляет человека
        # выяснять то, что страница уже говорит. Сам текст действия —
        # методика и не правится: к нему добавляется наш ответ на ту часть
        # вопроса, на которую ответ у нас есть.
        add(f"Действие: {verdict.actions[0]}{_already_known(item)}\n")
    # **Ссылка на список ставится только на собранный.** Карточку открывают
    # из списка и возвращаются в него; обещать страницу, которой на диске нет,
    # хуже, чем не обещать ничего.
    if (found := _newest_list()) is not None:
        add(f"[← Список наблюдения]({found.as_posix()})\n")

    add("\n## Основания корзины\n")
    if not verdict.findings:
        add("оснований нет — это и означает «человек не нужен».\n")
    # **Основания корзины и обстоятельства эмитента — разные перечни.**
    # Корзину называют только свои основания, а сработало обычно больше:
    # они остаются в карточке, потому что человеку нужны все, — но помечены,
    # иначе читатель решит, что корзину назвало каждое.
    own = set(verdict.grounds)
    for entry in verdict.findings:
        mark = "" if entry.ground in own else " *(сведение)*"
        add(f"- **{names.get(entry.ground, entry.ground)}**{mark}: {entry.text}")
    if verdict.notes:
        add("\n**Справочно** — корзину не называет, но и не исчезает:\n")
        for entry in verdict.notes:
            add(f"- {entry.text}")

    add("\n## Величины маршрута\n")
    if not item.shown_values:
        add("не рассчитано ни одной: отчётности нет либо она в карантине.\n")
    else:
        # **Величина названа вместе с комплектом, из которого взята.**
        # «Автономия 0,75» без отчётной даты читается как сегодняшнее
        # положение, а описывает она конец прошлого года.
        add(
            f"Комплект: {item.basis or 'не назван'}"
            + (
                f", отчётная дата {item.report_date:%d.%m.%Y}"
                if item.report_date is not None
                else ""
            )
            + f", единица — {item.unit or 'не названа'}\n"
        )
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
    _stale(item, said)
    _market(item, said)

    add("\n## История корзин\n")
    rows = fetch_all(_HISTORY, {"inn": item.inn}, conn=conn)
    recount = [row for row in rows if row["kind"] == "backfill"]
    watched = [row for row in rows if row["kind"] != "backfill"]
    moves = _moves(recount)
    if not recount:
        add("пересчёта назад по этому эмитенту нет.\n")
    else:
        add(
            f"Пересчёт назад: точек {len(recount)} за "
            f"{recount[0]['as_of']:%d.%m.%Y} — {recount[-1]['as_of']:%d.%m.%Y}, "
            f"изменений {len(moves) - 1}. Наблюдений прогонами {len(watched)} — "
            "с пересчётом они не сравниваются: первое говорит, что мы видели, "
            "второе — что было бы видно.\n"
        )
        add("\n| Дата | Корзина | Основания | Что изменилось |")
        add("|---|---|---|---|")
        for row, before in moves:
            listed = ", ".join(names.get(code, code) for code in row["grounds"])
            add(
                f"| {row['as_of']:%d.%m.%Y} | "
                f"{routing.basket(row['basket']).name} | {listed or '—'} "
                f"| {_changed(row, before, names)} |"
            )

    add("\n## Выпуски и события\n")
    # Счётчики и перечень — о том, что **наступило**: событие позже дня сбора
    # маршрут называет справочно, и складывать его с прочими нельзя.
    events = _known(item)
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
    # **Рейтинги печатаются независимо от выпусков.** Прежде раздел стоял
    # внутри ветки «перечень выпусков есть», и у эмитента, чьих выпусков нет
    # на диске, рейтинги исчезали вместе с ними — хотя снимок рейтингов
    # приходит другим методом и о выпусках ничего не знает.
    _ratings(item, routing, said)

    _calendar(item, said, actions, bound_names)

    add("\n## Комплекты отчётности\n")
    sets = fetch_all(_SETS, {"inn": item.inn}, conn=conn)
    if not sets:
        add("комплектов нет: отчётность до нас не дошла.\n")
    else:
        add("| Стандарт | Год | Откуда | Вид | Состояние | Раскрыта | В величинах |")
        add("|---|---|---|---|---|---|---|")
        for row in sets:
            add(
                f"| {row['standard']} | {row['report_year']} | {row['source']} "
                f"| {row['reporting_kind'] or '—'} | {row['status']} "
                f"| {row['disclosed'] or 'дата не сообщена — срок закона'} "
                f"| {_in_values(item, row)} |"
            )
        # **За год у эмитента два актуальных комплекта, и оба намеренно.**
        # Величины собираются по фактам обоих с приоритетом первоисточника:
        # у ГИР БО и документа он старше агрегатора, а агрегатор заполняет
        # то, чего в них нет. Поэтому графа отвечает «идёт ли комплект
        # в величины», а не «какой один из них выбран», — выбранного одного
        # не существует.
        add(
            "\nВеличины собираются из фактов **всех** комплектов строки "
            "с приоритетом первоисточника: ГИР БО и документ эмитента старше "
            "агрегатора, агрегатор заполняет то, чего в них нет."
        )

    _gaps(item, routing, list(sets), said)

    # **Границы метода у всех карточек одни, и повторять их целиком незачем.**
    # Две с половиной тысячи знаков на каждой из 900 страниц — это не сведение,
    # а стена, которую перестают читать вместе с тем, что стоит выше неё.
    # Печатается первая фраза каждой границы, полный текст лежит рядом
    # и собирается один раз.
    add("\n## Границы метода\n")
    add(
        "Они у каждой карточки одни и доставкой не закрываются; полностью — "
        f"в [{LIMITS.name}]({LIMITS.name}).\n"
    )
    for line in routing.limitations:
        add(f"- {_first_sentence(' '.join(line.split()))}")
    return "\n".join(said) + "\n"


def _first_sentence(text: str) -> str:
    """Первая фраза ограничения; длинная обрывается на границе предложения."""
    head = text.split(". ")[0].rstrip(".")
    return f"{head}." if len(head) + 2 >= len(text) else f"{head}. […]"


def write_limits(routing) -> None:  # noqa: ANN001
    """Кладёт полный текст границ метода рядом с карточками, один раз."""
    OUT.mkdir(parents=True, exist_ok=True)
    said = ["# Границы метода\n", "Они одни у всех карточек списка.\n"]
    said += [f"- {' '.join(line.split())}\n" for line in routing.limitations]
    LIMITS.write_text("\n".join(said), encoding="utf-8")


def _market(item, said: list) -> None:  # noqa: ANN001
    """Рыночный ряд эмитента: спред против ориентира и цена против границы.

    **Величина рынка меняется ежедневно, и график отвечает на то, чего число
    не говорит**: стоит ли бумага дёшево давно или подешевела на прошлой
    неделе. Основание маршрута называет день, с которого признак держится,
    а ряд показывает, как он туда пришёл.

    Молчание здесь объявляется причиной, а не пустотой: ряда не бывает у того,
    чьи бумаги не торговались, и это не то же, что «рынок ничего не говорит».
    """
    add = said.append
    add("\n## Рынок\n")
    market, policy = _market_series(), _market_rules()
    points = market.ordered(item.inn)
    if not points:
        add(market.silence(item.inn) + ".\n")
        return
    last = points[-1]
    level = market.benchmark.get(last.day)
    add(
        f"Наблюдений {len(points)} за {points[0].day:%d.%m.%Y} — "
        f"{last.day:%d.%m.%Y}. На последний день: "
        + (
            f"спред {digits(last.spread, 0)} б. п."
            if last.spread is not None
            else "спреда нет (доходность к сроку не определена)"
        )
        + (
            f" при ориентире рынка {digits(level, 0)} б. п. "
            f"(кратность {digits(last.spread / level, 2)}x)"
            if level and last.spread is not None
            else ""
        )
        + (
            f", цена {digits(last.price, 1)} % номинала"
            if last.price is not None
            else ", цены нет: в этот день не торговались"
        )
        + ".\n"
    )
    for svg, alt in zip(
        charts(points, market, policy),
        ("Спред к кривой ОФЗ", "Цена в процентах номинала"),
        strict=False,
    ):
        if svg:
            add("\n" + as_image(svg, alt) + "\n")


def _stale(item, said: list) -> None:  # noqa: ANN001
    """Событие позже отчётной даты: величины описывают положение до него.

    **У Кириллицы величины отчётности здоровые — автономия 0,75, долговая
    нагрузка 1,75, — а по выпуску дефолт.** Противоречия здесь нет: отчётность
    описывает конец прошлого года, дефолт случился в сентябре нынешнего.
    Молчание об этом оставляет читателя с двумя несовместимыми утверждениями
    на одной странице.
    """
    events, moment = _known(item), item.report_date
    if events is None or moment is None:
        return
    # **Считается от ближайшего наступившего события, а не от самого позднего.**
    # У «Группы Продовольствие» самым поздним стоял купон 28.09.2026 — он ещё
    # не наступил, — и разрыв выходил 271 день вместо 239 по событию 27.08.2026.
    later = [
        record.moment
        for record in events.open_records
        if record.moment is not None and record.moment > moment
    ]
    if not later:
        return
    said.append(
        f"\n**Величины описывают положение на {moment:%d.%m.%Y}.** Неисполненное "
        f"обязательство наступило {min(later):%d.%m.%Y}, то есть "
        f"{_days((min(later) - moment).days)} спустя: здоровые величины "
        "отчётности ему не противоречат — они о другом дне.\n"
    )


def _in_values(item, row: dict) -> str:  # noqa: ANN001
    """Идёт ли комплект в величины маршрута и почему нет.

    Комплект другого периода либо стандарта — не пробел и не дефект:
    маршрут строится по одному периоду, и сказать об этом надо словами,
    иначе пустая графа читается как «отбракован».
    """
    if row["status"] == "quarantine":
        return "нет: карантин"
    if not row["is_actual"]:
        return "нет: не актуален"
    if item.standard is None or row["standard"] != item.standard.value:
        return "нет: другой стандарт"
    if item.report_date is None or row["report_year"] != item.report_date.year:
        return "нет: другой период"
    source = SOURCE_NAMES.get(row["source"], row["source"])
    return "да, первоисточник" if row["source"] != "cbonds" else f"да, {source}"


def _already_known(item) -> str:  # noqa: ANN001
    """Чем карточка сама отвечает на действие; пусто — отвечать нечем.

    Перечень выпусков лежит у нас, и «есть ли выпуски в обращении» он
    закрывает наполовину: сколько их по нашим данным. Вторая половина
    вопроса — обязан ли эмитент сдавать отчётность — остаётся человеку.
    """
    events = _known(item)
    if events is None or not getattr(events, "issues_known", False):
        return ""
    alive = sum(1 for issue in events.issues if issue.status == "в обращении")
    return (
        f" — по нашему перечню выпусков в обращении {alive} "
        f"из {len(events.issues)}"
    )


def _days(count: int) -> str:
    """Число дней вместе с согласованным словом: «271 день», не «271 дней».

    Правило закрытое и грамматическое, а не методическое: одиннадцать —
    двадцать — всегда «дней», дальше решает последняя цифра.
    """
    tail, last = count % 100, count % 10
    if 11 <= tail <= 14 or last == 0 or last >= 5:
        return f"{count} дней"
    return f"{count} день" if last == 1 else f"{count} дня"


def _known(item) -> object:  # noqa: ANN001
    """События эмитента так, как они известны сегодня.

    **Событие, которое не наступило, в счётчик не идёт.** У «Группы
    Продовольствие» стояло «событий дефолта 4, из них неисполненных 4»,
    и четвёртым был купон 28.09.2026 — позже дня сбора. В основаниях маршрут
    относит его в справочные верно, а счётчик карточки считал его наравне
    с прочими: правило одно (`IssuerEvents.as_of`), и звать его обязаны оба.
    """
    events = item.events
    if events is None or not hasattr(events, "as_of"):
        return events
    return events.as_of(date.today())


def main() -> int:
    """Собирает карточки названных эмитентов либо всех сразу."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    wanted = [item for item in sys.argv[1:] if item.isdigit()]
    everyone = "--all" in sys.argv[1:]
    if not wanted and not everyone:
        print("назовите ИНН либо --all: карточка собирается по эмитенту")
        return 1
    routing = load_routing()
    OUT.mkdir(parents=True, exist_ok=True)
    write_limits(routing)
    # Календарь читается один раз на прогон: выгрузка ручная, и по карточке
    # её перечитывать незачем.
    try:
        actions = read_actions()
        bound_names, _, _ = bound(actions)
    except FileNotFoundError:
        actions, bound_names = (), {}
    with connection() as conn:
        rows, _ = routing_rows(conn, date.today())
        found = {row.inn: row for row in rows}
        for inn in sorted(found) if everyone else wanted:
            item = found.get(inn)
            if item is None:
                print(f"{inn}: в списке нет — карточку собирать не из чего")
                continue
            text = card(item, routing, conn, actions, bound_names)
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
            if not everyone:
                print(
                    f"{path}: {item.name}, "
                    f"{routing.basket(item.verdict.basket).name}"
                )
        if everyone:
            print(f"{OUT}: карточек {len(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
