"""Производные величины: изменения за период и вертикальная структура.

Темпы изменения нужны в заключении постоянно, а во входных блоках модели их
нет. Без них модель вынуждена либо посчитать процент сама — прямое нарушение
инварианта 1, либо промолчать о динамике, ради которой методика и построена.
Поэтому изменения и доли считаются здесь, в Python, и подаются готовыми,
каждая со своим кодом.

Код производной — «{база}_{вид}», где база это код строки отчётности (1230)
либо код показателя (cur_liq). В балльную оценку производные не входят:
скоринг перебирает показатели справочника, а динамика уже учтена в балле
показателя отдельным слагаемым.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from finlib.metrics.definitions import MetricDef, MetricsCatalog, Unit
from finlib.metrics.engine import MetricResult, MetricStatus, PeriodValues
from finlib.metrics.formula import NotCalculableReason
from finlib.normalize.lines import LinesCatalog, ReportingType
from finlib.quality.periods import PeriodConfidence

logger = logging.getLogger(__name__)

HUNDRED = Decimal(100)

_CODE = re.compile(r"^(?P<base>[a-z0-9][a-z0-9_]*?)_(?P<kind>chg_abs|chg_pct|share)$")


class DerivedKind(StrEnum):
    """Вид производной величины."""

    CHANGE_ABS = "chg_abs"
    CHANGE_PCT = "chg_pct"
    SHARE = "share"


@dataclass(frozen=True, slots=True)
class DerivedCode:
    """Разобранный код производной величины."""

    base: str
    kind: DerivedKind

    @property
    def code(self) -> str:
        """Код целиком."""
        return f"{self.base}_{self.kind.value}"

    @property
    def base_is_line(self) -> bool:
        """Построена ли производная от строки отчётности, а не от показателя."""
        return len(self.base) == 4 and self.base.isdigit()


def parse(code: str) -> DerivedCode | None:
    """Разбирает код производной; для обычного показателя возвращает None."""
    match = _CODE.match(code)
    if match is None:
        return None
    return DerivedCode(match.group("base"), DerivedKind(match.group("kind")))


def unit_of(parsed: DerivedCode, metric: MetricDef | None) -> Unit:
    """Единица измерения производной величины.

    Проценты и доли — проценты. Абсолютное изменение измеряется в единицах
    базы: для строки отчётности это тысячи рублей, для показателя — его
    собственная единица.
    """
    if parsed.kind is not DerivedKind.CHANGE_ABS:
        return Unit.PERCENT
    if parsed.base_is_line:
        return Unit.THOUSAND_RUB
    return metric.unit if metric is not None else Unit.RATIO


def describe(
    parsed: DerivedCode,
    lines: LinesCatalog,
    metrics: MetricsCatalog,
    reporting_type: ReportingType,
) -> str | None:
    """Наименование производной для блока ПОКАЗАТЕЛИ; None, если база неизвестна."""
    if parsed.base_is_line:
        line = lines.get(parsed.base, reporting_type)
        if line is None:
            return None
        base_name = f"{line.name} ({parsed.base})"
    else:
        metric = metrics.get(parsed.base)
        if metric is None:
            return None
        base_name = f"{metric.name} ({parsed.base})"
    if parsed.kind is DerivedKind.CHANGE_ABS:
        return f"{base_name}, изменение за период"
    if parsed.kind is DerivedKind.CHANGE_PCT:
        return f"{base_name}, изменение за период в процентах"
    return f"{base_name}, доля в валюте баланса"


def compute_derived(
    periods: dict[date, PeriodValues],
    metrics: Sequence[MetricResult],
    usable: Sequence[date],
    confidences: dict[date, PeriodConfidence],
    catalog: MetricsCatalog,
) -> list[MetricResult]:
    """Считает изменения и доли по пригодным периодам, от свежего к старому.

    Периоды приходят отсортированными по убыванию: базой изменения служит
    следующий в списке, то есть ближайший пригодный предыдущий период.
    """
    spec = catalog.derived
    by_metric = _metric_values(metrics) if spec.change.metrics else {}

    results: list[MetricResult] = []
    for index, report_date in enumerate(usable):
        previous_date = usable[index + 1] if index + 1 < len(usable) else None
        current = periods[report_date].values if report_date in periods else {}
        previous = (
            periods[previous_date].values
            if previous_date is not None and previous_date in periods
            else None
        )
        confidence = _worse(confidences, report_date, previous_date)

        for code in spec.change.lines:
            base = previous.get(code) if previous is not None else None
            results += _change(code, current.get(code), base, report_date, confidence)

        total = current.get(spec.share.denominator)
        for code in spec.share.lines:
            result = _share(
                code,
                current.get(code),
                total,
                spec.share.denominator,
                report_date,
                confidence,
            )
            if result is not None:
                results.append(result)

        for code, values in by_metric.items():
            base = values.get(previous_date) if previous_date is not None else None
            results += _change(code, values.get(report_date), base, report_date, confidence)

    logger.info("производные величины: рассчитано %d значений", len(results))
    return results


def _metric_values(metrics: Sequence[MetricResult]) -> dict[str, dict[date, Decimal]]:
    """Рассчитанные значения показателей по коду и периоду."""
    found: dict[str, dict[date, Decimal]] = {}
    for item in metrics:
        if item.is_ok and item.value is not None:
            found.setdefault(item.metric_code, {})[item.report_date] = item.value
    return found


def _change(
    base_code: str,
    current: Decimal | None,
    previous: Decimal | None,
    report_date: date,
    confidence: PeriodConfidence,
) -> list[MetricResult]:
    """Абсолютное и процентное изменение за период.

    Нет одного из двух значений — производная не пишется вовсе, а не пишется
    с not_calculable: иначе за самый ранний период в блок ушли бы десятки
    строк «нет предыдущего периода», и содержательный отказ утонул бы в них.
    """
    if current is None or previous is None:
        return []

    results = [
        MetricResult(
            metric_code=f"{base_code}_{DerivedKind.CHANGE_ABS.value}",
            report_date=report_date,
            value=current - previous,
            status=MetricStatus.OK,
            confidence=confidence,
        )
    ]

    code = f"{base_code}_{DerivedKind.CHANGE_PCT.value}"
    # Процент от неположительной базы содержательного смысла не имеет:
    # «рост на 55 %» при движении с -442 до -200 вводит в заблуждение,
    # а не описывает улучшение. Это тот же запрет, что и на отрицательный
    # знаменатель коэффициента.
    if previous == 0:
        results.append(
            _refused(
                code,
                report_date,
                confidence,
                "процентное изменение не определено: на начало периода величина равна нулю",
                NotCalculableReason.ZERO_DENOMINATOR,
            )
        )
    elif previous < 0:
        results.append(
            _refused(
                code,
                report_date,
                confidence,
                "процентное изменение не определено: на начало периода величина отрицательна",
                NotCalculableReason.NEGATIVE_DENOMINATOR,
            )
        )
    elif current < 0:
        # Смена знака: (−50 − 100) / 100 даёт «−150 %», и это арифметически
        # верно, но читается как невозможное. Переход прибыли в убыток
        # описывается самими величинами и абсолютным изменением, а процент
        # здесь только вводил бы в заблуждение.
        results.append(
            _refused(
                code,
                report_date,
                confidence,
                "процентное изменение не приводится: величина сменила знак, "
                "и процент вводил бы в заблуждение",
                NotCalculableReason.SIGN_CHANGE,
            )
        )
    else:
        results.append(
            MetricResult(
                metric_code=code,
                report_date=report_date,
                value=(current - previous) / previous * HUNDRED,
                status=MetricStatus.OK,
                confidence=confidence,
            )
        )
    return results


def _share(
    line_code: str,
    value: Decimal | None,
    total: Decimal | None,
    denominator: str,
    report_date: date,
    confidence: PeriodConfidence,
) -> MetricResult | None:
    """Доля строки в валюте баланса."""
    if value is None or total is None:
        return None
    code = f"{line_code}_{DerivedKind.SHARE.value}"
    if total == 0:
        return _refused(
            code,
            report_date,
            confidence,
            f"доля не определена: валюта баланса (строка {denominator}) равна нулю",
            NotCalculableReason.ZERO_DENOMINATOR,
        )
    if total < 0:
        return _refused(
            code,
            report_date,
            confidence,
            f"доля не определена: валюта баланса (строка {denominator}) отрицательна",
            NotCalculableReason.NEGATIVE_DENOMINATOR,
        )
    return MetricResult(
        metric_code=code,
        report_date=report_date,
        value=value / total * HUNDRED,
        status=MetricStatus.OK,
        confidence=confidence,
    )


def _refused(
    code: str,
    report_date: date,
    confidence: PeriodConfidence,
    reason: str,
    reason_code: NotCalculableReason,
) -> MetricResult:
    """Содержательный отказ считать производную."""
    return MetricResult(
        metric_code=code,
        report_date=report_date,
        value=None,
        status=MetricStatus.NOT_CALCULABLE,
        confidence=confidence,
        reason=reason,
        reason_code=reason_code.value,
    )


def _worse(
    confidences: dict[date, PeriodConfidence],
    report_date: date,
    previous_date: date | None,
) -> PeriodConfidence:
    """Доверие к производной — худшее из доверия к двум её периодам."""
    current = confidences.get(report_date, PeriodConfidence.VERIFIED)
    if previous_date is None:
        return current
    previous = confidences.get(previous_date, PeriodConfidence.VERIFIED)
    if PeriodConfidence.COMPARATIVE_ONLY in (current, previous):
        return PeriodConfidence.COMPARATIVE_ONLY
    return current
