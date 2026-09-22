"""Шесть случаев экспертной проверки: корзина, основание и его источник.

    uv run python eval/watchlist_cases_run.py > data/output/watchlist_cases.md

**Это критерий событийного слоя, а не иллюстрация.** Шесть эмитентов записки —
ЕвроТранс, Кириллица, Антерра, ЖКХ РС(Я), Уральская кузница, Русагро — были
найдены человеком там, где слой отчётности молчал. По ним и проверяется, видит
ли их теперь проект, и **чем именно**: выпуском, рейтингом, отчётностью или
группой. Корзина без источника основания ничего не доказывает: совпасть она
может и по другой причине.

**Источник основания выводится из самого основания**, а не из прозы: у каждого
`Finding` объявлен код, и код говорит, откуда пришло обстоятельство.

**Замер не считает сам**: корзины берёт боевая маршрутизация через
`scoring.routing_store.routing_rows`.
"""

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402

logger = logging.getLogger(__name__)

# Шесть случаев записки и то, чего от них ждал эксперт.
CASES: dict[str, tuple[str, str]] = {
    "5029169023": ("ЕвроТранс", "не ниже разбора: дефолт между отчётными датами"),
    "4004021785": ("Кириллица", "не ниже разбора: дефолт по погашению БО-03"),
    "7730176955": ("Антерра", "не ниже разбора: дефолт и рейтинг D"),
    "1435133520": ("ЖКХ РС(Я)", "не ниже разбора: рейтинг ruC"),
    "7420000133": ("Уральская кузница", "не ниже внимания: группа «Мечел» в разборе"),
    "5003077160": ("Русагро", "не ниже внимания: контурное ожидание"),
}

# Откуда приходит основание. Перечень не «для красоты»: без него корзина
# совпадает с ожиданием и по чужой причине, а это не то же самое.
SOURCE_OF: dict[str, str] = {
    "emission_default": "выпуск",
    "rating_default": "рейтинг",
    "rating_watch": "рейтинг",
    "default_unsettled_stale": "выпуск",
    "default_settled_recent": "выпуск",
    "default_settled_stale": "выпуск (справочно)",
    "group_under_review": "группа",
    "financing_structure": "группа (SPV)",
    "assessed_class_low": "отчётность (наша оценка)",
    "stop_factor_severe": "отчётность",
    "stop_factor_capped": "отчётность",
    "level_off_scale": "отчётность",
    "metric_in_lower_band": "отчётность",
    "bound_above_threshold": "отчётность",
    "negative_ebitda": "отчётность",
    "operating_loss": "отчётность",
    "data_insufficient": "отчётность (пробел)",
    "disclosure_overdue": "отчётность (срок)",
    "reporting_two_cycles_old": "отчётность (срок)",
    "zero_check_failed": "отчётность (карантин)",
}


def cell(text: str) -> str:
    """Текст в графе таблицы: черта экранируется.

    Написание точки шкалы бывает с чертой — «D|ru|», — и графа таблицы
    разъезжается ровно на том рейтинге, ради которого её и читают.
    """
    return text.replace("|", "\\|")


def main() -> int:
    """Печатает таблицу шести случаев; 1 — если ни одного нет в списке."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    routing = load_routing()
    with connection() as conn:
        rows, counts = routing_rows(conn, date.today())
    by_inn = {item.inn: item for item in rows}

    print("# Шесть случаев экспертной проверки: корзина и источник основания\n")
    print(
        f"Список — {counts['эмитентов']} эмитентов. Источник основания выводится "
        "из кода самого основания: корзина без него могла бы совпасть "
        "с ожиданием и по другой причине.\n"
    )
    print(
        "| Эмитент | ИНН | Корзина | Подгруппа | Главное основание | Откуда "
        "| Прочие основания (источник) | Ожидание эксперта |"
    )
    print("|---|---|---|---|---|---|---|---|")
    found = 0
    for inn, (label, expected) in CASES.items():
        row = by_inn.get(inn)
        if row is None:
            print(
                f"| {label} | {inn} | **в списке нет** | — | — | — | — "
                f"| {expected} |"
            )
            continue
        found += 1
        verdict = row.verdict
        basket = routing.basket(verdict.basket)
        declared = [item.code for item in basket.grounds]
        main = next(
            (item for item in verdict.findings if item.ground in declared), None
        )
        # **Однородные основания сворачиваются в одно с числом.** У ЕвроТранса
        # дефолт стоит по двенадцати выпускам, и двенадцать одинаковых по роду
        # формулировок в строке — не сведение, а её нечитаемость: род называется
        # один раз, число рядом.
        others: list[str] = []
        for ground in dict.fromkeys(
            item.ground for item in verdict.findings if item is not main
        ):
            same = [
                item
                for item in verdict.findings
                if item.ground == ground and item is not main
            ]
            where = SOURCE_OF.get(ground, ground)
            tail = f" и ещё {len(same) - 1}" if len(same) > 1 else ""
            others.append(f"{cell(same[0].text)}{tail} ({where})")
        print(
            f"| {label} | {inn} | **{verdict.basket_name}** "
            f"| {verdict.subgroup_names[0] if verdict.subgroup_names else '—'} "
            f"| {cell(main.text) if main is not None else '—'} "
            f"| {SOURCE_OF.get(main.ground, '—') if main is not None else '—'} "
            f"| {'; '.join(others) or '—'} | {expected} |"
        )
    print(
        f"\nИз шести случаев в списке **{found}**. Эмитент, которого в списке "
        "нет, ожиданию не противоречит и его не подтверждает: у него нет "
        "комплекта вне карантина, и маршрут о нём не высказывается вовсе.\n"
    )

    # Что видит слой событий по каждому из шести — порознь от корзины:
    # совпадение корзины и наличие данных — разные утверждения.
    print("## Что событийный слой знает о каждом\n")
    print(
        "| Эмитент | Выпусков | С дефолтом сейчас | Дефолт в прошлом "
        "| Дата события | Кредитных рейтингов | Худшая точка | Отозваны |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for inn, (label, _) in CASES.items():
        row = by_inn.get(inn)
        events = row.events if row is not None else None
        if events is None:
            print(f"| {label} | данных нет | — | — | — | — | — | — |")
            continue
        live = events.live
        # **Худшая точка сравнивается местом в шкале, а не написанием.** «AA»
        # длиннее «C» и любым сравнением строк выходит «больше»: графа считала
        # бы не то, как называется.
        worst = events.worst
        event = events.event()
        when = (
            f"{event.when:%d.%m.%Y} ({event.origin})"
            if event.known
            else ("не определена" if events.unsettled_default else "—")
        )
        print(
            f"| {label} | {len(events.issues) if events.issues_known else 'данных нет'} "
            f"| {len(events.defaulted)} | {len(events.settled)} | {when} "
            f"| {len(live)} "
            f"| {cell(f'{worst.point} ({worst.category})') if worst is not None else '—'} "
            f"| {'да' if events.ratings and not live else 'нет'} |"
        )
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
