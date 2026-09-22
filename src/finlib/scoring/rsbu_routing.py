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
from finlib.normalize.facts import source_preference
from finlib.standards import Standard

logger = logging.getLogger(__name__)

# Последний отчётный период РСБУ вне карантина. Выборка называет стандарт:
# запись о комплекте МСФО неотличима на вид от своей.
_LATEST = """
SELECT f.inn, max(f.report_date) AS report_date, max(o.name) AS name
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
LEFT JOIN organization o ON o.inn = f.inn
WHERE f.standard = 'rsbu' AND s.is_actual AND s.status <> 'quarantine'
GROUP BY f.inn
"""

# Величина строки отчётности комплекта. Выборка называет и стандарт,
# и предпочтение источника: за год комплектов бывает два — ГИР БО
# и агрегатор, — и первоисточник старше.
_LINE = f"""
SELECT f.value, s.source FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = 'rsbu' AND f.report_date = %(d)s
  AND f.line_code = %(code)s AND s.is_actual AND s.status <> 'quarantine'
{source_preference("s")}
LIMIT 1
"""

_SOURCES = """
SELECT DISTINCT source, unit_code, reporting_type FROM src_file
WHERE inn = %(inn)s AND standard = 'rsbu' AND is_actual
  AND status <> 'quarantine' AND report_year = %(year)s
"""


def latest_annual(conn: PgConnection) -> dict[str, tuple[date, str]]:
    """ИНН → отчётная дата свежего комплекта РСБУ и наименование организации.

    Один запрос на прогон, а не на эмитента: эмитентов сотни, а вопрос один.
    """
    return {
        row["inn"]: (row["report_date"], (row["name"] or row["inn"]).strip())
        for row in fetch_all(_LATEST, {}, conn=conn)
    }


def sources_of(inn: str, moment: date, conn: PgConnection) -> list[dict]:
    """Доставки комплекта: способ получения, единица и вид отчётности."""
    return fetch_all(_SOURCES, {"inn": inn, "year": moment.year}, conn=conn)


def line_value(inn: str, moment: date, code: str, conn: PgConnection) -> Decimal | None:
    """Величина строки отчётности; None — строка не раскрыта.

    **Ноль агрегатора величиной не считается и здесь.** Правило объявлено
    у вида отчёта (`cbonds_mapping.yaml`, `zero_reading`) и действует всюду,
    где ноль участвует в суждении: у прибыли от продаж ноль от агрегатора
    читался бы как отсутствие операционного результата, то есть как
    утверждение об эмитенте, сделанное по нераскрытой величине.
    """
    found = fetch_all(_LINE, {"inn": inn, "d": moment, "code": code}, conn=conn)
    if not found:
        return None
    value, source = Decimal(found[0]["value"]), found[0]["source"]
    if value == 0 and _zero_is_unknown(source):
        logger.info(
            "%s за %s: строка %s доставлена нулём (%s) — величиной не считается",
            inn,
            moment,
            code,
            source,
        )
        return None
    return value


def _zero_is_unknown(source: str) -> bool:
    """Означает ли ноль этого способа получения «неизвестно»."""
    if source != "cbonds":
        return False
    from finlib.normalize.cbonds_mapping import load_cbonds_mapping

    mapping = load_cbonds_mapping()
    return any(
        report.zero_reading is not None and report.zero_reading.as_not_disclosed
        for report in mapping.reports.values()
    )


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
