"""Тесты расчёта и оценки по МСФО (задача 27).

Случаи с живых комплектов: у Сегежи EBITDA отрицательна и отношение к ней
читалось бы наоборот; у ЛСР ликвидность не считается без величины эскроу;
у Автодора знаменатель покрытия процентов берётся только из примечаний;
у Норникеля группа долговой нагрузки забирает больше половины веса, и класс
не присваивается.
"""

from datetime import date
from decimal import Decimal

from finlib.metrics.ifrs import Inputs, Reason, compute_all, months_of
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.ifrs import assess

HEALTHY = {
    "ifrs.long_term_borrowings": Decimal(119062),
    "ifrs.short_term_borrowings": Decimal(209715),
    "ifrs.cash": Decimal(14681),
    "ifrs.operating_profit": Decimal(135605),
    "ifrs.depreciation": Decimal(40712),
    "ifrs.cash_before_working_capital_changes": Decimal(176519),
    "ifrs.interest_paid": Decimal(-23400),
    "ifrs.income_taxes_paid": Decimal(-34500),
    "ifrs.total_equity": Decimal(239793),
    "ifrs.total_assets": Decimal(663888),
    "ifrs.total_current_assets": Decimal(217976),
    "ifrs.total_current_liabilities": Decimal(268545),
    "ifrs.revenue": Decimal(573628),
}


def value_of(metrics, code):
    """Показатель по коду."""
    return next(item for item in metrics if item.code == code)


def test_interest_cover_takes_the_denominator_only_from_the_notes() -> None:
    """Величина из формы в знаменатель покрытия процентов не подставляется.

    У Автодора это дало бы 414 вместо 54 382 начисленных. Правило проверяется
    тем, что величины формы в словаре примечаний просто нет.
    """
    policy = load_ifrs_metrics()
    without = compute_all(Inputs(HEALTHY, {}), policy)
    assert not value_of(without, "interest_cover_accrued").calculable
    assert value_of(without, "interest_cover_accrued").reason is Reason.MISSING_INPUT

    with_notes = compute_all(
        Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}), policy
    )
    found = value_of(with_notes, "interest_cover_accrued")
    assert found.calculable
    assert found.value.quantize(Decimal("0.01")) == Decimal("4.38")


def test_negative_denominator_cancels_the_metric() -> None:
    """Отрицательная EBITDA отменяет отношение, а не даёт низкую нагрузку.

    У Сегежи «−1,86» арифметически верно и читается противоположно смыслу.
    """
    values = dict(HEALTHY)
    values["ifrs.operating_profit"] = Decimal(-50628)
    values["ifrs.depreciation"] = Decimal(14503)
    found = value_of(compute_all(Inputs(values, {})), "net_debt_ebitda")
    assert not found.calculable
    assert found.reason is Reason.NEGATIVE_DENOMINATOR


def test_developer_liquidity_is_refused_without_the_escrow_value() -> None:
    """Поправку нечем посчитать — исходный показатель не приводится."""
    found = value_of(
        compute_all(Inputs(HEALTHY, {}, issuer_type="developer")), "cur_liq"
    )
    assert not found.calculable
    assert found.reason is Reason.ADJUSTMENT_IMPOSSIBLE
    assert "ifrs.escrow_balance" in found.missing


def test_ffo_is_not_computed_on_interim_reporting() -> None:
    """FFO на промежуточной отчётности не считается вовсе.

    Выбор объявлен: операционный поток сезонен, и приведение его к году
    умножением дало бы величину, которую нечем проверить.
    """
    found = compute_all(Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}, months=6))
    assert not value_of(found, "ffo_to_debt").calculable
    assert value_of(found, "ffo_to_debt").reason is Reason.INTERIM_NOT_ANNUALISED
    # EBITDA при этом приводится и помечается.
    assert value_of(found, "net_debt_ebitda").annualised


def test_class_is_not_assigned_when_one_group_dominates() -> None:
    """Группа, забравшая больше половины веса, класса не даёт.

    У Норникеля покрытие процентов не считается, и долговая нагрузка весом
    40 после перераспределения берёт 57 % — правило РСБУ перенесено
    намеренно, и здесь оно не умозрительно.
    """
    policy = load_ifrs_metrics()
    without_cover = compute_all(Inputs(HEALTHY, {}), policy)
    result = assess(without_cover, policy)
    assert result.class_code is None
    assert "одной группой" in result.no_class_reason

    with_cover = compute_all(
        Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}), policy
    )
    assigned = assess(with_cover, policy)
    assert assigned.class_code == "B"
    assert assigned.score.quantize(Decimal("0.1")) == Decimal("66.0")


def test_excluded_metric_does_not_enter_the_score() -> None:
    """Показатель, исключённый неприменимостью, в балл не идёт.

    Он при этом рассчитан и в приложение попадает: неприменимость меняет
    не расчёт, а участие в оценке.
    """
    policy = load_ifrs_metrics()
    computed = compute_all(Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}), policy)
    without = assess(computed, policy, excluded=("interest_cover_accrued",))
    assert all(
        item.code != "debt_service" for group in without.groups for item in group.metrics
    )
    assert value_of(computed, "interest_cover_accrued").calculable


def test_число_месяцев_берётся_из_отчётной_даты() -> None:
    """Период промежуточного комплекта считается по методике, а не задаётся.

    Аннуализация была написана в задаче 27 и не срабатывала ни разу: число
    месяцев приходило в расчёт двенадцатью, и величины полугодия шли в балл
    как годовые. Ноль срабатываний был неотличим от невыполненного правила.
    """
    assert months_of(date(2026, 6, 30), "interim") == 6
    assert months_of(date(2026, 3, 31), "interim") == 3
    assert months_of(date(2025, 12, 31), "full") == 12
    # Годовая отчётность на 30 июня годом и остаётся: приведение относится
    # к виду отчётности, а не к месяцу отчётной даты.
    assert months_of(date(2026, 6, 30), "full") == 12


def test_промежуточные_величины_приводятся_к_году() -> None:
    """Потоковая величина полугодия удваивается, балансовая — нет."""
    policy = load_ifrs_metrics()
    half = compute_all(
        Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}, months=6), policy
    )
    whole = compute_all(
        Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}, months=12), policy
    )
    assert value_of(half, "equity_ratio").value == value_of(whole, "equity_ratio").value
    assert value_of(half, "ebitda_margin").annualised
    # FFO на промежуточной отчётности не считается вовсе — решение методики.
    assert not value_of(half, "ffo_to_debt").calculable


def test_divergence_gap_is_reported_even_below_the_threshold() -> None:
    """Разрыв двух мер печатается всегда: ноль превышений — не ноль разрыва."""
    policy = load_ifrs_metrics()
    computed = compute_all(Inputs(HEALTHY, {"interest_accrued": Decimal(30974)}), policy)
    result = assess(computed, policy)
    assert result.divergence_gap is not None
    assert result.divergence == ()
