"""Сбор отказов обоих контуров в один перечень для «Ограничений анализа».

Механизм один на РСБУ и МСФО намеренно: не раскрытая строка баланса
и не извлечённая величина примечания различаются местом, а не природой.
Здесь только сбор — формулировки живут в `methodology/refusals.yaml`,
а тип отказа в `quality/refusals.py`.
"""

import logging

from finlib.quality.refusals import Refusal, RefusalCatalog, load_refusals, refusal

logger = logging.getLogger(__name__)

# Причины расчёта РСБУ и МСФО названы одинаковыми словами там, где означают
# одно и то же. Соответствие объявлено здесь, а не подразумевается совпадением
# строк: коды перечислений живут своей жизнью и однажды разойдутся.
METRIC_REASONS: dict[str, str] = {
    "missing_input": "missing_input",
    "missing_lines": "missing_lines",
    "no_previous_period": "no_previous_period",
    "no_comparable_period": "no_comparable_period",
    "zero_denominator": "zero_denominator",
    "negative_denominator": "negative_denominator",
    "sign_change": "sign_change",
    "not_extracted_yet": "not_extracted_yet",
    "adjustment_impossible": "adjustment_impossible",
    "interim_not_annualised": "interim_not_annualised",
}

# Причины исключения показателя из балла: машинный вид из РСБУ и решение
# методики МСФО.
EXCLUSION_REASONS: dict[str, str] = {
    "stop_factor": "excluded_stop_factor",
    "no_level_scale": "excluded_no_level_scale",
    "duplicate": "excluded_duplicate",
    "no_data": "excluded_no_data",
    "not_routing": "excluded_not_routing",
}


def from_ifrs_metrics(
    values: tuple,
    names: dict[str, str],
    catalog: RefusalCatalog | None = None,
    adjustments: tuple = (),
) -> tuple[Refusal, ...]:
    """Отказы расчёта показателей МСФО с указанием недостающих величин.

    Для отказа по невозможной поправке местом служит текст методики: он
    и называет, где величина лежит в отчётности — «раскрыты сноской под
    балансом, а не строкой формы». Код позиции читателю ничего не говорит.
    """
    catalog = catalog or load_refusals()
    by_metric = {item.metric: item for item in adjustments}
    found: list[Refusal] = []
    for item in values:
        if item.calculable or item.reason is None:
            continue
        adjustment = by_metric.get(item.code)
        if item.reason.value == "adjustment_impossible" and adjustment is not None:
            where = adjustment.where
        else:
            where = ", ".join(
                names.get(code, code) for code in item.missing
            ) or _where_of(item.reason.value)
        found.append(
            refusal(METRIC_REASONS[item.reason.value], item.name, where, catalog)
        )
    return tuple(found)


def from_ifrs_notes(
    outcomes: tuple, catalog: RefusalCatalog | None = None, names: dict | None = None
) -> tuple[Refusal, ...]:
    """Отказы величин из примечаний с указанием номера примечания.

    Предмет называется наименованием строки примечания, а не кодом: код —
    механизм проверки, а читать раздел будет человек.
    """
    catalog = catalog or load_refusals()
    names = names or {}
    found: list[Refusal] = []
    for item in outcomes:
        if item.found or item.refusal is None:
            continue
        where = str(item.note) if item.note is not None else "ссылки из формы нет"
        found.append(
            refusal(item.refusal.value, names.get(item.code, item.code), where, catalog)
        )
    return tuple(found)


def from_ifrs_audit(report, catalog: RefusalCatalog | None = None) -> tuple[Refusal, ...]:
    """Отказы чтения аудиторского заключения с номерами страниц."""
    from finlib.sources.ifrs_audit import Determination

    catalog = catalog or load_refusals()
    found: list[Refusal] = []
    if report.determination is Determination.ABSENT:
        found.append(
            refusal("audit_absent", "Аудиторское заключение", "документ его не содержит", catalog)
        )
        return tuple(found)
    if report.determination is Determination.NOT_READABLE:
        pages = ", ".join(str(item) for item in report.unreadable_pages) or "страницы не определены"
        found.append(
            refusal("audit_not_readable", "Аудиторское заключение", f"страницы {pages}", catalog)
        )
        return tuple(found)
    for item in report.texts:
        if item.found:
            continue
        found.append(
            refusal("audit_section_not_readable", item.name, _where_of(item.refusal.value), catalog)
        )
    return tuple(found)


