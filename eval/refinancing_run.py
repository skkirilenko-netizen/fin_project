"""Рефинансирование: платежи ближайших 12 месяцев против денежных средств.

    uv run python eval/refinancing_run.py [--months 12] > data/output/refinancing.md

**Годовая отчётность срочности долга не говорит.** В ней остаток, а не график:
«долг 40 млрд» у эмитента с погашением через восемь лет и у эмитента
с погашением в марте означает разное, и балансовые коэффициенты их не
различают. График платежей это различает прямо.

**Распределение, а не порог.** Отсечка — решение человека, и назначать её
по двум наблюдениям значило бы мерить размер набора. Здесь только величины
и то, у скольких эмитентов их удалось посчитать.

**Величина приводится к единице комплекта по коду ОКЕИ.** Объём выпуска
источник отдаёт в рублях, консолидированная отчётность составляется
в миллионах: без приведения отношение «платежи к денежным средствам»
ошибалось бы в тысячу раз — тот же класс дефекта, что единица измерения
комплекта.

**Оферта считается порознь от платежей графика.** Предъявление бумаги
к выкупу — право владельца, а не обязанность, и сложенное с купоном оно
выдало бы возможное за состоявшееся.

**Знаменатель печатается рядом с величиной.** «К погашению ноль» у эмитента,
графиков которого нет на диске, и у эмитента без платежей — разные сведения.

**Замер не считает сам**: вердикты и входы берёт `routing_store.routing_rows`,
приведение единиц — `cbonds_events.in_unit`, платежи — `cbonds_flows`.
"""

import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finlib.db import connection  # noqa: E402
from finlib.metrics.display import money, ratio  # noqa: E402
from finlib.normalize.lines import load_lines  # noqa: E402
from finlib.scoring.routing_store import routing_rows  # noqa: E402
from finlib.sources.cbonds_events import in_unit  # noqa: E402
from finlib.sources.cbonds_flows import refinancing  # noqa: E402

logger = logging.getLogger(__name__)

# Доли, по которым раскладывается распределение. Границы описывают набор,
# а не оценивают эмитента: порог — решение человека.
BANDS: tuple[tuple[str, Decimal | None], ...] = (
    ("платежей в окне нет", Decimal(0)),
    ("до 0,25 денежных средств", Decimal("0.25")),
    ("0,25–0,5", Decimal("0.5")),
    ("0,5–1", Decimal(1)),
    ("1–2", Decimal(2)),
    ("более 2", None),
)


class Measured(NamedTuple):
    """Посчитанный эмитент: величины названы полями, а не местами.

    **Позиционный кортеж однажды разъезжается.** В этом проекте это уже
    случалось — зрелость порогов легла в перечень погашенных, — и здесь цена
    та же: перепутанные местами отношение и величина дадут правдоподобную
    таблицу с неверными числами.
    """

    name: str
    basket: str
    scheduled: Decimal
    offered: Decimal
    cash: Decimal
    share: Decimal
    unit: str


def band(share: Decimal) -> str:
    """Доля в разложении распределения."""
    if share == 0:
        return BANDS[0][0]
    for name, edge in BANDS[1:]:
        if edge is None or share <= edge:
            return name
    return BANDS[-1][0]


