"""Маршрутизация: корзина по обстоятельствам, а не по нашему усмотрению.

Структура правил утверждена человеком, пороги остаются предварительными,
и тест закрепляет не пороги, а **развод оснований по корзинам**. Стоп-фактор
с ограничением средним и стоп-фактор с ограничением низшим — разные
обстоятельства, и первое отправляло к человеку половину универсума, пока
разводом не занялись.
"""

from datetime import date
from decimal import Decimal

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.routing import RoutingPolicy, load_routing, route


def metric(code: str, value: str, name: str = "показатель") -> MetricValue:
    """Рассчитанный показатель для маршрута."""
    return MetricValue(
        code=code, name=name, group="debt", in_scoring=True, value=Decimal(value)
    )


def healthy() -> tuple[MetricValue, ...]:
    """Величины маршрута, при которых оснований нет ни одного."""
    return (
        metric("net_debt_ebitda", "1.0", "Чистый долг / EBITDA"),
        metric("equity_ratio", "0.6", "Коэффициент автономии"),
        metric("cur_liq", "2.5", "Текущая ликвидность"),
    )


def routed(**kwargs) -> tuple[str, tuple[str, ...]]:
    """Корзина и основания при здоровых величинах и названных обстоятельствах."""
    computed = kwargs.pop("computed", healthy())
    verdict = route(
        computed,
        quarantined=kwargs.pop("quarantined", False),
        today=kwargs.pop("today", date(2026, 5, 1)),
        latest_annual=kwargs.pop("latest_annual", date(2025, 12, 31)),
        **kwargs,
    )
    return verdict.basket, verdict.grounds


def test_healthy_issuer_needs_no_one() -> None:
    """Все величины в пределах шкал — корзина «Без внимания»."""
    assert routed() == ("clear", ())


def test_capped_stop_factor_is_attention() -> None:
    """Ограничение класса средним — обстоятельство внимания, а не разбора.

    Норма неприменимости отрицательного оборотного капитала объявлена
    по покрытию процентов, которого у нормализованных данных нет вовсе:
    признак срабатывает у всех, и разбор по нему означал бы разбор половины
    универсума.
    """
    assert routed(stop_factors=("negative_nwc",)) == (
        "attention",
        ("stop_factor_capped",),
    )


def test_severe_stop_factor_is_review() -> None:
    """Ограничение класса низшим и неустойчивым — разбор."""
    for code in ("negative_equity", "going_concern_uncertainty"):
        basket, grounds = routed(stop_factors=(code,))
        assert (basket, grounds) == ("review", ("stop_factor_severe",))


def test_severity_is_taken_from_the_methodology() -> None:
    """Стоп-фактор без объявленной градации молча штатным не становится."""
    with pytest.raises(ValueError, match="градация"):
        routed(stop_factors=("нет такого стоп-фактора",))


def test_missing_values_are_attention_not_review() -> None:
    """Нехватка данных — не риск: она называет поле и идёт во внимание."""
    basket, grounds = routed(computed=(metric("equity_ratio", "0.6"),))
    assert basket == "attention"
    assert grounds == ("data_insufficient",)


def test_assessed_class_outweighs_normalised_values() -> None:
    """Присвоенный нами класс D или E — разбор, а класс выше — нет."""
    assert routed(assessed_class="E") == ("review", ("assessed_class_low",))
    assert routed(assessed_class="B") == ("clear", ())


def test_value_past_the_last_calibration_point_is_review() -> None:
    """Балл уровня ноль означает конец шкалы, и одной такой величины хватает."""
    policy = load_ifrs_metrics()
    edge = policy.calibration_points.metrics["net_debt_ebitda"].points[0][0]
    computed = (
        metric("net_debt_ebitda", str(edge + 1), "Чистый долг / EBITDA"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    assert routed(computed=computed) == ("review", ("level_off_scale",))


def test_lower_band_and_off_scale_are_not_counted_twice() -> None:
    """Одна величина даёт одно основание: ноль балла не сопровождается нижней частью."""
    policy = load_ifrs_metrics()
    edge = policy.calibration_points.metrics["equity_ratio"].points[0][0]
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", str(edge - 1), "Коэффициент автономии"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert (basket, grounds) == ("review", ("level_off_scale",))


def test_stop_factor_speaks_for_its_metric() -> None:
    """Величина не повторяет стоп-фактор: обстоятельство одно, решение одно.

    Отрицательный чистый оборотный капитал означает ликвидность ниже единицы,
    и её положение за концом шкалы о новом не говорит. Иначе правило о тяжести
    отменялось бы следующим правилом — у 82 эмитентов набора ровно так
    и выходило.
    """
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "0.3", "Текущая ликвидность"),
    )
    assert routed(computed=computed, stop_factors=("negative_nwc",)) == (
        "attention",
        ("stop_factor_capped",),
    )
    # Без стоп-фактора та же величина основание даёт: правило гасит повтор,
    # а не саму проверку.
    assert routed(computed=computed) == ("review", ("level_off_scale",))


def test_matching_metric_code_needs_no_declaration() -> None:
    """Совпадение кода показателя действует само — это тот же показатель."""
    computed = (
        metric("net_debt_ebitda", "1.0"),
        metric("equity_ratio", "-0.2", "Коэффициент автономии"),
        metric("cur_liq", "2.5"),
    )
    assert routed(computed=computed, stop_factors=("negative_autonomy",)) == (
        "review",
        ("stop_factor_severe",),
    )


