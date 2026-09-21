"""Стоп-факторы ветки МСФО: объявлены методикой и проведены в расчёт по фактам.

Механизм задачи 26 существовал и **не вызывался**: перечень стоп-факторов стоял
в методике, величины, по которым они проверяются, — в `eval/ifrs_scoring_run.py`,
а расчёт по фактам звал оценку с пустым перечнем исключённых и писал
`stop_factor_code = NULL`. В замере стоп-фактор менял исход, в документе его
не было вовсе, и отличить «ни один не сработал» от «не проверяли» было нечем.

Проверяется поток величин, а не упоминания: объявленный стоп-фактор обязан
опираться на существующий показатель, срабатывать на величине и доходить
до записанной оценки.
"""

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from finlib.metrics.ifrs import MetricValue
from finlib.normalize.ifrs_issuer_type import load_issuer_types
from finlib.normalize.ifrs_metrics import load_ifrs_metrics
from finlib.scoring.ifrs import assess, evaluate_stop_factors

ROOT = Path(__file__).resolve().parent.parent


def value(code: str, amount: Decimal) -> MetricValue:
    """Рассчитанный показатель с величиной — по справочнику показателей ветки."""
    metric = {item.code: item for item in load_ifrs_metrics().metrics}[code]
    return MetricValue(
        code=code,
        name=metric.name,
        group=metric.group,
        in_scoring=metric.in_scoring,
        value=amount,
    )


def test_every_declared_stop_factor_names_an_existing_metric() -> None:
    """Стоп-фактор проверяется по показателю, который в справочнике есть.

    Условие, ссылающееся на величину, которой не бывает, не срабатывает
    никогда и от невыполненного неотличимо: так норма неприменимости
    ссылалась на `interest_cover`, которого в ветке нет.
    """
    codes = {item.code for item in load_ifrs_metrics().metrics}
    types = load_issuer_types()
    for factor in types.stop_factors:
        assert factor.metric in codes, f"{factor.code}: {factor.metric}"
    for norm in types.not_applicable:
        if norm.when is not None:
            assert norm.when.metric in codes, f"{norm.stop_factor}: {norm.when.metric}"


def test_every_declared_cap_is_a_known_class() -> None:
    """Последствие стоп-фактора называет класс, который в методике есть."""
    classes = {item.code for item in load_ifrs_metrics().classes}
    for factor in load_issuer_types().stop_factors:
        assert factor.cap in classes, f"{factor.code}: {factor.cap}"


def test_negative_equity_lowers_the_class_to_the_lowest() -> None:
    """Отрицательный капитал опускает класс до низшего, а не понижает балл."""
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(-15008)),
        value("equity_ratio", Decimal("0.36")),
        value("net_debt_ebitda", Decimal("1.78")),
        value("ffo_to_debt", Decimal("0.36")),
        value("interest_cover_accrued", Decimal("4.38")),
        value("cur_liq", Decimal("0.81")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "negative_equity" in stops.triggered
    assert stops.cap == policy.classes[-1].code

    result = assess(metrics, policy, stops)
    assert result.class_code == policy.classes[-1].code
    # Класс до применения стоп-фактора сохраняется: класс E у набравшего
    # по баллу B и класс E у набравшего E — разные сведения.
    assert result.class_before_stop != result.class_code
    assert result.stop_factor_code == "negative_equity"


def test_interest_cover_below_one_caps_the_class_at_the_middle() -> None:
    """Покрытие процентов ниже единицы ограничивает класс средним."""
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(255)),
        value("equity_ratio", Decimal("0.0018")),
        value("interest_cover_accrued", Decimal("-1.99")),
        value("cur_liq", Decimal("0.45")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "interest_cover_below_one" in stops.triggered
    assert stops.cap == "C"


def test_inapplicable_stop_factor_does_not_lower_the_class() -> None:
    """Неприменимый стоп-фактор оценку не понижает, а оговорку называет.

    У ФосАгро оборотный капитал отрицателен, но покрытие процентов выше порога,
    при котором это объясняется возобновляемым краткосрочным финансированием.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("nwc", Decimal(-50569)),
        value("interest_cover_accrued", Decimal("4.38")),
        value("equity", Decimal(239793)),
        value("equity_ratio", Decimal("0.36")),
    )
    stops = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert "negative_nwc" not in stops.triggered
    assert "nwc" in stops.excluded
    assert stops.limitation_of("nwc")
    assert stops.cap is None


def test_confirmation_by_the_audit_report_is_recorded() -> None:
    """Сверка сработавшего стоп-фактора с заключением идёт вместе с ним.

    У Сегежи аудитор объявил существенную неопределённость в отношении
    непрерывности деятельности — это внешнее подтверждение стоп-фактора,
    и без него формулировки обязаны быть осторожнее.
    """
    policy = load_ifrs_metrics()
    metrics = (
        value("equity", Decimal(-15008)),
        value("interest_cover_accrued", Decimal("-1.99")),
    )
    confirmed = evaluate_stop_factors(
        metrics, "corporate", ("going_concern_uncertainty",), True, policy
    )
    assert confirmed.audit_state == "confirmed"
    alone = evaluate_stop_factors(metrics, "corporate", (), True, policy)
    assert alone.audit_state == "unconfirmed"
    unreadable = evaluate_stop_factors(metrics, "corporate", (), False, policy)
    assert unreadable.audit_state == "not_readable"


def test_checked_count_stands_next_to_the_triggered() -> None:
    """Число проверенных стоп-факторов идёт рядом с числом сработавших."""
    policy = load_ifrs_metrics()
    stops = evaluate_stop_factors(
        (value("equity", Decimal(239793)),), "corporate", (), False, policy
    )
    assert stops.checked == len(load_issuer_types().stop_factors)
    assert stops.triggered == ()
    assert "проверено" in stops.describe()


def test_assessment_cannot_be_made_without_checking_stop_factors() -> None:
    """Оценка не считается, не получив стоп-факторов: довод обязательный.

    Довод с умолчанием означал бы, что расчёт можно вызвать, не проверив
    ни одного стоп-фактора, — ровно так ветка и работала.
    """
    with pytest.raises(TypeError):
        assess((), load_ifrs_metrics())  # type: ignore[call-arg]


def test_no_production_call_assesses_with_empty_stop_factors() -> None:
    """Боевой вызов оценки не подставляет пустые стоп-факторы.

    Проверка структурная и по потоку величин: `StopFactors()` без доводов
    означает «проверено ноль», и в расчёте по фактам такого вызова быть
    не должно. Запись оценки — исключение: там довод необязателен ради
    вызовов, которым оценка уже передана.
    """
    allowed = {"src/finlib/scoring/ifrs_store.py"}
    found: list[str] = []
    for path in sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "eval").rglob("*.py")]):
        relative = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", "")
            if name == "assess" and len(node.args) >= 3:
                third = node.args[2]
                empty = isinstance(third, ast.Tuple) and not third.elts
                bare = (
                    isinstance(third, ast.Call)
                    and getattr(third.func, "id", "") == "StopFactors"
                    and not third.args
                    and not third.keywords
                )
                if empty or bare:
                    found.append(f"{relative}:{node.lineno}")
            if (
                name == "StopFactors"
                and not node.args
                and not node.keywords
                and relative not in allowed
            ):
                found.append(f"{relative}:{node.lineno}")
    assert not found, f"оценка без проверенных стоп-факторов: {found}"
