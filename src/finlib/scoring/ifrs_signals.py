"""Надзорные сигналы ветки МСФО: входы свои, арифметика общая.

Справочник у ветки свой (`methodology/ifrs_signals.yaml`): величины опознаются
кодами позиций, отсечки привязаны к валюте баланса, состав признаков другой.
Но считает их та же реализация, что и признаки РСБУ, — `scoring/signals.py`.
Второе выражение того же правила разошлось бы с первым, и увидеть это можно
было бы только сравнив два документа.

**Отсутствие признака объявляется.** Что не перенесено в ветку — перечислено
в справочнике с причиной: перечень признаков ветки сравнивают с перечнем РСБУ,
и отсутствующий признак без причины неотличим от забытого.
"""

import logging
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection
from finlib.scoring.signals import SignalHit, structure_shifts
from finlib.standards import Standard

logger = logging.getLogger(__name__)


def ifrs_signals(
    inn: str,
    conn: PgConnection,
    report_date: date,
    catalog=None,
    stop_factors: tuple[str, ...] = (),
) -> list[SignalHit]:
    """Признаки ветки по величинам отчётного и предыдущего периодов.

    Предыдущий период — ближайший более ранний из загруженных: у ветки МСФО
    это сравнительная колонка того же комплекта, и другого источника прошлых
    величин эмитента у нас нет. Признак, которому не с чем сравнивать,
    не срабатывает — величины нет, а не «нет сдвига».

    `stop_factors` — коды сработавших стоп-факторов: выплата акционерам при
    состоянии, ограничившем класс, есть обстоятельство независимо от размера
    выплаты, и условие это об исходе оценки, а не о величинах.
    """
    from finlib.metrics.engine import load_period_values
    from finlib.normalize.ifrs_signals import load_ifrs_signals
    from finlib.scoring.engine import shares_of
    from finlib.scoring.signals import evaluate_signals

    catalog = catalog if catalog is not None else load_ifrs_signals()
    periods = load_period_values(inn, conn, Standard.IFRS)
    current = periods[report_date].values if report_date in periods else {}
    earlier = sorted((item for item in periods if item < report_date), reverse=True)
    previous: dict[str, Decimal | None] = (
        periods[earlier[0]].values if earlier else {}
    )

    # Единица — комплекта, а не стандарта: консолидированная отчётность
    # составляется в миллионах, и «46 620» без единицы читатель прочтёт
    # в тех единицах, которые предположит сам.
    found = list(
        evaluate_signals(
            current,
            previous,
            catalog,
            _unit(inn, report_date, conn),
            stop_factors,
        )
    )
    rule = catalog.structure_shift
    found += structure_shifts(
        shares_of(current, rule),
        shares_of(previous, rule),
        # Наименования приходят из справочника вместе с кодами: формулировка
        # называет статью так, как её называет методика.
        dict(rule.lines),
        catalog,
    )
    # Счётчик проверенного рядом с числом сработавших: ноль признаков при
    # неизвестном числе проверенных не означает, что их проверяли.
    active = sum(1 for item in catalog.signals if item.active)
    logger.info(
        "сигналы МСФО %s за %s: проверено признаков %d (объявлено %d, "
        "непереносимых %d), сработало %d",
        inn,
        report_date,
        active + (1 if rule.active else 0),
        len(catalog.signals) + 1,
        len(catalog.not_transferred),
        len(found),
    )
    return found


def _unit(inn: str, report_date: date, conn: PgConnection) -> str:
    """Наименование денежной единицы комплекта; пусто — комплект не найден.

    **Считает её один код на весь проект** (`normalize.facts.unit_of`): её
    набирают и тезисы, и список наблюдения, и выгрузка, и второй набор уже
    расходился с первым — графы печатали «млн руб.», а основания рядом
    «тыс. руб.». Единица берётся у комплекта, а не у стандарта: правило то же,
    по которому документ печатает «млн руб.» там, где отчётность составлена
    в миллионах.
    """
    from finlib.normalize.facts import unit_name_of

    return unit_name_of(inn, report_date, conn, Standard.IFRS.value)


