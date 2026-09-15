"""Контроли качества отчётности. Выполняются до любых расчётов."""

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finlib.normalize.lines import LineDef, Operator, ReportingType, UnitSource
from finlib.quality.codes import CheckCode, CheckStatus, Severity
from finlib.quality.context import PeriodFacts, ReportContext
from finlib.quality.values import as_addend

logger = logging.getLogger(__name__)

BALANCE = "0710001"
PROFIT = "0710002"

# Строки цепочки прибыли проверяются отдельным контролем profit_chain,
# поэтому в section_sum они не попадают: один и тот же итог не должен
# порождать две записи журнала.
PROFIT_CHAIN_LINES: dict[ReportingType, tuple[str, ...]] = {
    ReportingType.FULL: ("2100", "2200"),
    ReportingType.SIMPLIFIED: ("2400",),
}

# Строка, по которой отслеживается прирост накопленной прибыли.
RETAINED_EARNINGS_LINE: dict[ReportingType, str] = {
    ReportingType.FULL: "1370",
    ReportingType.SIMPLIFIED: "1300",
}


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Результат одного контроля по одной строке или периоду."""

    check_code: CheckCode
    status: CheckStatus
    report_date: date | None = None
    form_code: str | None = None
    line_code: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    severity: Severity | None = None

    @property
    def is_blocking_failure(self) -> bool:
        """Провал ли это, останавливающий расчёт."""
        return self.status is CheckStatus.FAIL and self.severity is Severity.BLOCKING


def _severity(context: ReportContext, code: CheckCode, report_date: date | None) -> Severity:
    """Уровень контроля с понижением для сравнительных периодов.

    Блокирующий контроль останавливает расчёт только по отчётному периоду
    комплекта. За сравнительные периоды отвечают их собственные комплекты,
    и ошибка в сравнительной колонке даёт предупреждение, а не карантин.
    """
    base = context.thresholds.severity_of(code.value)
    if base is Severity.BLOCKING and not context.is_reporting_period(report_date):
        return Severity.WARNING
    return base


def _skipped(
    check_code: CheckCode, report_date: date, reason: str, **kwargs: Any
) -> CheckOutcome:
    """Контроль не выполнялся: проверять нечего."""
    return CheckOutcome(
        check_code=check_code,
        status=CheckStatus.INFO,
        report_date=report_date,
        message=f"Контроль не выполнялся: {reason}",
        severity=Severity.INFO,
        **kwargs,
    )


def _not_verifiable(
    check_code: CheckCode, report_date: date, reason: str, **kwargs: Any
) -> CheckOutcome:
    """Контроль невозможен из-за незагруженной строки — это наш пробел, не провал."""
    return CheckOutcome(
        check_code=check_code,
        status=CheckStatus.INFO,
        report_date=report_date,
        message=f"Контроль невозможен: {reason}",
        severity=Severity.WARNING,
        **kwargs,
    )


def _sum_components(
    line: LineDef, facts: PeriodFacts, form_code: str
) -> tuple[Decimal, list[str], dict[str, str]]:
    """Сумма состава итоговой строки, а также нераскрытые и незагруженные слагаемые."""
    total = Decimal(0)
    undisclosed: list[str] = []
    blocked: dict[str, str] = {}
    for component in line.components:
        reason = facts.blocked_reason(form_code, component.code)
        if reason is not None:
            blocked[component.code] = reason
            continue
        value = facts.get(form_code, component.code)
        if value is None:
            undisclosed.append(component.code)
        amount = as_addend(value)
        total += amount if component.op is Operator.PLUS else -amount
    return total, undisclosed, blocked


def _compare(
    check_code: CheckCode,
    context: ReportContext,
    facts: PeriodFacts,
    form_code: str,
    line: LineDef,
) -> CheckOutcome:
    """Сверяет итог с суммой его состава по правилам обеих трактовок."""
    report_date = facts.report_date
    kwargs = {"form_code": form_code, "line_code": line.code}

    if facts.blocked_reason(form_code, line.code) is not None:
        return _not_verifiable(
            check_code, report_date, str(facts.blocked_reason(form_code, line.code)), **kwargs
        )

    total = facts.get(form_code, line.code)
    if total is None:
        return _skipped(check_code, report_date, f"итог {line.code} не раскрыт", **kwargs)

    computed, undisclosed, blocked = _sum_components(line, facts, form_code)
    if blocked:
        # Причину называем полностью: строка отсутствует не потому, что её
        # не раскрыли, а потому, что мы отказались угадывать её принадлежность.
        reasons = "; ".join(f"{code} — {reason}" for code, reason in sorted(blocked.items()))
        return _not_verifiable(
            check_code,
            report_date,
            f"слагаемые итога {line.code} не загружены ({reasons}), сумму проверить нельзя",
            **kwargs,
        )
    if len(undisclosed) == len(line.components):
        return _skipped(
            check_code, report_date, "ни одно слагаемое не раскрыто", **kwargs
        )

    difference = computed - total
    tolerance = context.thresholds.rounding.tolerance(total)
    details = {
        "total": str(total),
        "computed": str(computed),
        "difference": str(difference),
        "tolerance": str(tolerance),
        "components": [f"{c.op.value}{c.code}" for c in line.components],
        "undisclosed_components": undisclosed,
    }
    if abs(difference) <= tolerance:
        return CheckOutcome(
            check_code=check_code,
            status=CheckStatus.PASS,
            report_date=report_date,
            message=f"Итог {line.code} сходится с суммой состава",
            details=details,
            severity=_severity(context, check_code, report_date),
            **kwargs,
        )
    return CheckOutcome(
        check_code=check_code,
        status=CheckStatus.FAIL,
        report_date=report_date,
        message=(
            f"Итог {line.code} = {total} не равен сумме состава {computed}, "
            f"расхождение {difference} при допуске {tolerance}"
        ),
        details=details,
        severity=_severity(context, check_code, report_date),
        **kwargs,
    )


# --- контроли ---------------------------------------------------------------


def balance_equality(context: ReportContext) -> Iterator[CheckOutcome]:
    """Актив равен пассиву: 1600 = 1700."""
    code = CheckCode.BALANCE_EQUALITY
    for report_date in context.ordered_periods:
        facts = context.periods[report_date]
        if not facts.has_form(BALANCE):
            continue
        assets = facts.get(BALANCE, "1600")
        liabilities = facts.get(BALANCE, "1700")
        if assets is None or liabilities is None:
            yield _skipped(code, report_date, "итог баланса не раскрыт", form_code=BALANCE)
            continue
        difference = assets - liabilities
        tolerance = context.thresholds.rounding.tolerance(assets)
        details = {
            "1600": str(assets),
            "1700": str(liabilities),
            "difference": str(difference),
            "tolerance": str(tolerance),
        }
        severity = _severity(context, code, report_date)
        if abs(difference) <= tolerance:
            yield CheckOutcome(
                code, CheckStatus.PASS, report_date, BALANCE, "1600",
                "Актив равен пассиву", details, severity,
            )
        else:
            yield CheckOutcome(
                code, CheckStatus.FAIL, report_date, BALANCE, "1600",
                f"Актив {assets} не равен пассиву {liabilities}, расхождение {difference}",
                details, severity,
            )


def section_sum(context: ReportContext) -> Iterator[CheckOutcome]:
    """Итог каждого раздела равен сумме входящих строк."""
    code = CheckCode.SECTION_SUM
    skip = PROFIT_CHAIN_LINES[context.reporting_type]
    for report_date in context.ordered_periods:
        facts = context.periods[report_date]
        for form_code in context.catalog.forms_of(context.reporting_type):
            if not facts.has_form(form_code):
                continue
            for line in context.catalog.totals(form_code, context.reporting_type):
                if line.code in skip:
                    continue
                yield _compare(code, context, facts, form_code, line)


def profit_chain(context: ReportContext) -> Iterator[CheckOutcome]:
    """Цепочка прибыли: 2110 − 2120 = 2100, 2100 − 2210 − 2220 = 2200.

    В упрощённом наборе валовой прибыли и прибыли от продаж нет, поэтому
    проверяется формула чистой прибыли: 2110 − 2120 − 2330 + 2340 − 2350 − 2410.
    """
    code = CheckCode.PROFIT_CHAIN
    for report_date in context.ordered_periods:
        facts = context.periods[report_date]
        if not facts.has_form(PROFIT):
            continue
        for line_code in PROFIT_CHAIN_LINES[context.reporting_type]:
            line = context.catalog.get(line_code, context.reporting_type)
            if line is None:
                continue
            yield _compare(code, context, facts, PROFIT, line)


def mandatory_fields(context: ReportContext) -> Iterator[CheckOutcome]:
    """Обязательные строки отчётности заполнены.

    Проверяется только отчётный период комплекта. Полнота сравнительных колонок
    — ответственность тех комплектов, для которых эти периоды отчётные;
    требовать её здесь значило бы отправлять в карантин за чужую отчётность.
    """
    code = CheckCode.MANDATORY_FIELDS
    required = context.thresholds.mandatory_for(context.reporting_type)
    severity = context.thresholds.severity_of(code.value)
    for report_date in context.ordered_periods:
        if not context.is_reporting_period(report_date):
            continue
        facts = context.periods[report_date]
        for line_code in required:
            line = context.catalog.get(line_code, context.reporting_type)
            if line is None:
                continue
            if not facts.has_form(line.form):
                # Форма за этот период не представлена: у ОФР нет третьего
                # периода, и требовать выручку на позапрошлую дату бессмысленно.
                continue
            value = facts.get(line.form, line_code)
            if value is not None:
                yield CheckOutcome(
                    code, CheckStatus.PASS, report_date, line.form, line_code,
                    f"Строка {line_code} заполнена", {"value": str(value)}, severity,
                )
            else:
                yield CheckOutcome(
                    code, CheckStatus.FAIL, report_date, line.form, line_code,
                    f"Обязательная строка {line_code} «{line.name}» не раскрыта",
                    {"value_status": "not_disclosed"}, severity,
                )


def period_revised(context: ReportContext) -> Iterator[CheckOutcome]:
    """Пересмотр отчётности прошлых периодов и наличие предыдущего периода.

    Классическая проверка «сальдо на начало равно сальдо на конец предыдущего»
    в нашей модели выполняется конструктивно: это одна и та же строка
    fact_report. Содержательная проверка — расхождение отчётного значения
    со сравнительным из более позднего комплекта; оно фиксируется при загрузке.
    """
    code = CheckCode.PERIOD_REVISED
    severity = context.thresholds.severity_of(code.value)
    for report_date in context.ordered_periods:
        revised = {
            (form, line): values
            for (period, form, line), values in context.revisions.items()
            if period == report_date
        }
        if revised:
            lines = sorted(line for _, line in revised)
            yield CheckOutcome(
                code, CheckStatus.WARNING, report_date, None, None,
                f"Отчётность за период пересмотрена: расходится строк — {len(revised)}",
                {"lines": lines[:50], "count": len(revised)}, severity,
            )
        else:
            yield CheckOutcome(
                code, CheckStatus.PASS, report_date, None, None,
                "Расхождений с более поздней отчётностью нет", {}, severity,
            )

    latest = max(context.ordered_periods) if context.ordered_periods else None
    if latest is not None and context.previous_period(latest) is None:
        yield CheckOutcome(
            code, CheckStatus.INFO, latest, None, None,
            "Предыдущий период отсутствует: средние балансовые величины "
            "рассчитать нельзя",
            {}, Severity.WARNING,
        )


def jump_detection(context: ReportContext) -> Iterator[CheckOutcome]:
    """Изменение показателя более чем в заданное число раз — предупреждение."""
    code = CheckCode.JUMP_DETECTION
    policy = context.thresholds.jump_detection
    severity = context.thresholds.severity_of(code.value)
    periods = context.ordered_periods
    for newer, older in zip(periods, periods[1:], strict=False):
        new_facts, old_facts = context.periods[newer], context.periods[older]
        for key, item in sorted(new_facts.values.items()):
            form_code, line_code = key
            previous = old_facts.get(form_code, line_code)
            current = item.value
            if previous is None or current is None or previous == 0:
                continue
            if abs(previous) < policy.min_base:
                continue
            ratio = abs(current) / abs(previous)
            if ratio <= policy.factor:
                continue
            yield CheckOutcome(
                code, CheckStatus.WARNING, newer, form_code, line_code,
                f"Строка {line_code} изменилась в {ratio:.1f} раза: "
                f"{previous} → {current}",
                {
                    "previous": str(previous),
                    "current": str(current),
                    "ratio": f"{ratio:.2f}",
                    "previous_period": str(older),
                    "factor": str(policy.factor),
                },
                severity,
            )


def retained_earnings_link(context: ReportContext) -> Iterator[CheckOutcome]:
    """Прирост накопленной прибыли объясняется чистой прибылью периода.

    Полностью сходится редко: разницу дают дивиденды, переоценка и исправления
    прошлых лет. Сигналим только на крупном необъяснённом расхождении.
    """
    code = CheckCode.RETAINED_EARNINGS_LINK
    equity_line = RETAINED_EARNINGS_LINE[context.reporting_type]
    severity = context.thresholds.severity_of(code.value)
    periods = context.ordered_periods
    for newer, older in zip(periods, periods[1:], strict=False):
        new_facts, old_facts = context.periods[newer], context.periods[older]
        if not new_facts.has_form(PROFIT):
            continue
        current_equity = new_facts.get(BALANCE, equity_line)
        previous_equity = old_facts.get(BALANCE, equity_line)
        net_profit = new_facts.get(PROFIT, "2400")
        if current_equity is None or previous_equity is None or net_profit is None:
            yield _skipped(
                code, newer,
                f"не раскрыты строка {equity_line} за оба периода или чистая прибыль",
                form_code=BALANCE, line_code=equity_line,
            )
            continue

        growth = current_equity - previous_equity
        unexplained = growth - net_profit
        tolerance = context.thresholds.retained_earnings_link.tolerance(
            new_facts.get(BALANCE, "1600")
        )
        details = {
            "equity_line": equity_line,
            "growth": str(growth),
            "net_profit": str(net_profit),
            "unexplained": str(unexplained),
            "tolerance": str(tolerance),
            "previous_period": str(older),
        }
        if abs(unexplained) <= tolerance:
            yield CheckOutcome(
                code, CheckStatus.PASS, newer, BALANCE, equity_line,
                "Прирост накопленной прибыли объясняется чистой прибылью",
                details, severity,
            )
        else:
            yield CheckOutcome(
                code, CheckStatus.WARNING, newer, BALANCE, equity_line,
                f"Прирост строки {equity_line} на {growth} не объясняется чистой прибылью "
                f"{net_profit}: необъяснённая часть {unexplained}. Возможны дивиденды, "
                "переоценка или исправление прошлых лет",
                details, severity,
            )


def unit_not_determined(context: ReportContext) -> Iterator[CheckOutcome]:
    """Единица измерения определена формой комплекта.

    Прежде она принималась как предположение. Это дефект данных, а не
    представления: ошибка в тысячу раз не ловится ни одним другим контролем —
    баланс сойдётся, сходимость разделов сойдётся, коэффициенты будут верны,
    а все абсолютные величины окажутся неверны в тысячу раз.

    Контроль относится к комплекту целиком, а не к периоду: единицу задаёт
    форма, а она у комплекта одна.
    """
    code = CheckCode.UNIT_NOT_DETERMINED
    # Уровень берётся как есть, без понижения для сравнительных периодов:
    # контроль относится к комплекту целиком, периода у него нет.
    severity = context.thresholds.severity_of(code.value)
    details = {
        "unit_code": context.unit_code,
        "unit_source": context.unit_source,
    }
    if context.unit_source == UnitSource.UNKNOWN.value:
        yield CheckOutcome(
            code, CheckStatus.FAIL, None, None, None,
            "Единица измерения не определена: набор форм комплекта "
            "не предусмотрен справочником",
            details, severity,
        )
        return
    yield CheckOutcome(
        code, CheckStatus.PASS, None, None, None,
        f"Единица измерения определена формой отчётности: код ОКЕИ {context.unit_code}",
        details, severity,
    )


def balance_magnitude(context: ReportContext) -> Iterator[CheckOutcome]:
    """Валюта баланса правдоподобна при заявленной единице измерения.

    Границы грубые намеренно: контроль ловит подмену единицы, а не отклонение
    от отраслевой нормы. Поэтому он предупреждающий — величина за границами
    требует ручного подтверждения, а не карантина.
    """
    code = CheckCode.BALANCE_MAGNITUDE
    bounds = context.thresholds.magnitude.balance_total
    for report_date in context.ordered_periods:
        facts = context.periods[report_date]
        if not facts.has_form(BALANCE):
            continue
        total = facts.get(BALANCE, "1600")
        if total is None:
            yield _skipped(code, report_date, "валюта баланса не раскрыта", form_code=BALANCE)
            continue
        severity = _severity(context, code, report_date)
        details = {
            "1600": str(total),
            "min": str(bounds.min),
            "max": str(bounds.max),
            "unit_code": context.unit_code,
        }
        problem = bounds.implausible(total)
        if problem is None:
            yield CheckOutcome(
                code, CheckStatus.PASS, report_date, BALANCE, "1600",
                "Валюта баланса правдоподобна при заявленной единице измерения",
                details, severity,
            )
        else:
            yield CheckOutcome(
                code, CheckStatus.WARNING, report_date, BALANCE, "1600",
                f"Валюта баланса {total} тыс. руб. {problem}: требуется ручное "
                f"подтверждение единицы измерения",
                details, severity,
            )


def period_magnitude_shift(context: ReportContext) -> Iterator[CheckOutcome]:
    """Между смежными периодами величины не меняются в тысячу раз.

    Непрерывно действующая организация так не меняется: ровно тысячекратное
    отношение означает, что периоды пришли в разных единицах. Рост в триста
    раз бывает хозяйственным событием и ловится контролем jump_detection.
    """
    code = CheckCode.PERIOD_MAGNITUDE_SHIFT
    rule = context.thresholds.magnitude.period_shift
    periods = context.ordered_periods
    for index, report_date in enumerate(periods):
        previous_date = periods[index + 1] if index + 1 < len(periods) else None
        if previous_date is None:
            continue
        facts = context.periods[report_date]
        earlier = context.periods[previous_date]
        if not (facts.has_form(BALANCE) and earlier.has_form(BALANCE)):
            continue
        current = facts.get(BALANCE, "1600")
        previous = earlier.get(BALANCE, "1600")
        if current is None or previous is None:
            continue
        severity = _severity(context, code, report_date)
        details = {
            "current": str(current),
            "previous": str(previous),
            "factor": str(rule.factor),
        }
        ratio = rule.shifted(current, previous)
        if ratio is None:
            yield CheckOutcome(
                code, CheckStatus.PASS, report_date, BALANCE, "1600",
                "Порядок величин согласован с предыдущим периодом",
                details, severity,
            )
        else:
            yield CheckOutcome(
                code, CheckStatus.FAIL, report_date, BALANCE, "1600",
                f"Валюта баланса изменилась в {ratio:.0f} раз относительно "
                f"{previous_date:%d.%m.%Y}: признак того, что периоды пришли "
                f"в разных единицах измерения",
                {**details, "ratio": str(ratio)}, severity,
            )


ALL_CHECKS = (
    unit_not_determined,
    balance_magnitude,
    period_magnitude_shift,
    balance_equality,
    section_sum,
    profit_chain,
    mandatory_fields,
    period_revised,
    jump_detection,
    retained_earnings_link,
)