def test_attention_is_split_by_nature_of_the_circumstance() -> None:
    """Внимание показывается по старшей подгруппе, остальные называются."""
    verdict = route(
        (metric("equity_ratio", "0.6"),),
        quarantined=False,
        latest_annual=date(2024, 12, 31),
        today=date(2026, 6, 2),
    )
    assert verdict.basket == "attention"
    assert verdict.subgroups == ("data_gap", "disclosure")
    assert verdict.actions[0] == "добрать данные"
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_nwc",),
        latest_annual=date(2024, 12, 31),
        today=date(2026, 6, 2),
    )
    assert verdict.subgroups == ("value_risk", "disclosure")
    assert verdict.subgroup == "value_risk"


def test_review_has_no_subgroups() -> None:
    """Корзина без подгрупп их не выдумывает: показывать было бы нечего."""
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_equity",),
        latest_annual=date(2025, 12, 31),
        today=date(2026, 5, 1),
    )
    assert verdict.basket == "review"
    assert verdict.subgroups == ()
    assert verdict.subgroup == ""


def test_overdue_disclosure_is_its_own_ground() -> None:
    """Срок раскрытия нарушен — внимание, и величинам это не приписывается."""
    basket, grounds = routed(
        latest_annual=date(2024, 12, 31), today=date(2026, 6, 2)
    )
    assert (basket, grounds) == ("attention", ("disclosure_overdue",))


def test_status_and_maturity_travel_with_the_verdict() -> None:
    """Утверждённая структура и зрелость порогов — два разных сведения.

    Согласие с составом корзин не делает величины калиброванными, и вердикт
    обязан нести оба: умолчание о зрелости выдало бы предварительный порог
    за проверенный.
    """
    policy = load_routing()
    verdict = route(healthy(), quarantined=False, latest_annual=date(2025, 12, 31),
                    today=date(2026, 5, 1))
    assert verdict.status == policy.status
    assert verdict.thresholds == policy.thresholds
    if policy.status != "approved":
        assert "черновик" in verdict.describe()
    elif policy.thresholds == "preliminary":
        assert "пороги предварительны" in verdict.describe()


def test_approval_and_maturity_are_declared_with_their_reasons() -> None:
    """Утверждение называет автора, предварительность — причину."""
    policy = load_routing()
    raw = policy.model_dump()
    raw["approved_by"] = None
    with pytest.raises(ValueError, match="кем"):
        RoutingPolicy.model_validate(raw)
    raw = policy.model_dump()
    raw["thresholds_origin"] = ""
    raw["thresholds"] = "preliminary"
    with pytest.raises(ValueError, match="почему"):
        RoutingPolicy.model_validate(raw)


def test_bound_is_not_a_substitute_for_the_missing_metric() -> None:
    """Оценка сверху заменой не считается: не рассчитан — не ниже внимания.

    Правило «ниже порога — критерий пройден доказуемо» верно арифметически,
    но давало «без внимания» там, где долговая нагрузка не рассчитана:
    у восьми эмитентов набора решение принималось по величине, которой нет.
    Оценка сверху при этом остаётся сведением и печатается рядом.
    """
    computed = (
        metric("net_debt_op_profit", "3.3", "Чистый долг / EBITDA, оценка сверху"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert basket == "attention"
    assert grounds == ("data_insufficient",)


def test_non_positive_ebitda_is_its_own_ground() -> None:
    """Неположительная EBITDA не делает долговую нагрузку хорошей.

    Отношение чистого долга к неположительной EBITDA отрицательно, и шкала
    читает его как низкую нагрузку: эмитент с убытком оставался бы без
    внимания. Знак объявлен своим основанием, и величина отношения своего
    основания уже не даёт — обстоятельство одно.
    """
    computed = (
        metric("net_debt_ebitda", "-2.0", "Чистый долг / EBITDA"),
        metric("ebitda", "-500", "EBITDA"),
        metric("equity_ratio", "0.6"),
        metric("cur_liq", "2.5"),
    )
    basket, grounds = routed(computed=computed)
    assert basket == "attention"
    assert grounds == ("negative_ebitda",)


def test_two_cycles_without_reporting_open_their_own_queue() -> None:
    """Давность старше двух циклов раскрытия уводит из корзин тяжести.

    По числам такой давности маршрут не строится: они описывают организацию,
    которой могло не стать, и вопрос к ней другой — о статусе. Очередь
    старше любого основания тяжести, в том числе стоп-фактора.
    """
    verdict = route(
        healthy(),
        quarantined=False,
        stop_factors=("negative_equity",),
        latest_annual=date(2022, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "status_unknown"
    assert verdict.grounds == ("reporting_two_cycles_old",)
    # Обстоятельство тяжести при этом не исчезает: человек, которому комплект
    # передают, обязан видеть и его.
    assert any(item.ground == "stop_factor_severe" for item in verdict.findings)


def test_one_cycle_behind_stays_in_the_severity_baskets() -> None:
    """Один пропущенный цикл очередь статуса не открывает."""
    verdict = route(
        healthy(),
        quarantined=False,
        latest_annual=date(2024, 12, 31),
        today=date(2026, 9, 22),
    )
    assert verdict.basket == "attention"
    assert verdict.grounds == ("disclosure_overdue",)
