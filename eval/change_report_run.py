"""Отчёт «что изменилось»: кто сменил корзину и почему.

Устройство утверждено 23.09.2026 (`docs/change_report.md`). Главное требование —
**читается за пять минут**: не перечень из 900 строк, а десяток изменений,
у каждого из которых названа причина.

**Изменение — смена вердикта, и сравнивается он кодами, а не текстами.**
Правка одного слова в методике дала бы 900 «изменений», и отчёт, в котором
их 900, не читается ни в какой день.

**Три причины изменения разводятся отпечатком входов.** Изменился отпечаток —
причина у эмитента; тот же при изменившихся версиях кода и методики — причина
у нас; тот же при тех же версиях — **беспричинное изменение**, то есть дефект
недетерминированности, и это остановка, а не строка отчёта.

**Наблюдение и пересчёт между собой не сравниваются.** Пересчёт знает меньше
по устройству — признаки карточки истории не имеют и в него не идут, — и
разница читалась бы как изменение у эмитента.

    uv run python eval/change_report_run.py                  # последняя пара точек
    uv run python eval/change_report_run.py --on 2026-05-06  # отчёт того дня
    uv run python eval/change_report_run.py --kind backfill

**Замер не считает сам**: корзины он берёт из истории, записанной боевой
маршрутизацией, и своей арифметики не имеет.
"""

import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection, fetch_all  # noqa: E402
from finlib.scoring.routing import load_routing  # noqa: E402
from finlib.sources.cbonds import bond_issuers  # noqa: E402
from finlib.sources.cbonds_events import events_of  # noqa: E402
from finlib.sources.moex_risk import risk_sectors  # noqa: E402

logger = logging.getLogger(__name__)

_DATES = """
SELECT DISTINCT as_of FROM routing_history WHERE kind = %(kind)s ORDER BY as_of
"""

_POINTS = """
SELECT h.inn, h.basket, h.subgroup, h.grounds, h.fingerprint, h.standard,
       h.report_date, r.code_version, r.methodology
FROM routing_history h LEFT JOIN routing_run r ON r.id = h.run_id
WHERE h.kind = %(kind)s AND h.as_of = %(as_of)s
"""

# Наименования корзин и оснований берутся у справочника: технических кодов
# в отчёте нет — их читает не человек.
_NAMES: dict[str, str] = {}


def _basket_name(routing, code: str) -> str:  # noqa: ANN001
    """Наименование корзины по коду."""
    return routing.basket(code).name if code else "нет"


def _ground_names(routing) -> dict[str, str]:  # noqa: ANN001
    """Код основания → наименование, одним словарём на все корзины."""
    if not _NAMES:
        for basket in routing.baskets:
            for ground in basket.grounds:
                _NAMES[ground.code] = ground.name
        for ground in routing.reference:
            _NAMES[ground.code] = ground.name
    return _NAMES


def _why(routing, inn: str, since: date, until: date, appeared: set[str]) -> str:  # noqa: ANN001
    """Чем изменились данные: слой, в котором нашлась датированная запись.

    **Слой называется существующим перечнем** `routing.ground_sources` —
    второго не заводится. Причина, не нашедшаяся в слое, так и печатается:
    изменение с неназванной причиной читалось бы как объяснённое.
    """
    said: list[str] = []
    events = events_of(inn)
    for item in events.records:
        if item.moment is not None and since < item.moment <= until:
            what = "не исполнено" if not item.settled else "исполнено"
            said.append(
                f"{item.kind.lower()} {item.moment:%d.%m.%Y}, {what}"
            )
    for item in events.ratings:
        if item.assigned is not None and since < item.assigned <= until:
            said.append(f"{item.agency}: {item.point} {item.assigned:%d.%m.%Y}")
    risky = risk_sectors()
    for issue in events.issues:
        entry = risky.get(issue.isin) if issue.isin else None
        if entry is not None and entry.since is not None and since < entry.since <= until:
            said.append(
                f"{issue.name} переведён в {entry.board} {entry.since:%d.%m.%Y}"
            )
    if said:
        return "; ".join(dict.fromkeys(said))
    # Отчётность датированной записи не имеет — её раскрытие видно по тому,
    # что сменился отчётный период строки.
    if appeared:
        names = _ground_names(routing)
        return "появилось основание: " + ", ".join(
            names.get(code, code) for code in sorted(appeared)
        )
    return "данные изменились, слой не назван"


def _read(conn, kind: str, moment: date) -> dict[str, dict]:  # noqa: ANN001
    """Точки истории на дату: ИНН → вердикт."""
    return {
        row["inn"]: row
        for row in fetch_all(_POINTS, {"kind": kind, "as_of": moment}, conn=conn)
    }