def from_assessment(
    assessment, groups_missing: str, catalog: RefusalCatalog | None = None
) -> tuple[Refusal, ...]:
    """Отказ в присвоении класса с указанием, чего не хватило."""
    catalog = catalog or load_refusals()
    if assessment.class_code is not None:
        return ()
    code = "no_class_metrics"
    if "одной группой" in assessment.no_class_reason:
        code = "no_class_group_weight"
    elif "к одной группе" in assessment.no_class_reason:
        code = "no_class_groups"
    return (refusal(code, "Класс финансового состояния", groups_missing, catalog),)


def from_excluded(
    names: dict[str, str], limitation: str, catalog: RefusalCatalog | None = None
) -> tuple[Refusal, ...]:
    """Показатели, рассчитанные, но исключённые из оценки неприменимостью.

    Формулировка отдельная от «не рассчитан»: читатель обязан видеть разницу
    между «величины нет» и «величина есть, но в оценку не идёт».
    """
    catalog = catalog or load_refusals()
    return tuple(
        refusal("excluded_not_applicable", name, limitation, catalog)
        for name in names.values()
    )


def from_rsbu_metrics(
    rows: list[dict], names: dict[str, str], catalog: RefusalCatalog | None = None
) -> tuple[Refusal, ...]:
    """Отказы расчёта показателей РСБУ из `metric_value`.

    Берутся показатели методики: производные величины (`_chg_pct`, `_share`)
    отказом раздела не являются — запрашивать по ним нечего, а их причина
    объясняется рядом с самой величиной.
    """
    catalog = catalog or load_refusals()
    found: list[Refusal] = []
    for row in rows:
        code = row.get("reason_code")
        if code not in METRIC_REASONS:
            continue
        name = names.get(row["metric_code"], row["metric_code"])
        where = row.get("reason") or "причина не названа"
        found.append(refusal(METRIC_REASONS[code], name, _short(where), catalog))
    return tuple(found)


def from_rsbu_exclusions(
    rows: list[dict],
    catalog: RefusalCatalog | None = None,
    refused: frozenset[str] = frozenset(),
) -> tuple[Refusal, ...]:
    """Показатели, исключённые из балла решением методики.

    `refused` — показатели, отказ по которым в разделе уже назван. Исключение
    из балла по причине «не рассчитан» такому показателю не добавляет ничего:
    нерассчитанный показатель в балл войти не может по устройству, — а семейство
    отказа у двух строк выходило разным, и раздел просил у организации то,
    о чём строкой выше сказано «запрашивать нечего, извлечение за нами».
    """
    catalog = catalog or load_refusals()
    found: list[Refusal] = []
    for row in rows:
        if row.get("included"):
            continue
        kind = row.get("exclusion_kind") or "no_level_scale"
        if kind == "no_data" and row["metric_code"] in refused:
            continue
        reason = EXCLUSION_REASONS.get(kind, "excluded_no_level_scale")
        found.append(
            refusal(reason, row.get("name") or row["metric_code"],
                    _short(row.get("exclusion_reason") or "причина не названа"), catalog)
        )
    return tuple(found)


def _where_of(reason: str) -> str:
    """Место по коду причины, когда перечня недостающих величин нет."""
    return {
        "not_extracted_yet": "таблица сроков погашения примечания о заёмных средствах",
        "interim_not_annualised": "операционный поток сезонен и приведению не подлежит",
        "zero_denominator": "знаменатель раскрыт и равен нулю",
        "negative_denominator": "знаменатель раскрыт и отрицателен",
        "pages_not_readable": "внутри раздела страницы без текстового слоя",
        "boundary_not_determined": "конец раздела не определён",
        "empty": "под заголовком раздела нет текста",
    }.get(reason, reason)


def _short(text: str, limit: int = 160) -> str:
    """Причина одной строкой: в разделе она стоит рядом с запросом.

    **Обрезается по концу предложения, а не по числу знаков.** У рентабельности
    по EBITDA причина методики длиннее предела, и посреди фразы выходило
    «…а высокая маржа ничего не отменяет. Показатель при…» — текст, который
    читатель дочитать не может. Предложение целиком длиннее предела остаётся
    как есть: полная фраза лучше обрубка.
    """
    squeezed = " ".join(str(text).split())
    if len(squeezed) > limit:
        cut = squeezed.rfind(". ", 0, limit + 1)
        squeezed = squeezed[:cut] if cut > 0 else squeezed
    # Точка на конце не нужна: формулировка отказа ставит свою, и рядом
    # выходило «…ничего не отменяет.. Запрашивать нечего».
    return squeezed.rstrip(".")


def totals(refusals: tuple[Refusal, ...]) -> dict[str, int]:
    """Сколько отказов какого семейства произведено — счётчик для замера."""
    found: dict[str, int] = {}
    for item in refusals:
        found[item.kind.value] = found.get(item.kind.value, 0) + 1
    found["всего"] = len(refusals)
    return found
