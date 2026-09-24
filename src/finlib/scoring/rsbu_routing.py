"""Входы маршрута по отчётности РСБУ: те же вопросы, свой справочник.

**Маршрут по РСБУ появился 22.09.2026** (дорожная карта, фаза 1). Причина
в охвате: из 702 эмитентов с выпусками в обращении консолидированную
отчётность раскрывают 206, а 403 — только отчётность отдельного юридического
лица. Прежде список видел первых и не видел вторых, причём отсутствие вторых
не было видно даже как отсутствие: универсум собирался из доставок МСФО.

**Величины считает боевой расчёт** (`metrics.engine.compute_all`), а не своя
выборка: второй путь к тем же числам разошёлся бы с первым, и корзина
зависела бы от того, кто спросил. Здесь только приведение к виду, который
принимает маршрут: расчёт РСБУ отдаёт результат с периодом и статусом,
а маршрут спрашивает значение с наименованием.

**Стоп-факторы берутся у методики РСБУ** (`scoring.yaml`) и проверяются той же
функцией, что и при оценке (`scoring.engine.triggered_stop_factors`): перечень,
написанный здесь во второй раз, разошёлся бы с первым — ровно так стоп-факторы
ветки МСФО и жили в замере, пока расчёт по фактам звал оценку с пустым
перечнем.
"""

import logging
from datetime import date
from decimal import Decimal

from finlib.db import PgConnection, fetch_all
from finlib.metrics.definitions import load_metrics
from finlib.metrics.engine import MetricStatus, compute_all
from finlib.metrics.ifrs import MetricValue
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Последний отчётный период РСБУ вне карантина. Выборка называет стандарт:
# запись о комплекте МСФО неотличима на вид от своей.
# **Комплект виден не с отчётной даты, а с даты раскрытия** — то же правило,
# что у МСФО (`routing_store._LATEST`), и оно нужно при пересчёте истории
# назад: отчётность за 2025 год 15 февраля 2026-го ещё не существовала.
_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date, max(o.name) AS name
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'rsbu' AND s.is_actual AND s.status <> 'quarantine'
  -- **Основание маршрута — годовой комплект** (`standards.yaml`,
  -- `period_preference.basis`). С загрузкой промежуточных периодов
  -- (24.09.2026) `max(report_date)` без этого условия стал бы возвращать
  -- полугодовую дату, и маршрут молча сменил бы основание: шкалы
  -- откалиброваны на годовых величинах, и полугодовая выручка в них —
  -- ошибка не в данных, а в мере.
  AND COALESCE(s.reporting_kind, 'full') <> 'interim'
  AND (
      %(as_of)s::date IS NULL
      -- Настоящая дата раскрытия старше смоделированной: правило берётся
      -- только там, где источник о дате молчит.
      OR COALESCE(
          (s.meta->>'disclosed_on')::date,
          f.report_date + (
              CASE WHEN s.reporting_kind = 'interim'
                   THEN %(interim)s ELSE %(annual)s END
          )
      ) <= %(as_of)s::date
  )
GROUP BY f.inn
"""

# **Величина строки берётся одним запросом на оба стандарта**
# (`routing_store._LINE`): код строки у стандартов свой, а вопрос один,
# и второй запрос к тому же ответу разошёлся бы с первым при первой же
# правке правила выборки — предпочтения источника, например.


def latest_annual(
    conn: PgConnection, as_of: date | None = None
) -> dict[str, tuple[date, str]]:
    """ИНН → отчётная дата свежего комплекта РСБУ и наименование организации.

    Один запрос на прогон, а не на эмитента: эмитентов сотни, а вопрос один.

    `as_of` называет день, на который строится маршрут: комплект виден с даты
    раскрытия, а не с отчётной. `None` — сегодня, и видно всё загруженное.
    """
    from finlib.scoring.routing import load_routing

    known = load_routing().history.known_from
    return {
        row["inn"]: (row["report_date"], (row["name"] or row["inn"]).strip())
        for row in fetch_all(
            _LATEST,
            {
                "as_of": as_of,
                "annual": known.days(Standard.RSBU, interim=False),
                "interim": known.days(Standard.RSBU, interim=True),
            },
            conn=conn,
        )
    }


def computed_of(
    inn: str, moment: date, conn: PgConnection
) -> tuple[MetricValue, ...]:
    """Показатели РСБУ отчётного периода в виде, который принимает маршрут.

    Считает их боевой расчёт целиком по всем периодам, а сюда попадает
    отчётный: маршрут спрашивает о состоянии на последнюю отчётную дату,
    а динамику по этой отчётности он не спрашивает вовсе.

    **Части отношения хранятся рядом с ним.** По знаку знаменателя решается,
    доказывает ли вывод по границе хоть что-нибудь: чистый долг к убытку
    отрицателен, и шкала прочла бы его как низкую нагрузку.
    """
    catalog = load_metrics()
    names = {item.code: item for item in catalog.metrics}
    results = compute_all(inn, conn, standard=Standard.RSBU, with_derived=False)
    found: list[MetricValue] = []
    for item in results:
        if item.report_date != moment:
            continue
        metric = names.get(item.metric_code)
        if metric is None:
            continue
        found.append(
            MetricValue(
                code=item.metric_code,
                name=metric.name,
                group=metric.group,
                in_scoring=metric.in_scoring,
                value=item.value if item.status is MetricStatus.OK else None,
            )
        )
    return tuple(found)


def with_denominator(
    computed: tuple[MetricValue, ...], code: str, value: Decimal | None
) -> tuple[MetricValue, ...]:
    """Приписывает показателю знаменатель, которым он посчитан.

    Расчёт РСБУ частей отношения не хранит, а маршруту нужен знак
    знаменателя: отношение чистого долга к отрицательной прибыли от продаж
    отрицательно и читается шкалой как низкая нагрузка. Величина берётся
    из той же строки отчётности, по которой показатель и считается, —
    второго её набора здесь не появляется.
    """
    from dataclasses import replace

    return tuple(
        replace(item, denominator=value) if item.code == code else item
        for item in computed
    )