def main() -> int:
    """Печатает распределение; 1 — если считать не удалось ни у кого."""
    logging.basicConfig(level=logging.ERROR, format="%(message)s")
    months = (
        int(sys.argv[sys.argv.index("--months") + 1]) if "--months" in sys.argv else 12
    )
    today = date.today()
    units = load_lines().units
    with connection() as conn:
        rows, counts = routing_rows(conn, today)

    print(f"# Рефинансирование: платежи {months} месяцев против денежных средств\n")
    print(
        f"Считано {today:%d.%m.%Y}. **Порога здесь нет** — только величины "
        "и знаменатель: отсечка остаётся решением человека, а назначенная "
        "по двум наблюдениям она мерила бы размер набора.\n"
    )

    measured: list[Measured] = []
    dry: list[tuple[str, str, Decimal, str]] = []
    no_issues = no_schedule = no_cash = no_unit = no_offers = 0
    for item in rows:
        if item.events is None or not item.events.issues_known:
            no_issues += 1
            continue
        plan = refinancing(item.events.issues, months, today)
        if plan.issues == 0:
            no_issues += 1
            continue
        if not plan.known:
            no_schedule += 1
            continue
        no_offers += plan.without_offers
        if item.unit_code is None:
            no_unit += 1
            continue
        due = in_unit(plan.scheduled, item.unit_code)
        offered = in_unit(plan.offered, item.unit_code)
        money_on_hand = item.cash
        if due is None or offered is None:
            no_unit += 1
            continue
        if money_on_hand is None or money_on_hand <= 0:
            # **Платежи есть, а денежных средств нет — самое тяжёлое сочетание,
            # и отношением его не выразить.** Ноль у агрегатора означает
            # и нераскрытие, поэтому эмитент называется отдельно, а не выпадает
            # из замера молча.
            no_cash += 1
            if due > 0:
                dry.append((item.name, item.verdict.basket_name, due,
                            units.name_of(item.unit_code)))
            continue
        # Денежные средства несутся величиной, а не восстанавливаются делением
        # обратно из отношения: одна величина — один код, считающий её.
        measured.append(
            Measured(
                name=item.name,
                basket=item.verdict.basket_name,
                scheduled=due,
                offered=offered,
                cash=money_on_hand,
                share=due / money_on_hand,
                unit=units.name_of(item.unit_code),
            )
        )

    print(
        f"Эмитентов в списке {counts['эмитентов']}. Посчитано **{len(measured)}**; "
        f"без выпусков в обращении {no_issues}, без графика платежей "
        f"{no_schedule}, без раскрытых денежных средств {no_cash}, "
        f"без единицы измерения {no_unit}. Выпусков, по которым ответа "
        f"об офертах нет, — {no_offers}: у них «оферт ноль» означает "
        "недошедшую доставку, а не отсутствие права предъявления.\n"
    )
    if not measured:
        print(
            "Считать не удалось ни у кого: это не «платежей нет», а отсутствие "
            "данных — графики платежей забираются `scripts/events_fetch.py`."
        )
        return 1

    # **Распределение показывается по корзинам, и это главное в нём.** Маршрут
    # рефинансирования не видит вовсе, и вопрос не в том, сколько эмитентов
    # с высоким отношением, а сколько из них стоит в «Без внимания»: отсечка
    # имеет смысл ровно настолько, насколько она добавляет неувиденное.
    baskets = ["Разбор", "Внимание", "Без внимания", "Установить статус эмитента"]
    print(
        "| Доля платежей в денежных средствах | Всего | "
        + " | ".join(baskets)
        + " |"
    )
    print("|---" * (len(baskets) + 2) + "|")
    for name, _ in BANDS:
        rows_here = [entry for entry in measured if band(entry.share) == name]
        counted = " | ".join(
            str(sum(1 for entry in rows_here if entry.basket == basket))
            for basket in baskets
        )
        print(f"| {name} | {len(rows_here)} | {counted} |")

    # **Кого отсечка добавила бы к увиденному.** Маршрут рефинансирования
    # не видит, и ценность порога измеряется этим перечнем, а не числом
    # эмитентов с высоким отношением.
    unseen = sorted(
        (
            entry
            for entry in measured
            if entry.basket == "Без внимания" and entry.share > 1
        ),
        key=lambda entry: entry.share,
        reverse=True,
    )
    print(
        f"\n**Отсечка добавила бы к увиденному {len(unseen)} эмитентов** — "
        "тех, кто стоит в «Без внимания» при платежах выше денежных средств. "
        "Маршрут рефинансирования не видит вовсе, и ценность порога измеряется "
        "этим перечнем, а не числом эмитентов с высоким отношением.\n"
    )
    if unseen:
        print("| Эмитент | Платежи 12 мес. | Денежные средства | Отношение | Единица |")
        print("|---|---|---|---|---|")
        for entry in unseen:
            print(
                f"| {entry.name} | {money(entry.scheduled)} | {money(entry.cash)} "
                f"| {ratio(entry.share)} | {entry.unit} |"
            )

    if dry:
        print(
            "\n## Платежи есть, денежных средств нет вовсе\n\n"
            "Отношением это не выражается, а обстоятельство тяжелее любого "
            "отношения. **Ноль у агрегатора означает и нераскрытие**, поэтому "
            "сказано ровно то, что известно: величина платежей есть, величины "
            "денежных средств нет.\n"
        )
        print("| Эмитент | Корзина | Платежи 12 мес. | Единица |")
        print("|---|---|---|---|")
        for name, basket, due, unit in sorted(
            dry, key=lambda entry: entry[2], reverse=True
        ):
            print(f"| {name} | {basket} | {money(due)} | {unit} |")

    print("\n## Двадцать наибольших отношений\n")
    print(
        "| Эмитент | Корзина | Платежи 12 мес. | По офертам | Денежные средства "
        "| Отношение | Единица |"
    )
    print("|---|---|---|---|---|---|---|")
    for entry in sorted(measured, key=lambda item: item.share, reverse=True)[:20]:
        print(
            f"| {entry.name} | {entry.basket} | {money(entry.scheduled)} "
            f"| {money(entry.offered)} | {money(entry.cash)} "
            f"| {ratio(entry.share)} | {entry.unit} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