def main() -> int:
    """Печатает отчёт изменений между двумя соседними точками истории."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    kind = "backfill"
    if "--kind" in sys.argv:
        kind = sys.argv[sys.argv.index("--kind") + 1]
    routing = load_routing()
    with connection() as conn:
        dates = [row["as_of"] for row in fetch_all(_DATES, {"kind": kind}, conn=conn)]
        if len(dates) < 2:
            print(
                "# Отчёт изменений\n\nТочек истории меньше двух: сравнивать "
                "не с чем. Это не «изменений нет» — это отсутствие истории.\n"
            )
            return 1
        until = dates[-1]
        if "--on" in sys.argv:
            until = date.fromisoformat(sys.argv[sys.argv.index("--on") + 1])
            if until not in dates:
                print(f"# Отчёт изменений\n\nТочки {until} в истории нет.\n")
                return 1
        since = max(item for item in dates if item < until)
        was, now = _read(conn, kind, since), _read(conn, kind, until)
        bonds = set(bond_issuers())
        _report(routing, kind, since, until, was, now, bonds)
    return 0


def _report(routing, kind, since, until, was, now, bonds) -> None:  # noqa: ANN001
    """Собирает и печатает сам отчёт."""
    names = _ground_names(routing)
    order = {basket.code: basket.order for basket in routing.baskets}
    print(f"# Что изменилось: {until:%d.%m.%Y}\n")
    print(
        f"Сравнение с {since:%d.%m.%Y} — это **предыдущая точка истории**, "
        f"а не «вчера»: заголовок, обещающий сутки, при разрыве врёт. "
        f"Род точек — {'пересчёт' if kind == 'backfill' else 'наблюдение'}; "
        "наблюдение с пересчётом не сравнивается вовсе.\n"
    )
    # **Беспричинное изменение — остановка, а не строка.** Считается первым:
    # отчёт, начавшийся с перечня изменений, о нём умолчал бы.
    causeless = [
        inn
        for inn, row in now.items()
        if inn in was
        and row["basket"] != was[inn]["basket"]
        and row["fingerprint"] == was[inn]["fingerprint"]
        and row["code_version"] == was[inn]["code_version"]
        and row["methodology"] == was[inn]["methodology"]
    ]
    ours = [
        inn
        for inn, row in now.items()
        if inn in was
        and row["basket"] != was[inn]["basket"]
        and row["fingerprint"] == was[inn]["fingerprint"]
        and inn not in causeless
    ]
    moved = [
        inn
        for inn, row in now.items()
        if inn in was
        and row["basket"] != was[inn]["basket"]
        and row["fingerprint"] != was[inn]["fingerprint"]
    ]
    entered = sorted(set(now) - set(was))
    left = sorted(set(was) - set(now))

    print(f"## Сменили корзину: {len(moved)} из {len(now)}\n")
    if not moved:
        print("ни одного.\n")
    else:
        print("| Эмитент | Было | Стало | Почему |")
        print("|---|---|---|---|")
        # Порядок — по тяжести движения: ухудшения прежде улучшений.
        def weight(inn: str) -> tuple[int, int]:
            before, after = order[was[inn]["basket"]], order[now[inn]["basket"]]
            return (0 if after < before else 1, after)

        for inn in sorted(moved, key=weight):
            before, after = was[inn], now[inn]
            appeared = set(after["grounds"]) - set(before["grounds"])
            print(
                f"| {inn} | {_basket_name(routing, before['basket'])} "
                f"| {_basket_name(routing, after['basket'])} "
                f"| {_why(routing, inn, since, until, appeared)} |"
            )
    print()

    # **Новое основание без смены корзины — только старшей подгруппы.**
    # Новый дефолт у эмитента, уже стоящего во «Внимании», терять нельзя;
    # прочие показываются числом.
    senior = {
        ground.code
        for basket in routing.baskets
        for ground in basket.grounds
        if basket.group_of(ground.code) == "event_risk"
    }
    events_added = []
    other_added = 0
    for inn, row in now.items():
        if inn not in was or row["basket"] != was[inn]["basket"]:
            continue
        appeared = set(row["grounds"]) - set(was[inn]["grounds"])
        if appeared & senior:
            events_added.append((inn, appeared & senior))
        elif appeared:
            other_added += 1
    print(f"## Новое основание без смены корзины: {len(events_added)}\n")
    for inn, appeared in events_added[:20]:
        said = ", ".join(names.get(code, code) for code in sorted(appeared))
        print(f"- {inn}: {said}")
    print(f"\nПрочих изменений оснований {other_added} — показаны числом.\n")

    print(f"## Вошли в периметр: {len(entered)}   Вышли: {len(left)}\n")
    for inn in entered[:10]:
        mark = " (с выпусками в обращении)" if inn in bonds else ""
        print(f"- вошёл {inn}{mark}: {_basket_name(routing, now[inn]['basket'])}")
    for inn in left[:10]:
        print(f"- вышел {inn}: было {_basket_name(routing, was[inn]['basket'])}")
    print()

    print(f"## Наши правки: {len(ours)}\n")
    if ours:
        print(
            "Отпечаток входов тот же, версия кода либо методики изменилась: "
            "это наша правка, а не изменение эмитента. Причина одна на всех "
            "и называется версией.\n"
        )
        print(Counter(now[inn]["basket"] for inn in ours).most_common())
    else:
        print("ни одной.\n")

    print(f"\n## Беспричинных изменений: {len(causeless)}\n")
    if causeless:
        print(
            "**Остановка.** Вердикт изменился при том же отпечатке входов "
            "и тех же версиях: у изменения нет причины, а значит, расчёт "
            "недетерминирован. Эмитенты: " + ", ".join(causeless[:20]) + "\n"
        )
    else:
        print("ни одного — вердикт воспроизводим.\n")


if __name__ == "__main__":
    sys.exit(main())
