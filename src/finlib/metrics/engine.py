"""Расчёт показателей по периодам. Все вычисления в Decimal."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.db import PgConnection, fetch_all
from finlib.metrics.definitions import MetricDef, MetricsCatalog, load_metrics
from finlib.metrics.formula import (
    FormulaError,
    NotCalculableReason,
    ZeroDenominatorError,
    average_codes,
    denominator_of,
    describe,
    evaluate,
    line_codes,
)
from finlib.normalize.lines import (
    LinesCatalog,
    Measure,
    ReportingType,
    load_lines,
)
from finlib.quality.periods import PeriodConfidence, period_quality
from finlib.quality.thresholds import Thresholds, load_thresholds
from finlib.standards import Standard, load_standards

logger = logging.getLogger(__name__)

_SELECT_FACTS = """
SELECT f.report_date, f.line_code, f.value, f.standard
FROM fact_report f
JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND s.status <> 'quarantine'
ORDER BY f.report_date DESC
"""

_SELECT_REPORTING_TYPE = """
SELECT DISTINCT s.reporting_type
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s AND f.report_date = %(d)s
  AND f.period_role = 'current'
"""

_SELECT_ANY_TYPE = """
SELECT s.reporting_type, count(*) AS n
FROM fact_report f JOIN src_file s ON s.id = f.src_file_id
WHERE f.inn = %(inn)s AND f.standard = %(standard)s
GROUP BY s.reporting_type ORDER BY n DESC LIMIT 1
"""


class MetricStatus(StrEnum):
    """Статус рассчитанного показателя."""

    OK = "ok"
    NOT_CALCULABLE = "not_calculable"


@dataclass(frozen=True, slots=True)
class MetricResult:
    """Результат расчёта одного показателя за один период."""

    metric_code: str
    report_date: date
    value: Decimal | None
    status: MetricStatus
    confidence: PeriodConfidence
    reason: str | None = None
    reason_code: str | None = None

    @property
    def is_ok(self) -> bool:
        """Рассчитан ли показатель."""
        return self.status is MetricStatus.OK


@dataclass(frozen=True, slots=True)
class Baseline:
    """Начало периода: у величины на дату и у величины за период оно разное.

    **Предыдущий период определяется видом величины, а не близостью даты.**
    Балансовая величина стоит на дату, и её началом служит конец предыдущего
    годового периода: у комплекта за полугодие это 31 декабря прошлого года.
    Величина за период сравнивается только с сопоставимым по длительности
    отрезком прошлого года: полугодие с полугодием, год с годом.

    Прежде базой служил ближайший предыдущий период любой длины. Пока
    в базе одна годовая отчётность, это то же самое; с промежуточной —
    нет: годовая прибыль сравнивалась бы с полугодовым изменением капитала,
    а средняя балансовая величина считалась бы по полугодию.

    **Сопоставимого периода нет — отказ, а не подстановка ближайшего.**
    Промежуточный комплект без прошлогоднего полугодия динамики не даёт,
    и показатель получает `not_calculable` с названной причиной.
    """

    stock_date: date | None = None
    flow_date: date | None = None
    stock: dict[str, Decimal | None] = field(default_factory=dict)
    flow: dict[str, Decimal | None] = field(default_factory=dict)
    # Есть ли вообще более ранний пригодный период. Отличает «это самый
    # ранний период организации» от «предыдущий период есть, но он
    # несопоставим»: причины разные, и читателю заключения они говорят разное.
    has_earlier: bool = False

    def date_for(self, measure: Measure) -> date | None:
        """Дата начала периода для величины этого вида."""
        return self.stock_date if measure is Measure.STOCK else self.flow_date

    def values_for(self, measure: Measure) -> dict[str, Decimal | None]:
        """Величины начала периода для этого вида."""
        return self.stock if measure is Measure.STOCK else self.flow

    def describe(self) -> str:
        """Сводка для журнала: с чем сравниваются величины обоих видов."""
        stock = f"{self.stock_date:%d.%m.%Y}" if self.stock_date else "нет"
        flow = f"{self.flow_date:%d.%m.%Y}" if self.flow_date else "нет"
        return f"на дату — {stock}, за период — {flow}"


@dataclass
class PeriodValues:
    """Значения строк одного периода, плоско по кодам.

    Рядом со значением хранится стандарт, которым оно пришло. Величина сама
    по себе о своём происхождении не говорит, а показатель, собранный
    из величин двух стандартов, выглядит настоящим и не значит ничего.
    """

    report_date: date
    values: dict[str, Decimal | None]
    standards: dict[str, str] = field(default_factory=dict)


def load_period_values(
    inn: str, conn: PgConnection | None = None, standard: Standard = Standard.RSBU
) -> dict[date, PeriodValues]:
    """Читает значения строк организации по периодам, минуя карантин."""
    periods: dict[date, PeriodValues] = {}
    for row in fetch_all(_SELECT_FACTS, {"inn": inn, "standard": standard.value}, conn=conn):
        period = periods.setdefault(
            row["report_date"], PeriodValues(row["report_date"], {})
        )
        period.values[row["line_code"]] = row["value"]
        period.standards[row["line_code"]] = row["standard"]
    return periods


def reporting_type_of(
    inn: str,
    report_date: date,
    conn: PgConnection | None = None,
    standard: Standard = Standard.RSBU,
) -> ReportingType:
    """Набор строк, по которому сдан период; при отсутствии — преобладающий у организации."""
    params = {"inn": inn, "standard": standard.value, "d": report_date}
    rows = fetch_all(_SELECT_REPORTING_TYPE, params, conn=conn)
    if rows:
        return ReportingType(rows[0]["reporting_type"])
    fallback = fetch_all(_SELECT_ANY_TYPE, {"inn": inn, "standard": standard.value}, conn=conn)
    if not fallback:
        raise ValueError(f"для ИНН {inn} нет загруженной отчётности")
    return ReportingType(fallback[0]["reporting_type"])


def compute_metric(
    metric: MetricDef,
    reporting_type: ReportingType,
    report_date: date,
    current: dict[str, Decimal | None],
    previous: Baseline,
    confidence: PeriodConfidence,
    thresholds: Thresholds,
    standards: Mapping[str, str],
    measure_of: Callable[[str], Measure],
) -> MetricResult | None:
    """Считает один показатель за один период.

    Если хотя бы один нужный код не раскрыт, показатель не рассчитывается
    и получает not_calculable с перечнем отсутствующих кодов. Подстановка
    приближений запрещена. Показатель, неприменимый к набору отчётности,
    не рассчитывается вовсе и результата не даёт.

    standards — стандарт отчётности каждой величины. Параметр обязателен
    намеренно: контроль смешения, который можно молча не передать,
    неотличим от невыполненного. По той же причине обязателен `measure_of`:
    от вида величины зависит, с каким периодом она сравнивается, и умолчание
    здесь давало бы настоящее число при чужом сравнении.
    """
    tree = metric.tree_for(reporting_type)
    if tree is None:
        return None

    mixed = _mixed_standards(tree, standards)
    if mixed is not None:
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason=f"{load_standards().mixing.reason} Задействованы: {mixed}",
            reason_code=NotCalculableReason.MIXED_STANDARDS.value,
        )

    baseline, without = _baseline_values(tree, previous, measure_of)
    if without:
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason=_no_baseline_reason(previous, without, measure_of),
            reason_code=(
                NotCalculableReason.NO_PREVIOUS_PERIOD.value
                if not previous.has_earlier
                else NotCalculableReason.NO_COMPARABLE_PERIOD.value
            ),
        )

    missing = _missing_codes(tree, current, baseline)
    if missing:
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason="Не раскрыты строки: " + ", ".join(missing),
            reason_code=NotCalculableReason.MISSING_LINES.value,
        )

    if metric.denominator_must_be_positive:
        denominator = denominator_of(tree)
        if denominator is not None:
            try:
                computed = evaluate(denominator, current, baseline, thresholds.constants)
            except (ZeroDenominatorError, FormulaError):
                # Знаменатель сам не вычислился: причину назовёт основной
                # расчёт ниже, у него формулировки точнее. Проверка знака —
                # уточнение, и падать на ней нельзя.
                computed = None
            if computed is not None and computed < 0:
                return MetricResult(
                    metric.code,
                    report_date,
                    None,
                    MetricStatus.NOT_CALCULABLE,
                    confidence,
                    reason=(
                        "Знаменатель отрицателен, коэффициент не интерпретируется: "
                        f"{describe(denominator)} = {computed}"
                    ),
                    reason_code=NotCalculableReason.NEGATIVE_DENOMINATOR.value,
                )

    try:
        value = evaluate(tree, current, baseline, thresholds.constants)
    except ZeroDenominatorError as exc:
        reason = f"Коэффициент не определён: {exc.expression} равен нулю"
        if metric.zero_denominator_note:
            reason = f"{reason}. {metric.zero_denominator_note.strip()}"
        return MetricResult(
            metric.code,
            report_date,
            None,
            MetricStatus.NOT_CALCULABLE,
            confidence,
            reason=reason,
            reason_code=NotCalculableReason.ZERO_DENOMINATOR.value,
        )

    return MetricResult(metric.code, report_date, value, MetricStatus.OK, confidence)


def _baseline_values(
    tree, previous: Baseline, measure_of: Callable[[str], Measure]
) -> tuple[dict[str, Decimal | None] | None, list[str]]:
    """Собирает начало периода по видам величин и называет коды без базы.

    Каждый код берёт свою базу: балансовый — конец предыдущего годового
    периода, потоковый — сопоставимый период прошлого года. Интерпретатор
    формул о видах не знает и знать не должен: он получает уже собранное
    начало периода, а разрешает вид тот, кто знает справочник.
    """
    needed = average_codes(tree)
    if not needed:
        return None, []
    values: dict[str, Decimal | None] = {}
    without: list[str] = []
    for code in sorted(needed):
        measure = measure_of(code)
        if previous.date_for(measure) is None:
            without.append(code)
            continue
        values[code] = previous.values_for(measure).get(code)
    return values, without


def _no_baseline_reason(
    previous: Baseline, without: list[str], measure_of: Callable[[str], Measure]
) -> str:
    """Почему начала периода нет: самый ранний период или несопоставимый.

    Две причины, и путать их нельзя. «Это самый ранний период организации» —
    свойство её истории; «предыдущий период есть, но он другой длины» —
    свойство состава комплектов, и второе исправляется загрузкой
    сопоставимого периода, а первое не исправляется ничем.
    """
    codes = ", ".join(without)
    if not previous.has_earlier:
        return (
            "Нет данных на начало периода: средняя балансовая величина требует "
            "двух точек, подстановка значения на конец периода запрещена. "
            f"Строки: {codes}"
        )
    kinds = {measure_of(code) for code in without}
    what = (
        "сопоставимого периода прошлого года"
        if Measure.FLOW in kinds
        else "конца предыдущего годового периода"
    )
    return (
        f"Нет {what}: сравнивать не с чем, а подстановка ближайшего периода "
        f"другой длительности запрещена. Строки: {codes}"
    )


def _mixed_standards(tree, standards: Mapping[str, str]) -> str | None:
    """Стандарты величин показателя, если их больше одного; иначе None.

    Смешение даёт число, которое выглядит настоящим и не значит ничего:
    чистый долг группы к выручке управляющей компании — не долговая
    нагрузка, а артефакт. Величины, стандарт которых неизвестен, в проверке
    не участвуют: их нет и в расчёте — показатель отсеется как нерассчитанный
    с перечнем нераскрытых кодов.
    """
    found = {standards[code] for code in line_codes(tree) if code in standards}
    if len(found) <= 1:
        return None
    return ", ".join(sorted(found))


def _missing_codes(
    tree, current: dict[str, Decimal | None], previous: dict[str, Decimal | None] | None
) -> list[str]:
    """Коды, без которых показатель не посчитать; порядок устойчив."""
    missing: set[str] = set()
    averages = average_codes(tree)
    for code in line_codes(tree):
        if current.get(code) is None:
            missing.add(code)
            continue
        if code not in averages:
            continue
        # Средняя величина требует значения и на начало периода.
        if previous is None or previous.get(code) is None:
            missing.add(code)
    return sorted(missing)


def compute_all(
    inn: str,
    conn: PgConnection | None = None,
    *,
    catalog: MetricsCatalog | None = None,
    thresholds: Thresholds | None = None,
    standard: Standard = Standard.RSBU,
    with_derived: bool = True,
) -> list[MetricResult]:
    """Считает все применимые показатели по всем периодам организации.

    Расчёт всегда идёт в пределах одного стандарта: ряды по РСБУ и по МСФО
    несопоставимы и смешению не подлежат.

    Следом считаются производные величины — изменения за период и доли
    в валюте баланса. Они нужны модели готовыми: без них она вынуждена
    считать проценты сама, а это нарушение инварианта 1.
    """
    catalog = catalog if catalog is not None else load_metrics()
    thresholds = thresholds if thresholds is not None else load_thresholds()
    lines = load_lines()

    periods = load_period_values(inn, conn, standard)
    quality = period_quality(inn, conn, standard)
    ordered = sorted(periods, reverse=True)

    results: list[MetricResult] = []
    usable: list[date] = []
    confidences: dict[date, PeriodConfidence] = {}
    baselines: dict[date, Baseline] = {}
    checked_for_mixing = 0
    for report_date in ordered:
        info = quality.get(report_date)
        if info is not None and not info.is_usable:
            logger.info("период %s в карантине, показатели не считаются", report_date)
            continue
        confidence = info.confidence if info is not None else PeriodConfidence.VERIFIED
        usable.append(report_date)
        confidences[report_date] = confidence
        reporting_type = reporting_type_of(inn, report_date, conn, standard)
        previous = baseline_of(report_date, periods, quality, lines)
        baselines[report_date] = previous
        logger.info(
            "период %s: начало периода %s", report_date, previous.describe()
        )

        # Стандарт величины хранится рядом с ней, и в проверку идут величины
        # обоих периодов: средняя балансовая берёт значение и на начало.
        standards = dict(periods[report_date].standards)
        for item in (previous.stock_date, previous.flow_date):
            if item is not None:
                standards |= periods[item].standards

        def measure_of(code: str, _type: ReportingType = reporting_type) -> Measure:
            return lines.measure_of(code, _type)

        for metric in catalog.for_type(reporting_type):
            result = compute_metric(
                metric,
                reporting_type,
                report_date,
                periods[report_date].values,
                previous,
                confidence,
                thresholds,
                standards,
                measure_of,
            )
            if result is not None:
                results.append(result)
                checked_for_mixing += 1

    # Счётчик проверенного рядом со счётчиком сработавшего: ноль отвергнутых
    # показателей при неизвестном числе проверок не означает ничего.
    mixed = sum(
        1
        for item in results
        if item.reason_code == NotCalculableReason.MIXED_STANDARDS.value
    )
    logger.info(
        "контроль смешения стандартов: проверено показателей %d, отвергнуто %d",
        checked_for_mixing,
        mixed,
    )

    if with_derived:
        # Импорт внутри функции: derived.py опирается на типы этого модуля,
        # и на уровне модуля вышел бы цикл.
        from finlib.metrics.derived import compute_derived

        results += compute_derived(
            periods, results, usable, confidences, catalog, baselines, lines
        )
    return results


def period_months(moment: date, catalog: LinesCatalog) -> int:
    """Длительность периода в месяцах по его отчётной дате.

    Правило объявлено методикой, а не выведено из устройства программы:
    годовой период кончается 31 декабря (402-ФЗ, статья 15), у прочих дат
    длительность равна номеру месяца — то же правило, по которому
    приводится к году промежуточная отчётность.
    """
    return 12 if catalog.measures.is_annual_end(moment) else moment.month


def baseline_of(
    report_date: date,
    periods: Mapping[date, PeriodValues],
    quality: Mapping[date, object],
    catalog: LinesCatalog,
) -> Baseline:
    """Начало периода для величин обоих видов.

    **Ближайший предыдущий период базой не служит.** Балансовая величина
    сравнивается с концом предыдущего годового периода, величина за период —
    с отрезком той же длительности за прошлый год. Совпадают они только
    у годовой отчётности, и там правило ничего не меняет.

    Периода нет либо он в карантине — базы нет, и показатель откажется
    считаться. Подстановка ближайшего запрещена: она даёт настоящее число
    при чужом сравнении, а такую ошибку не ловит ни один контроль.
    """

    def usable(moment: date | None) -> date | None:
        if moment is None or moment not in periods:
            return None
        info = quality.get(moment)
        return moment if info is None or getattr(info, "is_usable", True) else None

    earlier = [
        item
        for item in sorted(periods, reverse=True)
        if item < report_date and usable(item) is not None
    ]
    stock_date = usable(catalog.measures.previous_annual_end(report_date))
    wanted = _year_earlier(report_date)
    flow_date = usable(wanted)
    if flow_date is not None and period_months(flow_date, catalog) != period_months(
        report_date, catalog
    ):
        flow_date = None
    return Baseline(
        stock_date=stock_date,
        flow_date=flow_date,
        stock=periods[stock_date].values if stock_date is not None else {},
        flow=periods[flow_date].values if flow_date is not None else {},
        has_earlier=bool(earlier),
    )


def _year_earlier(moment: date) -> date:
    """Та же дата годом раньше; 29 февраля переносится на 28-е."""
    try:
        return moment.replace(year=moment.year - 1)
    except ValueError:  # pragma: no cover — только 29 февраля
        return moment.replace(year=moment.year - 1, day=28)
